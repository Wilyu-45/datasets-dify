"""plan.md §3.1 — PostgreSQL 驱动的文件读取与状态管理。

核心流程（input/ 目录驱动，manifest 表为状态台账）：
    1. 枚举 input/ 目录中的真实文件（按 allowed_extensions 过滤）
    2. 对每个文件：
       - 在 manifest 表中查找对应行（精确文件名 / 裸名匹配）
       - 无对应行 → 自动登记新行（自动登记），再进入处理
       - 有对应行但「导入状态」非空 → 跳过（幂等）
       - 未处理 → 算 MD5，处理 pending/ 冲突，shutil.move 到 pending/
       - 更新 manifest（import_status / process_status / status / md5 / 时间戳）
    3. 对 manifest 中「导入状态」为空、但 input/ 中找不到对应文件的行 → 记 MISSING
    4. 返回 ScanReport

历史说明：
    - 早期版本以「Excel 清单行」为主线（需先在 Excel/清单里登记文件名才处理）。
    - 现已完全删除对 Excel 的依赖：扫描直接以 input/ 目录为准，
      新文件丢进 input/ 后点「扫描」即自动登记进 manifest 并进入待处理（pending/）。

启动约束：
    - 服务启动时（main.lifespan）只调用 manifest_store.bootstrap() 确保 PostgreSQL manifest 表就绪，
      **不会**调用本函数，也不会移动任何文件。
    - 本函数只在用户点击前端「扫描」按钮时由 /api/scan 触发。

幂等性：
    - 第二次扫描：所有文件都已「导入状态」非空 → 全部跳过，staged=0。
"""

from __future__ import annotations

import logging
import shutil
import time
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from app.config import settings
from app.models.schemas import (
    FileAction,
    FileActionRecord,
    ManifestRow,
    ScanReport,
)
from app.services import hasher, manifest_store

log = logging.getLogger("ragsystem.scanner")


def _is_already_imported(row: ManifestRow) -> bool:
    """「导入状态」列有非空文本即视为已处理。"""
    return bool(row.import_status and str(row.import_status).strip())


def _list_pending_files() -> Set[str]:
    if not settings.pending_dir.exists():
        return set()
    return {p.name for p in settings.pending_dir.iterdir() if p.is_file()}


def _resolve_input_path(name_in_manifest: str) -> Optional[Path]:
    """在 input/ 解析清单中的「文件名」到真实文件路径。

    规则：
    1) 精确匹配（包含用户写出的扩展名如果有）
    2) 否则按 allowed_extensions 顺序追加扩展名尝试
    3) 多个候选 → 按配置优先级取第一个命中

    Returns:
        命中的 Path，未找到返回 None
    """
    if not settings.input_dir.exists():
        return None
    # 1) 精确匹配
    exact = settings.input_dir / name_in_manifest
    if exact.is_file():
        return exact
    # 2) 按优先级尝试追加扩展名
    for ext in settings.allowed_extensions:
        candidate = settings.input_dir / f"{name_in_manifest}{ext}"
        if candidate.is_file():
            return candidate
    return None


def _match_manifest_by_stem(file: Path, manifest: Dict[str, ManifestRow]) -> Optional[ManifestRow]:
    """按「裸文件名」（去掉扩展名）在 manifest 中找对应行。

    历史兼容：早期在 Excel/清单里登记文件名时常省略扩展名
    （如清单行「手册」，实际文件为「手册.pdf」）。
    返回匹配到的 ManifestRow；未找到返回 None。
    """
    stem = file.stem.lower()
    for mname, mrow in manifest.items():
        if Path(mname).stem.lower() == stem:
            return mrow
    return None


def _collision_rename(target: Path) -> Path:
    """pending/ 同名冲突 → 在扩展名前插入 _<6hex>。"""
    stem = target.stem
    suffix = target.suffix
    import hashlib
    suffix_hex = hashlib.md5(str(time.time_ns()).encode()).hexdigest()[:6]
    new_name = f"{stem}_{suffix_hex}{suffix}"
    return target.with_name(new_name)


