"""§3.2 解析测试（mock MinerU API）。"""

from __future__ import annotations

import importlib
import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

# 抑制日志输出
logging.disable(logging.CRITICAL)


# ============ fixtures ============


@pytest.fixture(autouse=True)
def fresh_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """每个用例：设置 RAG_DATA_ROOT → tmp_path/<uuid>，重新实例化 settings。"""
    test_data_root = tmp_path / "data"
    monkeypatch.setenv("RAG_DATA_ROOT", str(test_data_root))

    from app import config as cfg_mod
    importlib.reload(cfg_mod)
    settings = cfg_mod.settings

    # 把 services 引用的 settings 同步替换
    from app.services import scanner, parser
    scanner.settings = settings
    parser.settings = settings

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
    """在 manifest.xlsx 中创建 17 列表头并写入 filename 行。"""
    from app.services import manifest_store
    manifest_store.ensure_exists(path)
    for name in filenames:
        manifest_store.upsert(path, _row(filename=name))


def _put_in_pending(settings, name: str, content: bytes = b"PDF DATA") -> Path:
    return _put(settings.pending_dir / name, content)


def _make_manifest_row_with_import_status(settings, filename: str):
    """在 manifest 里建一行：import_status=已移入待处理，parse 列为空。"""
    from app.services import manifest_store
    manifest_store.ensure_exists(settings.manifest_path)
    manifest_store.upsert(
        settings.manifest_path,
        _row(filename=filename, import_status="已移入待处理", process_status="已扫描"),
    )


# ============ mock MinerUClient ============


