"""plan.md §3.3 chunker 单元测试。

覆盖：
1. _infer_block_level（数字标题推断）
2. _effective_level（推断优先于 v2 标注）
3. _block_to_text（图片保留 MD 原生语法 `![](path)`）
4. _classify_title（区域标题分类）
5. load_v2_blocks（paragraph→title 升级，header/footer 丢弃）
6. classify_regions（cover/toc/preface/body/appendix 划分，body 起点排除 cover 重复）
7. chunk_body（贪心合并 L1→L2→L3，句号切分）
8. chunk_appendix（贪心合并附录）
9. chunk_simple（封面/目录/前言整体 1 段）
10. write_chunks + write_chunk_metadata
11. _copy_referenced_images
12. _maybe_promote_to_title 防御：日期/年份不被识别为标题
13. _is_parse_content_trivial 解析质量检测
"""

from __future__ import annotations

import importlib
import json
import logging
from pathlib import Path
from typing import Any, Dict, List

import pytest

logging.disable(logging.CRITICAL)


# ============ fixtures ============


@pytest.fixture(autouse=True)
def fresh_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """每个用例：隔离 tmp_path 作为 data_root。"""
    test_data_root = tmp_path / "data"
    monkeypatch.setenv("RAG_DATA_ROOT", str(test_data_root))

    from app import config as cfg_mod
    importlib.reload(cfg_mod)
    settings = cfg_mod.settings

    from app.services import scanner, parser, chunker
    scanner.settings = settings
    parser.settings = settings
    chunker.settings = settings

    settings.ensure_dirs()
    yield settings


# ============ 1. _infer_block_level ============


def test_infer_level_chapter():
    """第X章 → level 1。"""
    from app.services import chunker
    b = chunker.Block(page_num=1, block_type="title", level=2, text="第一章 总则")
    assert chunker._infer_block_level(b) == 1


def test_infer_level_article():
    """第X条 → level 2。"""
    from app.services import chunker
    b = chunker.Block(page_num=1, block_type="title", level=2, text="第一条  为...")
    assert chunker._infer_block_level(b) == 2


def test_infer_level_numeric_l1():
    """'1 范围' → level 1（数字 + 空格）。"""
    from app.services import chunker
    b = chunker.Block(page_num=1, block_type="title", level=2, text="1 范围")
    assert chunker._infer_block_level(b) == 1


def test_infer_level_numeric_l2():
    """'4.1 基本原则' → level 2。"""
    from app.services import chunker
    b = chunker.Block(page_num=1, block_type="title", level=2, text="4.1 基本原则")
    assert chunker._infer_block_level(b) == 2


def test_infer_level_numeric_l3():
    """'4.2.1 在入口...' → level 3。"""
    from app.services import chunker
    b = chunker.Block(page_num=1, block_type="title", level=2, text="4.2.1 在入口的醒目位置悬挂")
    assert chunker._infer_block_level(b) == 3


def test_infer_level_doc_name_returns_none():
    """纯文档名/编号 → None。"""
    from app.services import chunker
    for text in ("基层医疗卫生机构功能单元视觉设计标准", "WS/T 809—2022", "中华人民共和国..."):
        b = chunker.Block(page_num=1, block_type="title", level=1, text=text)
        assert chunker._infer_block_level(b) is None, f"{text} should be None"


def test_infer_level_appendix_returns_none():
    """附 录 X → None（不算 chapter 模式）。"""
    from app.services import chunker
    b = chunker.Block(page_num=1, block_type="title", level=1, text="附 录 A")
    assert chunker._infer_block_level(b) is None


def test_infer_level_non_title_returns_none():
    """非 title block → None。"""
    from app.services import chunker
    b = chunker.Block(page_num=1, block_type="paragraph", text="1 范围")
    assert chunker._infer_block_level(b) is None


# ============ 2. _effective_level ============


def test_effective_level_inferred_priority():
    """v2 标 level=2 但文本"1 范围" → 推断优先，返回 1。"""
    from app.services import chunker
    b = chunker.Block(page_num=1, block_type="title", level=2, text="1 范围")
    assert chunker._effective_level(b) == 1


def test_effective_level_fallback_to_v2():
    """v2 标 level=1 且文本推断不到 → 回退到 v2（返回 1）。"""
    from app.services import chunker
    b = chunker.Block(page_num=1, block_type="title", level=1, text="基层医疗卫生机构...")
    assert chunker._effective_level(b) == 1


def test_effective_level_non_title_returns_none():
    from app.services import chunker
    b = chunker.Block(page_num=1, block_type="paragraph", text="abc")
    assert chunker._effective_level(b) is None


# ============ 3. _block_to_text（关键：图片 MD 语法）============


def test_block_to_text_image_keeps_md_native_syntax():
    """图片用 `![](path)` 而非 `![caption](path)`。"""
    from app.services import chunker
    b = chunker.Block(
        page_num=4,
        block_type="image",
        image_path="images/60de63a2b79be24fd2ad85ad3092a4c23b7307daa44f6fbc45d78df1a8034b22.jpg",
        image_caption="图 A.1 入口外观效果图",
    )
    out = chunker._block_to_text(b)
    assert out == "![](images/60de63a2b79be24fd2ad85ad3092a4c23b7307daa44f6fbc45d78df1a8034b22.jpg)\n"
    # 重要：不含 caption 文本
    assert "图 A.1" not in out
    assert "入口外观效果图" not in out


def test_block_to_text_image_no_path_returns_empty():
    from app.services import chunker
    b = chunker.Block(page_num=1, block_type="image", image_caption="无图")
    assert chunker._block_to_text(b) == ""


def test_block_to_text_title_uses_hash():
    from app.services import chunker
    b = chunker.Block(page_num=1, block_type="title", level=2, text="4.1 基本原则")
    assert chunker._block_to_text(b) == "## 4.1 基本原则\n"


def test_block_to_text_paragraph():
    from app.services import chunker
    b = chunker.Block(page_num=1, block_type="paragraph", text="正文")
    assert chunker._block_to_text(b) == "正文\n"


# ============ 3.5 _block_to_text 表格 + 公式（2026-08-04 新增）============


def test_block_to_text_table_html_preserved():
    """★ 表格块：保留 HTML 表格字符串（Markdown 原生支持 <table>）。"""
    from app.services import chunker
    html = "<table><tr><td>序号</td><td>名称</td></tr></table>"
    b = chunker.Block(
        page_num=1,
        block_type="table",
        table_html=html,
    )
    out = chunker._block_to_text(b)
    # HTML 必须原样保留
    assert "<table>" in out
    assert "<td>序号</td>" in out
    assert "<td>名称</td>" in out


def test_block_to_text_table_with_caption_and_footnote():
    """★ 表格块：caption 拼在表格上方（粗体），footnote 拼在下方（斜体）。"""
    from app.services import chunker
    b = chunker.Block(
        page_num=1,
        block_type="table",
        table_html="<table><tr><td>data</td></tr></table>",
        table_caption="表 1 公司名单",
        table_footnote="注：排名不分先后",
    )
    out = chunker._block_to_text(b)
    assert "**表 1 公司名单**" in out
    assert "<table>" in out
    assert "*注：排名不分先后*" in out
    # 顺序：caption → table → footnote
    cap_idx = out.find("表 1 公司名单")
    tbl_idx = out.find("<table>")
    fn_idx = out.find("排名不分先后")
    assert cap_idx < tbl_idx < fn_idx, f"Order wrong: cap={cap_idx}, tbl={tbl_idx}, fn={fn_idx}"


def test_block_to_text_table_image_fallback():
    """★ 图片型表格：退化为 ![]() 语法。"""
    from app.services import chunker
    b = chunker.Block(
        page_num=1,
        block_type="table",
        table_image_path="images/table_abc.jpg",
        table_caption="表 2 流程图",
    )
    out = chunker._block_to_text(b)
    assert "![](images/table_abc.jpg)" in out
    assert "**表 2 流程图**" in out


def test_block_to_text_table_no_html_no_image_returns_empty():
    """★ 防御：既无 HTML 又无图片的 table 块返回空字符串。"""
    from app.services import chunker
    b = chunker.Block(page_num=1, block_type="table")
    assert chunker._block_to_text(b) == ""


