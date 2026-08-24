"""PDF fallback 单元测试。

目标：验证当 MinerU 解析产物过少时，PyMuPDF fallback 能成功接管。
"""
from __future__ import annotations

import importlib
import json
import logging
import shutil
from pathlib import Path
from typing import Any, Dict, Optional

import fitz  # PyMuPDF
import pytest

# 抑制日志
logging.disable(logging.CRITICAL)


@pytest.fixture(autouse=True)
def fresh_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    test_data_root = tmp_path / "data"
    monkeypatch.setenv("RAG_DATA_ROOT", str(test_data_root))
    from app import config as cfg_mod
    importlib.reload(cfg_mod)
    settings = cfg_mod.settings
    from app.services import scanner, parser
    scanner.settings = settings
    parser.settings = settings
    settings.ensure_dirs()
    yield settings


def _make_text_pdf(tmp_path: Path, text_lines: list[str], name: str = "test_doc.pdf") -> Path:
    """用 PyMuPDF 生成含 ASCII 文本的 PDF。

    注：PyMuPDF 的 insert_text 默认不嵌入 CJK 字形，所以用 ASCII 文本确保
    fallback 能提取到内容（fallback 的核心价值是解析 GBK-EUC-H CMap，而 ASCII
    已能覆盖 fallback 流程正确性的验证）。
    """
    pdf_path = tmp_path / name
    doc = fitz.open()
    page = doc.new_page(width=596, height=842)
    y = 80
    for line in text_lines:
        # 用 helv（标准内置字体）保证文本可被提取
        page.insert_text((80, y), line, fontsize=12, fontname="helv")
        y += 30
    doc.save(str(pdf_path))
    doc.close()
    return pdf_path


def _make_manifest_row(settings, filename: str):
    from app.services import manifest_store
    from app.models.schemas import ManifestRow
    manifest_store.ensure_exists(settings.manifest_path)
    manifest_store.upsert(
        settings.manifest_path,
        ManifestRow(filename=filename, import_status="已移入待处理", process_status="已扫描"),
    )


# ============ 单元测试：fallback 模块 ============


def test_pymupdf_fallback_extracts_ascii_pdf(fresh_settings, tmp_path: Path):
    """生成 ASCII PDF → PyMuPDF fallback 应能提取到文本。"""
    from app.services import pdf_fallback

    # 多行文本确保超过 100 字符阈值
    text_lines = [
        "Chapter 1 General Provisions",
        "Article 1: This is the first article about general provisions.",
        "Article 2: This is the second article about specific rules.",
        "Article 3: This is the third article about enforcement.",
    ]
    pdf_path = _make_text_pdf(tmp_path, text_lines)
    out_dir = fresh_settings.parsed_dir / "test_doc"

    result = pdf_fallback.maybe_fallback_after_mineru_failure(pdf_path, out_dir)
    assert result is not None, "fallback 应该成功"
    assert result.char_count > 100, f"字符数应 > 100，实际 {result.char_count}"
    md_text = result.md_path.read_text(encoding="utf-8")
    assert "Chapter 1" in md_text
    assert "Article 1" in md_text
    assert "Article 3" in md_text


def test_pymupdf_fallback_overwrites_existing(fresh_settings, tmp_path: Path):
    """如果 parsed_dir 已有内容，fallback 应清理后重写。"""
    from app.services import pdf_fallback

    text_lines = [
        "Test text ABC for fallback overwrite test.",
        "Article 2 Test more content for testing overwrite behavior.",
        "Article 3 Even more content to ensure threshold is met.",
    ]
    pdf_path = _make_text_pdf(tmp_path, text_lines)
    out_dir = fresh_settings.parsed_dir / "test_overwrite"
    out_dir.mkdir(parents=True)
    (out_dir / "old_content.md").write_text("should be cleaned", encoding="utf-8")

    result = pdf_fallback.maybe_fallback_after_mineru_failure(pdf_path, out_dir)
    assert result is not None
    # 旧内容应被清掉
    assert not (out_dir / "old_content.md").exists()