class FakeResult:
    """Fake ParseResult for monkeypatch."""

    def __init__(self, *, parse_dir: Path, md_text: str = "# title", json_obj: Optional[Dict[str, Any]] = None) -> None:
        self.parse_dir = parse_dir
        self.md_path = parse_dir / f"{parse_dir.name}.md"
        self.json_path = parse_dir / f"{parse_dir.name}.json"
        self.images: List[Path] = []
        self.other_files: List[Path] = []
        self.attempts = 1
        self.response_kind = "fake"
        # 落盘 .md / .json
        parse_dir.mkdir(parents=True, exist_ok=True)
        self.md_path.write_text(md_text, encoding="utf-8")
        self.json_path.write_text(
            json.dumps(json_obj or {"blocks": []}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    @property
    def file_count(self) -> int:
        return 2  # .md + .json


class FakeMinerUClient:
    """完全可控的 fake：可注入成功/失败/重试次数。

    注意：mineru_client.parse_file 内部自带重试循环，但 fake 是 monkeypatch 到
    parser.MinerUClient 的，所以 fake 必须自己实现重试循环以模拟该行为。
    也就是说：fake.parse_file 被调用一次 = parser 调 client.parse_file 一次。
    fake 内部循环 max_retries 次，每次都决定抛/返。
    """

    def __init__(
        self,
        *,
        raise_with: Optional[Exception] = None,
        attempts_before_fail: int = 0,
        max_retries: int = 3,
        backoff: float = 0.0,
        fail_predicate=None,
    ) -> None:
        """raise_with: 设置后 fake 调 parse_file 会抛；
        attempts_before_fail: N>0 时前 N 次抛，之后成功；
        attempts_before_fail=0 + raise_with 设置：一直失败（重试 max_retries 次后抛）。
        fail_predicate: 可选 callable(filename) -> bool，返回 True 表示该文件注定失败。
                        若提供，则 raise_with 仅作用于 fail_predicate(filename)=True 的文件。
        """
        self.raise_with = raise_with
        self.attempts_before_fail = attempts_before_fail
        self.max_retries = max_retries
        self.backoff = backoff
        self.fail_predicate = fail_predicate
        self.calls: List[Path] = []
        self.api_url = "http://fake-mineru"
        # 与 MinerUClient 对齐：pdf_fallback 通过 client.backend 记录日志
        self.backend = "hybrid-engine"

    def parse_file(self, file_path: Path, parsed_dir: Path):
        import time as _t

        # 是否对当前文件抛错
        should_fail = (
            self.raise_with is not None
            and (self.fail_predicate is None or self.fail_predicate(file_path.name))
        )

        last_err: Optional[Exception] = None
        for attempt in range(1, self.max_retries + 1):
            self.calls.append(file_path)
            n = len(self.calls)
            if should_fail and (
                n <= self.attempts_before_fail or self.attempts_before_fail == 0
            ):
                last_err = self.raise_with
                if attempt < self.max_retries and self.backoff:
                    _t.sleep(self.backoff ** (attempt - 1))
                continue
            # 成功
            return FakeResult(parse_dir=parsed_dir)
        # 全部失败
        assert last_err is not None
        raise last_err


def _install_fake_client(monkeypatch, fake: FakeMinerUClient):
    from app.services import parser
    monkeypatch.setattr(parser, "MinerUClient", lambda: fake)


# ============ 列扩展测试 ============


def test_manifest_auto_adds_parse_and_chunks_columns(fresh_settings):
    """旧 16 列的 manifest 启动时自动追加『parse』『chunks』『dify_doc_id』『dify_status』列到 20 列。"""
    from app.services import manifest_store
    s = fresh_settings

    # 16 列的 manifest
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.append([
        "序号", "文件名称", "一级分类", "二级分类", "关键词标签",
        "适用科室", "生效日期", "导入情况", "处理情况", "校对",
        "处理说明", "status", "md5", "create_time", "update_time", "error_msg",
    ])
    ws.append([1, "x.pdf", "a", "b", "c", "d", "e", "已移入待处理", "已扫描", "", "", "pending", "abc", "2026-01-01", "", ""])
    wb.save(s.manifest_path)
    wb.close()

    # bootstrap → 补列
    changed, headers = manifest_store.ensure_columns(s.manifest_path)
    assert changed is True
    assert headers[-1] == "dify_status"
    assert headers[-2] == "dify_doc_id"
    assert headers[-3] == "chunks"
    assert headers[-4] == "parse"
    assert len(headers) == 20


def test_manifest_loads_parse_column(fresh_settings):
    """manifest 加载后 ManifestRow.parse 字段能正确读出。"""
    from app.services import manifest_store
    s = fresh_settings

    manifest_store.ensure_exists(s.manifest_path)
    manifest_store.upsert(
        s.manifest_path,
        _row(filename="a.pdf", parse="已解析 → data/parsed/a/"),
    )
    manifest = manifest_store.load(s.manifest_path)
    assert manifest["a.pdf"].parse == "已解析 → data/parsed/a/"


# ============ 主流程测试 ============


def test_parse_pending_success_moves_outputs_to_parsed(fresh_settings, monkeypatch):
    """正常解析：pending/ 中的文件 → mineru API → 落盘到 parsed/{stem}/。"""
    from app.services import parser
    s = fresh_settings
    _make_manifest_row_with_import_status(s, "a.pdf")
    _put_in_pending(s, "a.pdf", b"DATA")

    fake = FakeMinerUClient()
    _install_fake_client(monkeypatch, fake)

    report = parser.parse_pending(dry_run=False)
    assert report.parsed == 1
    assert report.failed == 0
    # 输出目录
    out_dir = s.parsed_dir / "a"
    assert out_dir.is_dir()
    assert (out_dir / "a.md").is_file()
    assert (out_dir / "a.json").is_file()
    # manifest 已更新
    from app.services import manifest_store
    manifest = manifest_store.load(s.manifest_path)
    row = manifest["a.pdf"]
    assert "parsed" in row.parse.lower() or "data/parsed" in (row.parse or "")
    assert row.status == "parsing_done"


def test_parse_pending_dry_run_does_not_call_api(fresh_settings, monkeypatch):
    """dry_run=True：不调 API、不动文件。"""
    from app.services import parser, manifest_store
    s = fresh_settings
    _make_manifest_row_with_import_status(s, "dry.pdf")
    _put_in_pending(s, "dry.pdf", b"DATA")

    fake = FakeMinerUClient()
    _install_fake_client(monkeypatch, fake)

    report = parser.parse_pending(dry_run=True)
    assert report.parsed == 1
    assert report.dry_run is True
    # fake 没被调用
    assert fake.calls == []
    # 文件还在 pending/
    assert (s.pending_dir / "dry.pdf").exists()
    # manifest parse 列被标记为"试运行-已识别"
    manifest = manifest_store.load(s.manifest_path)
    assert manifest["dry.pdf"].parse == "试运行-已识别"


def test_parse_already_parsed_is_skipped(fresh_settings, monkeypatch):
    """parse 列非空 → 跳过（幂等）。"""
    from app.services import parser, manifest_store
    s = fresh_settings

    # 准备：parse 列已有内容 + parsed/a/ 目录有效
    out_dir = s.parsed_dir / "a"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "a.md").write_text("# title", encoding="utf-8")

    manifest_store.ensure_exists(s.manifest_path)
    manifest_store.upsert(
        s.manifest_path,
        _row(
            filename="a.pdf",
            import_status="已移入待处理",
            parse=str(out_dir.resolve()),
        ),
    )

    fake = FakeMinerUClient()
    _install_fake_client(monkeypatch, fake)

    report = parser.parse_pending(dry_run=False)
    assert report.skipped_done == 1
    assert report.parsed == 0
    # 没调 API
    assert fake.calls == []


def test_parse_failure_moves_file_to_error(fresh_settings, monkeypatch):
    """重试耗尽后失败：原文件移入 error/，manifest 标记 error。"""
    from app.services import mineru_client, parser, manifest_store
    s = fresh_settings
    _make_manifest_row_with_import_status(s, "bad.pdf")
    _put_in_pending(s, "bad.pdf", b"BAD")

    fake = FakeMinerUClient(
        raise_with=mineru_client.MinerUError("mineru 5xx: 500 server error", attempts=3),
    )
    _install_fake_client(monkeypatch, fake)

    report = parser.parse_pending(dry_run=False)
    assert report.failed == 1
    assert report.parsed == 0
    # 文件移到 error/
    assert (s.error_dir / "bad.pdf").exists()
    assert not (s.pending_dir / "bad.pdf").exists()
    # manifest 标记
    manifest = manifest_store.load(s.manifest_path)
    row = manifest["bad.pdf"]
    assert row.status == "error"
    assert "解析失败" in (row.parse or "")
    assert row.error_msg and "mineru" in row.error_msg


def test_parse_pending_missing_is_logged(fresh_settings, monkeypatch):
    """manifest 标记待解析但 pending/ 找不到 → NO_PENDING。"""
    from app.services import parser
    s = fresh_settings
    _make_manifest_row_with_import_status(s, "ghost.pdf")
    # 不放文件到 pending/

    fake = FakeMinerUClient()
    _install_fake_client(monkeypatch, fake)

    report = parser.parse_pending(dry_run=False)
    # 没调 API
    assert fake.calls == []
    # actions 中应有 NO_PENDING
    no_pending_actions = [a for a in report.actions if a.action.value == "no_pending"]
    assert len(no_pending_actions) == 1


def test_resolve_pending_path_stem_fallback(fresh_settings):
    """manifest 写 .doc，pending/ 放同名 .docx → 通过 stem 匹配命中。"""
    from app.services import parser
    s = fresh_settings
    # 放一个 .docx（manifest 中记录的是 .doc）
    _put(s.pending_dir / "医院感染.docx", b"DATA")

    # 1) 精确匹配找不到
    assert (s.pending_dir / "医院感染.doc").is_file() is False
    # 2) 按 allowed_extensions 也找不到（第一个扩展名 .pdf 也没有）
    # 3) stem 模糊匹配命中
    found = parser._resolve_pending_path("医院感染.doc")
    assert found is not None
    assert found.name == "医院感染.docx"


def test_resolve_pending_path_stem_ambiguous(fresh_settings):
    """同 stem 但多个候选 → 按 allowed_extensions 优先级选第一个。"""
    from app.services import parser
    s = fresh_settings
    # 放同名 .docx 和 .pdf
    _put(s.pending_dir / "amb.docx", b"DATA1")
    _put(s.pending_dir / "amb.pdf", b"DATA2")

    # manifest 写 .doc（不存在）→ 应回退到 .docx 或 .pdf
    # allowed_extensions 顺序: .pdf > .docx > .doc
    # 所以 .pdf 优先
    found = parser._resolve_pending_path("amb.doc")
    assert found is not None
    assert found.suffix.lower() in (".pdf", ".docx")
    # 应该是 .pdf（优先级最高）
    assert found.name == "amb.pdf"


def test_parser_syncs_manifest_filename_on_stem_match(fresh_settings, monkeypatch):
    """parser 解析时发现 manifest 与 pending 实际文件 stem 一致但扩展名不同 → 同步 manifest。"""
    from app.services import parser, manifest_store
    s = fresh_settings

    # manifest 记 .doc，pending 放 .docx（用户手动转换后放回）
    _make_manifest_row_with_import_status(s, "医院感染.doc")
    _put(s.pending_dir / "医院感染.docx", b"DATA")

    fake = FakeMinerUClient()
    _install_fake_client(monkeypatch, fake)

    report = parser.parse_pending(dry_run=False)
    assert report.parsed == 1
    # 真实文件被解析
    assert (s.parsed_dir / "医院感染" / "医院感染.md").exists()
    # manifest 现在应有 .docx 行，parse 字段已更新
    manifest = manifest_store.load(s.manifest_path)
    assert "医院感染.docx" in manifest
    new_row = manifest["医院感染.docx"]
    # parse 字段是 parsed 目录路径（含 stem，不含扩展名）
    assert "医院感染" in (new_row.parse or "")
    assert new_row.status == "parsing_done"
    # 旧 .doc 行保留（仍处于"已移入待处理"但找不到文件，会被后续 no_pending 跳过）
    assert "医院感染.doc" in manifest


def test_parse_retry_eventually_succeeds(fresh_settings, monkeypatch):
    """前 2 次失败，第 3 次成功：parse 列记录 attempts 信息。"""
    from app.services import mineru_client, parser
    s = fresh_settings
    _make_manifest_row_with_import_status(s, "flaky.pdf")
    _put_in_pending(s, "flaky.pdf", b"DATA")

    # 前 2 次失败
    fake = FakeMinerUClient(
        raise_with=mineru_client.MinerUError("timeout", attempts=2),
        attempts_before_fail=2,
    )
    _install_fake_client(monkeypatch, fake)

    report = parser.parse_pending(dry_run=False)
    assert report.parsed == 1
    assert report.failed == 0
    # fake 被调 3 次
    assert len(fake.calls) == 3
    # 落盘
    out_dir = s.parsed_dir / "flaky"
    assert (out_dir / "flaky.md").exists()


def test_mineru_client_4xx_raises_immediately(fresh_settings, monkeypatch):
    """mineru_client 收到 4xx 响应 → 不重试，立即抛 _FatalMinerUError。"""
    from app.services import mineru_client
    s = fresh_settings
    src = _put(s.pending_dir / "x.pdf", b"DATA")

    call_count = {"n": 0}

    class Resp4xx:
        status_code = 400
        headers = {"content-type": "application/json"}
        text = "bad request"
        reason_phrase = "Bad Request"
        content = b'{"err":"bad"}'

        def json(self):
            return {"err": "bad"}

    class Cli:
        def __init__(self, *a, **kw):  # noqa: ARG002
            pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def post(self, url, files=None, data=None, headers=None):  # noqa: A002
            call_count["n"] += 1
            return Resp4xx()

    import httpx
    monkeypatch.setattr(httpx, "Client", Cli)
    client = mineru_client.MinerUClient(api_url="http://x", timeout=10, max_retries=3, backoff=1.0)
    with pytest.raises(mineru_client.MinerUError) as exc:
        client.parse_file(src, s.parsed_dir / "x")
    assert exc.value.status_code == 400
    # 4xx 只调 1 次（不重试）
    assert call_count["n"] == 1


def test_mineru_client_5xx_retries_then_raises(fresh_settings, monkeypatch):
    """mineru_client 收到 5xx → 重试 max_retries 次后抛 MinerUError。"""
    from app.services import mineru_client
    s = fresh_settings
    src = _put(s.pending_dir / "x.pdf", b"DATA")

    call_count = {"n": 0}

    class Resp5xx:
        status_code = 503
        headers = {}
        text = "oops"
        reason_phrase = "Service Unavailable"
        content = b""

        def json(self):
            raise ValueError("not json")

    class Cli:
        def __init__(self, *a, **kw):  # noqa: ARG002
            pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def post(self, url, files=None, data=None, headers=None):  # noqa: A002
            call_count["n"] += 1
            return Resp5xx()

    import httpx
    monkeypatch.setattr(httpx, "Client", Cli)
    client = mineru_client.MinerUClient(api_url="http://x", timeout=10, max_retries=3, backoff=0.0)
    with pytest.raises(mineru_client.MinerUError):
        client.parse_file(src, s.parsed_dir / "x")
    # 5xx 重试 3 次
    assert call_count["n"] == 3


def test_parse_idempotent_second_run(fresh_settings, monkeypatch):
    """第二次解析：所有 parse 列非空 → 全部 SKIPPED_DONE。"""
    from app.services import parser
    s = fresh_settings
    _make_manifest_row_with_import_status(s, "a.pdf")
    _put_in_pending(s, "a.pdf", b"DATA")

    fake = FakeMinerUClient()
    _install_fake_client(monkeypatch, fake)

    first = parser.parse_pending(dry_run=False)
    assert first.parsed == 1
    second = parser.parse_pending(dry_run=False)
    assert second.parsed == 0
    assert second.skipped_done == 1


def test_parse_mixed_files(fresh_settings, monkeypatch):
    """混合：1 个成功、1 个失败、1 个已解析、1 个 MISSING。"""
    from app.services import mineru_client, parser
    s = fresh_settings

    # ok.pdf
    _make_manifest_row_with_import_status(s, "ok.pdf")
    _put_in_pending(s, "ok.pdf", b"OK")
    # bad.pdf
    _make_manifest_row_with_import_status(s, "bad.pdf")
    _put_in_pending(s, "bad.pdf", b"BAD")
    # done.pdf：先建好 parsed/done/ + manifest parse 列
    out_dir = s.parsed_dir / "done"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "done.md").write_text("ok", encoding="utf-8")
    from app.services import manifest_store
    manifest_store.upsert(
        s.manifest_path,
        _row(
            filename="done.pdf",
            import_status="已移入待处理",
            parse=str(out_dir.resolve()),
        ),
    )
    # ghost.pdf
    _make_manifest_row_with_import_status(s, "ghost.pdf")

    # 只对 bad.pdf 失败
    fake = FakeMinerUClient(
        raise_with=mineru_client.MinerUError("5xx", attempts=3),
        fail_predicate=lambda name: name.startswith("bad"),
    )
    _install_fake_client(monkeypatch, fake)

    report = parser.parse_pending(dry_run=False)
    assert report.parsed == 1
    assert report.failed == 1
    assert report.skipped_done == 1
    no_pending = [a for a in report.actions if a.action.value == "no_pending"]
    assert len(no_pending) == 1


