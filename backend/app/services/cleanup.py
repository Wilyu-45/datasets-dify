"""文件定期清理服务（2026-09 生产部署改造，计划板块 C）。

目的：保障生产服务器存储空间——只保留最重要的文件（源文件 + MinerU 解析产物），
按保留期自动清掉可再生的中间产物。

保留 / 清理清单：
    永久保留（不在任何清理目标里，硬编码不碰）：
        input/       源文件（流水线源头，删了无法重跑）
        parsed/      MinerU 解析产物（md / json / images，重建成本高）
        pending/     上传暂存（是否清理属管理员决策，不自动删）
        manual_fix/  人工修复区（人工产物）
    按保留期清理（顶层条目判期，单位天；retention_days<=0 禁用该目录）：
        chunks/      切分产物（可由 parsed/ 重切）      cleanup_chunks_retention_days
        output/      静态托管缓存（可重新导出）          cleanup_output_retention_days
        error/       失败暂存（超期自动放弃）            cleanup_error_retention_days
        webscrape/   网站抓取任务临时区                  cleanup_webscrape_retention_days
    logs/ 由 logging_config 的 TimedRotatingFileHandler 按 log_retention_days
    自行轮转，本服务不重复处理（避免双重删除）。

判期规则：
    - 顶层文件：mtime 超期 → 删除；
    - 顶层子目录（chunks/{stem}/、webscrape/{task_id}/ 等）：取整棵目录树内的
      最新 mtime 判期，全树超期才整树删除——半新半旧的目录整体保留，
      避免删一半留下残缺产物（chunker / dify 找不到配套文件）。

安全性：
    - 删除前校验路径 resolve() 后仍位于 settings.data_root 之内
      （防误配置 / 软链指向外部导致越界删除）；
    - input/、parsed/、pending/、manual_fix/ 从目标列表层面排除，代码写死；
    - 单项删除失败（OSError）记入 errors 并继续，不中断整轮；
    - dry_run=True 只统计「将删除什么」，不实际删除，供手动 API 预览。
"""

from __future__ import annotations

import logging
import os
import shutil
import sys
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from app.config import settings

log = logging.getLogger("ragsystem.cleanup")

# webscrape 临时区目录名（与 services/webscraper.py 的 WEBSCRAPE_DIRNAME 一致；
# 不直接 import 是为降低耦合——cleanup 是基础运维服务，不依赖抓取业务模块）
_WEBSCRAPE_DIRNAME = "webscrape"

# git 占位文件：体积为 0 且删掉会让空目录失去版本跟踪，跳过
_KEEP_FILENAMES = frozenset({".gitkeep"})

_DAY_SECONDS = 86400.0

# 定时任务与手动 API 可能并发；串行化避免同一目录被两轮清理交叉操作
_run_lock = threading.Lock()


def _long_path(p: Path) -> str:
    """Windows 长路径支持（>260 字符），其他平台直接返回字符串。

    与 mineru_client._long_path 同源逻辑（那里是私有函数，为避免
    运维服务依赖解析模块，此处独立保留一份）。
    """
    s = str(p)
    if sys.platform == "win32" and len(s) >= 240 and not s.startswith("\\\\?\\"):
        return "\\\\?\\" + s
    return s


@dataclass
class DirStat:
    """单个目标目录的清理明细。"""

    dir: str
    retention_days: int
    expired_entries: int = 0   # 判定期超期的顶层条目数（dry_run 时也统计）
    removed_files: int = 0     # 删除文件数（dry_run 时 = 计划删除数）
    removed_dirs: int = 0      # 删除子目录数（整树计 1）
    freed_bytes: int = 0       # 释放字节（dry_run 时 = 预计释放）
    errors: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "dir": self.dir,
            "retention_days": self.retention_days,
            "expired_entries": self.expired_entries,
            "removed_files": self.removed_files,
            "removed_dirs": self.removed_dirs,
            "freed_bytes": self.freed_bytes,
            "errors": list(self.errors),
        }


