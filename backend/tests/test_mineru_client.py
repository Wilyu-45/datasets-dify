"""MinerUClient 重试 + 长文档路由 单元测试（2026-08-06 新增）。

目标：
1. 验证 _compute_retry_wait 公式正确（initial * factor^attempt, capped by max）
2. 验证 _resolve_long_doc_routing 根据页数决定是否切换 backend
3. 验证 _post 使用传入的 backend/effort（不污染 self）
4. 验证 _count_pdf_pages 在 PDF 不可读时返回 None（不误切）

注意：这些测试不发起实际 HTTP 请求（mock 掉 _post）。
"""
from __future__ import annotations

import importlib
import logging
from pathlib import Path
from unittest.mock import MagicMock, patch

import fitz  # PyMuPDF
import pytest

logging.disable(logging.CRITICAL)


@pytest.fixture(autouse=True)
def settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """每个测试前重载 settings（隔离环境变量）。"""
    test_data_root = tmp_path / "data"
    monkeypatch.setenv("RAG_DATA_ROOT", str(test_data_root))
    from app import config as cfg_mod
    importlib.reload(cfg_mod)
    # ★ .env 优先级高于环境变量（settings_customise_sources），
    #   必须用 init kwargs 覆盖 data_root 才能真正隔离测试目录
    cfg = cfg_mod.Settings(data_root=test_data_root)
    cfg_mod.settings = cfg
    from app.services import mineru_client
    mineru_client.settings = cfg
    yield cfg


@pytest.fixture
def client(settings):
    """构造一个测试用 MinerUClient（指向不存在的 URL，仅测试本地逻辑）。"""
    from app.services.mineru_client import MinerUClient
    return MinerUClient(
        api_url="http://localhost:9999",
        timeout=10,
        max_retries=3,
        retry_initial_wait=30.0,
        retry_backoff_factor=2.0,
        retry_max_wait=300.0,
        backend="hybrid-engine",
        effort="high",
        long_doc_pages_threshold=10,
        long_doc_backend="vlm-engine",
        long_doc_effort="high",
    )


def _make_pdf(tmp_path: Path, num_pages: int, name: str = "test.pdf") -> Path:
    """用 PyMuPDF 生成指定页数的 PDF（每页一段 ASCII 文本）。"""
    pdf_path = tmp_path / name
    doc = fitz.open()
    for i in range(num_pages):
        page = doc.new_page(width=596, height=842)
        page.insert_text((80, 80), f"Page {i + 1}", fontsize=12, fontname="helv")
    doc.save(str(pdf_path))
    doc.close()
    return pdf_path


# ============ _compute_retry_wait 公式 ============

class TestComputeRetryWait:
    """验证退避公式：min(initial * factor^(attempt-1), max_wait)"""

    def test_default_formula_30_2x(self, client):
        """默认 30/2.0/300 → 30s, 60s, 120s"""
        assert client._compute_retry_wait(1) == 30.0
        assert client._compute_retry_wait(2) == 60.0
        assert client._compute_retry_wait(3) == 120.0

    def test_capped_by_max_wait(self, client):
        """超过 max_wait 时被封顶"""
        # 100 * 2^9 = 51200, 上限 300
        assert client._compute_retry_wait(10) == 300.0

    def test_custom_initial_factor(self, settings):
        """自定义 initial / factor：10s * 3x → 10, 30, 90"""
        from app.services.mineru_client import MinerUClient
        c = MinerUClient(
            api_url="http://x",
            retry_initial_wait=10.0,
            retry_backoff_factor=3.0,
            retry_max_wait=10000.0,
        )
        assert c._compute_retry_wait(1) == 10.0
        assert c._compute_retry_wait(2) == 30.0
        assert c._compute_retry_wait(3) == 90.0

    def test_invalid_attempt_returns_zero(self, client):
        """attempt < 1 返回 0（防御性）"""
        assert client._compute_retry_wait(0) == 0.0
        assert client._compute_retry_wait(-1) == 0.0

    def test_legacy_backoff_field_still_works(self, client):
        """兼容旧 self.backoff 字段（不影响新公式）"""
        assert client.backoff == 5.0  # settings 默认
        # 但 _compute_retry_wait 不读 self.backoff
        assert client._compute_retry_wait(1) == 30.0


# ============ _count_pdf_pages ============