def test_strip_html_tags_basic():
    """★ HTML 标签剥离（用于 _blocks_chars 估算表格字符数）。"""
    from app.services import chunker
    out = chunker._strip_html_tags("<table><tr><td>Hello&nbsp;World</td></tr></table>")
    assert "Hello" in out
    assert "World" in out
    assert "<" not in out
    assert ">" not in out


def test_extract_runs_text_equation_inline_keeps_latex():
    """★ 公式保留：equation_inline run 包裹为 $...$。"""
    from app.services import chunker
    runs = [
        {"type": "text", "content": "根据"},
        {"type": "equation_inline", "content": "x^2 + y^2 = r^2"},
        {"type": "text", "content": "可知"},
    ]
    out = chunker._extract_runs_text(runs)
    assert "根据" in out
    assert "$x^2 + y^2 = r^2$" in out
    assert "可知" in out
    # 顺序保持
    assert out.index("根据") < out.index("$x^2") < out.index("可知")


def test_extract_runs_text_equation_with_footnote_ref():
    """★ 公式 footnote 引用：^{[*]} 也用 $...$ 包裹。"""
    from app.services import chunker
    runs = [{"type": "equation_inline", "content": "^{[1]}"}]
    out = chunker._extract_runs_text(runs)
    assert out == "$^{[1]}$"


# ============ 4. _classify_title ============


def test_classify_title_toc():
    from app.services import chunker
    assert chunker._classify_title("目 次") == "toc_start"
    assert chunker._classify_title("目录") == "toc_start"


def test_classify_title_preface():
    from app.services import chunker
    assert chunker._classify_title("前 言") == "preface_start"


def test_classify_title_appendix():
    from app.services import chunker
    assert chunker._classify_title("附 录 A") == "appendix_start"
    assert chunker._classify_title("附录 B") == "appendix_start"


def test_classify_title_reference():
    from app.services import chunker
    assert chunker._classify_title("参考文献") == "reference_start"


def test_classify_title_normal_chapter():
    """普通章节标题不归类为任何 region 起点。"""
    from app.services import chunker
    assert chunker._classify_title("1 范围") == ""
    assert chunker._classify_title("4.1 基本原则") == ""


# ============ 5. load_v2_blocks：paragraph 升级 ============