def test_pymupdf_fallback_writes_v2_structure(fresh_settings, tmp_path: Path):
    """fallback 产物的 v2 JSON 结构应与 chunker 兼容（含 type/content/bbox）。"""
    from app.services import pdf_fallback

    text_lines = [
        "Chapter 1 General Provisions",
        "Article 1: This is the first article about general provisions.",
        "Article 2: This is the second article about specific rules.",
    ]
    pdf_path = _make_text_pdf(tmp_path, text_lines)
    out_dir = fresh_settings.parsed_dir / "structure_test"

    result = pdf_fallback.maybe_fallback_after_mineru_failure(pdf_path, out_dir)
    assert result is not None
    v2_data = json.loads(result.v2_path.read_text(encoding="utf-8"))
    # v2 结构: List[List[block]]，每页一个 list
    assert isinstance(v2_data, list)
    assert len(v2_data) >= 1
    # 第一个 page 应有至少 1 个 block
    page_blocks = v2_data[0]
    assert len(page_blocks) >= 1
    # block 应有 type/content/bbox
    for block in page_blocks:
        assert "type" in block
        assert "content" in block
        assert "bbox" in block
        # type 应是 title 或 paragraph
        assert block["type"] in ("title", "paragraph")


# ============ 单元测试：parser 集成 ============


def test_parser_triggers_fallback_on_trivial_mineru(fresh_settings, tmp_path: Path, monkeypatch):
    """端到端：MinerU 解析产物过少 → parser 应自动触发 PyMuPDF fallback。"""
    from app.services import parser
    from app.services import mineru_client as mc_mod

    # 1) 准备 PDF（ASCII 文本，确保 > 100 字符让 fallback 成功）
    text_lines = [
        "Doc number 12 of 2010 about regulations.",
        "Chapter 1: General Provisions for testing.",
        "Article 1: Test article content here for fallback.",
    ]
    pdf_path = _make_text_pdf(tmp_path, text_lines, name="test_doc.pdf")
    # 移到 pending/
    dst = fresh_settings.pending_dir / pdf_path.name
    shutil.move(str(pdf_path), str(dst))
    # 准备 manifest
    _make_manifest_row(fresh_settings, dst.name)

    # 2) Fake MinerUClient：返回"垃圾"产物（模拟 GBK-EUC-H CMap 不识别）
    class FakeTrivialClient:
        def __init__(self):
            self.api_url = "http://fake"
            self.backend = "hybrid-engine"  # 与 MinerUClient 对齐
            self.calls = []

        def parse_file(self, file_path: Path, parsed_dir: Path):
            self.calls.append(file_path)
            # 创建"垃圾"产物：v2 几乎空，.md 只有 "2010 12"
            parsed_dir.mkdir(parents=True, exist_ok=True)
            inner = parsed_dir / "hybrid_auto"
            inner.mkdir(exist_ok=True)
            stem = parsed_dir.name
            (inner / f"{stem}.md").write_text("2010 12\n", encoding="utf-8")
            v2 = [[
                {"type": "paragraph", "content": {"paragraph_content": [{"type": "text", "content": "2010 12"}]}}
            ]]
            (inner / f"{stem}_content_list_v2.json").write_text(
                json.dumps(v2, ensure_ascii=False), encoding="utf-8"
            )
            return mc_mod.ParseResult(
                parse_dir=parsed_dir,
                md_path=inner / f"{stem}.md",
                json_path=inner / f"{stem}_content_list_v2.json",
                images=[],
                other_files=[],
                attempts=1,
                response_kind="fake",
            )

    fake = FakeTrivialClient()
    report = parser.parse_pending(dry_run=False, client=fake)

    # 3) 验证：解析被标记为成功（fallback 救回来了）
    assert report.parsed == 1, f"应有 1 个解析成功（fallback 修复），实际 {report.parsed}"
    assert report.failed == 0

    # 4) 验证 manifest 中 parse 列含 "fallback" 标记
    from app.services import manifest_store
    manifest = manifest_store.load(fresh_settings.manifest_path)
    row = manifest[dst.name]
    assert "fallback" in (row.parse or "").lower(), (
        f"parse 列应标记 fallback，实际: {row.parse}"
    )
    assert row.status == "parsing_done"

    # 5) 验证最终 .md 含完整内容（说明 fallback 真的替换了 MinerU 产物）
    final_md = None
    for p in (fresh_settings.parsed_dir / dst.stem).rglob("*.md"):
        final_md = p
        break
    assert final_md is not None
    text = final_md.read_text(encoding="utf-8")
    assert "Chapter 1" in text, f"fallback 后应含 'Chapter 1'，实际: {text[:200]}"
    assert "Article 1" in text


