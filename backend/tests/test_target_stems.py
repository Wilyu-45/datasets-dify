"""★ 2026-08 新增测试：target_stems 白名单过滤（单文件上传 + 一键入库）。

业务背景：
  单文件上传后跑流水线，target_stems=[stem] 必须只处理这一个文件，
  绝对不能处理 manifest / chunks 目录里其他走完整 Excel 流程的文档。

测试范围：
  1. parser.parse_pending(target_stems=...) 只解析白名单文件
  2. chunker.chunk_parsed(target_stems=...) 只切分白名单文件
  3. dify_ingest._list_chunk_dirs(target_stems=...) 只列白名单目录
  4. dify_ingest.upload_all_docs(target_stems=...) 只入库白名单文件
  5. pipeline.run_pipeline(target_stems=...) 正确下传白名单到三阶段
  6. 端到端：上传 file1 + file2，触发 file1 的 ingest → 只有 file1 被处理
"""

from __future__ import annotations

import importlib
import logging
from pathlib import Path
from typing import Any, Dict, List

import pytest

logging.disable(logging.CRITICAL)


# ============================================================
# 通用 fixture
# ============================================================


@pytest.fixture(autouse=True)
def fresh_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """每个用例：隔离 tmp_path 作为 data_root，并预填 Dify 配置。"""
    test_data_root = tmp_path / "data"
    monkeypatch.setenv("RAG_DATA_ROOT", str(test_data_root))
    monkeypatch.setenv("RAG_DIFY_API_KEY", "dataset-test-key")
    monkeypatch.setenv("RAG_DIFY_DATASET_ID", "test-dataset-id")
    monkeypatch.delenv("RAG_PUBLIC_BASE_URL", raising=False)
    monkeypatch.setenv("RAG_DIFY_APP_API_KEY", "app-test-key")
    monkeypatch.setenv("RAG_MINERU_API_URL", "http://fake-mineru")

    import sys
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


def _add_manifest_row(
    settings, filename: str, import_status: str = "已移入待处理",
) -> None:
    """往 manifest 追加一行（用于测试 target_stems 过滤）。"""
    from app.models.schemas import ManifestRow
    from app.services import manifest_store

    manifest_store.bootstrap(settings.data_root)
    row = ManifestRow(
        filename=filename,
        status="pending",
        md5="d41d8cd98f00b204e9800998ecf8427e",
        create_time="2026-08-04 00:00:00",
        update_time="2026-08-04 00:00:00",
        import_status=import_status,
        process_status=import_status,
    )
    manifest_store.upsert(settings.manifest_path, row)


# ============================================================
# parser.parse_pending
# ============================================================


