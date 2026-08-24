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
    cfg = cfg_mod.settings
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

        with patch.object(client, "_post") as mock_post:
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

        with patch.object(client, "_post") as mock_post:
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
        with patch.object(client, "_post") as mock_post, \
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