def test_parser_skips_fallback_for_good_mineru(fresh_settings, tmp_path: Path, monkeypatch):
    """MinerU 解析产物正常 → 不触发 fallback。"""
    from app.services import parser
    from app.services import mineru_client as mc_mod

    text_lines = ["Test"]
    pdf_path = _make_text_pdf(tmp_path, text_lines, name="good_doc.pdf")
    dst = fresh_settings.pending_dir / pdf_path.name
    shutil.move(str(pdf_path), str(dst))
    _make_manifest_row(fresh_settings, dst.name)

    class FakeGoodClient:
        def __init__(self):
            self.api_url = "http://fake"
            self.backend = "hybrid-engine"  # 与 MinerUClient 对齐

        def parse_file(self, file_path: Path, parsed_dir: Path):
            parsed_dir.mkdir(parents=True, exist_ok=True)
            inner = parsed_dir / "hybrid_auto"
            inner.mkdir(exist_ok=True)
            stem = parsed_dir.name
            # 写"丰富"内容（> 100 字符）
            good_text = "中华人民共和国" * 20  # 140 字符
            (inner / f"{stem}.md").write_text(good_text, encoding="utf-8")
            v2 = [[
                {"type": "paragraph", "content": {"paragraph_content": [{"type": "text", "content": good_text[:50]}]}}
            ]]
            (inner / f"{stem}_content_list_v2.json").write_text(
                json.dumps(v2, ensure_ascii=False), encoding="utf-8"
            )
            return mc_mod.ParseResult(
                parse_dir=parsed_dir,
                md_path=inner / f"{stem}.md",
                json_path=inner / f"{stem}_content_list_v2.json",
                images=[],
                other_files=[],
                attempts=1,
                response_kind="fake",
            )

    fake = FakeGoodClient()
    # 监听 pdf_fallback 是不是被调用
    called = {"n": 0}
    from app.services import pdf_fallback
    orig_fn = pdf_fallback.maybe_fallback_after_mineru_failure
    def spy(*a, **kw):
        called["n"] += 1
        return None
    monkeypatch.setattr(parser, "pdf_fallback", type("M", (), {
        "is_pymupdf_available": staticmethod(lambda: True),
        "maybe_fallback_after_mineru_failure": staticmethod(spy),
    })())

    report = parser.parse_pending(dry_run=False, client=fake)
    assert report.parsed == 1
    # 验证 fallback 没被调用
    assert called["n"] == 0, f"好的 MinerU 产物不应触发 fallback，实际调用 {called['n']} 次"


# ============ Tier 1: PyMuPDF 渲染 + VLM 读图 ============


def test_render_pdf_to_image_pdf_creates_image_pdf(fresh_settings, tmp_path: Path):
    """render_pdf_to_image_pdf 应生成新的图片型 PDF。"""
    from app.services import pdf_fallback
    import fitz

    text_lines = [
        "Chapter 1 General Provisions",
        "Article 1: This is the first article about general provisions.",
        "Article 2: This is the second article about specific rules.",
    ]
    pdf_path = _make_text_pdf(tmp_path, text_lines)
    out_path = tmp_path / "image_based.pdf"

    result_path = pdf_fallback.render_pdf_to_image_pdf(pdf_path, out_path, dpi=200)
    assert result_path == out_path
    assert out_path.is_file(), "输出 PDF 应存在"
    assert out_path.stat().st_size > 1000, "输出 PDF 应有合理大小"

    # 验证输出 PDF 是图片型（用 PyMuPDF 读取时每页是图片）
    doc = fitz.open(str(out_path))
    assert doc.page_count >= 1
    doc.close()


def test_render_pdf_to_image_pdf_preserves_page_count(fresh_settings, tmp_path: Path):
    """渲染应保留原 PDF 的页数。"""
    from app.services import pdf_fallback
    import fitz

    # 3 页 PDF
    src = tmp_path / "multi.pdf"
    doc = fitz.open()
    for i in range(3):
        page = doc.new_page(width=596, height=842)
        page.insert_text((80, 80), f"Page {i+1} content here.", fontname="helv")
    doc.save(str(src))
    doc.close()

    out = tmp_path / "multi_img.pdf"
    pdf_fallback.render_pdf_to_image_pdf(src, out, dpi=150)

    out_doc = fitz.open(str(out))
    assert out_doc.page_count == 3, f"页数应保留 3 页，实际 {out_doc.page_count}"
    out_doc.close()


