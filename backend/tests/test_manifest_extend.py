"""manifest_store（PostgreSQL manifest 表）读写 + scanner 协同测试。"""

from __future__ import annotations

import importlib
import logging
from pathlib import Path

import pytest

logging.disable(logging.CRITICAL)


@pytest.fixture(autouse=True)
def fresh_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """隔离数据目录；manifest 数据存于 PostgreSQL（见 pg_ready 的保存/恢复）。"""
    test_data_root = tmp_path / "data"
    monkeypatch.setenv("RAG_DATA_ROOT", str(test_data_root))
    from app import config as cfg_mod
    importlib.reload(cfg_mod)
    settings = cfg_mod.settings
    from app.services import scanner
    scanner.settings = settings
    settings.ensure_dirs()
    yield settings


@pytest.fixture(autouse=True)
def pg_ready(fresh_settings):
    """确保 manifest 表存在；测试结束后恢复原表内容，避免污染开发库。"""
    from app.services import manifest_store
    manifest_store.bootstrap()
    saved = list(manifest_store.load().values())
    manifest_store.clear()
    yield
    manifest_store.clear()
    if saved:
        manifest_store.bulk_upsert(saved)


def _row(**kw):
    from app.models.schemas import ManifestRow
    return ManifestRow(**kw)


# ============ 基础读写 ============


def test_bootstrap_creates_manifest_table():
    from app.services import manifest_store
    assert manifest_store.exists() is True


def test_upsert_load_roundtrip():
    """upsert 一行 → load 能读回全部字段。"""
    from app.services import manifest_store
    manifest_store.upsert(
        _row(
            filename="GBT 12130-2005.pdf", seq=1,
            category_l1="国标", category_l2="医用氧舱",
            keywords="医用空气加压氧舱", department="高压氧科",
            effective_date="2006/4/1", process_note="扫描件、大文件",
        )
    )
    manifest = manifest_store.load()
    assert "GBT 12130-2005.pdf" in manifest
    row = manifest["GBT 12130-2005.pdf"]
    assert row.seq == 1
    assert row.category_l1 == "国标"
    assert row.category_l2 == "医用氧舱"
    assert row.keywords == "医用空气加压氧舱"
    assert row.department == "高压氧科"
    assert row.effective_date == "2006/4/1"
    assert row.process_note == "扫描件、大文件"
    # 系统列默认空
    assert row.status is None
    assert row.md5 is None
    assert row.create_time is not None  # upsert 自动填充


def test_upsert_updates_existing_row():
    """同 filename 再次 upsert → 字段被更新而非新增。"""
    from app.services import manifest_store
    manifest_store.upsert(_row(filename="x.pdf", seq=1, category_l1="a"))
    manifest_store.upsert(_row(filename="x.pdf", seq=2, category_l1="b"))
    assert manifest_store.count() == 1
    row = manifest_store.fetch("x.pdf")
    assert row is not None
    assert row.seq == 2
    assert row.category_l1 == "b"


def test_bulk_upsert():
    from app.services import manifest_store
    manifest_store.bulk_upsert([
        _row(filename="a.pdf", seq=1),
        _row(filename="b.pdf", seq=2),
        _row(filename="c.pdf", seq=3),
    ])
    assert manifest_store.count() == 3


def test_delete_row():
    from app.services import manifest_store
    manifest_store.upsert(_row(filename="del.pdf"))
    manifest_store.delete("del.pdf")
    assert manifest_store.fetch("del.pdf") is None
    assert manifest_store.count() == 0


def test_fetch_missing_row():
    from app.services import manifest_store
    assert manifest_store.fetch("not_exist.pdf") is None


def test_load_returns_dict_keyed_by_filename():
    from app.services import manifest_store
    manifest_store.bulk_upsert([
        _row(filename="a.pdf"),
        _row(filename="b.pdf"),
    ])
    manifest = manifest_store.load()
    assert set(manifest.keys()) == {"a.pdf", "b.pdf"}


# ============ scanner 与 manifest 协同测试 ============


def test_scan_updates_user_columns(fresh_settings):
    """扫描后 manifest 的『导入情况』『处理情况』『处理说明』被正确更新。"""
    from app.services import manifest_store, scanner

    s = fresh_settings
    manifest_store.upsert(
        _row(filename="new.pdf", seq=1, category_l1="国标",
             category_l2="医用氧舱", department="高压氧科", process_note="原备注")
    )
    # 放一个文件到 input
    (s.input_dir / "new.pdf").write_bytes(b"hello world")
    report = scanner.scan_and_stage(dry_run=False)
    assert report.staged == 1
    manifest = manifest_store.load()
    row = manifest["new.pdf"]
    # 用户原列被系统更新
    assert row.import_status == "已移入待处理"
    assert row.process_status == "已扫描"
    # 处理说明被覆盖为 md5 摘要（系统行为）
    assert "md5=" in (row.process_note or "")
    # 系统 5 列
    assert row.status == "pending"
    assert row.md5 and len(row.md5) == 32
    assert row.create_time
    assert row.update_time


def test_dry_run_sets_dry_run_marker_in_user_columns(fresh_settings):
    """dry_run=True 时不应写入 manifest，也不移动文件。"""
    from app.services import manifest_store, scanner

    s = fresh_settings
    manifest_store.upsert(_row(filename="dry.pdf"))
    (s.input_dir / "dry.pdf").write_bytes(b"dry data")
    report = scanner.scan_and_stage(dry_run=True)
    assert report.dry_run is True
    # dry_run 不应写入 → 导入情况仍为空
    row = manifest_store.fetch("dry.pdf")
    assert row.import_status is None
    # 但文件不移动
    assert (s.input_dir / "dry.pdf").exists()
    # 报告里能看到这是 dry_run
    assert any(a.action.value == "dry_run" for a in report.actions)


def test_collision_rename_marks_in_user_columns(fresh_settings):
    """pending/ 同名但 md5 不同时 → 触发重命名，用户列写『已移入待处理(重命名)』。"""
    from app.services import manifest_store, scanner

    s = fresh_settings
    manifest_store.upsert(_row(filename="clash.pdf"))
    (s.pending_dir / "clash.pdf").write_bytes(b"OLD")
    (s.input_dir / "clash.pdf").write_bytes(b"NEW")
    report = scanner.scan_and_stage(dry_run=False)
    assert report.renamed == 1
    assert report.staged == 1
    row = manifest_store.fetch("clash.pdf")
    assert row.import_status == "已移入待处理(重命名)"
    assert row.process_status == "已扫描(重命名)"
