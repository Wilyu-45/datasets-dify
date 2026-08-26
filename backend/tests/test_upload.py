"""plan.md §3 单文件上传 + 一键入库 API 单元测试。

覆盖：
1. POST /api/upload/single 文件上传 + manifest 写入
2. POST /api/upload/single/ingest 已上传文件触发全流程
3. 文件名清理与扩展名校验
4. 重复文件处理（md5 匹配跳过 / 不匹配重命名）
"""

from __future__ import annotations

import importlib
import io
import logging
from pathlib import Path

import pytest

logging.disable(logging.CRITICAL)


@pytest.fixture(autouse=True)
def fresh_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """每个用例：隔离 tmp_path 作为 data_root，并预填 Dify 配置。"""
    test_data_root = tmp_path / "data"
    monkeypatch.setenv("RAG_DATA_ROOT", str(test_data_root))
    monkeypatch.setenv("RAG_DIFY_API_KEY", "dataset-test-key")
    monkeypatch.setenv("RAG_DIFY_DATASET_ID", "test-dataset-id")
    # ★ 测试环境禁止外部 API 调起
    monkeypatch.delenv("RAG_PUBLIC_BASE_URL", raising=False)
    monkeypatch.setenv("RAG_DIFY_APP_API_KEY", "app-test-key")
    # ★ 防止 PyMuPDF 实际调起 mineru API
    monkeypatch.setenv("RAG_MINERU_API_URL", "http://fake-mineru")

    import sys
    # 清理可能缓存的 app.main，避免 settings 引用旧值
    for mod_name in list(sys.modules.keys()):
        if mod_name.startswith("app."):
            del sys.modules[mod_name]
    if "app" in sys.modules:
        del sys.modules["app"]

    from app import config as cfg_mod
    importlib.reload(cfg_mod)
    # ★ .env 优先级高于环境变量（settings_customise_sources），
    #   必须用 init kwargs 覆盖 data_root 才能真正隔离测试目录
    settings = cfg_mod.Settings(data_root=test_data_root)
    cfg_mod.settings = settings

    from app.services import (
        chunker,
        dify_ingest,
        dify_uploader,
        image_host,
        manifest_store,
        parser,
        scanner,
    )
    scanner.settings = settings
    parser.settings = settings
    chunker.settings = settings
    dify_uploader.settings = settings
    dify_ingest.settings = settings
    image_host.settings = settings
    manifest_store.settings = settings

    settings.ensure_dirs()
    yield settings


def _make_test_pdf_bytes() -> bytes:
    """生成一个最小合法 PDF 字节串（仅用于文件大小/类型测试）。"""
    # 最小 PDF 结构：1 页空白
    return (
        b"%PDF-1.4\n"
        b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
        b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
        b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]>>endobj\n"
        b"xref\n0 4\n0000000000 65535 f\n0000000009 00000 n\n"
        b"0000000054 00000 n\n0000000101 00000 n\ntrailer<</Size 4/Root 1 0 R>>\n"
        b"startxref\n149\n%%EOF"
    )


def test_safe_stem_basic() -> None:
    """_safe_stem 正常文件名清理。"""
    from app.api.upload import _safe_stem

    assert _safe_stem("test.pdf") == "test"
    assert _safe_stem("中文文档.docx") == "中文文档"
    # Windows 禁止字符替换
    assert _safe_stem("a/b\\c.pdf").replace("/", "_").replace("\\", "_") in (
        _safe_stem("a/b\\c.pdf"),
    )
    # 空 stem 兜底（纯扩展名视为空）
    from pathlib import Path as _P
    empty_stem = _P(".pdf").stem
    if not empty_stem:
        assert _safe_stem(".pdf").startswith("uploaded_")
    # 全部点号的 stem → 清理后变空 → 兜底
    assert _safe_stem("...").startswith("uploaded_")


def test_upload_endpoint_rejects_unsupported_ext(tmp_path: Path, fresh_settings) -> None:
    """不支持的扩展名 → 400。"""
    from fastapi.testclient import TestClient

    from app.main import app

    client = TestClient(app)
    resp = client.post(
        "/api/upload/single",
        files={"file": ("test.txt", b"hello world", "text/plain")},
    )
    assert resp.status_code == 400
    assert "不支持的扩展名" in resp.json()["detail"]


def test_upload_endpoint_no_filename(fresh_settings) -> None:
    """无文件名 → 400 / 422。"""
    from fastapi.testclient import TestClient

    from app.main import app

    client = TestClient(app)
    resp = client.post(
        "/api/upload/single",
        files={"file": ("", b"hello", "application/octet-stream")},
    )
    # FastAPI 把空文件名视为 422（Unprocessable Entity）
    assert resp.status_code in (400, 422)