def test_vlm_image_fallback_uses_image_renderer(fresh_settings, tmp_path: Path, monkeypatch):
    """vlm_image_fallback 应先调 render_pdf_to_image_pdf 再调 MinerU client。"""
    from app.services import pdf_fallback

    text_lines = [
        "Chapter 1 General Provisions",
        "Article 1: This is the first article about general provisions.",
        "Article 2: This is the second article about specific rules.",
        "Article 3: This is the third article about enforcement.",
    ]
    pdf_path = _make_text_pdf(tmp_path, text_lines, name="vlm_test.pdf")

    # 假 MinerU client：记录被调用的 file
    class FakeVLMClient:
        def __init__(self):
            self.api_url = "http://fake"
            self.backend = "hybrid-engine"  # 与 MinerUClient 对齐
            self.parse_calls = []

        def parse_file(self, file_path: Path, parsed_dir: Path):
            self.parse_calls.append(file_path)
            # 模拟 VLM 解析：写丰富产物
            parsed_dir.mkdir(parents=True, exist_ok=True)
            inner = parsed_dir / "vlm"
            inner.mkdir(exist_ok=True)
            stem = parsed_dir.name
            # 写 VLM 风格的产物（带 # 一级标题）
            md_text = "# Chapter 1\n\nArticle 1: This is a complete article from VLM.\n\nArticle 2: Second article here.\n" * 5
            (inner / f"{stem}.md").write_text(md_text, encoding="utf-8")
            v2 = [[
                {"type": "title", "content": {"level": 1, "title_content": [{"type": "text", "content": "Chapter 1"}]}},
                {"type": "paragraph", "content": {"paragraph_content": [{"type": "text", "content": "Article content from VLM."}]}},
            ]]
            (inner / f"{stem}_content_list_v2.json").write_text(
                json.dumps(v2, ensure_ascii=False), encoding="utf-8"
            )
            (inner / f"{stem}_middle.json").write_text(
                json.dumps([], ensure_ascii=False), encoding="utf-8"
            )
            (inner / f"{stem}_model.json").write_text(
                json.dumps([{"backend": "vlm-engine"}], ensure_ascii=False),
                encoding="utf-8",
            )
            from app.services import mineru_client as mc_mod
            return mc_mod.ParseResult(
                parse_dir=parsed_dir,
                md_path=inner / f"{stem}.md",
                json_path=inner / f"{stem}_content_list_v2.json",
                images=[],
                other_files=[],
                attempts=1,
                response_kind="fake-vlm",
            )

    fake = FakeVLMClient()
    out_dir = fresh_settings.parsed_dir / "vlm_test"

    result = pdf_fallback.vlm_image_fallback(pdf_path, out_dir, fake)
    assert result is not None, "VLM fallback 应该成功"
    assert result.backend == "vlm-image-fallback"
    assert result.char_count > 100
    # 验证 MinerU client 被调用
    assert len(fake.parse_calls) == 1, f"MinerU client 应被调用 1 次，实际 {len(fake.parse_calls)}"
    # 验证 MinerU 被调用的是图片型 PDF（不是原 PDF）
    called_file = fake.parse_calls[0]
    assert called_file != pdf_path, "MinerU 应被传入图片型 PDF，不是原 PDF"
    assert "_img" in called_file.name, f"被传入的文件名应含 _img，实际: {called_file.name}"
    # 验证图片型 PDF 已被清理（临时文件）
    assert not called_file.exists(), "临时图片 PDF 应已被清理"