class TestCountPdfPages:
    """PDF 页数检测：成功 / 失败 / 非 PDF 文件"""

    def test_valid_pdf(self, tmp_path):
        from app.services.mineru_client import _count_pdf_pages
        pdf = _make_pdf(tmp_path, num_pages=15)
        assert _count_pdf_pages(pdf) == 15

    def test_single_page(self, tmp_path):
        from app.services.mineru_client import _count_pdf_pages
        pdf = _make_pdf(tmp_path, num_pages=1)
        assert _count_pdf_pages(pdf) == 1

    def test_nonexistent_file(self, tmp_path):
        from app.services.mineru_client import _count_pdf_pages
        assert _count_pdf_pages(tmp_path / "missing.pdf") is None

    def test_non_pdf_file(self, tmp_path):
        """非 PDF 文件应返回 None（不误切 backend）"""
        from app.services.mineru_client import _count_pdf_pages
        fake = tmp_path / "fake.txt"
        fake.write_text("hello")
        assert _count_pdf_pages(fake) is None


# ============ _resolve_long_doc_routing ============

class TestLongDocRouting:
    """PDF 页数 >= 阈值时切换到 long_doc_backend"""

    def test_short_doc_uses_default(self, client, tmp_path):
        """< 阈值：用默认 backend"""
        pdf = _make_pdf(tmp_path, num_pages=5, name="short.pdf")
        backend, effort, pages = client._resolve_long_doc_routing(pdf)
        assert backend is None  # 不切换
        assert effort is None
        assert pages == 5  # 页数已检测
        # self.backend 未变
        assert client.backend == "hybrid-engine"

    def test_long_doc_switches_to_vlm(self, client, tmp_path):
        """>= 阈值：切换到 vlm-engine"""
        pdf = _make_pdf(tmp_path, num_pages=20, name="long.pdf")
        backend, effort, pages = client._resolve_long_doc_routing(pdf)
        assert backend == "vlm-engine"
        assert effort == "high"
        assert pages == 20
        # self.backend 未变（仅本次调用使用 vlm）
        assert client.backend == "hybrid-engine"

    def test_threshold_boundary(self, client, tmp_path):
        """正好 = 阈值时切换（>= 比较）"""
        pdf = _make_pdf(tmp_path, num_pages=10, name="boundary.pdf")
        backend, _, _ = client._resolve_long_doc_routing(pdf)
        assert backend == "vlm-engine"  # >= threshold 触发

    def test_disabled_threshold_keeps_default(self, tmp_path, settings):
        """threshold=0 禁用路由"""
        from app.services.mineru_client import MinerUClient
        c = MinerUClient(
            api_url="http://x",
            long_doc_pages_threshold=0,
        )
        pdf = _make_pdf(tmp_path, num_pages=100, name="disabled.pdf")
        backend, _, pages = c._resolve_long_doc_routing(pdf)
        assert backend is None
        assert pages is None  # 禁用时甚至不检测页数

    def test_non_pdf_file_no_routing(self, client, tmp_path):
        """非 PDF 文件不切（DOCX/PPTX 等页数估算不准）"""
        fake = tmp_path / "doc.docx"
        fake.write_bytes(b"PK fake docx content")
        backend, _, pages = client._resolve_long_doc_routing(fake)
        assert backend is None
        assert pages is None  # 非 PDF 直接跳过页数检测

    def test_low_quality_backend_keeps_long_doc(self, tmp_path, settings):
        """如果 long_doc_backend 配置是 pipeline（低质量），_resolve_backend 会升级"""
        from app.services.mineru_client import MinerUClient
        c = MinerUClient(
            api_url="http://x",
            long_doc_pages_threshold=10,
            long_doc_backend="pipeline",  # 低质量
            enforce_high_quality=True,
        )
        pdf = _make_pdf(tmp_path, num_pages=20, name="long.pdf")
        backend, _, _ = c._resolve_long_doc_routing(pdf)
        # enforce_high_quality=True 会把 pipeline 升级到 hybrid-engine
        assert backend == "hybrid-engine"

    def test_custom_long_doc_effort(self, tmp_path, settings):
        """long_doc_effort=medium 时正确传递"""
        from app.services.mineru_client import MinerUClient
        c = MinerUClient(
            api_url="http://x",
            long_doc_pages_threshold=10,
            long_doc_backend="hybrid-engine",
            long_doc_effort="medium",
        )
        pdf = _make_pdf(tmp_path, num_pages=20, name="long.pdf")
        backend, effort, _ = c._resolve_long_doc_routing(pdf)
        assert backend == "hybrid-engine"
        assert effort == "medium"


# ============ parse_file 集成（mock _post）============


