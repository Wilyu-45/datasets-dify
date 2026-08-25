"""scanner §3.1 端到端测试（Excel 驱动）。"""

from __future__ import annotations

import importlib
import logging
from pathlib import Path

import pytest

# 抑制日志输出，避免污染 pytest
logging.disable(logging.CRITICAL)


@pytest.fixture(autouse=True)
def fresh_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """每个用例：设置 RAG_DATA_ROOT → tmp_path/<uuid>，重新实例化 settings。"""
    test_data_root = tmp_path / "data"
    monkeypatch.setenv("RAG_DATA_ROOT", str(test_data_root))

    # 重新加载 config 模块以拿到新 env
    from app import config as cfg_mod
    importlib.reload(cfg_mod)
    settings = cfg_mod.settings

    # 把 services 引用的 settings 同步替换
    from app.services import scanner
    scanner.settings = settings

    settings.ensure_dirs()
    yield settings


def _put(path: Path, content: bytes = b"hello") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def _row(**kw):
    from app.models.schemas import ManifestRow
    return ManifestRow(**kw)


def _ensure_manifest_with_rows(path: Path, filenames: list[str]) -> None:
    """在 manifest 表（PostgreSQL）中写入 filename 行（导入情况留空）。"""
    from app.services import manifest_store
    manifest_store.ensure_exists(path)
    for name in filenames:
        manifest_store.upsert(path, _row(filename=name))


# ============ Excel 驱动核心测试 ============


def test_empty_manifest_no_input_no_action(fresh_settings):
    """manifest 为空 → 什么也不做。"""
    from app.services import scanner

    s = fresh_settings
    _put(s.input_dir / "a.pdf", b"AAA")  # input 有文件

    report = scanner.scan_and_stage(dry_run=False)
    # 没有任何 manifest 行，所以无文件被处理
    assert report.staged == 0
    assert report.missing_on_disk == 0
    # input/ 里的文件不应被移动
    assert (s.input_dir / "a.pdf").exists()
    assert not (s.pending_dir / "a.pdf").exists()


def test_manifest_with_empty_import_status_moves_file(fresh_settings):
    """Excel 中「导入情况」为空 → 移入 pending/。"""
    from app.services import manifest_store, scanner

    s = fresh_settings
    _ensure_manifest_with_rows(s.manifest_path, ["a.pdf", "b.docx"])
    _put(s.input_dir / "a.pdf", b"AAA")
    _put(s.input_dir / "b.docx", b"BBB")

    report = scanner.scan_and_stage(dry_run=False)
    assert report.staged == 2
    assert report.new == 2
    assert not (s.input_dir / "a.pdf").exists()
    assert (s.pending_dir / "a.pdf").exists()
    assert (s.pending_dir / "b.docx").exists()

    manifest = manifest_store.load(s.manifest_path)
    for name in ("a.pdf", "b.docx"):
        row = manifest[name]
        assert row.status == "pending"
        assert row.md5 and len(row.md5) == 32
        assert row.import_status == "已移入待处理"
        assert row.process_status == "已扫描"


def test_already_imported_is_skipped(fresh_settings):
    """Excel 中「导入情况」已非空 → 跳过（幂等）。"""
    from app.services import manifest_store, scanner

    s = fresh_settings
    _put(s.input_dir / "a.pdf", b"AAA")
    manifest_store.ensure_exists(s.manifest_path)
    manifest_store.upsert(
        s.manifest_path,
        _row(filename="a.pdf", import_status="已移入待处理", process_status="已扫描"),
    )

    report = scanner.scan_and_stage(dry_run=False)
    assert report.staged == 0
    assert report.skipped_done == 1
    # input/ 文件不应被移动
    assert (s.input_dir / "a.pdf").exists()
    assert not (s.pending_dir / "a.pdf").exists()


def test_status_done_with_empty_import_status_is_still_moved(fresh_settings):
    """注意：旧 status=done 字段 + 新「导入情况」列语义不同。
    新规则：只看「导入情况」列；为空就移入。
    这里验证：旧 status=done 行的「导入情况」为空 → 仍会被移入 pending/。
    """
    from app.services import manifest_store, scanner

    s = fresh_settings
    _put(s.input_dir / "x.pdf", b"X")
    manifest_store.ensure_exists(s.manifest_path)
    manifest_store.upsert(
        s.manifest_path,
        _row(filename="x.pdf", status="done", md5="0" * 32),
    )

    report = scanner.scan_and_stage(dry_run=False)
    # 因为「导入情况」为空，所以会被处理
    assert report.staged == 1
    assert (s.pending_dir / "x.pdf").exists()