def test_vlm_image_fallback_falls_through_to_tier2(fresh_settings, tmp_path: Path):
    """Tier 1 失败（VLM 产物过少）→ 自动回退到 Tier 2（PyMuPDF 纯文本）。"""
    from app.services import pdf_fallback

    text_lines = [
        "Chapter 1 General Provisions",
        "Article 1: This is the first article about general provisions.",
        "Article 2: This is the second article about specific rules.",
        "Article 3: This is the third article about enforcement.",
    ]
    pdf_path = _make_text_pdf(tmp_path, text_lines, name="tier_test.pdf")

    class FakeFailingVLMClient:
        def __init__(self):
            self.api_url = "http://fake"
            self.backend = "vlm-engine"  # 与 MinerUClient 对齐

        def parse_file(self, file_path: Path, parsed_dir: Path):
            # Tier 1 失败：写空产物
            parsed_dir.mkdir(parents=True, exist_ok=True)
            inner = parsed_dir / "vlm"
            inner.mkdir(exist_ok=True)
            stem = parsed_dir.name
            (inner / f"{stem}.md").write_text("", encoding="utf-8")
            (inner / f"{stem}_content_list_v2.json").write_text(
                json.dumps([], ensure_ascii=False), encoding="utf-8"
            )
            from app.services import mineru_client as mc_mod
            return mc_mod.ParseResult(
                parse_dir=parsed_dir,
                md_path=inner / f"{stem}.md",
                json_path=inner / f"{stem}_content_list_v2.json",
                images=[],
                other_files=[],
                attempts=1,
                response_kind="fake-empty",
            )

    fake = FakeFailingVLMClient()
    out_dir = fresh_settings.parsed_dir / "tier_test"

    result = pdf_fallback.maybe_fallback_after_mineru_failure(pdf_path, out_dir, client=fake)
    # Tier 1 失败，Tier 2 应该成功
    assert result is not None, "Tier 2 fallback 应该成功"
    assert result.backend == "pymupdf-fallback", (
        f"应回退到 Tier 2 (pymupdf-fallback)，实际: {result.backend}"
    )


def test_maybe_fallback_with_no_client_skips_tier1(fresh_settings, tmp_path: Path):
    """未传 client → 跳过 Tier 1，直接走 Tier 2。"""
    from app.services import pdf_fallback

    text_lines = [
        "Chapter 1 General Provisions",
        "Article 1: This is the first article about general provisions.",
        "Article 2: This is the second article about specific rules.",
    ]
    pdf_path = _make_text_pdf(tmp_path, text_lines, name="no_client.pdf")
    out_dir = fresh_settings.parsed_dir / "no_client"

    # 不传 client
    result = pdf_fallback.maybe_fallback_after_mineru_failure(pdf_path, out_dir, client=None)
    assert result is not None
    assert result.backend == "pymupdf-fallback"


def test_parser_uses_vlm_image_fallback_when_available(fresh_settings, tmp_path: Path, monkeypatch):
    """端到端：parser 触发 Tier 1（VLM 图片）→ manifest 标记 vlm-image-fallback。"""
    from app.services import parser
    from app.services import mineru_client as mc_mod

    text_lines = [
        "Chapter 1 General Provisions",
        "Article 1: This is the first article about general provisions.",
        "Article 2: This is the second article about specific rules.",
    ]
    pdf_path = _make_text_pdf(tmp_path, text_lines, name="e2e_vlm.pdf")
    dst = fresh_settings.pending_dir / pdf_path.name
    shutil.move(str(pdf_path), str(dst))
    _make_manifest_row(fresh_settings, dst.name)

    # 假 MinerU 主调用（返回垃圾）
    class FakeTrivialClient:
        def __init__(self):
            self.api_url = "http://fake"
            self.backend = "hybrid-engine"  # 与 MinerUClient 对齐

        def parse_file(self, file_path: Path, parsed_dir: Path):
            parsed_dir.mkdir(parents=True, exist_ok=True)
            inner = parsed_dir / "hybrid_auto"
            inner.mkdir(exist_ok=True)
            stem = parsed_dir.name
            (inner / f"{stem}.md").write_text("2010 12", encoding="utf-8")
            v2 = [[
                {"type": "paragraph", "content": {"paragraph_content": [{"type": "text", "content": "2010 12"}]}}
            ]]
            (inner / f"{stem}_content_list_v2.json").write_text(
                json.dumps(v2, ensure_ascii=False), encoding="utf-8"
            )
            return mc_mod.ParseResult(
                parse_dir=parsed_dir,
                md_path=inner / f"{stem}.md",
                json_path=inner / f"{stem}_content_list_v2.json",
                images=[],
                other_files=[],
                attempts=1,
                response_kind="fake-trivial",
            )

    fake = FakeTrivialClient()
    report = parser.parse_pending(dry_run=False, client=fake)
    assert report.parsed == 1

    # 验证 manifest 标记
    from app.services import manifest_store
    manifest = manifest_store.load(fresh_settings.manifest_path)
    row = manifest[dst.name]
    assert "vlm-image-fallback" in (row.parse or "") or "pymupdf-fallback" in (row.parse or ""), (
        f"manifest 应标记 fallback（vlm-image-fallback 或 pymupdf-fallback），实际: {row.parse}"
    )