class TestParseFileWithRouting:
    """验证 parse_file 把 long_doc 路由传到 _post（不污染 self）"""

    def test_short_pdf_uses_default_backend(self, client, tmp_path):
        """短 PDF → _post 收到 hybrid-engine（默认）"""
        from app.services.mineru_client import MinerUError
        pdf = _make_pdf(tmp_path, num_pages=5, name="short.pdf")
        parsed_dir = tmp_path / "out"
        parsed_dir.mkdir()

        with patch.object(client._impl, "_post") as mock_post:
            mock_post.side_effect = MinerUError("test fail", attempts=1)
            try:
                client.parse_file(pdf, parsed_dir)
            except MinerUError:
                pass
            # _post 收到的 backend 参数
            call_kwargs = mock_post.call_args.kwargs
            assert call_kwargs["backend"] == "hybrid-engine"
            assert call_kwargs["effort"] == "high"

    def test_long_pdf_uses_vlm_engine(self, client, tmp_path):
        """长 PDF → _post 收到 vlm-engine"""
        from app.services.mineru_client import MinerUError
        pdf = _make_pdf(tmp_path, num_pages=25, name="long.pdf")
        parsed_dir = tmp_path / "out"
        parsed_dir.mkdir()

        with patch.object(client._impl, "_post") as mock_post:
            mock_post.side_effect = MinerUError("test fail", attempts=1)
            try:
                client.parse_file(pdf, parsed_dir)
            except MinerUError:
                pass
            call_kwargs = mock_post.call_args.kwargs
            assert call_kwargs["backend"] == "vlm-engine"
            assert call_kwargs["effort"] == "high"

    def test_parse_file_uses_new_retry_formula(self, client, tmp_path):
        """parse_file 用新退避公式（30/60/120s）而不是旧 (1/5/25s)

        注意：只有 _RetryableMinerUError 才走 sleep+retry 逻辑，
        MinerUError 是终态异常（重试耗尽后才抛），会直接 propagate。
        """
        from app.services.mineru_client import _RetryableMinerUError
        pdf = _make_pdf(tmp_path, num_pages=5, name="test.pdf")
        parsed_dir = tmp_path / "out"
        parsed_dir.mkdir()

        sleeps: list[float] = []
        with patch.object(client._impl, "_post") as mock_post, \
             patch("app.services.mineru_client.time.sleep", side_effect=lambda s: sleeps.append(s)):
            mock_post.side_effect = _RetryableMinerUError("simulated retryable error")
            try:
                client.parse_file(pdf, parsed_dir)
            except Exception:
                pass

        # 3 次尝试 → 2 次 sleep（最后一次不 sleep）
        assert len(sleeps) == 2
        assert sleeps[0] == 30.0  # attempt 1 失败 → 等 30s
        assert sleeps[1] == 60.0  # attempt 2 失败 → 等 60s


# ============ _post 直接传 backend ============

class TestPostAcceptsBackend:
    """验证 _post 接受 backend/effort 参数（不污染 self）"""

    def test_post_uses_passed_backend(self, client, tmp_path):
        """传 backend=vlm-engine 时，data 里 backend=vlm-engine（不是 self.backend）"""
        pdf = _make_pdf(tmp_path, num_pages=1, name="x.pdf")

        with patch("app.services.mineru_client.httpx.Client") as mock_client_cls:
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.headers = {"content-type": "application/zip"}
            mock_response.content = b"not a real zip"  # 故意失败以触发 _write_from_zip
            mock_client_instance = MagicMock()
            mock_client_instance.post.return_value = mock_response
            mock_client_instance.__enter__ = MagicMock(return_value=mock_client_instance)
            mock_client_instance.__exit__ = MagicMock(return_value=False)
            mock_client_cls.return_value = mock_client_instance

            # _post 走通到 _write_outputs，zip 解压失败会抛 _RetryableMinerUError
            from app.services.mineru_client import _RetryableMinerUError
            try:
                client._post(pdf, backend="vlm-engine", effort="high")
            except (_RetryableMinerUError, Exception):
                pass

            # 关键断言：httpx post 收到的 form data 含 backend=vlm-engine
            call_args = mock_client_instance.post.call_args
            form_data = call_args.kwargs["data"]
            assert form_data["backend"] == "vlm-engine"
            # self.backend 未变
            assert client.backend == "hybrid-engine"