def _stage_one_file(
    src: Path,
    existing: Optional[ManifestRow],
    dry_run: bool,
    actions: List[FileActionRecord],
    rows_to_write: List[ManifestRow],
    t0: float,
) -> Dict[str, int]:
    """处理单个 input/ 文件：算 MD5、处理 pending/ 冲突、移动到 pending/、写 manifest 行。

    existing 为 None 表示清单中尚无该文件（自动登记新行）。
    返回计数 {'staged','new','skipped','renamed','failed'}。
    """
    counts = {"staged": 0, "new": 0, "skipped": 0, "renamed": 0, "failed": 0}
    actual_filename = src.name
    try:
        md5_val = hasher.md5_of_file(src, settings.scan_chunk_size)

        if dry_run:
            # dry_run：不移动、不写盘，但记录
            counts["new"] += 1
            actions.append(
                FileActionRecord(
                    filename=actual_filename,
                    action=FileAction.DRY_RUN,
                    md5=md5_val,
                    from_path=str(src),
                    duration_ms=int((time.perf_counter() - t0) * 1000),
                )
            )
            rows_to_write.append(
                _row_after_stage(
                    existing, actual_filename, md5_val, str(src), None,
                    action="dry_run",
                )
            )
            return counts

        # 实际移动流程
        dst = settings.pending_dir / actual_filename
        renamed_this = False

        if dst.exists():
            if hasher.md5_of_file(dst, settings.scan_chunk_size) == md5_val:
                # md5 一致，视作已就绪
                counts["skipped"] += 1
                log.info(
                    "pending/ 中已有同 md5 文件，跳过移动",
                    extra={
                        "file_name": actual_filename,
                        "step": "scan",
                        "status": "skipped_duplicate",
                    },
                )
                actions.append(
                    FileActionRecord(
                        filename=actual_filename,
                        action=FileAction.SKIPPED_DONE,
                        md5=md5_val,
                        from_path=str(src),
                        to_path=str(dst),
                        duration_ms=int((time.perf_counter() - t0) * 1000),
                    )
                )
                rows_to_write.append(
                    _row_after_stage(
                        existing, actual_filename, md5_val, str(src), str(dst),
                        action="staged",
                    )
                )
                return counts
            else:
                # md5 不一致，重命名
                dst = _collision_rename(dst)
                renamed_this = True
                counts["renamed"] += 1
                log.warning(
                    "pending/ 同名 md5 不一致，已重命名",
                    extra={
                        "file_name": actual_filename,
                        "step": "scan",
                        "status": "renamed",
                        "error_msg": dst.name,
                    },
                )

        # 移动文件
        shutil.move(str(src), str(dst))
        counts["staged"] += 1
        counts["new"] += 1
        action_type = (
            FileAction.COLLISION_RENAMED if renamed_this else FileAction.STAGED
        )

        actions.append(
            FileActionRecord(
                filename=actual_filename,
                action=action_type,
                md5=md5_val,
                from_path=str(src),
                to_path=str(dst),
                duration_ms=int((time.perf_counter() - t0) * 1000),
            )
        )
        action_str = "renamed" if renamed_this else "staged"
        rows_to_write.append(
            _row_after_stage(
                existing, actual_filename, md5_val, str(src), str(dst),
                action=action_str,
            )
        )
        return counts

    except Exception as e:  # noqa: BLE001
        counts["failed"] += 1
        log.exception(
            "扫描单文件失败",
            extra={
                "file_name": actual_filename,
                "step": "scan",
                "status": "error",
                "error_msg": str(e),
            },
        )
        actions.append(
            FileActionRecord(
                filename=actual_filename,
                action=FileAction.FAILED,
                from_path=str(src),
                error=str(e),
                duration_ms=int((time.perf_counter() - t0) * 1000),
            )
        )
        # 失败也要写一次 manifest 以记录错误
        try:
            try:
                md5_fail = hasher.md5_of_file(src, settings.scan_chunk_size)
            except Exception:  # noqa: BLE001
                md5_fail = existing.md5 if existing else ""
            rows_to_write.append(
                _row_after_stage(
                    existing, actual_filename, md5_fail, str(src), None,
                    action="failed", note=str(e),
                )
            )
        except Exception:  # noqa: BLE001
            log.exception("记录失败行到 manifest 时再次失败", extra={"file_name": actual_filename})
        return counts