# ============ mineru_client 单元测试（用真实 httpx Mock） ============


def test_mineru_client_sends_correct_request(fresh_settings, monkeypatch):
    """mineru_client 把 file 正确 POST 到 /file_parse（multipart）。"""
    from app.services import mineru_client
    import zipfile
    import io

    s = fresh_settings
    src = _put(s.pending_dir / "x.pdf", b"DATA")

    captured: Dict[str, Any] = {}

    def _make_zip():
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("x.md", "# hi")
            zf.writestr("x.json", json.dumps({"blocks": []}, ensure_ascii=False))
            zf.writestr("images/x.png", b"\x89PNG\r\n\x1a\n")
        return buf.getvalue()

    class FakeResponse:
        status_code = 200
        headers = {"content-type": "application/zip"}
        text = ""
        content = _make_zip()

        def json(self):
            return {}

    class FakeClient:
        def __init__(self, *a, **kw):  # noqa: ARG002
            pass
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def post(self, url, files=None, data=None, headers=None):  # noqa: A002
            captured["url"] = url
            captured["files"] = files
            captured["data"] = data
            captured["headers"] = headers
            return FakeResponse()

    import httpx
    monkeypatch.setattr(httpx, "Client", FakeClient)

    client = mineru_client.MinerUClient(api_url="http://x", timeout=10, max_retries=1)
    out = client.parse_file(src, s.parsed_dir / "x")
    assert captured["url"] == "http://x/file_parse"
    # multipart: files 应有 'files' 字段
    assert "files" in captured["files"]
    # data 应有 response_format_zip=true, backend, lang_list 等
    assert captured["data"]["response_format_zip"] == "true"
    assert captured["data"]["backend"] == "hybrid-engine"
    # ★ 关键：5 个 return_* 开关必须全为 true，否则 MinerU 只返 md
    assert captured["data"]["return_md"] == "true"
    assert captured["data"]["return_middle_json"] == "true"
    assert captured["data"]["return_model_output"] == "true"
    assert captured["data"]["return_content_list"] == "true"
    assert captured["data"]["return_images"] == "true"
    # ★ 高质量模型：默认 backend=hybrid-engine + effort=high
    assert captured["data"]["backend"] == "hybrid-engine"
    assert captured["data"]["effort"] == "high"
    assert out.md_path.is_file()
    assert out.json_path.is_file()