class TestWriteFromZipPathCleaning:
    """★ 2026-08-07：ZIP 解压路径清理（修复 Windows 上 extractall 失败问题）。"""

    def test_write_from_zip_strips_top_directory(self, tmp_path: Path, client):
        """ZIP 内顶层目录（如 {stem}_text/）应被去掉，文件直接放到 parsed_dir。"""
        import io
        import zipfile
        
        # 构造一个模拟 MinerU 返回的 ZIP（带顶层目录）
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, 'w') as zf:
            zf.writestr('stem_text/vlm/stem_text.md', '# Test content')
            zf.writestr('stem_text/vlm/stem_text.json', '{"key": "value"}')
        buf.seek(0)
        
        # 模拟 response
        response = MagicMock()
        response.content = buf.getvalue()
        
        parsed_dir = tmp_path / "parsed"
        parsed_dir.mkdir()
        
        # 调用 _write_from_zip
        result = client._write_from_zip(response, parsed_dir, attempts=1)
        
        # 断言：文件被正确解压到 parsed_dir（去掉了顶层目录）
        assert result.md_path is not None
        assert result.md_path.exists()
        assert result.md_path.read_text(encoding='utf-8') == '# Test content'
        assert result.json_path is not None
        assert result.json_path.exists()

    def test_write_from_zip_handles_nested_paths(self, tmp_path: Path, client):
        """ZIP 内多层路径应被正确处理。"""
        import io
        import zipfile
        
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, 'w') as zf:
            zf.writestr('top/middle/file.md', 'content')
        buf.seek(0)
        
        response = MagicMock()
        response.content = buf.getvalue()
        
        parsed_dir = tmp_path / "parsed"
        parsed_dir.mkdir()
        
        result = client._write_from_zip(response, parsed_dir, attempts=1)
        
        # 文件应在 parsed_dir/middle/file.md（去掉了 top/）
        assert result.md_path is not None
        assert result.md_path.exists()
        assert 'middle' in str(result.md_path)


# ============ mineru.net 官方 API provider（2026-09 生产部署改造，板块 B） ============


def _patch_httpx_session(monkeypatch: pytest.MonkeyPatch, post=None, get=None) -> MagicMock:
    """把 mineru_client 模块引用的 httpx.Client patch 成假 session。

    post/get 传单个响应对象时走 return_value；传列表时走 side_effect（按序返回/抛出）。
    """
    from app.services import mineru_client as mc

    session = MagicMock()
    if post is not None:
        if isinstance(post, list):
            session.post.side_effect = post
        else:
            session.post.return_value = post
    if get is not None:
        if isinstance(get, list):
            session.get.side_effect = get
        else:
            session.get.return_value = get
    client_cls = MagicMock()
    client_cls.return_value.__enter__.return_value = session
    monkeypatch.setattr(mc.httpx, "Client", client_cls)
    return session


def _net_zip_bytes(stem: str = "report") -> bytes:
    """构造带顶层目录的官方产物 ZIP（模拟 full_zip_url 下载内容）。"""
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(f"{stem}/full.md", "# 标题\n正文内容")
        zf.writestr(f"{stem}/layout.json", '{"pdf_info": []}')
        zf.writestr(f"{stem}/{stem}_model.json", '{"model_output": true}')
        zf.writestr(f"{stem}/{stem}_content_list.json", '[{"type": "text"}]')
        zf.writestr(f"{stem}/images/fig1.jpg", b"\xff\xd8\xff\xe0fake")
        zf.writestr(f"{stem}/main.html", "<html><body>x</body></html>")
    return buf.getvalue()


def _make_net_client(monkeypatch: pytest.MonkeyPatch, **overrides):
    """构造 _MinerUNetClient（轮询步长/重试退避均为 0，不真等待），httpx 已被 patch。"""
    from app.services import mineru_client as mc

    session = _patch_httpx_session(monkeypatch)
    kwargs = dict(
        token="test-token",
        base_url="https://mineru.net",
        model="vlm",
        poll_interval=0.0,       # 轮询不真等待
        poll_timeout=30,
        http_timeout=1.0,
        upload_timeout=1.0,
        max_retries=2,
        retry_initial_wait=0.0,  # 重试退避不真等待
        retry_backoff_factor=1.0,
        retry_max_wait=0.0,
    )
    kwargs.update(overrides)
    return mc._MinerUNetClient(**kwargs), session