def scan_and_stage(dry_run: bool = False, force: bool = False) -> ScanReport:
    """§3.1 主入口（input/ 目录驱动 + manifest 表状态台账）。

    两轮处理：
      ① 循环1（清单行驱动）：对 manifest 中「导入状态」为空的记录，在 input/ 找同名文件
         → 移到 pending/、更新 manifest；找不到 → 记 MISSING（历史兼容，保持原行为）。
      ② 循环2（input/ 目录驱动，★ 已删除 Excel 依赖后的核心）：对 input/ 中尚未登记的
         新文件 → 自动登记进 manifest 并进入待处理。

    ★ 2026-08 新增 force 参数（流水线一致性）：
        - force=True：忽略 import_status 检查，重新扫描所有行
          （如果文件已不在 input/，记 MISSING；如还在则重新移入 pending/）
        - force=False（默认）：import_status 非空 → 跳过（幂等）
    """
    started = time.perf_counter()
    log.info(
        "scan started",
        extra={"step": "scan", "status": "start", "dry_run": dry_run, "force": force},
    )

    settings.ensure_dirs()
    # bootstrap：确保 manifest 表存在且列齐全
    manifest_store.bootstrap(settings.data_root)

    # 1) 加载 manifest
    manifest: Dict[str, ManifestRow] = manifest_store.load()

    actions: List[FileActionRecord] = []
    new_count = staged_count = skipped_count = renamed_count = failed_count = 0
    missing_on_disk = 0
    rows_to_write: List[ManifestRow] = []
    # 行名被更新为实际文件名后，需删除的旧主键（如清单行「无扩展」→ 实际文件「无扩展.pdf」）
    old_keys_to_remove: List[str] = []

    # 已由「清单行」处理过的 input 实际文件名（避免循环2重复处理）
    processed_files: Set[str] = set()

    # ---- 循环1：清单行驱动（历史兼容） ----
    for fname, row in manifest.items():
        t0 = time.perf_counter()

        # 已经处理过（导入状态非空） → 跳过（除非 force）
        if _is_already_imported(row) and not force:
            skipped_count += 1
            actions.append(
                FileActionRecord(
                    filename=fname,
                    action=FileAction.SKIPPED_DONE,
                    note=f"导入状态={row.import_status}，已处理",
                    duration_ms=int((time.perf_counter() - t0) * 1000),
                )
            )
            continue
        # ★ force=True 时即使已 staged 也重新走一遍
        if _is_already_imported(row) and force:
            log.info(
                "scan: force=True，强制重扫（已 staged 行）",
                extra={"step": "scan", "status": "force_rescan", "file_name": fname},
            )

        # 未导入：在 input/ 找文件（支持扩展名自动补全）
        src = _resolve_input_path(fname)
        if src is None:
            # 找不到 → 仅 WARNING + 记 MISSING，不动 manifest
            missing_on_disk += 1
            log.warning(
                "清单中导入状态为空但 input/ 找不到该文件",
                extra={
                    "file_name": fname,
                    "step": "scan",
                    "status": "missing",
                    "error_msg": f"已尝试扩展名: {', '.join(settings.allowed_extensions[:5])}…",
                },
            )
            actions.append(
                FileActionRecord(
                    filename=fname,
                    action=FileAction.MISSING,
                    error=f"清单中『{fname}』标记未导入，input/ 也找不到（已尝试常见扩展名）",
                )
            )
            continue

        # 实际文件名（可能含扩展名，可能与 fname 不同）
        processed_files.add(src.name)
        # 行名已更新为实际文件名 → 旧主键需要删除（避免残留重复行）
        if row.filename != src.name:
            old_keys_to_remove.append(row.filename)

        counts = _stage_one_file(src, row, dry_run, actions, rows_to_write, t0)
        staged_count += counts["staged"]
        new_count += counts["new"]
        skipped_count += counts["skipped"]
        renamed_count += counts["renamed"]
        failed_count += counts["failed"]

    # ---- 循环2：input/ 目录驱动（★ 新文件自动登记进清单） ----
    if settings.input_dir.exists():
        for file in sorted(settings.input_dir.iterdir()):
            if not file.is_file():
                continue
            if file.suffix.lower() not in settings.allowed_extensions:
                # 只登记可处理的文档类型
                continue
            if file.name in processed_files:
                # 已由清单行处理过（如扩展名补全命中）
                continue

            # 尝试精确 / 裸名匹配清单行
            existing = manifest.get(file.name)
            if existing is None:
                existing = _match_manifest_by_stem(file, manifest)
            if existing is not None:
                # 该行已由循环1处理或跳过 → 不重复处理（保持扩展名优先级语义）
                continue

            # 纯新文件 → 自动登记进清单并进入待处理
            log.info(
                "scan: input/ 发现未登记文件，自动登记进清单",
                extra={"file_name": file.name, "step": "scan", "status": "auto_register"},
            )
            t0 = time.perf_counter()
            counts = _stage_one_file(file, None, dry_run, actions, rows_to_write, t0)
            staged_count += counts["staged"]
            new_count += counts["new"]
            skipped_count += counts["skipped"]
            renamed_count += counts["renamed"]
            failed_count += counts["failed"]

    # 3) 批量写 manifest
    if not dry_run and rows_to_write:
        try:
            manifest_store.bulk_upsert(rows_to_write)
        except Exception as e:  # noqa: BLE001
            log.exception(
                "manifest 写盘失败",
                extra={"step": "scan", "status": "error", "error_msg": str(e)},
            )
            failed_count += len(rows_to_write)
            raise

    # 4) 删除被重命名覆盖的旧主键（如「无扩展」→「无扩展.pdf」）
    if not dry_run and old_keys_to_remove:
        for k in set(old_keys_to_remove):
            try:
                manifest_store.delete(k)
            except Exception as e:  # noqa: BLE001
                log.warning(
                    "删除旧清单行失败",
                    extra={"step": "scan", "file_name": k, "error_msg": str(e)},
                )

    duration_ms = int((time.perf_counter() - started) * 1000)
    report = ScanReport(
        dry_run=dry_run,
        scanned=staged_count + missing_on_disk + failed_count + sum(
            1 for a in actions if a.action in (FileAction.DRY_RUN,)
        ),
        staged=staged_count,
        new=new_count,
        skipped_done=skipped_count,
        renamed=renamed_count,
        missing_on_disk=missing_on_disk,
        failed=failed_count,
        actions=actions,
    )
    log.info(
        "scan finished",
        extra={
            "step": "scan",
            "status": "done",
            "duration_ms": duration_ms,
            "scanned": report.scanned,
            "staged": report.staged,
            "new": report.new,
            "skipped": report.skipped_done,
            "renamed": report.renamed,
            "missing": report.missing_on_disk,
            "failed": report.failed,
        },
    )
    return report