def test_upload_endpoint_creates_manifest_row(
    fresh_settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """单文件上传后应：
    1. 保存到 input/
    2. 在 manifest 表（PostgreSQL）插入一行
    3. auto_ingest=False 时不触发 pipeline
    """
    from fastapi.testclient import TestClient

    from app.services import manifest_store
    # ★ 在 reload settings 之后 import app（否则 app.main 会被旧 settings 初始化）
    from app.main import app

    # ★ 阻止 pipeline 跑通（测试只关心上传行为，不关心 pipeline 内部）
    monkeypatch.setattr(
        "app.api.upload._run_single_file_pipeline",
        lambda *a, **k: {"status": "skipped", "scan": {}, "parse": {}, "chunk": {}, "dify": {}},
    )

    client = TestClient(app)
    pdf_bytes = _make_test_pdf_bytes()
    resp = client.post(
        "/api/upload/single",
        files={"file": ("test_doc.pdf", pdf_bytes, "application/pdf")},
        data={"auto_ingest": "false"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()

    # 1) 文件已落盘到 pending/
    assert (fresh_settings.pending_dir / "test_doc.pdf").exists()

    # 2) manifest 写入了行
    manifest = manifest_store.load(fresh_settings.manifest_path)
    assert "test_doc.pdf" in manifest
    assert manifest["test_doc.pdf"].import_status == "已移入待处理"

    # 3) auto_ingest=False → pipeline=None
    assert data["pipeline"] is None
    assert data["filename"] == "test_doc.pdf"
    assert data["size"] == len(pdf_bytes)
    assert len(data["md5"]) == 32  # md5 hex


def test_upload_endpoint_duplicate_md5_no_clobber(
    fresh_settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """重复上传同 md5 文件 → input/ 已有则跳过移动（不覆盖）。"""
    from fastapi.testclient import TestClient

    from app.main import app

    monkeypatch.setattr(
        "app.api.upload._run_single_file_pipeline",
        lambda *a, **k: {"status": "skipped"},
    )

    client = TestClient(app)
    pdf_bytes = _make_test_pdf_bytes()

    # 第一次上传
    resp1 = client.post(
        "/api/upload/single",
        files={"file": ("test.pdf", pdf_bytes, "application/pdf")},
        data={"auto_ingest": "false"},
    )
    assert resp1.status_code == 200

    # 第二次上传（同名同 md5）
    resp2 = client.post(
        "/api/upload/single",
        files={"file": ("test.pdf", pdf_bytes, "application/pdf")},
        data={"auto_ingest": "false"},
    )
    assert resp2.status_code == 200
    # pending/ 中只有一个文件
    files = list(fresh_settings.pending_dir.iterdir())
    assert len(files) == 1
    assert files[0].name == "test.pdf"


def test_upload_endpoint_auto_ingest_called(
    fresh_settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """auto_ingest=True → 触发 pipeline。
    ★ 2026-08 升级：pipeline 接收的是 stem（不含扩展名），用于 target_stems 白名单。
    """
    from fastapi.testclient import TestClient

    from app.main import app

    call_args: list = []

    def fake_pipeline(target_stem: str, profile=None):
        call_args.append(target_stem)
        return {
            "status": "ok",
            "scan": {"scanned": 0, "staged": 0},
            "parse": {"scanned": 0, "parsed": 0, "failed": 0, "skipped_done": 0},
            "chunk": {"scanned": 0, "chunked": 0, "failed": 0, "skipped_done": 0},
            "dify": {"scanned": 0, "uploaded": 0, "failed": 0, "skipped_done": 0},
        }

    monkeypatch.setattr(
        "app.api.upload._run_single_file_pipeline",
        fake_pipeline,
    )

    client = TestClient(app)
    pdf_bytes = _make_test_pdf_bytes()
    resp = client.post(
        "/api/upload/single",
        files={"file": ("auto_ingest_test.pdf", pdf_bytes, "application/pdf")},
        data={"auto_ingest": "true"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["pipeline"] is not None
    assert data["pipeline"]["status"] == "ok"
    # ★ 2026-08：传入的是 stem（用于 target_stems 白名单）
    assert call_args == ["auto_ingest_test"]


def test_ingest_endpoint_invokes_pipeline(
    fresh_settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """POST /api/upload/single/ingest 触发 pipeline。
    ★ 2026-08 升级：pipeline 接收的是 stem（不含扩展名）。
    """
    from fastapi.testclient import TestClient

    from app.main import app

    call_args: list = []

    def fake_pipeline(target_stem: str, profile=None):
        call_args.append(target_stem)
        return {"status": "ok", "scan": {}, "parse": {}, "chunk": {}, "dify": {}}

    monkeypatch.setattr(
        "app.api.upload._run_single_file_pipeline",
        fake_pipeline,
    )

    client = TestClient(app)
    resp = client.post("/api/upload/single/ingest?filename=test.pdf")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"
    # ★ 2026-08：传入的是 stem
    assert call_args == ["test"]