class TestMinerUNetEntryNormalization:
    """产物文件名归一化映射（对齐本地命名约定，chunker 依赖）。"""

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("full.md", "报告.md"),                                 # full.md → {stem}.md
            ("layout.json", "报告_middle.json"),                    # layout.json → {stem}_middle.json
            ("报告_model.json", "报告_model.json"),                  # *_model.json 幂等
            ("xxx_model.json", "报告_model.json"),
            ("报告_content_list.json", "报告_content_list_v2.json"),  # ★ chunker 主输入
            ("main.html", "main.html"),                             # 原样保留
            ("abc.docx", "abc.docx"),
        ],
    )
    def test_entry_mapping(self, settings, raw, expected):
        from app.services.mineru_client import _MinerUNetClient

        out = _MinerUNetClient._normalize_entry_name(Path(raw), "报告")
        assert out == Path(expected)

    def test_images_dir_kept(self, settings):
        from app.services.mineru_client import _MinerUNetClient

        out = _MinerUNetClient._normalize_entry_name(Path("images/fig1.jpg"), "报告")
        assert out == Path("images") / "fig1.jpg"


class TestMinerUNetAppearanceContract:
    """外观契约：parser 读 .api_url、pdf_fallback 读 .backend，语义须与 local 一致。"""

    def test_api_url_and_backend(self, settings):
        from app.services import mineru_client as mc

        c = mc._MinerUNetClient(token="t", base_url="https://mineru.net/", model="vlm")
        assert c.api_url == "https://mineru.net"  # rstrip("/")
        assert c.backend == "mineru-net/vlm"

    def test_filter_net_kwargs_drops_local_only(self, settings):
        from app.services import mineru_client as mc

        picked = mc._filter_net_kwargs({
            "token": "t", "poll_interval": 1,
            "api_url": "http://x", "backend": "vlm-engine", "effort": "high",
        })
        assert picked == {"token": "t", "poll_interval": 1}

    def test_defaults_from_settings(self, settings):
        from app.services import mineru_client as mc

        c = mc._MinerUNetClient()
        assert c.base_url == mc.settings.mineru_net_base_url.rstrip("/")
        assert c.model == mc.settings.mineru_net_model
        assert c.token == mc.settings.mineru_net_token


class TestMinerUNetUnwrapErrorClassification:
    """_unwrap 错误分级：token 失效熔断（不重试），业务码/非 JSON 可重试。"""

    def _client(self):
        from app.services import mineru_client as mc

        return mc._MinerUNetClient(token="t")

    def test_code_zero_returns_data(self, settings):
        import httpx

        resp = httpx.Response(200, json={"code": 0, "data": {"batch_id": "b1"}})
        assert self._client()._unwrap(resp, "x") == {"batch_id": "b1"}

    @pytest.mark.parametrize("code", ["A0202", "A0211"])
    def test_token_codes_are_fatal(self, settings, code):
        import httpx
        from app.services import mineru_client as mc

        resp = httpx.Response(200, json={"code": code, "msg": "token 无效"})
        with pytest.raises(mc._FatalMinerUError) as ei:
            self._client()._unwrap(resp, "file-urls/batch")
        assert ei.value.status_code == 200

    def test_http_401_is_fatal(self, settings):
        import httpx
        from app.services import mineru_client as mc

        resp = httpx.Response(401, json={"code": -1, "msg": "unauthorized"})
        with pytest.raises(mc._FatalMinerUError):
            self._client()._unwrap(resp, "x")

    def test_business_error_is_retryable(self, settings):
        import httpx
        from app.services import mineru_client as mc

        resp = httpx.Response(200, json={"code": -60009, "msg": "队列已满"})
        with pytest.raises(mc._RetryableMinerUError) as ei:
            self._client()._unwrap(resp, "x")
        assert "-60009" in str(ei.value)

    def test_non_json_body_is_retryable(self, settings):
        import httpx
        from app.services import mineru_client as mc

        resp = httpx.Response(502, content=b"<html>bad gateway</html>")
        with pytest.raises(mc._RetryableMinerUError):
            self._client()._unwrap(resp, "x")