def test_manifest_says_not_imported_but_input_missing(fresh_settings):
    """Excel 中「导入情况」为空，但 input/ 找不到该文件 → MISSING。"""
    from app.services import manifest_store, scanner

    s = fresh_settings
    _ensure_manifest_with_rows(s.manifest_path, ["ghost.pdf"])
    # input/ 没有 ghost.pdf

    report = scanner.scan_and_stage(dry_run=False)
    assert report.missing_on_disk == 1
    assert report.staged == 0
    # manifest 行不被删除
    manifest = manifest_store.load(s.manifest_path)
    assert "ghost.pdf" in manifest
    # 「导入情况」仍为空（不自动写）
    assert manifest["ghost.pdf"].import_status is None


def test_idempotent_second_run(fresh_settings):
    """第二次扫描：所有行的「导入情况」都已非空 → 全部跳过。"""
    from app.services import scanner

    s = fresh_settings
    _ensure_manifest_with_rows(s.manifest_path, ["a.pdf"])
    _put(s.input_dir / "a.pdf", b"AAA")

    first = scanner.scan_and_stage(dry_run=False)
    assert first.staged == 1

    second = scanner.scan_and_stage(dry_run=False)
    assert second.staged == 0
    assert second.skipped_done == 1


def test_dry_run_does_not_move(fresh_settings):
    """dry_run=True：不移动文件、不写盘（导入情况保持空）。"""
    from app.services import manifest_store, scanner

    s = fresh_settings
    _ensure_manifest_with_rows(s.manifest_path, ["dry.pdf"])
    _put(s.input_dir / "dry.pdf", b"DD")
    report = scanner.scan_and_stage(dry_run=True)
    assert report.dry_run is True
    # dry_run 时仍记录为 new，但文件不移动、manifest 不写
    assert report.new == 1
    assert (s.input_dir / "dry.pdf").exists()
    assert not (s.pending_dir / "dry.pdf").exists()
    manifest = manifest_store.load(s.manifest_path)
    row = manifest["dry.pdf"]
    # dry_run 不写盘 → 导入情况保持空
    assert row.import_status is None


def test_mixed_imported_and_pending(fresh_settings):
    """混合：部分行已导入（跳过），部分未导入（移入）。"""
    from app.services import manifest_store, scanner

    s = fresh_settings
    manifest_store.ensure_exists(s.manifest_path)
    # done1：已导入
    manifest_store.upsert(
        s.manifest_path,
        _row(filename="done1.pdf", import_status="已移入待处理"),
    )
    # new1：未导入
    manifest_store.upsert(s.manifest_path, _row(filename="new1.pdf"))

    _put(s.input_dir / "new1.pdf", b"NEW")
    _put(s.input_dir / "done1.pdf", b"DONE")

    report = scanner.scan_and_stage(dry_run=False)
    assert report.staged == 1
    assert report.new == 1
    assert report.skipped_done == 1
    assert (s.pending_dir / "new1.pdf").exists()
    # done1 不应被移动
    assert (s.input_dir / "done1.pdf").exists()
    assert not (s.pending_dir / "done1.pdf").exists()


def test_collision_rename_marks_in_user_columns(fresh_settings):
    """pending/ 同名但 md5 不同时 → 触发重命名，列写『已移入待处理(重命名)』。"""
    from app.services import manifest_store, scanner

    s = fresh_settings
    _ensure_manifest_with_rows(s.manifest_path, ["clash.pdf"])
    (s.pending_dir / "clash.pdf").write_bytes(b"OLD")
    (s.input_dir / "clash.pdf").write_bytes(b"NEW")

    report = scanner.scan_and_stage(dry_run=False)
    assert report.renamed == 1
    assert report.staged == 1

    manifest = manifest_store.load(s.manifest_path)
    row = manifest["clash.pdf"]
    assert row.import_status == "已移入待处理(重命名)"
    assert row.process_status == "已扫描(重命名)"