@dataclass
class CleanupReport:
    """一轮清理的总报告（POST /api/cleanup 与 status 端点直接 to_dict 返回）。"""

    started_at: str            # ISO 8601
    finished_at: str
    duration_ms: int
    dry_run: bool
    removed_files: int
    removed_dirs: int
    freed_bytes: int
    dirs: List[DirStat] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_ms": self.duration_ms,
            "dry_run": self.dry_run,
            "removed_files": self.removed_files,
            "removed_dirs": self.removed_dirs,
            "freed_bytes": self.freed_bytes,
            "dirs": [d.to_dict() for d in self.dirs],
            "errors": list(self.errors),
        }


# -------------------- 安全校验 --------------------


def _within_data_root(path: Path) -> bool:
    """校验路径 resolve() 后仍位于 data_root 之内（含边界等于 data_root）。

    防两类事故：配置误把清理目录指到系统路径；目录里有软链指向外部。
    Windows 文件系统大小写不敏感，用 normcase 统一比较。
    """
    try:
        root = str(settings.data_root.resolve())
        resolved = str(path.resolve())
    except OSError:  # 畸形路径（含非法字符等）
        return False
    root_c = os.path.normcase(root).rstrip("\\/")
    resolved_c = os.path.normcase(resolved)
    return resolved_c == root_c or resolved_c.startswith(root_c + os.sep)


# -------------------- 目标与统计 --------------------


def _cleanup_targets() -> List[Tuple[Path, int]]:
    """返回启用的清理目标 [(目录, 保留期天数)]。

    只列「可再生」目录；input/、parsed/、pending/、manual_fix/ 永不列入。
    retention_days <= 0 视为该目录禁用清理。
    """
    candidates: List[Tuple[Path, int]] = [
        (settings.chunks_dir, settings.cleanup_chunks_retention_days),
        (settings.output_dir, settings.cleanup_output_retention_days),
        (settings.error_dir, settings.cleanup_error_retention_days),
        (
            (settings.data_root / _WEBSCRAPE_DIRNAME).resolve(),
            settings.cleanup_webscrape_retention_days,
        ),
    ]
    return [(p, days) for p, days in candidates if days > 0]


def _scan_tree(root: Path) -> Tuple[int, float]:
    """统计目录树总字节数与最新 mtime（含各层目录自身的 mtime）。

    目录 mtime 会随直接子项增删更新，纳入统计可保证「最近仍在写入」的
    任务目录不被误删。读不到的条目（权限 / 竞态删除）静默忽略。
    """
    total = 0
    latest = 0.0
    try:
        latest = root.stat().st_mtime
    except OSError:
        pass
    for cur, _dirs, files in os.walk(root):
        cur_path = Path(cur)
        try:
            m = cur_path.stat().st_mtime
            if m > latest:
                latest = m
        except OSError:
            pass
        for name in files:
            try:
                st = (cur_path / name).stat()
            except OSError:
                continue
            total += st.st_size
            if st.st_mtime > latest:
                latest = st.st_mtime
    return total, latest


# -------------------- 单目录清理 --------------------