class TestMinerUNetParseFileFlow:
    """提交 → 上传 → 轮询 → 下载 → 归一化落盘（mock httpx 全链路）。"""

    def test_full_flow_normalizes_artifacts(self, settings, tmp_path, monkeypatch):
        import httpx

        client, session = _make_net_client(monkeypatch)
        session.post.return_value = httpx.Response(200, json={
            "code": 0, "data": {"batch_id": "b1", "file_urls": ["https://up.example.com/x"]}})
        session.put.return_value = httpx.Response(200)
        session.get.side_effect = [
            httpx.Response(200, json={"code": 0, "data": {"extract_result": [
                {"state": "running", "extract_progress": {"extracted_pages": 3}}]}}),
            httpx.Response(200, json={"code": 0, "data": {"extract_result": [
                {"state": "done", "full_zip_url": "https://cdn.example.com/r.zip"}]}}),
            httpx.Response(200, content=_net_zip_bytes("report")),
        ]

        pdf = tmp_path / "report.pdf"
        pdf.write_bytes(b"%PDF-1.4 fake")
        parsed_dir = (tmp_path / "parsed" / "report").resolve()
        result = client.parse_file(pdf, parsed_dir)

        # ---- 步骤 1：申请上传链接（body / 鉴权）----
        post_call = session.post.call_args
        assert post_call.args[0] == "https://mineru.net/api/v4/file-urls/batch"
        assert post_call.kwargs["headers"]["Authorization"] == "Bearer test-token"
        assert post_call.kwargs["json"]["files"][0]["name"] == "report.pdf"
        assert post_call.kwargs["json"]["model_version"] == "vlm"

        # ---- 步骤 2：PUT 上传不设 Content-Type（官方要求）----
        put_call = session.put.call_args
        assert put_call.args[0] == "https://up.example.com/x"
        assert "headers" not in put_call.kwargs

        # ---- 归一化产物落盘（计划验证项 2 核心断言）----
        assert (parsed_dir / "report.md").read_text(encoding="utf-8") == "# 标题\n正文内容"
        assert (parsed_dir / "report_middle.json").is_file()           # layout.json
        assert (parsed_dir / "report_model.json").is_file()            # *_model.json
        assert (parsed_dir / "report_content_list_v2.json").is_file()  # *_content_list.json ★
        assert (parsed_dir / "images" / "fig1.jpg").is_file()
        assert (parsed_dir / "main.html").is_file()
        # ZIP 顶层目录已剥离（不再有 report/ 子目录）
        assert not (parsed_dir / "report").exists()

        assert result.md_path == parsed_dir / "report.md"
        assert result.response_kind == "zip"
        assert result.attempts == 1
        assert len(result.images) == 1
        assert result.file_count == 4  # md + json_path + 1 张图 + main.html

    def test_html_forces_mineru_html_model(self, settings, tmp_path, monkeypatch):
        import httpx

        client, session = _make_net_client(monkeypatch)
        session.post.return_value = httpx.Response(200, json={
            "code": 0, "data": {"batch_id": "b2", "file_urls": ["https://up.example.com/h"]}})
        session.put.return_value = httpx.Response(200)
        session.get.side_effect = [
            httpx.Response(200, json={"code": 0, "data": {"extract_result": [
                {"state": "done", "full_zip_url": "https://cdn.example.com/h.zip"}]}}),
            httpx.Response(200, content=_net_zip_bytes("page")),
        ]

        html = tmp_path / "page.html"
        html.write_text("<html>hi</html>", encoding="utf-8")
        parsed_dir = (tmp_path / "out" / "page").resolve()
        result = client.parse_file(html, parsed_dir)

        assert session.post.call_args.kwargs["json"]["model_version"] == "MinerU-HTML"
        assert result.md_path == parsed_dir / "page.md"

    def test_token_error_aborts_without_retry(self, settings, tmp_path, monkeypatch):
        """token 失效（A0202）→ 熔断：不重试、错误透传、空目录清理。"""
        import httpx
        from app.services import mineru_client as mc

        client, session = _make_net_client(monkeypatch)
        session.post.return_value = httpx.Response(200, json={"code": "A0202", "msg": "token 无效"})
        pdf = tmp_path / "a.pdf"
        pdf.write_bytes(b"%PDF")
        parsed_dir = (tmp_path / "out" / "a").resolve()

        with pytest.raises(mc.MinerUError) as ei:
            client.parse_file(pdf, parsed_dir)

        assert "A0202" in str(ei.value)
        assert session.post.call_count == 1  # 熔断：没有第二次请求
        assert not parsed_dir.exists()       # 失败后空目录已清理

    def test_retryable_error_retries_then_succeeds(self, settings, tmp_path, monkeypatch):
        """网关抖动（非 JSON 500）→ 按 max_retries 重试，第二次成功。"""
        import httpx

        client, session = _make_net_client(monkeypatch)
        session.post.side_effect = [
            httpx.Response(500, content=b"<html>bad gateway</html>"),
            httpx.Response(200, json={"code": 0, "data": {
                "batch_id": "b9", "file_urls": ["https://up.example.com/r"]}}),
        ]
        session.put.return_value = httpx.Response(200)
        session.get.side_effect = [
            httpx.Response(200, json={"code": 0, "data": {"extract_result": [
                {"state": "done", "full_zip_url": "https://cdn.example.com/r.zip"}]}}),
            httpx.Response(200, content=_net_zip_bytes("r")),
        ]
        pdf = tmp_path / "r.pdf"
        pdf.write_bytes(b"%PDF")
        parsed_dir = (tmp_path / "out" / "r").resolve()

        result = client.parse_file(pdf, parsed_dir)

        assert session.post.call_count == 2
        assert result.attempts == 2
        assert (parsed_dir / "r.md").is_file()

    def test_state_failed_is_fatal(self, settings, tmp_path, monkeypatch):
        """轮询到 state=failed → 立即失败（err_msg 带进错误信息）。"""
        import httpx
        from app.services import mineru_client as mc

        client, session = _make_net_client(monkeypatch)
        session.post.return_value = httpx.Response(200, json={
            "code": 0, "data": {"batch_id": "b3", "file_urls": ["https://up.example.com/f"]}})
        session.put.return_value = httpx.Response(200)
        session.get.return_value = httpx.Response(200, json={"code": 0, "data": {"extract_result": [
            {"state": "failed", "err_msg": "page limit exceeded"}]}})
        pdf = tmp_path / "f.pdf"
        pdf.write_bytes(b"%PDF")

        with pytest.raises(mc.MinerUError) as ei:
            client.parse_file(pdf, (tmp_path / "out" / "f").resolve())

        assert "page limit exceeded" in str(ei.value)
        assert session.post.call_count == 1