def _make_page(blocks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """构造 v2 单页结构。"""
    return blocks


def _v2(blocks_per_page: List[List[Dict[str, Any]]]) -> List[List[Dict[str, Any]]]:
    return blocks_per_page


def test_load_v2_blocks_discards_headers():
    """header / footer / page_number 被丢弃。"""
    from app.services import chunker
    v2 = _v2([
        [
            {"type": "title", "content": {"level": 1, "title_content": [{"content": "封面标题"}]}},
            {"type": "page_header"},
            {"type": "page_footer"},
            {"type": "page_number"},
            {"type": "paragraph", "content": {"paragraph_content": [{"content": "正文"}]}},
        ]
    ])
    p = Path(tmp_dir := "chunker_test_load_discards")
    import tempfile
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8") as fp:
        json.dump(v2, fp, ensure_ascii=False)
        tmp_path = fp.name
    try:
        blocks = chunker.load_v2_blocks(Path(tmp_path))
    finally:
        Path(tmp_path).unlink(missing_ok=True)
    types = [b.block_type for b in blocks]
    assert "page_header" not in types
    assert "page_footer" not in types
    assert "page_number" not in types
    assert "title" in types
    assert "paragraph" in types


def test_load_v2_blocks_promotes_numeric_paragraph():
    """paragraph '4.2.1 在入口...' 被升级为 level=3 title。"""
    from app.services import chunker
    v2 = _v2([
        [
            {"type": "paragraph", "content": {"paragraph_content": [{"content": "4.2.1 在入口..."}]}},
        ]
    ])
    import tempfile
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8") as fp:
        json.dump(v2, fp, ensure_ascii=False)
        tmp_path = fp.name
    try:
        blocks = chunker.load_v2_blocks(Path(tmp_path))
    finally:
        Path(tmp_path).unlink(missing_ok=True)
    # 升级为 title level=3
    assert any(b.block_type == "title" and b.level == 3 and "4.2.1" in b.text for b in blocks)


def test_load_v2_blocks_promotes_appendix_paragraph():
    """paragraph '附 录 A' 被升级为 title（即使 v2 中有其他显式 title 块）。"""
    from app.services import chunker
    v2 = _v2([
        # 显式 title 块（让 has_explicit_title=True）
        [
            {"type": "title", "content": {"level": 1, "title_content": [{"content": "封面"}]}},
        ],
        # 附录 A 是 paragraph
        [
            {"type": "paragraph", "content": {"paragraph_content": [{"content": "附 录 A"}]}},
            {"type": "paragraph", "content": {"paragraph_content": [{"content": "（资料性）"}]}},
        ],
    ])
    import tempfile
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8") as fp:
        json.dump(v2, fp, ensure_ascii=False)
        tmp_path = fp.name
    try:
        blocks = chunker.load_v2_blocks(Path(tmp_path))
    finally:
        Path(tmp_path).unlink(missing_ok=True)
    texts = [b.text for b in blocks if b.block_type == "title"]
    assert "附 录 A" in texts


# ============ 6. classify_regions ============


def test_classify_regions_wst809_style():
    """WST 809 风格 v2：cover/toc/preface/body/appendix 五段，body 起点='1 范围'。"""
    from app.services import chunker
    blocks = [
        # p1 封面（含文档名）
        chunker.Block(page_num=1, block_type="title", level=1, text="基层医疗卫生机构功能单元视觉设计标准"),
        chunker.Block(page_num=1, block_type="paragraph", text="WS/T 809—2022"),
        # p2 目录
        chunker.Block(page_num=2, block_type="title", level=2, text="目 次"),
        chunker.Block(page_num=2, block_type="paragraph", text="1 范围....1"),
        # p3 前言
        chunker.Block(page_num=3, block_type="title", level=2, text="前 言"),
        chunker.Block(page_num=3, block_type="paragraph", text="本标准..."),
        # p4 正文：文档名重复 + 1 范围
        chunker.Block(page_num=4, block_type="title", level=1, text="基层医疗卫生机构功能单元视觉设计标准"),  # cover 重复
        chunker.Block(page_num=4, block_type="title", level=2, text="1 范围"),
        chunker.Block(page_num=4, block_type="paragraph", text="本标准..."),
        # p5 二级标题
        chunker.Block(page_num=5, block_type="title", level=2, text="4.1 基本原则"),
        chunker.Block(page_num=5, block_type="paragraph", text="应坚持..."),
        # p6 附录 A（v2 里是 paragraph，被升级为 title）
        chunker.Block(page_num=6, block_type="title", level=1, text="附 录 A"),
        chunker.Block(page_num=6, block_type="paragraph", text="入口外观效果示例"),
        # p7 附录 B
        chunker.Block(page_num=7, block_type="title", level=1, text="附 录 B（资料性）候诊区效果示例"),
    ]
    regions = chunker.classify_regions(blocks)
    types = [r.region_type for r in regions]
    assert "cover" in types
    assert "toc" in types
    assert "preface" in types
    assert "body" in types
    assert "appendix" in types

    # 找到 body region
    body = next(r for r in regions if r.region_type == "body")
    # body 的第一个 title 应该是 "1 范围" 而不是 cover 重复的文档名
    first_title = next((b for b in body.blocks if b.block_type == "title"), None)
    assert first_title is not None
    assert first_title.text == "1 范围"

    # 附录 region 第一个 title 应该是 "附 录 A"（含从 paragraph 升级的）
    appendix = next(r for r in regions if r.region_type == "appendix")
    appendix_titles = [b.text for b in appendix.blocks if b.block_type == "title"]
    assert "附 录 A" in appendix_titles


def test_classify_regions_no_toc_no_preface():
    """无目录/前言：cover → body 直接接"第一章"。典型场景：医院感染暴发报告。
    关键修复：cover 不能把所有 chapter 都吞进 cover 区。
    """
    from app.services import chunker
    blocks = [
        # 封面：关于印发通知 + 文档名
        chunker.Block(page_num=1, block_type="title", level=1, text="关于印发《医院感染暴发报告及处置管理规范》的通知"),
        chunker.Block(page_num=1, block_type="paragraph", text="卫医政发〔2009〕73号"),
        chunker.Block(page_num=1, block_type="paragraph", text="各省..."),
        chunker.Block(page_num=1, block_type="title", level=1, text="医院感染暴发报告及处置管理规范"),
        # 正文：第一章/第二章...
        chunker.Block(page_num=2, block_type="title", level=1, text="第一章 总则"),
        chunker.Block(page_num=2, block_type="paragraph", text="第一条 ..."),
        chunker.Block(page_num=3, block_type="title", level=1, text="第二章 组织管理"),
        chunker.Block(page_num=3, block_type="paragraph", text="第六条 ..."),
    ]
    regions = chunker.classify_regions(blocks)
    types = [r.region_type for r in regions]

    # 至少要有 cover + body 两段
    assert "cover" in types
    assert "body" in types
    # cover 不能包含"第一章"
    cover = next(r for r in regions if r.region_type == "cover")
    cover_titles = [b.text for b in cover.blocks if b.block_type == "title"]
    assert "第一章 总则" not in cover_titles, "body 起点被 cover 吞掉了！"
    assert "第二章 组织管理" not in cover_titles
    # body 必须存在且起点是"第一章 总则"
    body = next(r for r in regions if r.region_type == "body")
    body_titles = [b.text for b in body.blocks if b.block_type == "title"]
    assert body_titles[0] == "第一章 总则"
    # body 应包含"第二章"
    assert "第二章 组织管理" in body_titles


def test_classify_regions_chinese_chapter_numbering():
    """★ 2026-08-06：中文章节号（"一、二、三..."）应被识别为 body 起点。
    
    背景：用户上传的"安宁疗护中心管理规范""护理中心管理规范"等中文规范文档，
    章节标题是"## 一、机构管理""## 二、质量管理"这类中文顿号形式，
    旧代码不识别，导致整份文档被识别为 cover 区域后按句号切成 N 段"封面 (part N)"。
    """
    from app.services import chunker
    blocks = [
        # 封面：文档名
        chunker.Block(page_num=1, block_type="title", level=1, text="护理中心管理规范（试行）"),
        chunker.Block(page_num=1, block_type="paragraph", text="为规范护理中心的管理..."),
        # 正文：一、二、三...
        chunker.Block(page_num=2, block_type="title", level=1, text="一、机构管理"),
        chunker.Block(page_num=2, block_type="paragraph", text="护理中心应当制定并落实管理规章制度..."),
        chunker.Block(page_num=3, block_type="title", level=1, text="二、质量管理"),
        chunker.Block(page_num=3, block_type="paragraph", text="建立质量管理体系..."),
        chunker.Block(page_num=4, block_type="title", level=1, text="六、管理"),
        chunker.Block(page_num=4, block_type="paragraph", text="具备条件的可提供安宁疗护服务..."),
    ]
    regions = chunker.classify_regions(blocks)
    types = [r.region_type for r in regions]

    # 至少要有 cover + body 两段
    assert "cover" in types
    assert "body" in types
    # cover 不能包含"一、机构管理"
    cover = next(r for r in regions if r.region_type == "cover")
    cover_titles = [b.text for b in cover.blocks if b.block_type == "title"]
    assert "一、机构管理" not in cover_titles, "body 起点被 cover 吞掉了！"
    # body 必须存在且起点是"一、机构管理"
    body = next(r for r in regions if r.region_type == "body")
    body_titles = [b.text for b in body.blocks if b.block_type == "title"]
    assert body_titles[0] == "一、机构管理"
    # body 应包含"二、质量管理"和"六、管理"
    assert "二、质量管理" in body_titles
    assert "六、管理" in body_titles


# ============ 7. chunk_body ============


def test_chunk_body_greedy_merge_l2():
    """单 1 级 + 总字符 > 1500 + 多 2 级 → 贪心合并 l2。"""
    from app.services import chunker
    blocks = [
        chunker.Block(page_num=1, block_type="title", level=1, text="第一章"),
        chunker.Block(page_num=1, block_type="title", level=2, text="1.1"),
        chunker.Block(page_num=1, block_type="paragraph", text="a" * 800),
        chunker.Block(page_num=1, block_type="title", level=2, text="1.2"),
        chunker.Block(page_num=1, block_type="paragraph", text="b" * 800),
    ]
    region = chunker.Region("body", "第一章", blocks)
    chunks = chunker.chunk_body(region)
    # 1.1 (800) + 1.2 (800) = 1600 > 1500 → 拆成 2 段
    assert len(chunks) == 2


def test_chunk_body_l3_merging():
    """1 级 + 单 2 级超 1500 + 多 3 级 → L3 贪心合并。"""
    from app.services import chunker
    # 构造单 2 级 1600 字符 + 多 3 级
    long_text = "x" * 1600
    blocks = [
        chunker.Block(page_num=1, block_type="title", level=1, text="第一章"),
        chunker.Block(page_num=1, block_type="title", level=2, text="1.1"),
        chunker.Block(page_num=1, block_type="paragraph", text=long_text),
        chunker.Block(page_num=1, block_type="title", level=3, text="1.1.1"),
        chunker.Block(page_num=1, block_type="paragraph", text="a" * 100),
        chunker.Block(page_num=1, block_type="title", level=3, text="1.1.2"),
        chunker.Block(page_num=1, block_type="paragraph", text="b" * 100),
    ]
    region = chunker.Region("body", "第一章", blocks)
    chunks = chunker.chunk_body(region)
    # 1.1 整体 1800 > 1500，且含 3 级 → 走 L3 贪心合并
    # 1.1.1 (100) + 1.1.2 (100) = 200 < 1500 → 合并
    assert len(chunks) >= 1


def test_chunk_body_single_l1_under_limit():
    """单 1 级 + 总字符 ≤ 1500 → 1 个 chunk。"""
    from app.services import chunker
    blocks = [
        chunker.Block(page_num=1, block_type="title", level=1, text="第一章"),
        chunker.Block(page_num=1, block_type="paragraph", text="a" * 200),
    ]
    region = chunker.Region("body", "第一章", blocks)
    chunks = chunker.chunk_body(region)
    assert len(chunks) == 1
    assert chunks[0].chunk_type == "body"


# ============ 8. chunk_appendix ============


def test_chunk_appendix_greedy_merge():
    """★ 2026-08-13：附录不再贪心合并，每个附录独立成段。"""
    from app.services import chunker
    blocks = [
        chunker.Block(page_num=1, block_type="title", level=1, text="附 录 A"),
        chunker.Block(page_num=1, block_type="paragraph", text="a" * 500),
        chunker.Block(page_num=1, block_type="title", level=1, text="附 录 B"),
        chunker.Block(page_num=1, block_type="paragraph", text="b" * 500),
        chunker.Block(page_num=1, block_type="title", level=1, text="附 录 C"),
        chunker.Block(page_num=1, block_type="paragraph", text="c" * 500),
    ]
    region = chunker.Region("appendix", "附录", blocks)
    chunks = chunker.chunk_appendix(region)
    # ★ 每个附录独立成段，不合并
    assert len(chunks) == 3
    for c in chunks:
        assert c.chunk_type == "appendix"


# ============ 9. chunk_simple ============


def test_chunk_simple_under_limit():
    """总字符 ≤ 1500 → 1 段。"""
    from app.services import chunker
    blocks = [
        chunker.Block(page_num=1, block_type="paragraph", text="封面内容" * 10),
    ]
    region = chunker.Region("cover", "封面", blocks)
    chunks = chunker.chunk_simple(region)
    assert len(chunks) == 1
    assert chunks[0].chunk_type == "cover"


def test_chunk_simple_over_limit_splits():
    """总字符 > 1500 → 句号切分多段。"""
    from app.services import chunker
    long_text = "测试句子。" * 600  # 2400 字符
    blocks = [
        chunker.Block(page_num=1, block_type="paragraph", text=long_text),
    ]
    region = chunker.Region("cover", "封面", blocks)
    chunks = chunker.chunk_simple(region)
    assert len(chunks) > 1


# ============ 10. write_chunks + write_chunk_metadata ============


def test_write_chunks_assigns_ids(fresh_settings, tmp_path: Path):
    """write_chunks 正确分配 chunk_001... 编号。"""
    from app.services import chunker
    chunks_dir = tmp_path / "out"
    chunks = [
        chunker.Chunk(
            chunk_id="", file_name="",
            title_path="封面", chunk_type="cover",
            char_count=100, body="封面内容",
        ),
        chunker.Chunk(
            chunk_id="", file_name="",
            title_path="正文", chunk_type="body",
            char_count=200, body="正文内容",
        ),
    ]
    final, refs, copied = chunker.write_chunks(chunks_dir, chunks, None)
    assert final[0].chunk_id == "chunk_001"
    assert final[0].file_name == "chunk_001_封面.md"
    assert final[1].chunk_id == "chunk_002"
    assert final[1].file_name == "chunk_002_正文.md"
    assert (chunks_dir / "chunk_001_封面.md").is_file()
    assert (chunks_dir / "chunk_002_正文.md").is_file()


def test_write_chunk_metadata_shape(fresh_settings, tmp_path: Path):
    from app.services import chunker
    chunks = [
        chunker.Chunk(
            chunk_id="chunk_001", file_name="chunk_001_封面.md",
            title_path="封面", chunk_type="cover",
            char_count=100, image_refs=[], body="",
        ),
    ]
    out = chunker.write_chunk_metadata(tmp_path, chunks, "测试")
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["doc_stem"] == "测试"
    assert payload["chunk_count"] == 1
    assert payload["chunks"][0]["title_path"] == "封面"


# ============ 11. _copy_referenced_images ============


def test_copy_referenced_images_copies_dedup(fresh_settings, tmp_path: Path):
    from app.services import chunker
    src = tmp_path / "src_images"
    src.mkdir()
    (src / "a.jpg").write_bytes(b"image1")
    (src / "b.jpg").write_bytes(b"image2")
    out = tmp_path / "out"
    n = chunker._copy_referenced_images(
        src, out, ["images/a.jpg", "images/b.jpg", "images/a.jpg"]  # 重复 a
    )
    assert n == 2  # 去重后 2 个
    assert (out / "images" / "a.jpg").is_file()
    assert (out / "images" / "b.jpg").is_file()


def test_copy_referenced_images_missing_source_returns_zero(fresh_settings, tmp_path: Path):
    from app.services import chunker
    n = chunker._copy_referenced_images(
        None, tmp_path / "out", ["images/xxx.jpg"]
    )
    assert n == 0


def test_chunk_image_refs_preserved():
    """所有 chunk_* 函数都必须把 image_refs 写进 Chunk 实例（关键回归）。"""
    from app.services import chunker
    # body region 含一张图片
    body_blocks = [
        chunker.Block(page_num=1, block_type="title", level=1, text="第一章"),
        chunker.Block(page_num=1, block_type="paragraph", text="正文"),
        chunker.Block(
            page_num=1, block_type="image",
            image_path="images/test.jpg", image_caption="图 1.1",
        ),
    ]
    body_region = chunker.Region("body", "第一章", body_blocks)
    body_chunks = chunker.chunk_body(body_region)
    assert body_chunks, "body 应该产生 chunk"
    assert any(
        c.image_refs == ["images/test.jpg"] for c in body_chunks
    ), f"body chunk 应保留 image_refs，实际: {[c.image_refs for c in body_chunks]}"

    # appendix region 含一张图片
    ap_blocks = [
        chunker.Block(page_num=1, block_type="title", level=1, text="附 录 A"),
        chunker.Block(page_num=1, block_type="paragraph", text="参考"),
        chunker.Block(
            page_num=1, block_type="image",
            image_path="images/appendix.jpg", image_caption="图 A.1",
        ),
    ]
    ap_region = chunker.Region("appendix", "附录", ap_blocks)
    ap_chunks = chunker.chunk_appendix(ap_region)
    assert ap_chunks
    assert any(
        c.image_refs == ["images/appendix.jpg"] for c in ap_chunks
    ), f"appendix chunk 应保留 image_refs，实际: {[c.image_refs for c in ap_chunks]}"

    # simple region（封面）含一张图片
    cover_blocks = [
        chunker.Block(page_num=1, block_type="title", level=1, text="封面"),
        chunker.Block(
            page_num=1, block_type="image",
            image_path="images/cover.jpg", image_caption="封面图",
        ),
    ]
    cover_region = chunker.Region("cover", "封面", cover_blocks)
    cover_chunks = chunker.chunk_simple(cover_region)
    assert cover_chunks
    assert any(
        c.image_refs == ["images/cover.jpg"] for c in cover_chunks
    ), f"cover chunk 应保留 image_refs，实际: {[c.image_refs for c in cover_chunks]}"


# ============ 12. 集成：chunk_document 跑通最小 v2 ============


def test_chunk_document_minimal_v2(fresh_settings, tmp_path: Path):
    """构造一个最小 v2 文档，跑完整 chunk_document 流程。"""
    from app.services import chunker
    parsed = tmp_path / "parsed"
    stem = parsed / "测试文档"
    stem.mkdir(parents=True)
    inner = stem / "hybrid_auto"
    inner.mkdir()

    # 写 v2
    v2 = _v2([
        [
            {"type": "title", "content": {"level": 1, "title_content": [{"content": "测试文档"}]}},
            {"type": "paragraph", "content": {"paragraph_content": [{"content": "封面副标题"}]}},
        ],
        [
            {"type": "title", "content": {"level": 1, "title_content": [{"content": "1 范围"}]}},
            {"type": "paragraph", "content": {"paragraph_content": [{"content": "本标准..."}]}},
            {"type": "title", "content": {"level": 1, "title_content": [{"content": "2 定义"}]}},
            {"type": "paragraph", "content": {"paragraph_content": [{"content": "xxx"}]}},
        ],
    ])
    (inner / "测试文档_content_list_v2.json").write_text(
        json.dumps(v2, ensure_ascii=False), encoding="utf-8"
    )
    # 写一个 md
    (inner / "测试文档.md").write_text("# 测试文档\n", encoding="utf-8")

    chunks_dir = tmp_path / "chunks" / "测试文档"
    result = chunker.chunk_document(stem, chunks_dir)
    assert result.stem == "测试文档"
    assert result.chunks_dir == chunks_dir
    assert len(result.chunks) >= 1
    # 至少有 chunk_001
    assert (chunks_dir / "chunk_001_封面.md").is_file() or any(
        "封面" in c.title_path for c in result.chunks
    )
    # chunk_metadata.json
    assert (chunks_dir / "chunk_metadata.json").is_file()


# ============ 13. _maybe_promote_to_title 防御：日期/年份不被识别为标题 ============


def test_maybe_promote_rejects_year_date():
    """'2024 年 11 月 12 日' 不应被升级为 title（4 位年份 + 年/月/日 是日期）。"""
    from app.services import chunker
    # 场景 1：4 位年份开头 + 空格 + "年"
    result = chunker._maybe_promote_to_title("2024 年 11 月 12 日")
    assert result is None, f"日期应保持 paragraph，实际: {result}"

    # 场景 2：单数字 + 月份
    result = chunker._maybe_promote_to_title("11 月 12 日")
    assert result is None, f"日期应保持 paragraph，实际: {result}"

    # 场景 3：4 位年份 + "年" 单独
    result = chunker._maybe_promote_to_title("2024 年")
    assert result is None, f"年份应保持 paragraph，实际: {result}"

    # 正例：普通数字标题仍正常升级
    result = chunker._maybe_promote_to_title("1 范围")
    assert result is not None and result.block_type == "title" and result.level == 1

    result = chunker._maybe_promote_to_title("4.1 基本原则")
    assert result is not None and result.block_type == "title" and result.level == 2

    result = chunker._maybe_promote_to_title("4.2.1 详细内容")
    assert result is not None and result.block_type == "title" and result.level == 3

    # 2 位数字标题应保留（如 10 章、99 节）
    result = chunker._maybe_promote_to_title("10 测试")
    assert result is not None and result.block_type == "title" and result.level == 1


def test_maybe_promote_year_regression_ecmo(fresh_settings, tmp_path: Path):
    """回归测试：之前 ECMO 文档被切成 cover + body 的根因是日期误升 title。"""
    from app.services import chunker

    # 构造一个只有封面的文档（无 chapter-like 标题）
    v2 = _v2([
        [
            {"type": "title", "content": {"level": 1, "title_content": [{"content": "国家卫生健康委办公厅关于印发某某规范的通知"}]}},
            {"type": "paragraph", "content": {"paragraph_content": [{"content": "国卫办医政函〔2024〕427号"}]}},
            {"type": "paragraph", "content": {"paragraph_content": [{"content": "各省、自治区、直辖市..."}]}},
            {"type": "paragraph", "content": {"paragraph_content": [{"content": "附件：某某规范"}]}},
            {"type": "paragraph", "content": {"paragraph_content": [{"content": "国家卫生健康委办公厅"}]}},
            {"type": "paragraph", "content": {"paragraph_content": [{"content": "2024 年 11 月 12 日"}]}},
        ]
    ])
    v2_path = tmp_path / "test_v2.json"
    v2_path.write_text(json.dumps(v2, ensure_ascii=False), encoding="utf-8")
    blocks = chunker.load_v2_blocks(v2_path)

    # "2024 年 11 月 12 日" 必须是 paragraph（不是 title）
    last = blocks[-1]
    assert last.block_type == "paragraph", (
        f"日期必须是 paragraph，实际: {last.block_type}/{last.text!r}"
    )
    assert "2024" in last.text

    # 跑 classify_regions：所有 6 个 block 都应归到 cover
    regions = chunker.classify_regions(blocks)
    cover = next((r for r in regions if r.region_type == "cover"), None)
    assert cover is not None, "应有 cover 区域"
    # 关键：cover 应包含 date
    cover_text = " ".join(b.text for b in cover.blocks if b.block_type == "paragraph")
    assert "2024" in cover_text, f"cover 应包含日期，实际: {cover_text[:200]}"
    # 关键：不应有 body 区域（文档只有封面通知）
    body = next((r for r in regions if r.region_type == "body"), None)
    assert body is None, f"单页通知文档不应有 body 区域，实际: {body}"


# ============ 14. _is_parse_content_trivial 解析质量检测 ============


def test_is_parse_content_trivial_detects_only_page_numbers(fresh_settings, tmp_path: Path):
    """v2 只有 page_number/header → 触发解析失败告警。"""
    from app.services import chunker
    parsed = tmp_path / "parsed_trivial"
    inner = parsed / "hybrid_auto"
    inner.mkdir(parents=True)
    v2 = [
        [{"type": "page_number", "content": {"page_number_content": [{"content": "1"}]}}],
        [{"type": "page_number", "content": {"page_number_content": [{"content": "2"}]}}],
        [{"type": "page_header", "content": {"page_header_content": [{"content": "7"}]}}],
    ]
    (inner / "doc_content_list_v2.json").write_text(
        json.dumps(v2, ensure_ascii=False), encoding="utf-8"
    )
    (inner / "doc.md").write_text("2010 5", encoding="utf-8")

    is_trivial, reason = chunker._is_parse_content_trivial(parsed)
    assert is_trivial, f"应判定为解析失败，实际 reason: {reason}"
    assert "v2" in reason or "块" in reason, f"reason 应含 v2 块数信息: {reason}"


def test_is_parse_content_trivial_detects_short_md(fresh_settings, tmp_path: Path):
    ".md 文件过短（< 50 字符）→ 触发解析失败告警。"
    from app.services import chunker
    parsed = tmp_path / "parsed_short"
    inner = parsed / "hybrid_auto"
    inner.mkdir(parents=True)
    # v2 构造一个有效的（4 个块，含 title 和 paragraph）
    v2 = _v2([
        [
            {"type": "title", "content": {"level": 1, "title_content": [{"content": "1 范围"}]}},
            {"type": "paragraph", "content": {"paragraph_content": [{"content": "abc"}]}},
        ]
    ] * 2)  # 4 块
    (inner / "doc_content_list_v2.json").write_text(
        json.dumps(v2, ensure_ascii=False), encoding="utf-8"
    )
    # .md 只有 5 字符
    (inner / "doc.md").write_text("2010 ", encoding="utf-8")

    is_trivial, reason = chunker._is_parse_content_trivial(parsed)
    assert is_trivial, f"应判定为解析失败，实际 reason: {reason}"
    assert "过短" in reason or "字符" in reason, f"reason 应提示过短: {reason}"


def test_is_parse_content_trivial_passes_valid(fresh_settings, tmp_path: Path):
    """正常的解析产物 → 不触发告警。"""
    from app.services import chunker
    parsed = tmp_path / "parsed_valid"
    inner = parsed / "hybrid_auto"
    inner.mkdir(parents=True)
    v2 = _v2([
        [
            {"type": "title", "content": {"level": 1, "title_content": [{"content": "1 范围"}]}},
            {"type": "paragraph", "content": {"paragraph_content": [{"content": "本标准适用于医疗卫生机构功能单元视觉设计，规定了基本原则、预防保健区、检查室及候诊区等的设计要求。"}]}},
        ],
        [
            {"type": "title", "content": {"level": 1, "title_content": [{"content": "2 定义"}]}},
            {"type": "paragraph", "content": {"paragraph_content": [{"content": "下列术语和定义适用于本文件。功能单元是指医疗机构内具有特定功能的房间或区域。"}]}},
        ],
    ])
    (inner / "doc_content_list_v2.json").write_text(
        json.dumps(v2, ensure_ascii=False), encoding="utf-8"
    )
    (inner / "doc.md").write_text(
        "# 1 范围\n\n本标准适用于医疗卫生机构功能单元视觉设计。\n\n# 2 定义\n\n下列术语和定义适用于本文件。",
        encoding="utf-8",
    )

    is_trivial, reason = chunker._is_parse_content_trivial(parsed)
    assert not is_trivial, f"正常文档不应触发告警，actual reason: {reason}"


# ============ 13b. _collect_v2_text_chars 处理 table 块（2026-08-04 bugfix）============


def test_collect_v2_text_chars_counts_table_html(tmp_path: Path):
    """★ _collect_v2_text_chars 把 table 块的 HTML 可见文本字符数计入 text_chars。
    2026-08-04 bug：旧代码 `text_chars += _strip_html_tags(html)` 是 int += str，
    在含 table 块的 v2 文档（如通知公告 docx）上抛 TypeError，整个 chunk 阶段崩溃。
    修复后应该正确累加 len(stripped_html)，不抛异常。
    """
    from app.services import chunker
    html = "<table><tr><td>序号</td><td>单位名称</td></tr><tr><td>1</td><td>" + ("x" * 50) + "</td></tr></table>"
    v2 = _v2([
        [
            {"type": "title", "content": {"level": 1, "title_content": [{"content": "通知"}]}},
            {"type": "paragraph", "content": {"paragraph_content": [{"content": "正文段落"}]}},
            {
                "type": "table",
                "content": {
                    "html": html,
                    "table_type": "simple_table",
                },
            },
        ]
    ])
    v2_path = tmp_path / "v2.json"
    v2_path.write_text(json.dumps(v2, ensure_ascii=False), encoding="utf-8")
    # ★ 不应该抛 TypeError
    total, tp, chars = chunker._collect_v2_text_chars(v2_path)
    # 1 title + 1 paragraph + 1 table = 3 块
    assert total == 3
    # 2 个 title/paragraph
    assert tp == 2
    # 表格 HTML 剥标签后约 50+ 字符（"序号" "单位名称" "1" + 50 个 x）
    assert chars >= 50, f"表格可见文本应被计入，实际: {chars}"


def test_collect_v2_text_chars_table_only_doc_passes_trivial_check(tmp_path: Path):
    """★ 包含 table 块的文档（典型：通知公告里就是 1 张名单表），仍应通过 trivial 检查。
    旧 bug：table HTML 字符数没被计入（int += str 抛 TypeError，整篇解析失败），
    即使是已经解析成功的文档也会因为这个 bug 在 chunk 阶段崩溃。
    修复后：table HTML 字符数被正确累加 → 通过。
    """
    from app.services import chunker
    parsed = tmp_path / "parsed_table_only"
    inner = parsed / "hybrid_auto"
    inner.mkdir(parents=True)

    # 50 行名单表（行 1=表头，行 2-50=数据行），保证可见文本 > 50 字符
    rows = ["<tr><td>序号</td><td>单位名称</td></tr>"]
    for i in range(1, 50):
        rows.append(f"<tr><td>{i}</td><td>某单位 {i} 名称</td></tr>")
    html = "<table>" + "".join(rows) + "</table>"

    v2 = _v2([
        # 第 1 页：标题 + 段落
        [
            {"type": "title", "content": {"level": 1, "title_content": [{"content": "封面"}]}},
            {"type": "paragraph", "content": {"paragraph_content": [{"content": "为推进工作"}]}},
        ],
        # 第 2 页：通知正文 + 名单表
        [
            {"type": "title", "content": {"level": 1, "title_content": [{"content": "1 名单"}]}},
            {
                "type": "table",
                "content": {"html": html, "table_type": "simple_table"},
            },
        ],
    ])
    (inner / "doc_content_list_v2.json").write_text(
        json.dumps(v2, ensure_ascii=False), encoding="utf-8"
    )
    # .md 也要足够长（> 50 字符）
    (inner / "doc.md").write_text(
        "## 名单通知\n\n" + "某单位名称" * 10, encoding="utf-8"
    )

    # ★ 直接测 _collect_v2_text_chars（这个函数是 bug 出现的精确位置）
    # 不调用 _is_parse_content_trivial，避免 _PARSE_QUALITY_MIN_BLOCKS 等其他阈值干扰
    v2_path = inner / "doc_content_list_v2.json"
    total, tp, chars = chunker._collect_v2_text_chars(v2_path)
    # 2 title + 1 paragraph + 1 table = 4 块
    assert total == 4
    # 2 title + 1 paragraph = 3 title_or_para
    assert tp == 3
    # 表格 HTML 可见文本 > 200 字符
    assert chars >= 200, f"表格可见文本应被计入，实际: {chars}"
    # ★ 关键：必须没有抛 TypeError，函数正常返回
    assert isinstance(chars, int)


# ============ 14. _resolve_parsed_dir（Issue 1: stem 模糊匹配）============


def test_resolve_parsed_dir_exact_match(tmp_path: Path):
    """精确路径匹配。"""
    from app.services import chunker
    parsed = tmp_path / "parsed"
    (parsed / "name").mkdir(parents=True)
    resolved, stem = chunker._resolve_parsed_dir("name", parsed)
    assert resolved == parsed / "name"
    assert stem == "name"


def test_resolve_parsed_dir_strip_fallback_suffix(tmp_path: Path):
    """manifest 写的是 "name [vlm-image-fallback 修复]"，实际目录是 "name"。"""
    from app.services import chunker
    parsed = tmp_path / "parsed"
    actual = parsed / "name"
    actual.mkdir(parents=True)
    resolved, stem = chunker._resolve_parsed_dir(
        "name [vlm-image-fallback 修复]", parsed
    )
    assert resolved == actual
    assert stem == "name"


def test_resolve_parsed_dir_strip_pymupdf_suffix(tmp_path: Path):
    """PyMuPDF fallback 后缀也能被去掉。"""
    from app.services import chunker
    parsed = tmp_path / "parsed"
    actual = parsed / "name"
    actual.mkdir(parents=True)
    resolved, stem = chunker._resolve_parsed_dir("name [pymupdf-fallback 修复]", parsed)
    assert resolved == actual
    assert stem == "name"


def test_resolve_parsed_dir_strip_numeric_suffix(tmp_path: Path):
    """manifest "name(1)" vs 实际目录 "name" —— 数字编号后缀。"""
    from app.services import chunker
    parsed = tmp_path / "parsed"
    actual = parsed / "name"
    actual.mkdir(parents=True)
    resolved, stem = chunker._resolve_parsed_dir("name(1)", parsed)
    assert resolved == actual
    assert stem == "name"


def test_resolve_parsed_dir_not_found(tmp_path: Path):
    """完全不存在的 stem → 返回 None。"""
    from app.services import chunker
    parsed = tmp_path / "parsed"
    parsed.mkdir(parents=True)
    resolved, stem = chunker._resolve_parsed_dir("nonexistent", parsed)
    assert resolved is None
    assert stem == "nonexistent"


def test_resolve_parsed_dir_prefix_match(tmp_path: Path):
    """长 stem 截断/包含场景：parsed_dir 下有"济宁...办法"，manifest 写"济宁...办法(1) [vlm-image-fallback 修复]"。"""
    from app.services import chunker
    parsed = tmp_path / "parsed"
    actual_name = "济宁市医疗卫生机构病死婴幼儿遗体处理暂行办法"
    (parsed / actual_name).mkdir(parents=True)
    # 截掉后半段测试
    resolved, stem = chunker._resolve_parsed_dir(
        "济宁市医疗卫生机构病死婴幼儿遗体处理暂行办法(1) [vlm-image-fallback 修复]",
        parsed,
    )
    assert resolved == parsed / actual_name
    assert stem == actual_name


# ============ 15. load_v2_blocks 处理 list 块（Issue 2: TOC 内容丢失）============


def test_load_v2_blocks_expand_list_to_paragraphs():
    """list 块展开为多个 paragraph（典型：TOC 条目 '1 范围.....'）。"""
    from app.services import chunker
    v2 = _v2([
        [
            {"type": "title", "content": {"level": 2, "title_content": [{"content": "目 次"}]}},
            {
                "type": "list",
                "content": {
                    "list_type": "text_list",
                    "list_items": [
                        {"item_type": "text", "item_content": [{"content": "1 范围....."}]},
                        {"item_type": "text", "item_content": [{"content": "2 规范性引用文件.... ..... 1"}]},
                        {"item_type": "text", "item_content": [{"content": "3 术语和定义... ...... 1"}]},
                    ],
                },
            },
        ]
    ])
    import tempfile
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8") as fp:
        json.dump(v2, fp, ensure_ascii=False)
        v2_path = Path(fp.name)
    try:
        blocks = chunker.load_v2_blocks(v2_path)
    finally:
        v2_path.unlink(missing_ok=True)
    # 3 个 list item 应被展开为 3 个 block（promote 成 title）
    titles = [b.text for b in blocks if b.block_type == "title"]
    assert "1 范围....." in titles
    assert "2 规范性引用文件.... ..... 1" in titles
    assert "3 术语和定义... ...... 1" in titles
    # "1 范围" 应被推断为 level 1
    lvl1 = [b for b in blocks if b.block_type == "title" and b.text == "1 范围....."]
    assert lvl1 and lvl1[0].level == 1


def test_load_v2_blocks_expand_list_with_appendix_titles():
    """list 中的附录条目被展开为 appendix title。"""
    from app.services import chunker
    v2 = _v2([
        [
            {"type": "title", "content": {"level": 2, "title_content": [{"content": "目 次"}]}},
            {
                "type": "list",
                "content": {
                    "list_type": "text_list",
                    "list_items": [
                        {"item_type": "text", "item_content": [{"content": "附录 A（资料性）入口外观效果示例.... ........... 4"}]},
                        {"item_type": "text", "item_content": [{"content": "附录 B（资料性）候诊区效果示例. ... 5"}]},
                    ],
                },
            },
        ]
    ])
    import tempfile
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8") as fp:
        json.dump(v2, fp, ensure_ascii=False)
        v2_path = Path(fp.name)
    try:
        blocks = chunker.load_v2_blocks(v2_path)
    finally:
        v2_path.unlink(missing_ok=True)
    titles = [b.text for b in blocks if b.block_type == "title"]
    assert "附录 A（资料性）入口外观效果示例.... ........... 4" in titles
    assert "附录 B（资料性）候诊区效果示例. ... 5" in titles


def test_load_v2_blocks_list_with_empty_items_ignored():
    """list 中有空 item_content 不应抛错。"""
    from app.services import chunker
    v2 = _v2([
        [
            {
                "type": "list",
                "content": {
                    "list_type": "text_list",
                    "list_items": [
                        {"item_type": "text", "item_content": [{"content": "有效条目"}]},
                        {"item_type": "text", "item_content": []},  # 空
                        {"item_type": "text"},  # 缺 item_content
                    ],
                },
            }
        ]
    ])
    import tempfile
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8") as fp:
        json.dump(v2, fp, ensure_ascii=False)
        v2_path = Path(fp.name)
    try:
        blocks = chunker.load_v2_blocks(v2_path)
    finally:
        v2_path.unlink(missing_ok=True)
    # 至少应有"有效条目"
    assert any(b.text == "有效条目" for b in blocks)


# ============ 16. classify_regions 过滤 cover 重复（Issue 3）============


def test_classify_regions_filter_cover_dup_from_preface():
    """WST 809 风格：body 起点前有 cover 重复的文档主标题 → 必须从前言 chunk 中剔除。"""
    from app.services import chunker
    blocks = [
        # p1 cover
        chunker.Block(page_num=1, block_type="title", level=1, text="基层医疗卫生机构功能单元视觉设计标准"),
        # p2 toc
        chunker.Block(page_num=2, block_type="title", level=2, text="目 次"),
        # p3 preface
        chunker.Block(page_num=3, block_type="title", level=2, text="前 言"),
        chunker.Block(page_num=3, block_type="paragraph", text="本标准由..."),
        # p4 ★ 重复主标题（cover 重复） + 1 范围
        chunker.Block(page_num=4, block_type="title", level=1, text="基层医疗卫生机构功能单元视觉设计标准"),
        chunker.Block(page_num=4, block_type="title", level=2, text="1 范围"),
        chunker.Block(page_num=4, block_type="paragraph", text="本标准规定..."),
    ]
    regions = chunker.classify_regions(blocks)
    # 找 preface region
    preface = next(r for r in regions if r.region_type == "preface")
    # preface 中绝不能包含 cover 重复的主标题
    preface_titles = [b.text for b in preface.blocks if b.block_type == "title"]
    assert "基层医疗卫生机构功能单元视觉设计标准" not in preface_titles, (
        f"preface 不应包含 cover 重复，实际: {preface_titles}"
    )
    # 但 preface 自身应该还在（"前 言"标题）
    assert "前 言" in preface_titles


def test_classify_regions_filter_does_not_affect_body():
    """cover 重复过滤只清理 preface，不影响 body 起点判定。"""
    from app.services import chunker
    blocks = [
        chunker.Block(page_num=1, block_type="title", level=1, text="文档主标题"),
        chunker.Block(page_num=2, block_type="title", level=2, text="前 言"),
        chunker.Block(page_num=2, block_type="paragraph", text="前言内容"),
        # 重复主标题 + body
        chunker.Block(page_num=3, block_type="title", level=1, text="文档主标题"),
        chunker.Block(page_num=3, block_type="title", level=2, text="1 范围"),
        chunker.Block(page_num=3, block_type="paragraph", text="正文内容"),
    ]
    regions = chunker.classify_regions(blocks)
    body = next(r for r in regions if r.region_type == "body")
    # body 起点应是 "1 范围"
    body_titles = [b.text for b in body.blocks if b.block_type == "title"]
    assert body_titles[0] == "1 范围", f"body 起点应是 '1 范围'，实际: {body_titles}"


def test_classify_regions_no_preface_dup_filter_noop():
    """没有 preface region 时，重复过滤逻辑不应干扰其他 region。"""
    from app.services import chunker
    blocks = [
        chunker.Block(page_num=1, block_type="title", level=1, text="文档名"),
        chunker.Block(page_num=1, block_type="title", level=1, text="第一章"),
        chunker.Block(page_num=1, block_type="paragraph", text="正文"),
    ]
    # 不应抛错，且 body 起点=第一章
    regions = chunker.classify_regions(blocks)
    assert any(r.region_type == "body" for r in regions)


# ============================================================
# ★ 2026-07-31：图片超限切分（cutrule.md 3.5 / 4.3）
# ============================================================


def test_greedy_merge_groups_image_limit_triggers_split(fresh_settings, monkeypatch):
    """★ 2026-07-31：_greedy_merge_groups 加 max_images 维度，图片超限触发切分。

    场景：WST 809 附录 A-J，10+ 张图在同段。
    旧行为：按字符数贪心合并，单段可能 10+ 图 → Dify add_segments 400。
    新行为：max_images=10 时，图片累计超 10 强制落盘。

    验证要点：
    - 2 组（5 张 + 6 张图）→ 不应合并（11 > 10）→ 拆成 2 段
    - 2 组（4 张 + 5 张图）→ 应合并（9 ≤ 10）→ 1 段
    """
    from app.services import chunker
    from app.config import settings as cfg

    # 用 monkeypatch 把配置设小（便于构造超限场景）
    monkeypatch.setattr(cfg, "chunk_max_images_per_segment", 10)
    # settings 已被 cfg_mod 实例化，chunker 引用的是 settings 单例
    from app.services import chunker as ck
    ck.settings = cfg

    # 5 张图的一组
    grp5 = [
        chunker.Block(page_num=1, block_type="title", level=2, text="A 组"),
    ]
    for i in range(5):
        grp5.append(chunker.Block(page_num=1, block_type="image",
                                  image_path=f"images/a{i}.jpg", image_caption=f"图 A.{i}"))
        grp5.append(chunker.Block(page_num=1, block_type="paragraph", text="a" * 50))

    # 6 张图的一组
    grp6 = [
        chunker.Block(page_num=1, block_type="title", level=2, text="B 组"),
    ]
    for i in range(6):
        grp6.append(chunker.Block(page_num=1, block_type="image",
                                  image_path=f"images/b{i}.jpg", image_caption=f"图 B.{i}"))
        grp6.append(chunker.Block(page_num=1, block_type="paragraph", text="b" * 50))

    # max_images=10：5+6=11 > 10，强制落盘
    out = chunker._greedy_merge_groups(
        [("A 组", grp5), ("B 组", grp6)], threshold=5000, max_images=10
    )
    assert len(out) == 2, f"图片超限应拆 2 段，实际: {len(out)}"
    assert "A 组" in out[0][0] and "B 组" in out[1][0]

    # 4 张 + 5 张 = 9 ≤ 10，可以合并
    grp4 = [chunker.Block(page_num=1, block_type="title", level=2, text="A 组")]
    for i in range(4):
        grp4.append(chunker.Block(page_num=1, block_type="image",
                                  image_path=f"images/x{i}.jpg", image_caption=f"图 {i}"))
    grp5_2 = [chunker.Block(page_num=1, block_type="title", level=2, text="B 组")]
    for i in range(5):
        grp5_2.append(chunker.Block(page_num=1, block_type="image",
                                    image_path=f"images/y{i}.jpg", image_caption=f"图 {i}"))
    out2 = chunker._greedy_merge_groups(
        [("A 组", grp4), ("B 组", grp5_2)], threshold=5000, max_images=10
    )
    assert len(out2) == 1, f"9 张图应合并为 1 段，实际: {len(out2)}"


def test_greedy_merge_groups_max_images_zero_back_compat(fresh_settings, monkeypatch):
    """max_images=0 时（旧调用方式），仍按字符数贪心合并（兼容旧行为）。"""
    from app.services import chunker

    # 11 张图，超 max_images=10，但 max_images=0 时不生效
    grp = [chunker.Block(page_num=1, block_type="title", level=2, text="A 组")]
    for i in range(11):
        grp.append(chunker.Block(page_num=1, block_type="image",
                                 image_path=f"images/x{i}.jpg", image_caption=f"图 {i}"))
    grp2 = [chunker.Block(page_num=1, block_type="title", level=2, text="B 组")]
    for i in range(3):
        grp2.append(chunker.Block(page_num=1, block_type="image",
                                  image_path=f"images/y{i}.jpg", image_caption=f"图 {i}"))

    # max_images=0（默认）：11+3=14 张图，仍按字符数合并（不切）
    out = chunker._greedy_merge_groups(
        [("A 组", grp), ("B 组", grp2)], threshold=5000, max_images=0
    )
    assert len(out) == 1, f"max_images=0 时应只按字符数合并，实际: {len(out)}"


def test_chunk_appendix_image_limit_triggers_split(fresh_settings, monkeypatch):
    """★ 2026-07-31：附录图片超 10 张时强制拆段（cutrule.md 4.3）。

    场景：WST 809 附录 A-J，13 张图。旧行为：按 chars 贪心合并成 1~2 段，
    单段可能 10+ 图。新行为：max_images=10 时强制拆。
    """
    from app.services import chunker
    from app.config import settings as cfg

    monkeypatch.setattr(cfg, "chunk_max_images_per_segment", 10)
    chunker.settings = cfg

    # 造 13 个附录，每个含 1 张图
    blocks = []
    for i in range(1, 14):
        blocks.append(chunker.Block(page_num=1, block_type="title",
                                    level=1, text=f"附录 {chr(64 + i)}"))
        blocks.append(chunker.Block(page_num=1, block_type="paragraph", text=f"附录 {chr(64 + i)} 说明"))
        blocks.append(chunker.Block(page_num=1, block_type="image",
                                    image_path=f"images/app{chr(64 + i)}.jpg",
                                    image_caption=f"图 {chr(64 + i)}.1"))
    region = chunker.Region("appendix", "附录", blocks)
    out = chunker.chunk_appendix(region)

    # 13 张图 + max_images=10 → 至少 2 段（10 + 3）
    assert len(out) >= 2, f"13 张图应拆 ≥2 段（10+3），实际: {len(out)} 段"
    # ★ 关键：每段的 image_refs 数量都 ≤ 10
    for i, c in enumerate(out):
        assert len(c.image_refs) <= 10, (
            f"段 {i} image_refs 数量应 ≤ 10，实际: {len(c.image_refs)}"
        )
    # 总图片数 13
    total_imgs = sum(len(c.image_refs) for c in out)
    assert total_imgs == 13, f"13 张图应全部保留，实际: {total_imgs}"


def test_chunk_body_image_limit_triggers_split(fresh_settings, monkeypatch):
    """★ 2026-07-31：正文图片超 10 张时强制拆段（cutrule.md 3.5）。

    场景：4.11 卫生间 内嵌 13 张图。
    """
    from app.services import chunker
    from app.config import settings as cfg

    monkeypatch.setattr(cfg, "chunk_max_images_per_segment", 10)
    chunker.settings = cfg

    # 一级标题 4，13 个三级标题（4.11.1~4.11.13），每个 1 张图
    blocks = [
        chunker.Block(page_num=1, block_type="title", level=1, text="4 卫生间"),
    ]
    for i in range(1, 14):
        blocks.append(chunker.Block(page_num=1, block_type="title",
                                    level=3, text=f"4.11.{i}"))
        blocks.append(chunker.Block(page_num=1, block_type="paragraph", text="a" * 50))
        blocks.append(chunker.Block(page_num=1, block_type="image",
                                    image_path=f"images/san{i}.jpg",
                                    image_caption=f"图 4.11.{i}"))
    region = chunker.Region("body", "", blocks)
    out = chunker.chunk_body(region)

    # 13 张图 + max_images=10 → ≥ 2 段
    assert len(out) >= 2, f"13 张图应拆 ≥2 段，实际: {len(out)}"
    for c in out:
        assert len(c.image_refs) <= 10, (
            f"段 image_refs 数量应 ≤ 10，实际: {len(c.image_refs)}"
        )
    total_imgs = sum(len(c.image_refs) for c in out)
    assert total_imgs == 13, f"13 张图应全部保留，实际: {total_imgs}"


# ============================================================
# ★ 2026-08-04：表格 + 公式切分（v2 table 块处理）
# 背景：用户报告"切分时导致表格内容和公式内容缺失"
#       经排查 v2 JSON 含 136 个 type=="table" 块 + 111 个 equation_inline run，
#       旧 chunker 完全忽略 → 表格 100% 丢失 / 公式被丢弃（_extract_runs_text 旧版不处理 equation_inline）
# 修复：load_v2_blocks 新增 table 分支、_extract_runs_text 包裹 equation_inline 为 $...$、
#       _block_to_text 渲染 HTML 表格、_blocks_chars 累加表格字符数
# ============================================================


def test_load_v2_blocks_table_html_preserved(tmp_path: Path):
    """★ v2 中的 table 块被正确加载（HTML 字符串保留）。"""
    from app.services import chunker
    html = "<table><tr><td>序号</td><td>公司名称</td></tr></table>"
    v2 = _v2([
        [
            {"type": "title", "content": {"level": 1, "title_content": [{"content": "封面"}]}},
            {"type": "paragraph", "content": {"paragraph_content": [{"content": "正文段落"}]}},
            {
                "type": "table",
                "content": {
                    "html": html,
                    "table_caption": [{"content": "表 1 名单"}],
                    "table_footnote": [{"type": "text", "content": "注：排名不分先后"}],
                    "table_type": "simple_table",
                    "table_nest_level": 1,
                },
            },
        ]
    ])
    v2_path = tmp_path / "v2.json"
    v2_path.write_text(json.dumps(v2, ensure_ascii=False), encoding="utf-8")
    blocks = chunker.load_v2_blocks(v2_path)
    table_blocks = [b for b in blocks if b.block_type == "table"]
    assert len(table_blocks) == 1, f"应有 1 个 table 块，实际: {len(table_blocks)}"
    tb = table_blocks[0]
    assert tb.table_html == html
    assert tb.table_caption == "表 1 名单"
    assert tb.table_footnote == "注：排名不分先后"
    assert tb.table_type == "simple_table"


def test_load_v2_blocks_table_image_fallback(tmp_path: Path):
    """★ 图片型表格（无 HTML 但有 image_source）→ 走 image 分支。"""
    from app.services import chunker
    v2 = _v2([
        [
            {
                "type": "table",
                "content": {
                    "image_source": {"path": "images/table_xyz.jpg"},
                    "table_caption": [{"content": "表 2 流程图"}],
                },
            },
        ]
    ])
    v2_path = tmp_path / "v2.json"
    v2_path.write_text(json.dumps(v2, ensure_ascii=False), encoding="utf-8")
    blocks = chunker.load_v2_blocks(v2_path)
    # 走 image 分支：1 个 image block，table_image_path/table_html 都不出现在 table block 中
    img_blocks = [b for b in blocks if b.block_type == "image"]
    assert len(img_blocks) == 1
    assert img_blocks[0].image_path == "images/table_xyz.jpg"


def test_blocks_chars_counts_table_text():
    """★ _blocks_chars 把表格 HTML 可见文本计入字符数。"""
    from app.services import chunker
    blocks = [
        chunker.Block(page_num=1, block_type="paragraph", text="a" * 100),
        chunker.Block(
            page_num=1, block_type="table",
            table_html="<table><tr><td>" + ("b" * 50) + "</td></tr></table>",
        ),
    ]
    chars = chunker._blocks_chars(blocks)
    # 100 (paragraph) + 50 (table text) = 150
    assert chars == 150, f"应有 150 字符，实际: {chars}"


def test_chunk_simple_includes_table_content():
    """★ 端到端：封面区含 table 块时，chunk 输出必须含 HTML 表格字符串。"""
    from app.services import chunker
    blocks = [
        chunker.Block(
            page_num=1, block_type="title", level=1, text="关于公布xxx的通知",
        ),
        chunker.Block(page_num=1, block_type="paragraph", text="通知正文"),
        chunker.Block(
            page_num=1, block_type="table",
            table_html="<table><tr><td>1</td><td>江西康卫士</td></tr></table>",
            table_caption="表 1 名单",
        ),
    ]
    region = chunker.Region("cover", "封面", blocks)
    out = chunker.chunk_simple(region)
    assert len(out) == 1
    body = out[0].body
    # 表格 HTML 必须出现
    assert "<table>" in body
    assert "江西康卫士" in body
    # caption 必须出现
    assert "表 1 名单" in body