def test_parser_target_stems_only_processes_whitelisted(
    fresh_settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """parser.parse_pending(target_stems=['file1']) 只处理 file1，不处理 file2/file3。"""
    from app.services import parser

    # 在 manifest 加 3 行
    _add_manifest_row(fresh_settings, "file1.pdf", "已移入待处理")
    _add_manifest_row(fresh_settings, "file2.pdf", "已移入待处理")
    _add_manifest_row(fresh_settings, "file3.pdf", "已移入待处理")

    # 在 pending/ 放 3 个文件
    fresh_settings.pending_dir.mkdir(parents=True, exist_ok=True)
    for n in ("file1.pdf", "file2.pdf", "file3.pdf"):
        (fresh_settings.pending_dir / n).write_bytes(b"fake pdf content")

    # 记录 MinerU 客户端被调用的文件
    called_files: List[str] = []

    class _FakeMinerUClient:
        api_url = "http://fake-mineru"

        def parse_file(self, src: Path, parsed_dir: Path):
            called_files.append(src.name)
            from app.services.mineru_client import ParseResult

            parsed_dir.mkdir(parents=True, exist_ok=True)
            (parsed_dir / "out.md").write_text(
                f"# {src.stem}\n" + "x" * 200, encoding="utf-8"
            )
            return ParseResult(
                parse_dir=parsed_dir,
                md_path=parsed_dir / "out.md",
                json_path=None,
                attempts=1,
            )

    # ★ target_stems=["file1"] → 应该只调 MinerU 解析 file1
    report = parser.parse_pending(
        dry_run=False,
        client=_FakeMinerUClient(),
        force=False,
        target_stems=["file1"],
    )

    assert called_files == ["file1.pdf"], (
        f"应该只解析 file1.pdf, 实际: {called_files}"
    )
    # 报告里只有 1 个解析成功（其他 2 个被白名单过滤掉，不会出现在 report）
    assert report.parsed == 1


def test_parser_target_stems_none_processes_all(
    fresh_settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """parser.parse_pending(target_stems=None) 保持原行为：处理所有 manifest 行。"""
    from app.services import parser

    _add_manifest_row(fresh_settings, "a.pdf", "已移入待处理")
    _add_manifest_row(fresh_settings, "b.pdf", "已移入待处理")

    fresh_settings.pending_dir.mkdir(parents=True, exist_ok=True)
    for n in ("a.pdf", "b.pdf"):
        (fresh_settings.pending_dir / n).write_bytes(b"fake")

    called_files: List[str] = []

    class _FakeMinerUClient:
        api_url = "http://fake-mineru"

        def parse_file(self, src: Path, parsed_dir: Path):
            called_files.append(src.name)
            from app.services.mineru_client import ParseResult

            parsed_dir.mkdir(parents=True, exist_ok=True)
            (parsed_dir / "out.md").write_text(
                f"# {src.stem}\n" + "x" * 200, encoding="utf-8"
            )
            return ParseResult(
                parse_dir=parsed_dir, md_path=parsed_dir / "out.md",
                json_path=None, attempts=1,
            )

    # ★ target_stems=None → 应该处理所有（a + b）
    report = parser.parse_pending(
        dry_run=False, client=_FakeMinerUClient(), force=False,
    )

    assert sorted(called_files) == ["a.pdf", "b.pdf"]
    assert report.parsed == 2


# ============================================================
# chunker.chunk_parsed
# ============================================================


def test_chunker_target_stems_only_processes_whitelisted(
    fresh_settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """chunker.chunk_parsed(target_stems=['file1']) 只切分 file1。"""
    from app.services import chunker

    # 准备 manifest: file1 和 file2 都有 parse 列
    # v2 数据要够多（≥3 块、≥1 title/paragraph、≥50 字符）才能通过 _is_parse_content_trivial 检查
    v2_data = [
        [
            {
                "type": "title",
                "content": {
                    "title_content": [
                        {"content": "第一章 概述"}
                    ]
                },
            },
            {
                "type": "paragraph",
                "content": {
                    "paragraph_content": [
                        {"content": "本章介绍本文件的基本信息与适用范围。包含足够字符以通过质量校验。"}
                    ]
                },
            },
            {
                "type": "paragraph",
                "content": {
                    "paragraph_content": [
                        {"content": "本文件规定了相关技术要求与执行标准，适用于多个业务场景。"}
                    ]
                },
            },
        ]
    ]
    import json as _json
    v2_json = _json.dumps(v2_data, ensure_ascii=False)

    for fname in ("file1.pdf", "file2.pdf"):
        stem = Path(fname).stem
        # 在 parsed_root 下建解析目录
        parsed_dir = fresh_settings.parsed_dir / stem
        parsed_dir.mkdir(parents=True, exist_ok=True)
        (parsed_dir / f"{stem}_content_list_v2.json").write_text(
            v2_json, encoding="utf-8"
        )
        (parsed_dir / f"{stem}.md").write_text(
            f"# {fname}\n\n第一章 概述\n\n本章介绍本文件的基本信息与适用范围。包含足够字符以通过质量校验。\n"
            f"本文件规定了相关技术要求与执行标准，适用于多个业务场景。\n" * 5,
            encoding="utf-8",
        )

        _add_manifest_row(fresh_settings, fname, "已移入待处理")
        from app.models.schemas import ManifestRow
        from app.services import manifest_store
        row = manifest_store.load(fresh_settings.manifest_path)[fname]
        row = row.model_copy(update={"parse": str(parsed_dir.resolve())})
        manifest_store.upsert(fresh_settings.manifest_path, row)

    # ★ target_stems=["file1"] → 只切分 file1
    report = chunker.chunk_parsed(
        dry_run=False, force=False, target_stems=["file1"],
    )

    # 验证：只有 file1 的 chunks 目录被创建
    assert (fresh_settings.chunks_dir / "file1").exists(), (
        f"chunks_dir 应该是 {fresh_settings.chunks_dir / 'file1'}, "
        f"但当前目录: {list(fresh_settings.chunks_dir.iterdir()) if fresh_settings.chunks_dir.exists() else 'NOT EXIST'}"
    )
    assert not (fresh_settings.chunks_dir / "file2").exists(), (
        "file2 不应该在 target_stems 内，不应被切分"
    )
    assert report.chunked == 1, f"应该有 1 个 chunked, 实际: {report.chunked}, failed: {report.failed}"
    assert report.failed == 0, f"不应该有失败: {report}"


# ============================================================
# dify_ingest._list_chunk_dirs
# ============================================================


def test_dify_list_chunk_dirs_target_stems(
    fresh_settings,
) -> None:
    """dify_ingest._list_chunk_dirs(target_stems=['a']) 只返回 a 目录。"""
    from app.services import dify_ingest

    # 在 chunks/ 和 output/ 下建多个目录
    fresh_settings.chunks_dir.mkdir(parents=True, exist_ok=True)
    fresh_settings.output_dir.mkdir(parents=True, exist_ok=True)
    for name in ("a", "b", "c"):
        (fresh_settings.chunks_dir / name).mkdir()
        (fresh_settings.chunks_dir / name / "chunk_001.md").write_text("x")

    # 无白名单 → 返回所有
    all_dirs = dify_ingest._list_chunk_dirs()
    assert sorted([p.name for p in all_dirs]) == ["a", "b", "c"]

    # 有白名单 → 只返回 a
    filtered = dify_ingest._list_chunk_dirs(target_stems=["a"])
    assert [p.name for p in filtered] == ["a"]

    # 多元素白名单
    filtered2 = dify_ingest._list_chunk_dirs(target_stems=["a", "c"])
    assert sorted([p.name for p in filtered2]) == ["a", "c"]

    # 空白名单（不过滤任何）→ 返回 []
    empty = dify_ingest._list_chunk_dirs(target_stems=[])
    assert empty == []


# ============================================================
# dify_ingest.upload_all_docs
# ============================================================


def test_dify_upload_target_stems_only_uploads_whitelisted(
    fresh_settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """dify_ingest.upload_all_docs(target_stems=['a']) 只入库 a 目录。"""
    from app.services import dify_ingest

    # 在 chunks/ 下建 2 个目录，每个含 chunk_metadata.json + chunk_001.md
    fresh_settings.chunks_dir.mkdir(parents=True, exist_ok=True)
    for name in ("a", "b"):
        chunk_dir = fresh_settings.chunks_dir / name
        chunk_dir.mkdir()
        (chunk_dir / "chunk_001.md").write_text(
            f"# {name}\n\n这是 {name} 的测试段落。包含足够字符。" * 10,
            encoding="utf-8",
        )
        import json as _json
        (chunk_dir / "chunk_metadata.json").write_text(
            _json.dumps(
                {
                    "stem": name,
                    "chunks": [
                        {
                            "chunk_id": "chunk_001",
                            "file_name": "chunk_001.md",
                            "title_path": name,
                            "image_refs": [],
                        }
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        # 写 manifest 行（dify_status 不为 done 才能被处理）
        from app.models.schemas import ManifestRow
        from app.services import manifest_store
        manifest_store.bootstrap(fresh_settings.data_root)
        row = ManifestRow(
            filename=f"{name}.pdf", status="chunked",
            md5="x" * 32, create_time="x", update_time="x",
            import_status="已移入待处理", process_status="已切分",
            chunks=name,  # stem
            dify_status="",  # 待处理
        )
        manifest_store.upsert(fresh_settings.manifest_path, row)

    # 记录被上传的 stem
    uploaded_stems: List[str] = []

    class _FakeDifyClient:
        def create_document_by_text(self, name: str, text: str):
            from app.services.dify_uploader import DifyDocument
            uploaded_stems.append(name)
            return DifyDocument(document_id=f"doc-{name}", name=name)

        def wait_document_ready(self, document_id: str):
            return True

        def upload_file(self, file_path, user="abc"):
            from app.services.dify_uploader import DifyUploadedFile
            return DifyUploadedFile(
                file_id=f"file-{file_path.name}",
                name=file_path.name,
                size=file_path.stat().st_size,
                extension=file_path.suffix.lstrip("."),
                mime_type="image/jpeg",
                source_url=f"https://fake/{file_path.name}",
            )

        def add_segments(self, document_id, seg_payloads):
            from app.services.dify_uploader import DifySegment
            return [
                DifySegment(
                    segment_id=f"seg-{i}",
                    document_id=document_id,
                    position=i + 1,
                    content=p["content"],
                )
                for i, p in enumerate(seg_payloads)
            ]

        def update_segment(self, document_id, segment_id, **kwargs):
            return {"id": segment_id, "content": kwargs.get("content", "")}

        def list_documents(self, **kwargs):
            return []

        def delete_document(self, document_id):
            return True

    # ★ target_stems=["a"] → 只入库 a
    report = dify_ingest.upload_all_docs(
        dry_run=False, force=False, client=_FakeDifyClient(),
        target_stems=["a"],
    )

    assert uploaded_stems == ["a"], (
        f"应该只上传 a, 实际: {uploaded_stems}"
    )
    assert report.uploaded == 1
    assert report.scanned == 1


# ============================================================
# pipeline.run_pipeline 下传 target_stems
# ============================================================


def test_pipeline_passes_target_stems_to_all_stages(
    fresh_settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PipelineRequest.target_stems 必须下传到 parse/chunk/dify 三个阶段。"""
    from app.services import pipeline

    # 记录三阶段收到的 target_stems
    received: Dict[str, Any] = {}

    def fake_parse_pending(*, dry_run, force, target_stems):
        received["parse"] = target_stems
        from app.models.schemas import ParseReport
        return ParseReport(
            dry_run=dry_run, api_url="x", scanned=0, parsed=0,
            skipped_done=0, failed=0, actions=[],
        )

    def fake_chunk_parsed(*, dry_run, force, target_stems, strategy=""):
        received["chunk"] = target_stems
        from app.models.schemas import ChunkReport
        return ChunkReport(
            dry_run=dry_run, scanned=0, chunked=0,
            skipped_done=0, failed=0, actions=[],
        )

    def fake_upload_all_docs(*, dry_run, force, client=None, target_stems):
        received["dify"] = target_stems
        from app.models.schemas import DifyUploadReport
        return DifyUploadReport(
            dry_run=dry_run, scanned=0, uploaded=0,
            skipped_done=0, failed=0, actions=[],
            api_url="x", dataset_id="y",
        )

    monkeypatch.setattr(pipeline.parser, "parse_pending", fake_parse_pending)
    monkeypatch.setattr(pipeline.chunker, "chunk_parsed", fake_chunk_parsed)
    monkeypatch.setattr(pipeline.dify_ingest, "upload_all_docs", fake_upload_all_docs)

    from app.services.pipeline import PipelineRequest, PipelineStep
    req = PipelineRequest(
        scan=PipelineStep(enabled=False),
        parse=PipelineStep(enabled=True),
        chunk=PipelineStep(enabled=True),
        dify=PipelineStep(enabled=True),
        target_stems=["file1"],
    )
    report = pipeline.run_pipeline(req)

    # ★ 三阶段都收到了 target_stems
    assert received == {
        "parse": ["file1"],
        "chunk": ["file1"],
        "dify": ["file1"],
    }
    # ★ report.to_dict() 也包含 target_stems
    d = report.to_dict()
    assert d["target_stems"] == ["file1"]


def test_pipeline_without_target_stems_passes_none(
    fresh_settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """未指定 target_stems 时 → 三阶段都收到 None。"""
    from app.services import pipeline

    received: Dict[str, Any] = {}

    def fake_parse_pending(*, dry_run, force, target_stems):
        received["parse"] = target_stems
        from app.models.schemas import ParseReport
        return ParseReport(
            dry_run=dry_run, api_url="x", scanned=0, parsed=0,
            skipped_done=0, failed=0, actions=[],
        )

    def fake_chunk_parsed(*, dry_run, force, target_stems, strategy=""):
        received["chunk"] = target_stems
        from app.models.schemas import ChunkReport
        return ChunkReport(
            dry_run=dry_run, scanned=0, chunked=0,
            skipped_done=0, failed=0, actions=[],
        )

    def fake_upload_all_docs(*, dry_run, force, client=None, target_stems):
        received["dify"] = target_stems
        from app.models.schemas import DifyUploadReport
        return DifyUploadReport(
            dry_run=dry_run, scanned=0, uploaded=0,
            skipped_done=0, failed=0, actions=[],
            api_url="x", dataset_id="y",
        )

    monkeypatch.setattr(pipeline.parser, "parse_pending", fake_parse_pending)
    monkeypatch.setattr(pipeline.chunker, "chunk_parsed", fake_chunk_parsed)
    monkeypatch.setattr(pipeline.dify_ingest, "upload_all_docs", fake_upload_all_docs)

    from app.services.pipeline import PipelineRequest, PipelineStep
    req = PipelineRequest(
        scan=PipelineStep(enabled=False),
        parse=PipelineStep(enabled=True),
        chunk=PipelineStep(enabled=True),
        dify=PipelineStep(enabled=True),
    )
    pipeline.run_pipeline(req)

    assert received == {"parse": None, "chunk": None, "dify": None}


# ============================================================
# 端到端：单文件上传只处理这个文件
# ============================================================


def test_single_upload_only_processes_target_file(
    fresh_settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """端到端：上传 file1.pdf（auto_ingest=True），验证只有 file1 被处理。

    场景：
    - manifest 已有 file1.pdf + file2.pdf 两行
    - 用户上传 file1.pdf（auto_ingest=True）
    - 触发 pipeline 时 target_stems=["file1"]
    - 必须只处理 file1，file2 不被触碰
    """
    from fastapi.testclient import TestClient

    from app.services import manifest_store
    from app.main import app

    # 预填 2 个待处理文件
    fresh_settings.pending_dir.mkdir(parents=True, exist_ok=True)
    (fresh_settings.pending_dir / "file1.pdf").write_bytes(b"file1 content")
    (fresh_settings.pending_dir / "file2.pdf").write_bytes(b"file2 content")
    _add_manifest_row(fresh_settings, "file1.pdf", "已移入待处理")
    _add_manifest_row(fresh_settings, "file2.pdf", "已移入待处理")

    # 用 fake pipeline 拦截，记录接收到的 stem
    received_stems: List[str] = []

    def fake_pipeline(target_stem: str, profile=None):
        received_stems.append(target_stem)
        return {
            "status": "ok",
            "target_stems": [target_stem],
            "scan": {},
            "parse": {"scanned": 0, "parsed": 0, "failed": 0, "skipped_done": 0},
            "chunk": {"scanned": 0, "chunked": 0, "failed": 0, "skipped_done": 0},
            "dify": {"scanned": 0, "uploaded": 0, "failed": 0, "skipped_done": 0},
        }

    monkeypatch.setattr(
        "app.api.upload._run_single_file_pipeline", fake_pipeline,
    )

    client = TestClient(app)
    # 上传 file1.pdf，auto_ingest=True
    resp = client.post(
        "/api/upload/single",
        files={"file": ("file1.pdf", b"file1 content new", "application/pdf")},
        data={"auto_ingest": "true"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["pipeline"] is not None
    assert data["pipeline"]["status"] == "ok"

    # ★ 关键断言：pipeline 接收到的 target_stems 必须是 ["file1"]
    assert received_stems == ["file1"], (
        f"应该只处理 file1，实际收到: {received_stems}"
    )
    # ★ response 里也带 target_stems
    assert data["pipeline"]["target_stems"] == ["file1"]

    # 验证 manifest 仍有 file2.pdf（没被处理）
    manifest = manifest_store.load(fresh_settings.manifest_path)
    assert "file2.pdf" in manifest
    # file2 仍是 pending（没有被 pipeline 改写）
    assert manifest["file2.pdf"].import_status == "已移入待处理"


def test_single_ingest_endpoint_passes_stem(
    fresh_settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """/api/upload/single/ingest?filename=foo.pdf → 传入 ["foo"] 给 pipeline。"""
    from fastapi.testclient import TestClient

    from app.main import app

    received: List[str] = []

    def fake_pipeline(target_stem: str, profile=None):
        received.append(target_stem)
        return {"status": "ok", "target_stems": [target_stem]}

    monkeypatch.setattr(
        "app.api.upload._run_single_file_pipeline", fake_pipeline,
    )

    client = TestClient(app)
    resp = client.post("/api/upload/single/ingest?filename=foo.pdf")
    assert resp.status_code == 200
    # ★ 传入的是 stem（去扩展名）
    assert received == ["foo"]