def _clean_one_dir(
    target: Path, retention_days: int, *, ref_ts: float, dry_run: bool
) -> DirStat:
    """清理单个目标目录（顶层条目判期，子目录整树删）。"""
    stat = DirStat(dir=str(target), retention_days=retention_days)

    if not _within_data_root(target):
        msg = f"目标目录不在 data_root 内，拒绝清理: {target}"
        stat.errors.append(msg)
        log.error("cleanup 拒绝越界目标", extra={"path": str(target)})
        return stat
    if not target.is_dir():
        return stat  # 目录不存在（如从未抓取代过），正常跳过

    try:
        entries = sorted(target.iterdir(), key=lambda p: p.name)
    except OSError as e:
        stat.errors.append(f"无法枚举 {target.name}: {e}")
        return stat

    cutoff_ts = ref_ts - retention_days * _DAY_SECONDS

    for entry in entries:
        if entry.name in _KEEP_FILENAMES:
            continue
        try:
            # symlink 一律不碰：生产数据目录不应存在软链，出现即人工核查
            if entry.is_symlink():
                continue
            is_dir = entry.is_dir()
        except OSError as e:
            stat.errors.append(f"无法判断类型 {entry}: {e}")
            continue

        try:
            if is_dir:
                size, latest = _scan_tree(entry)
                if latest >= cutoff_ts:
                    continue  # 树内仍有未过期内容，整树保留
                stat.expired_entries += 1
                if dry_run:
                    stat.removed_dirs += 1
                    stat.freed_bytes += size
                    continue
                if not _within_data_root(entry):
                    stat.errors.append(f"路径越界，拒绝删除: {entry}")
                    continue
                shutil.rmtree(_long_path(entry))
                stat.removed_dirs += 1
                stat.freed_bytes += size
            else:
                st = entry.stat()
                if st.st_mtime >= cutoff_ts:
                    continue
                stat.expired_entries += 1
                if dry_run:
                    stat.removed_files += 1
                    stat.freed_bytes += st.st_size
                    continue
                if not _within_data_root(entry):
                    stat.errors.append(f"路径越界，拒绝删除: {entry}")
                    continue
                Path(_long_path(entry)).unlink()
                stat.removed_files += 1
                stat.freed_bytes += st.st_size
        except OSError as e:
            msg = f"删除失败 {entry}: {e}"
            stat.errors.append(msg)
            log.warning("cleanup 删除失败: %s", e, extra={"path": str(entry)})

    return stat


# -------------------- public API --------------------


def run_cleanup(now: Optional[datetime] = None, dry_run: bool = False) -> CleanupReport:
    """执行一轮文件清理，返回报告（自动任务与手动 API 共用入口）。

    Args:
        now: 判期基准时间（测试注入用），默认取当前时间。
        dry_run: True 时只统计不删除。
    """
    ref = now or datetime.now()
    ref_ts = ref.timestamp()
    started = datetime.now()

    with _run_lock:
        dir_stats: List[DirStat] = []
        errors: List[str] = []
        for target, days in _cleanup_targets():
            st = _clean_one_dir(target, days, ref_ts=ref_ts, dry_run=dry_run)
            errors.extend(f"[{target.name}] {e}" for e in st.errors)
            dir_stats.append(st)

    finished = datetime.now()
    report = CleanupReport(
        started_at=started.isoformat(timespec="seconds"),
        finished_at=finished.isoformat(timespec="seconds"),
        duration_ms=int((finished - started).total_seconds() * 1000),
        dry_run=dry_run,
        removed_files=sum(s.removed_files for s in dir_stats),
        removed_dirs=sum(s.removed_dirs for s in dir_stats),
        freed_bytes=sum(s.freed_bytes for s in dir_stats),
        dirs=dir_stats,
        errors=errors,
    )

    with _last_report_lock:
        global _last_report  # noqa: PLW0603
        _last_report = report

    log.info(
        "cleanup 完成%s",
        "（dry_run）" if dry_run else "",
        extra={
            "step": "cleanup",
            "status": "ok" if not errors else "partial",
            "removed_files": report.removed_files,
            "removed_dirs": report.removed_dirs,
            "freed_mb": round(report.freed_bytes / 1024 / 1024, 2),
            "errors": len(errors),
        },
    )
    return report


# 最近一次清理结果（GET /api/cleanup/status 展示用）
_last_report: Optional[CleanupReport] = None
_last_report_lock = threading.Lock()


def get_last_report() -> Optional[CleanupReport]:
    """返回进程内最近一次清理报告；从未清理过返回 None。"""
    with _last_report_lock:
        return _last_report