def test_bootstrap_does_not_move_files(fresh_settings):
    """启动时 bootstrap 不会移动任何文件。"""
    from app.services import manifest_store

    s = fresh_settings
    _put(s.input_dir / "a.pdf", b"AAA")

    # 仅调用 bootstrap
    manifest_store.bootstrap(s.data_root)

    # 文件不应被移动
    assert (s.input_dir / "a.pdf").exists()
    assert not (s.pending_dir / "a.pdf").exists()


# ============ 扩展名自动补全测试 ============


def test_filename_without_extension_is_resolved(fresh_settings):
    """Excel 写的文件名不含扩展名 → 按 allowed_extensions 顺序在 input/ 找。"""
    from app.services import scanner

    s = fresh_settings
    _ensure_manifest_with_rows(s.manifest_path, ["文档1"])
    _put(s.input_dir / "文档1.pdf", b"PDF DATA")

    report = scanner.scan_and_stage(dry_run=False)
    assert report.staged == 1
    # 实际文件在 pending/
    assert (s.pending_dir / "文档1.pdf").exists()
    # input/ 已清空
    assert not (s.input_dir / "文档1.pdf").exists()


def test_extension_priority_pdf_over_docx(fresh_settings):
    """同名文件 .pdf 和 .docx 同时存在 → 优先 .pdf。"""
    from app.services import scanner

    s = fresh_settings
    _ensure_manifest_with_rows(s.manifest_path, ["歧义文件"])
    _put(s.input_dir / "歧义文件.docx", b"DOCX DATA")
    _put(s.input_dir / "歧义文件.pdf", b"PDF DATA")

    report = scanner.scan_and_stage(dry_run=False)
    assert report.staged == 1
    # 移动的是 .pdf（高优先级）
    assert (s.pending_dir / "歧义文件.pdf").exists()
    assert not (s.pending_dir / "歧义文件.docx").exists()
    # .docx 留在 input/（因为我们没移走它）
    assert (s.input_dir / "歧义文件.docx").exists()


def test_filename_with_extension_exact_match(fresh_settings):
    """Excel 写的文件名已含扩展名 → 精确匹配。"""
    from app.services import scanner

    s = fresh_settings
    _ensure_manifest_with_rows(s.manifest_path, ["已含扩展.pdf"])
    _put(s.input_dir / "已含扩展.pdf", b"DATA")

    report = scanner.scan_and_stage(dry_run=False)
    assert report.staged == 1
    assert (s.pending_dir / "已含扩展.pdf").exists()


def test_manifest_filename_updated_to_actual(fresh_settings):
    """扫描后，manifest 的「文件名称」字段被更新为带扩展名的真实文件名。"""
    from app.services import manifest_store, scanner

    s = fresh_settings
    _ensure_manifest_with_rows(s.manifest_path, ["无扩展"])
    _put(s.input_dir / "无扩展.pdf", b"DATA")

    scanner.scan_and_stage(dry_run=False)
    manifest = manifest_store.load(s.manifest_path)
    # 旧 key 消失，新 key 出现
    assert "无扩展" not in manifest
    assert "无扩展.pdf" in manifest
    row = manifest["无扩展.pdf"]
    assert row.import_status == "已移入待处理"


def test_no_extension_match_is_missing(fresh_settings):
    """试遍所有扩展名都找不到 → MISSING。"""
    from app.services import scanner

    s = fresh_settings
    _ensure_manifest_with_rows(s.manifest_path, ["完全不存在的文件"])
    # 不放任何文件到 input/

    report = scanner.scan_and_stage(dry_run=False)
    assert report.missing_on_disk == 1
    assert report.staged == 0
    # manifest 行不被改写
    from app.services import manifest_store
    manifest = manifest_store.load(s.manifest_path)
    assert "完全不存在的文件" in manifest
    assert manifest["完全不存在的文件"].import_status is None


def test_second_scan_uses_actual_filename(fresh_settings):
    """第二次扫描：manifest 已是带扩展名的 key → 直接精确匹配。"""
    from app.services import scanner

    s = fresh_settings
    _ensure_manifest_with_rows(s.manifest_path, ["无扩展"])
    _put(s.input_dir / "无扩展.pdf", b"DATA")

    first = scanner.scan_and_stage(dry_run=False)
    assert first.staged == 1
    # 第二次应直接用 '无扩展.pdf' 匹配（不会再 probe 扩展名）
    second = scanner.scan_and_stage(dry_run=False)
    assert second.staged == 0
    assert second.skipped_done == 1