class TestMinerUNetHealthCheck:
    """健康检查：官方无 /health，借 file-urls/batch 鉴权层区分三态。"""

    def test_no_token_reports_error_without_http(self, settings, monkeypatch):
        from app.services import mineru_client as mc

        client_cls = MagicMock()
        monkeypatch.setattr(mc.httpx, "Client", client_cls)
        c = mc._MinerUNetClient(token="")
        out = c.health_check()
        assert out["healthy"] is False
        assert out["status"] == "error"
        assert "未配置" in out["detail"]
        client_cls.assert_not_called()  # 无 token 不发起 HTTP

    def test_healthy_ok(self, settings, monkeypatch):
        import httpx
        from app.services import mineru_client as mc

        session = _patch_httpx_session(
            monkeypatch, post=httpx.Response(200, json={"code": 0, "data": None}))
        c = mc._MinerUNetClient(token="t", model="vlm")
        out = c.health_check()
        assert out["healthy"] is True
        assert out["version"] == "mineru.net/vlm"
        assert out["status"] == "healthy"
        assert session.post.call_args.kwargs["json"] == {"files": [], "model_version": "vlm"}

    def test_token_invalid_reports_error(self, settings, monkeypatch):
        import httpx
        from app.services import mineru_client as mc

        _patch_httpx_session(
            monkeypatch, post=httpx.Response(200, json={"code": "A0211", "msg": "token 过期"}))
        c = mc._MinerUNetClient(token="t")
        out = c.health_check()
        assert out["healthy"] is False
        assert out["status"] == "error"
        assert "token" in out["detail"]

    def test_unreachable_on_connect_error(self, settings, monkeypatch):
        import httpx
        from app.services import mineru_client as mc

        _patch_httpx_session(monkeypatch, post=[httpx.ConnectError("connection refused")])
        c = mc._MinerUNetClient(token="t")
        out = c.health_check()
        assert out["healthy"] is False
        assert out["status"] == "unreachable"


