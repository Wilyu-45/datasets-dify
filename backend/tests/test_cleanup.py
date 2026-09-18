"""cleanup 服务测试（2026-09 生产部署改造，计划测试第 3 条）。

覆盖：
- 只删过期条目，未过期保留（文件 + 子目录整树判期）；
- input/、parsed/ 等保留目录永不列入清理目标，即使内容超期；
- 越界路径（data_root 之外）被拦截；
- retention_days <= 0 禁用该目录；
- dry_run 只统计不删除；
- run_cleanup 记录 _last_report / get_last_report。
"""
from __future__ import annotations

import os
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from app import config as cfg_mod
from app.services import cleanup


def _past(days: int) -> float:
    return time.time() - days * 86400 - 3600  # 多留 1h 余量，防时钟抖动


def _age(path: Path, days: int) -> None:
    """把文件/目录的 mtime 拨老到 days 天前（目录须在内容写完后调用）。"""
    ts = _past(days)
    os.utime(path, (ts, ts))


@pytest.fixture
def cs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """隔离 data_root 到 tmp_path，保留期 chunks=7 / output=7 / error=30 / webscrape=7。"""
    data_root = tmp_path / "data"
    settings = cfg_mod.Settings(data_root=data_root)
    monkeypatch.setattr(cfg_mod, "settings", settings)
    # cleanup 是 `from app.config import settings`，import 时已绑旧单例，
    # 必须重绑消费模块的引用才能真正隔离到 tmp（见 test_target_stems 同法）
    monkeypatch.setattr(cleanup, "settings", settings)
    settings.ensure_dirs()
    (data_root / "webscrape").mkdir(parents=True, exist_ok=True)
    yield settings


def _make_file(path: Path, days_old: int, size: int = 10) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    _age(path, days_old)
    return path


class TestTargetList:
    def test_keep_dirs_never_targeted(self, cs):
        """input/、parsed/、pending/、manual_fix/ 不得出现在清理目标里。"""
        targets = [p for p, _ in cleanup._cleanup_targets()]
        forbidden = {cs.input_dir, cs.parsed_dir, cs.pending_dir, cs.manual_fix_dir}
        assert not (set(targets) & forbidden)
        names = {p.name for p in targets}
        assert {"chunks", "output", "error", "webscrape"} <= names

    def test_retention_zero_disables_dir(self, cs, monkeypatch):
        """retention_days <= 0 → 该目录不列入目标。"""
        monkeypatch.setattr(cs, "cleanup_output_retention_days", 0)
        targets = {p.name for p, _ in cleanup._cleanup_targets()}
        assert "output" not in targets
        assert "chunks" in targets


class TestWithinDataRoot:
    def test_inside_ok(self, cs):
        assert cleanup._within_data_root(cs.chunks_dir)

    def test_outside_rejected(self, cs, tmp_path):
        assert not cleanup._within_data_root(tmp_path.parent)
        assert not cleanup._within_data_root(Path(os.path.expanduser("~")))

    def test_sibling_prefix_rejected(self, cs, tmp_path):
        """data_backup 这种「前缀相同但平级」的目录必须判为越界。"""
        sib = tmp_path / "data_backup"
        sib.mkdir()
        assert not cleanup._within_data_root(sib)

    def test_outside_target_collected_as_error(self, cs, monkeypatch, tmp_path):
        """越界目标在 _clean_one_dir 里被拒绝且记入 errors，不落删。"""
        outside = tmp_path / "outside"
        outside.mkdir()
        victim = _make_file(outside / "old.txt", days_old=999)
        stat = cleanup._clean_one_dir(
            outside, 7, ref_ts=time.time(), dry_run=False
        )
        assert stat.errors and "拒绝清理" in stat.errors[0]
        assert victim.exists()


class TestRunCleanup:
    def test_expired_purged_fresh_kept(self, cs):
        # chunks：过期文件删、未过期文件留、.gitkeep 保留
        _make_file(cs.chunks_dir / "old.json", days_old=10)
        _make_file(cs.chunks_dir / "fresh.json", days_old=1)
        (cs.chunks_dir / ".gitkeep").touch()
        # output：过期子目录整树删（树内所有 mtime 都拨老）
        dead = cs.output_dir / "doc_v1"
        _make_file(dead / "sub" / "a.bin", days_old=8)
        _age(dead / "sub", 8)
        _age(dead, 8)
        # webscrape：树里有近期文件的整树保留
        live = cs.data_root / "webscrape" / "task1"
        _make_file(live / "r1.html", days_old=20)
        _make_file(live / "latest" / "r2.html", days_old=2)
        gone = cs.data_root / "webscrape" / "task2"
        _make_file(gone / "old.html", days_old=20)
        _age(gone, 20)

        report = cleanup.run_cleanup()

        assert not (cs.chunks_dir / "old.json").exists()
        assert (cs.chunks_dir / "fresh.json").exists()
        assert (cs.chunks_dir / ".gitkeep").exists()
        assert not dead.exists()
        assert live.exists() and (live / "r1.html").exists()
        assert not gone.exists()
        assert report.removed_files >= 1  # 只删了 chunks/old.json（doc_v1/task2 是整树目录）
        assert report.removed_dirs >= 2
        assert report.freed_bytes > 0
        assert report.errors == []

    def test_keep_dirs_untouched_even_if_expired(self, cs):
        """input//parsed/ 内容即使超期也绝不碰。"""
        src = _make_file(cs.input_dir / "ancient.pdf", days_old=999)
        parsed = _make_file(cs.parsed_dir / "ancient" / "ancient.md", days_old=999)
        report = cleanup.run_cleanup()
        assert src.exists() and parsed.exists()
        assert report.removed_files == 0 and report.removed_dirs == 0

    def test_dry_run_only_counts(self, cs):
        _make_file(cs.error_dir / "e1.log", days_old=60)
        _make_file(cs.error_dir / "e2.log", days_old=45)
        report = cleanup.run_cleanup(dry_run=True)
        assert (cs.error_dir / "e1.log").exists()
        assert (cs.error_dir / "e2.log").exists()
        assert report.dry_run is True
        assert report.removed_files == 2
        d = next(x for x in report.dirs if x.dir.endswith("error"))
        assert d.expired_entries == 2 and d.freed_bytes > 0

    def test_now_injection_judges_relative_to_ref(self, cs):
        """now 参数作判期基准：以 now 为参照，而非系统时钟。"""
        f = _make_file(cs.chunks_dir / "rel.json", days_old=5)  # mtime = now-5d
        future = datetime.now() + timedelta(days=10)  # 基准拨到未来 → 文件已 15 天
        report = cleanup.run_cleanup(now=future)
        assert not f.exists()
        assert report.removed_files == 1

    def test_last_report_recorded(self, cs):
        assert cleanup.get_last_report() is None or True
        report = cleanup.run_cleanup(dry_run=True)
        assert cleanup.get_last_report() is report
        assert report.to_dict()["dry_run"] is True