def test_mineru_client_rejects_legacy_doc(fresh_settings):
    """mineru_client 提前拒绝 .doc 旧 OLE 格式（不调 API）。"""
    from app.services import mineru_client

    s = fresh_settings
    # 构造一个真正的 OLE 文件头：D0 CF 11 E0 A1 B1 1A E1
    ole_bytes = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 100
    doc_path = s.pending_dir / "legacy.doc"
    doc_path.write_bytes(ole_bytes)

    client = mineru_client.MinerUClient(api_url="http://x", timeout=10, max_retries=1)
    with pytest.raises(mineru_client._UnsupportedLegacyDocError) as exc:
        client.parse_file(doc_path, s.parsed_dir / "legacy")
    assert "legacy.doc" in str(exc.value)
    # 提示中应包含 docx 字样
    assert ".docx" in str(exc.value)
    # 不应在 parsed/ 留下空目录
    assert not (s.parsed_dir / "legacy").exists()


def test_mineru_client_allows_docx_without_rejection(fresh_settings):
    """mineru_client 不应把 .docx 错认为 .doc。"""
    from app.services import mineru_client
    import io
    import zipfile

    s = fresh_settings
    # 构造一个最小合法 docx
    docx_buf = io.BytesIO()
    with zipfile.ZipFile(docx_buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", "<?xml version='1.0'?><Types xmlns='http://schemas.openxmlformats.org/package/2006/content-types'></Types>")
    docx_path = s.pending_dir / "ok.docx"
    docx_path.write_bytes(docx_buf.getvalue())

    # Mock 让 httpx 返回一个合法 ZIP
    captured = {}

    def _ok_zip():
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("ok/auto/ok.md", "# hi")
            zf.writestr("ok/auto/ok_middle.json", "{}")
        return buf.getvalue()

    class FakeResp:
        status_code = 200
        headers = {"content-type": "application/zip"}
        text = ""
        content = _ok_zip()
        reason_phrase = "OK"
        def json(self): return {}

    class FakeCli:
        def __init__(self, *a, **kw): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def post(self, url, files=None, data=None, headers=None):
            captured["data"] = data
            return FakeResp()

    import httpx
    import unittest.mock
    with unittest.mock.patch.object(httpx, "Client", FakeCli):
        client = mineru_client.MinerUClient(api_url="http://x", timeout=10, max_retries=1)
        out = client.parse_file(docx_path, s.parsed_dir / "ok")
    # 应正常返回 ParseResult，不抛 _UnsupportedLegacyDocError
    assert out.md_path and out.md_path.is_file()
    # return_* 全为 true
    assert captured["data"]["return_middle_json"] == "true"
    assert captured["data"]["return_images"] == "true"


def test_parser_handles_legacy_doc_gracefully(fresh_settings, monkeypatch):
    """parser 对 .doc 旧格式：mineru_client 抛 _UnsupportedLegacyDocError → 移入 error/。

    注意：预检测本身在 mineru_client.parse_file 内（见 test_mineru_client_rejects_legacy_doc），
    本测试验证 parser 层对 _UnsupportedLegacyDocError 的处理（移文件、写 manifest）。
    """
    from app.services import parser, mineru_client
    s = fresh_settings

    # 构造真 .doc（仅供 parser 找文件用，不调 API）
    ole_bytes = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 100
    doc_path = s.pending_dir / "legacy.doc"
    doc_path.write_bytes(ole_bytes)

    _make_manifest_row_with_import_status(s, "legacy.doc")

    # 真实 MinerUClient，但 monkeypatch 它的 parse_file 直接抛 _UnsupportedLegacyDocError
    real_client = mineru_client.MinerUClient(api_url="http://x", timeout=10, max_retries=1)

    def _raise_unsupported(file_path, parsed_dir):
        raise mineru_client._UnsupportedLegacyDocError(file_path)

    monkeypatch.setattr(real_client, "parse_file", _raise_unsupported)
    monkeypatch.setattr(parser, "MinerUClient", lambda: real_client)

    report = parser.parse_pending(dry_run=False)
    assert report.failed == 1
    assert report.parsed == 0
    # 文件移到 error/
    assert (s.error_dir / "legacy.doc").exists()
    assert not (s.pending_dir / "legacy.doc").exists()
    # manifest 标记：parse 列说明原因、status=error
    from app.services import manifest_store
    manifest = manifest_store.load(s.manifest_path)
    row = manifest["legacy.doc"]
    assert row.status == "error"
    assert ".doc" in (row.error_msg or "") and ".docx" in (row.error_msg or "")


def test_mineru_client_retries_on_5xx(fresh_settings, monkeypatch):
    """mineru_client 在 5xx 时重试，最终成功。"""
    from app.services import mineru_client
    import io
    import zipfile

    s = fresh_settings
    src = _put(s.pending_dir / "x.pdf", b"DATA")

    call_count = {"n": 0}

    def _ok_zip():
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("x.md", "# hi")
            zf.writestr("x.json", "{}")
        return buf.getvalue()

    class FlakyResponse:
        def __init__(self, status: int):
            self.status_code = status
            self.headers = {} if status != 200 else {"content-type": "application/zip"}
            self.text = "oops"
            self.reason_phrase = "Internal"
            self.content = b"" if status != 200 else _ok_zip()

        def json(self):
            raise ValueError("not json")

    class FlakyClient:
        def __init__(self, *a, **kw):  # noqa: ARG002
            pass
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def post(self, url, files=None, data=None, headers=None):  # noqa: A002
            call_count["n"] += 1
            if call_count["n"] < 3:
                return FlakyResponse(500)
            return FlakyResponse(200)  # 第三次成功

    import httpx
    monkeypatch.setattr(httpx, "Client", FlakyClient)

    # 用非常短的 backoff 让测试快
    client = mineru_client.MinerUClient(
        api_url="http://x", timeout=10, max_retries=3, backoff=0.0
    )
    # parse_file 整体重试：前 2 次 5xx，第 3 次成功
    out = client.parse_file(src, s.parsed_dir / "x")
    assert out.attempts == 3
    assert out.md_path.is_file()
    assert call_count["n"] == 3


def test_parse_handles_zip_with_no_json(fresh_settings, monkeypatch):
    """回归测试：ZIP 响应里没有 .json 时，manifest 仍应正确更新（不能因 None 抛错）。

    背景：实际 MinerU 真实调用（端到端跑出）时，ZIP 产物可能没有顶层 .json
    （或 .json 在子目录 hybrid_auto/ 下，rglob 会找到但 zip 顶层就缺）。
    之前 bug：parser.py 调 result.json_path.resolve() → AttributeError
    修复：容忍 None，manifest 仍正确写。
    """
    from app.services import parser
    from app.services import manifest_store
    import io, zipfile

    s = fresh_settings
    _make_manifest_row_with_import_status(s, "no_json.pdf")
    _put_in_pending(s, "no_json.pdf", b"DATA")

    # 构造一个**只有 .md，没有 .json** 的 ZIP
    def _zip_only_md():
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            # ZIP 内的路径不含 stem（zip 整体被解压到 parsed_dir=parsed/no_json/）
            zf.writestr("hybrid_auto/no_json.md", "# title\n")
        return buf.getvalue()

    class _Resp:
        status_code = 200
        content = _zip_only_md()
        headers = {"content-type": "application/zip"}
        text = ""
        reason_phrase = "OK"

        def json(self):
            return {}

    class _Cli:
        def __init__(self, *a, **kw): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def post(self, url, files=None, data=None, headers=None): return _Resp()

    import httpx
    monkeypatch.setattr(httpx, "Client", _Cli)

    # 关键：不能抛 AttributeError
    report = parser.parse_pending(dry_run=False)
    assert report.parsed == 1
    assert report.failed == 0

    # manifest 仍正确更新
    manifest = manifest_store.load(s.manifest_path)
    row = manifest["no_json.pdf"]
    assert row.status == "parsing_done", f"status 应为 parsing_done, 实际 {row.status}"
    assert "parsed" in (row.parse or "").lower() or "no_json" in (row.parse or "")
    # md 落盘了
    out = s.parsed_dir / "no_json"
    assert (out / "hybrid_auto" / "no_json.md").is_file()


def test_is_parsed_dir_valid_finds_md_in_subdir(fresh_settings):
    """回归测试：_is_parsed_dir_valid 必须递归找 .md（ZIP 产物在子目录如 hybrid_auto/）。"""
    from app.services.parser import _is_parsed_dir_valid

    s = fresh_settings

    # 1) 顶层没 .md，子目录有 → 应判为 valid
    d1 = s.parsed_dir / "sub"
    (d1 / "hybrid_auto").mkdir(parents=True)
    (d1 / "hybrid_auto" / "doc.md").write_text("x", encoding="utf-8")
    assert _is_parsed_dir_valid(d1) is True, "子目录有 .md 应判 valid"

    # 2) 完全没 .md → invalid
    d2 = s.parsed_dir / "empty"
    d2.mkdir()
    assert _is_parsed_dir_valid(d2) is False

    # 3) 目录不存在 → invalid
    assert _is_parsed_dir_valid(s.parsed_dir / "notexist") is False

    # 4) 顶层有 .md → valid（兼容旧结构）
    d3 = s.parsed_dir / "flat"
    d3.mkdir()
    (d3 / "doc.md").write_text("x", encoding="utf-8")
    assert _is_parsed_dir_valid(d3) is True


# ============ 高质量后端强制（plan.md §3.2 优化）============


def test_mineru_client_defaults_to_high_quality_backend(fresh_settings):
    """默认：backend 必须是高质量（hybrid-engine）+ effort=high。"""
    from app.services import mineru_client

    client = mineru_client.MinerUClient(api_url="http://x", timeout=10, max_retries=1)
    assert client.backend in mineru_client._HIGH_QUALITY_BACKENDS, (
        f"默认 backend 应是高质量，实际: {client.backend}"
    )
    assert client.backend == "hybrid-engine", (
        f"默认 backend 应是 hybrid-engine，实际: {client.backend}"
    )
    assert client.effort == "high", f"默认 effort 应是 high，实际: {client.effort}"
    assert client.enforce_high_quality is True


def test_mineru_client_upgrades_pipeline_to_hybrid(fresh_settings, caplog):
    """.env 误填 pipeline → 自动升级到 hybrid-engine（保证效果）。"""
    import logging
    from app.services import mineru_client

    # 解除 module-level 的 logging.disable(CRITICAL) 以让 caplog 收到 WARNING
    logging.disable(logging.NOTSET)
    try:
        with caplog.at_level(logging.WARNING, logger="ragsystem.mineru_client"):
            client = mineru_client.MinerUClient(
                api_url="http://x", timeout=10, max_retries=1, backend="pipeline"
            )
    finally:
        logging.disable(logging.CRITICAL)
    # 升级
    assert client.backend == "hybrid-engine", (
        f"pipeline 应被升级到 hybrid-engine，实际: {client.backend}"
    )
    # 打印 WARNING（告知用户发生了升级）
    assert any("低质量" in rec.message or "pipeline" in rec.message for rec in caplog.records), (
        f"应打印 WARNING，实际日志: {[r.message for r in caplog.records]}"
    )


def test_mineru_client_preserves_vlm_engine(fresh_settings, caplog):
    """vlm-engine 本身是高质量 → 原样保留。"""
    import logging
    from app.services import mineru_client

    logging.disable(logging.NOTSET)
    try:
        with caplog.at_level(logging.WARNING, logger="ragsystem.mineru_client"):
            client = mineru_client.MinerUClient(
                api_url="http://x", timeout=10, max_retries=1, backend="vlm-engine"
            )
    finally:
        logging.disable(logging.CRITICAL)
    assert client.backend == "vlm-engine"
    # 不应触发升级 WARNING
    assert not any("升级" in rec.message for rec in caplog.records)


def test_mineru_client_preserves_hybrid_engine(fresh_settings):
    """hybrid-engine 是默认 → 原样保留。"""
    from app.services import mineru_client

    client = mineru_client.MinerUClient(
        api_url="http://x", timeout=10, max_retries=1, backend="hybrid-engine"
    )
    assert client.backend == "hybrid-engine"
    assert client.effort == "high"


def test_mineru_client_warns_on_unknown_backend(fresh_settings, caplog):
    """未知后端（如 'foo'）→ 打印 WARNING 但不修改（让 MinerU 自己报错）。"""
    import logging
    from app.services import mineru_client

    logging.disable(logging.NOTSET)
    try:
        with caplog.at_level(logging.WARNING, logger="ragsystem.mineru_client"):
            client = mineru_client.MinerUClient(
                api_url="http://x", timeout=10, max_retries=1, backend="foo-bar"
            )
    finally:
        logging.disable(logging.CRITICAL)
    # 未知值：原样保留（避免静默切换隐藏配置错误）
    assert client.backend == "foo-bar"
    # 打印 WARNING
    assert any("未知" in rec.message or "不在已知列表" in rec.message for rec in caplog.records), (
        f"应打印 WARNING，实际日志: {[r.message for r in caplog.records]}"
    )


def test_mineru_client_enforce_disabled_keeps_pipeline(fresh_settings, caplog):
    """enforce_high_quality=False 时 pipeline 保留（仅打 WARNING，不升级）。"""
    import logging
    from app.services import mineru_client

    logging.disable(logging.NOTSET)
    try:
        with caplog.at_level(logging.WARNING, logger="ragsystem.mineru_client"):
            client = mineru_client.MinerUClient(
                api_url="http://x",
                timeout=10,
                max_retries=1,
                backend="pipeline",
                enforce_high_quality=False,
            )
    finally:
        logging.disable(logging.CRITICAL)
    # 关闭强制 → 保留 pipeline（不升级）
    assert client.backend == "pipeline"
    # 仍打印 WARNING 提醒
    assert any("pipeline" in rec.message for rec in caplog.records)


def test_mineru_client_effort_high_default(fresh_settings):
    """默认 effort=high（极致精度 + image analysis）。"""
    from app.services import mineru_client

    client = mineru_client.MinerUClient(api_url="http://x", timeout=10, max_retries=1)
    assert client.effort == "high"


def test_mineru_client_effort_medium(fresh_settings):
    """显式传 effort=medium → 走快速档（无 image analysis）。"""
    from app.services import mineru_client

    client = mineru_client.MinerUClient(
        api_url="http://x", timeout=10, max_retries=1, effort="medium"
    )
    assert client.effort == "medium"


def test_mineru_client_effort_invalid_falls_back_to_high(fresh_settings, caplog):
    """非法 effort（如 'foo'）→ 警告 + 回退 high。"""
    import logging
    from app.services import mineru_client

    logging.disable(logging.NOTSET)
    try:
        with caplog.at_level(logging.WARNING, logger="ragsystem.mineru_client"):
            client = mineru_client.MinerUClient(
                api_url="http://x", timeout=10, max_retries=1, effort="foo"
            )
    finally:
        logging.disable(logging.CRITICAL)
    assert client.effort == "high"
    assert any("effort" in rec.message for rec in caplog.records)


def test_mineru_client_sends_effort_in_request(fresh_settings, monkeypatch):
    """POST 数据应包含 effort 字段（hybrid-engine 才识别，但总是发送也无害）。"""
    from app.services import mineru_client
    import io
    import zipfile

    s = fresh_settings
    src = _put(s.pending_dir / "x.pdf", b"DATA")

    captured: dict = {}

    def _ok_zip():
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("x.md", "# hi")
        return buf.getvalue()

    class _Resp:
        status_code = 200
        headers = {"content-type": "application/zip"}
        text = ""
        content = _ok_zip()
        reason_phrase = "OK"

        def json(self):
            return {}

    class _Cli:
        def __init__(self, *a, **kw): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def post(self, url, files=None, data=None, headers=None):
            captured["data"] = data
            return _Resp()

    import httpx
    monkeypatch.setattr(httpx, "Client", _Cli)
    client = mineru_client.MinerUClient(
        api_url="http://x", timeout=10, max_retries=1, backend="hybrid-engine", effort="high"
    )
    client.parse_file(src, s.parsed_dir / "x")
    assert captured["data"]["effort"] == "high"
    assert captured["data"]["backend"] == "hybrid-engine"


def test_mineru_client_pipeline_in_request_upgraded(fresh_settings, monkeypatch):
    """端到端：传入 backend=pipeline → 实际请求中是 hybrid-engine。"""
    from app.services import mineru_client
    import io
    import zipfile

    s = fresh_settings
    src = _put(s.pending_dir / "x.pdf", b"DATA")

    captured: dict = {}

    def _ok_zip():
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("x.md", "# hi")
        return buf.getvalue()

    class _Resp:
        status_code = 200
        headers = {"content-type": "application/zip"}
        text = ""
        content = _ok_zip()
        reason_phrase = "OK"

        def json(self):
            return {}

    class _Cli:
        def __init__(self, *a, **kw): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def post(self, url, files=None, data=None, headers=None):
            captured["data"] = data
            return _Resp()

    import httpx
    monkeypatch.setattr(httpx, "Client", _Cli)
    client = mineru_client.MinerUClient(
        api_url="http://x", timeout=10, max_retries=1, backend="pipeline"
    )
    client.parse_file(src, s.parsed_dir / "x")
    # 实际发给 MinerU 的 backend 应该是 hybrid-engine
    assert captured["data"]["backend"] == "hybrid-engine", (
        f"pipeline 应被自动升级，实际请求 backend={captured['data']['backend']}"
    )