class TestFacadeProviderDispatch:
    """facade 按 provider 分派 + auto 降级 + health_check 前缀/备用探测。"""

    def test_mineru_net_provider_filters_local_kwargs(self, settings):
        from app.services import mineru_client as mc

        c = mc.MinerUClient(
            provider="mineru_net", token="t",
            api_url="http://ignored:9999", backend="vlm-engine",  # local 专属，应被过滤
        )
        assert c.provider == "mineru_net"
        assert isinstance(c._impl, mc._MinerUNetClient)
        # 外观属性经 __getattr__ 委托给当前实现
        assert c.api_url == c._impl.api_url
        assert c.backend == c._impl.backend

    def test_local_provider_default_impl(self, settings):
        from app.services import mineru_client as mc

        c = mc.MinerUClient(provider="local", api_url="http://localhost:9999")
        assert isinstance(c._impl, mc._LocalMinerUClient)
        assert c.api_url == "http://localhost:9999"

    def test_invalid_provider_falls_back_to_local(self, settings):
        from app.services import mineru_client as mc

        c = mc.MinerUClient(provider="bogus", api_url="http://localhost:9999")
        assert c.provider == "local"
        assert isinstance(c._impl, mc._LocalMinerUClient)

    def test_auto_uses_local_when_healthy(self, settings, monkeypatch):
        from app.services import mineru_client as mc

        monkeypatch.setattr(
            mc._LocalMinerUClient, "health_check",
            lambda self, timeout=10.0: {"healthy": True, "version": "v", "status": "healthy", "detail": "ok"},
        )
        c = mc.MinerUClient(provider="auto", api_url="http://localhost:9999")
        assert isinstance(c._impl, mc._LocalMinerUClient)

    def test_auto_falls_back_to_net_at_runtime(self, settings, tmp_path, monkeypatch):
        """auto：本地解析失败且配置了 token → mineru.net 补救成功并粘性切换。"""
        from app.services import mineru_client as mc

        monkeypatch.setattr(mc.settings, "mineru_net_token", "tok")
        monkeypatch.setattr(
            mc._LocalMinerUClient, "health_check",
            lambda self, timeout=10.0: {"healthy": True, "version": "v", "status": "healthy", "detail": "ok"},
        )
        c = mc.MinerUClient(provider="auto", api_url="http://localhost:9999")
        assert isinstance(c._impl, mc._LocalMinerUClient)

        def _local_boom(self, file_path, parsed_dir):
            raise mc.MinerUError("local down", attempts=3)

        sentinel = object()

        def _net_ok(self, file_path, parsed_dir):
            return sentinel

        monkeypatch.setattr(mc._LocalMinerUClient, "parse_file", _local_boom)
        monkeypatch.setattr(mc._MinerUNetClient, "parse_file", _net_ok)

        f = tmp_path / "a.pdf"
        f.write_bytes(b"%PDF")
        assert c.parse_file(f, tmp_path / "out") is sentinel
        # 降级成功后粘性切换：后续文件直接走 mineru.net
        assert isinstance(c._impl, mc._MinerUNetClient)

    def test_auto_double_failure_raises_combined_error(self, settings, tmp_path, monkeypatch):
        from app.services import mineru_client as mc

        monkeypatch.setattr(mc.settings, "mineru_net_token", "tok")
        monkeypatch.setattr(
            mc._LocalMinerUClient, "health_check",
            lambda self, timeout=10.0: {"healthy": True, "version": "v", "status": "healthy", "detail": "ok"},
        )
        c = mc.MinerUClient(provider="auto", api_url="http://localhost:9999")

        def _boom(self, file_path, parsed_dir):
            raise mc.MinerUError("provider down", attempts=1)

        monkeypatch.setattr(mc._LocalMinerUClient, "parse_file", _boom)
        monkeypatch.setattr(mc._MinerUNetClient, "parse_file", _boom)

        f = tmp_path / "a.pdf"
        f.write_bytes(b"%PDF")
        with pytest.raises(mc.MinerUError) as ei:
            c.parse_file(f, tmp_path / "out")
        assert "auto 双 provider 均失败" in str(ei.value)

    def test_health_check_adds_provider_prefix(self, settings, monkeypatch):
        from app.services import mineru_client as mc

        monkeypatch.setattr(
            mc._MinerUNetClient, "health_check",
            lambda self, timeout=10.0: {
                "healthy": True, "version": "mineru.net/vlm",
                "status": "healthy", "detail": "token ok"},
        )
        c = mc.MinerUClient(provider="mineru_net", token="t")
        out = c.health_check()
        assert out["detail"] == "[provider=mineru_net] token ok"

    def test_auto_health_check_reports_backup(self, settings, monkeypatch):
        from app.services import mineru_client as mc

        monkeypatch.setattr(mc.settings, "mineru_net_token", "tok")
        monkeypatch.setattr(
            mc._LocalMinerUClient, "health_check",
            lambda self, timeout=10.0: {"healthy": True, "version": "v", "status": "healthy", "detail": "local ok"},
        )
        monkeypatch.setattr(
            mc._MinerUNetClient, "health_check",
            lambda self, timeout=10.0: {"healthy": True, "version": "n", "status": "healthy", "detail": "net ok"},
        )
        c = mc.MinerUClient(provider="auto", api_url="http://localhost:9999")
        out = c.health_check()
        assert out["detail"].startswith("[provider=auto] ")
        assert "备用 provider: healthy" in out["detail"]