def _row_after_stage(
    existing: Optional[ManifestRow],
    filename: str,
    md5_val: str,
    from_path: str,
    to_path: Optional[str],
    *,
    action: str = "staged",
    note: str = "",
) -> ManifestRow:
    """根据已有行（或新行）构造更新后的 ManifestRow。

    同步更新用户原列：
        导入状态  — 是否已移入待处理（"已移入待处理" / "已移入待处理(重命名)" / "移入失败" / "试运行-已识别"）
        处理状态  — 当前处理阶段（"已扫描" / "已扫描(重命名)" / "扫描失败"）
        处理备注  — 自由备注（默认写入"md5: <hash>"或错误信息）
    以及系统 5 列：
        status    — 管线 FSM
        md5/create_time/update_time/error_msg
    """
    now = manifest_store.now_iso()

    # 动作 → 用户列文本
    if action == "staged":
        import_status = "已移入待处理"
        process_status = "已扫描"
    elif action == "renamed":
        import_status = "已移入待处理(重命名)"
        process_status = "已扫描(重命名)"
    elif action == "dry_run":
        import_status = "试运行-已识别"
        process_status = "试运行-已识别"
    elif action == "failed":
        import_status = "移入失败"
        process_status = "扫描失败"
    else:
        import_status = None
        process_status = None

    # 处理备注默认内容
    if note:
        process_note = note
    elif action == "failed":
        process_note = note or "扫描过程中发生异常"
    else:
        # 默认记录 md5 便于人工核对
        process_note = f"md5={md5_val[:12]}…"

    # 系统 status
    if action == "failed":
        sys_status = "error"
    elif action == "dry_run":
        sys_status = existing.status if existing else "new"
    else:
        sys_status = "pending"

    if existing is None:
        return ManifestRow(
            filename=filename,
            status=sys_status,
            md5=md5_val,
            create_time=now,
            update_time=now,
            import_status=import_status,
            process_status=process_status,
            process_note=process_note,
            error_msg=note if action == "failed" else None,
        )

    update_kwargs: Dict[str, object] = {
        "filename": filename,  # 始终使用最新文件名（可能是补全扩展名后的）
        "status": sys_status,
        "md5": md5_val,
        "update_time": now,
        "create_time": existing.create_time or now,
        "error_msg": note if action == "failed" else None,
    }
    # 仅当 import_status / process_status 有新值时才覆盖
    if import_status is not None:
        update_kwargs["import_status"] = import_status
    if process_status is not None:
        update_kwargs["process_status"] = process_status
    if process_note is not None:
        update_kwargs["process_note"] = process_note

    return existing.model_copy(update=update_kwargs)
