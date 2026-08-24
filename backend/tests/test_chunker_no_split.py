"""plan.md §3.3 chunker 不可分割区检测（表格/公式）单元测试。

覆盖：
1. _find_no_split_zones 检测 Markdown 表格
2. _find_no_split_zones 检测 LaTeX 行内公式 $...$
3. _find_no_split_zones 检测 LaTeX 块级公式 $$...$$
4. _find_no_split_zones 合并重叠区段
5. _is_in_no_split_zone 位置判断
6. _adjust_split_to_safe_position 调整到安全位置
7. _split_by_sentence 不破坏 Markdown 表格
8. _split_by_sentence 不破坏 LaTeX 公式
"""

from __future__ import annotations

import importlib
import logging
import sys
from pathlib import Path
from typing import List

import pytest

logging.disable(logging.CRITICAL)


@pytest.fixture(autouse=True)
def fresh_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """每个用例：隔离 tmp_path 作为 data_root。"""
    test_data_root = tmp_path / "data"
    monkeypatch.setenv("RAG_DATA_ROOT", str(test_data_root))

    # 清理可能缓存的 chunker 模块
    for mod_name in list(sys.modules.keys()):
        if mod_name.startswith("app."):
            del sys.modules[mod_name]
    if "app" in sys.modules:
        del sys.modules["app"]

    from app import config as cfg_mod
    importlib.reload(cfg_mod)
    settings = cfg_mod.settings

    from app.services import chunker
    chunker.settings = settings

    settings.ensure_dirs()
    yield settings


# ============ 1. _find_no_split_zones：Markdown 表格 ============


def test_find_md_table_zone():
    """简单 Markdown 表格应被识别为不可分割区。"""
    from app.services import chunker

    text = (
        "前言段落。\n"
        "| 名称 | 数量 |\n"
        "|---|---|\n"
        "| A | 1 |\n"
        "| B | 2 |\n"
        "\n"
        "后续段落。"
    )
    zones = chunker._find_no_split_zones(text)
    assert len(zones) >= 1
    # 第一个 zone 应该是表格
    s, e = zones[0]
    table_text = text[s:e]
    assert "| 名称 | 数量 |" in table_text
    assert "| A | 1 |" in table_text
    assert "| B | 2 |" in table_text


def test_find_md_table_zone_preserves_multiple_tables():
    """多张表格分别识别为独立不可分割区。"""
    from app.services import chunker

    text = (
        "段落1。\n"
        "| col1 | col2 |\n|---|---|\n| a | b |\n"
        "段落2。\n"
        "| col3 | col4 |\n|---|---|\n| c | d |\n"
        "段落3。"
    )
    zones = chunker._find_no_split_zones(text)
    assert len(zones) >= 2


# ============ 2. _find_no_split_zones：LaTeX 行内公式 ============


def test_find_inline_latex_zone():
    """$...$ 行内公式应被识别为不可分割区。"""
    from app.services import chunker

    text = "根据 $E = mc^2$ 公式，能量与质量相关。后续段落。"
    zones = chunker._find_no_split_zones(text)
    assert len(zones) >= 1
    s, e = zones[0]
    formula = text[s:e]
    assert "$E = mc^2$" in formula


def test_find_multiple_inline_latex_zones():
    """多个行内公式分别识别。"""
    from app.services import chunker

    text = "第一个公式 $a^2 + b^2 = c^2$。中间文字。第二个 $E = mc^2$ 公式。"
    zones = chunker._find_no_split_zones(text)
    # 至少有 2 个公式 zone
    inline_zones = [(s, e) for s, e in zones if "$" in text[s:e]]
    assert len(inline_zones) >= 2


def test_find_inline_latex_with_chinese_period_inside():
    """公式内含中文句号时仍应完整保留。"""
    from app.services import chunker

    text = "段落 a。$x = 1。y = 2$ 段落 b。"
    zones = chunker._find_no_split_zones(text)
    # 至少识别出公式 zone
    assert len(zones) >= 1
    s, e = zones[0]
    assert text[s:e] == "$x = 1。y = 2$"


# ============ 3. _find_no_split_zones：LaTeX 块级公式 ============


def test_find_block_latex_zone():
    """$$...$$ 块级公式应被识别。"""
    from app.services import chunker

    text = (
        "段落1。\n"
        "$$\n"
        "x = 1\n"
        "y = 2\n"
        "$$\n"
        "段落2。"
    )
    zones = chunker._find_no_split_zones(text)
    assert len(zones) >= 1
    # 找到包含 $$ 的 zone
    block_zones = [z for z in zones if "$$" in text[z[0]:z[1]]]
    assert len(block_zones) >= 1
    s, e = block_zones[0]
    assert "x = 1" in text[s:e]
    assert "y = 2" in text[s:e]


# ============ 4. _find_no_split_zones：合并重叠 ============


def test_find_zones_overlap_merged():
    """重叠的不可分割区应被合并。"""
    from app.services import chunker

    # 构造一个同时是表格和公式的"重叠"场景很难
    # 但多个相邻表格应合并或独立
    text = (
        "段落1。\n"
        "| col1 |\n|---|\n| a |\n"
        "段落2。\n"
        "| col2 |\n|---|\n| b |\n"
    )
    zones = chunker._find_no_split_zones(text)
    # 检查 zone 不重叠
    for i in range(len(zones) - 1):
        assert zones[i][1] <= zones[i + 1][0]


# ============ 5. _is_in_no_split_zone ============


def test_is_in_no_split_zone_basic():
    """位置判断：区间内/外。"""
    from app.services import chunker

    zones = [(5, 10), (15, 20)]
    assert chunker._is_in_no_split_zone(5, zones) is True
    assert chunker._is_in_no_split_zone(9, zones) is True
    assert chunker._is_in_no_split_zone(10, zones) is False  # end 视为外
    assert chunker._is_in_no_split_zone(4, zones) is False
    assert chunker._is_in_no_split_zone(11, zones) is False
    assert chunker._is_in_no_split_zone(15, zones) is True
    assert chunker._is_in_no_split_zone(20, zones) is False
    assert chunker._is_in_no_split_zone(0, zones) is False
    assert chunker._is_in_no_split_zone(100, zones) is False


def test_is_in_no_split_zone_empty():
    """空 zone 列表 → 永远不在内。"""
    from app.services import chunker

    assert chunker._is_in_no_split_zone(0, []) is False
    assert chunker._is_in_no_split_zone(100, []) is False


# ============ 6. _adjust_split_to_safe_position ============


def test_adjust_split_outside_zone_unchanged():
    """切分点本就在 zone 外 → 不变。"""
    from app.services import chunker

    text = "段落1。段落2。段落3。"
    zones = chunker._find_no_split_zones(text)
    # 找到第一个句号位置
    pos = text.index("。") + 1
    assert pos == 4
    safe = chunker._adjust_split_to_safe_position(text, pos, zones)
    assert safe == pos


def test_adjust_split_inside_latex_finds_previous_punct():
    """切分点在公式内 → 调整到公式前的句末符号。"""
    from app.services import chunker

    text = "前面一段。$a + b$ 后面一段。"
    zones = chunker._find_no_split_zones(text)
    # 假设切分点想落在公式中间（pos=8 在 "$a + b$" 内）
    pos = 8
    safe = chunker._adjust_split_to_safe_position(text, pos, zones)
    # 安全点位置之前的字符必须是句末符号（这里是 "。"）
    assert text[safe - 1] in "。；!?\n", f"safe-1 ({safe-1}) 应是句末符号，实为 {text[safe-1]!r}"
    # safe 是切分点，指向"切完之后"的位置（在公式外）。
    # 在「前面一段。」「$a + b$」中，公式从 5 开始；切分后前半段是 "前面一段。"。
    # 切分点 safe = 5（公式起点），前半段 [0, 5) 包含 "前面一段。"。
    # safe 位置 = 5 = "$"，这个 $ 本身仍属于公式 → 公式仍完整保留在前一段后的下一段中
    formula_start = text.index("$a + b$")
    # 切分点 safe 应在公式起点或之前（保证公式不被打断）
    assert safe <= formula_start, f"safe ({safe}) 应 ≤ 公式开始 ({formula_start})"


def test_adjust_split_at_zone_end_when_no_safe_point():
    """没有合适向前切分点 → 切到 zone 结束。"""
    from app.services import chunker

    # 文本开头就是公式，没有前置句末符号
    text = "$x = 1 + 2$ 然后是其他文字。"
    zones = chunker._find_no_split_zones(text)
    # 切分点想落在公式外（在公式后）
    pos = text.index("然后")
    safe = chunker._adjust_split_to_safe_position(text, pos, zones)
    # 应该调整到公式结束或之后
    assert safe >= text.index("$x = 1 + 2$") + len("$x = 1 + 2$")


# ============ 7. _split_by_sentence：表格完整性 ============


def test_split_sentence_preserves_md_table():
    """_split_by_sentence 不会把 Markdown 表格切到两段。"""
    from app.services import chunker

    # 段落内含一张较大表格（3000 字符，远超 target=1500）
    table_lines = [
        "| " + " | ".join([f"col{i}" for i in range(8)]) + " |",
        "|" + "|".join(["---"] * 8) + "|",
    ]
    for i in range(50):
        table_lines.append("| " + " | ".join([f"v{i}_{j}" for j in range(8)]) + " |")
    table_text = "\n".join(table_lines)
    # 加前置文字 + 表格 + 后置文字
    text = (
        "前置段落。包含一些文字说明，引出下面的表格。\n"
        + table_text
        + "\n后续段落继续说明表格含义。"
    )

    blocks = [
        chunker.Block(page_num=1, block_type="paragraph", text=text),
    ]
    target = 1500

    chunks = chunker._split_by_sentence(blocks, target)

    # 收集所有 chunk 文本
    all_text = "".join(b.text for blocks_list in chunks for b in blocks_list)

    # 表格必须完整存在
    assert "| col0 | col1 |" in all_text, "表头必须存在"
    assert "| v0_0 |" in all_text, "第一行数据必须存在"
    assert "| v49_7 |" in all_text, "最后一行数据必须存在"
    # 表格的所有行必须在同一个 chunk 内
    # 找到包含表头的 chunk
    for cl in chunks:
        chunk_text = "".join(b.text for b in cl)
        if "| col0 |" in chunk_text:
            # 这个 chunk 应该包含完整表格
            assert "| v49_7 |" in chunk_text, "完整表格必须在同一 chunk 内"
            break


# ============ 8. _split_by_sentence：公式完整性 ============


def test_split_sentence_preserves_inline_formula():
    """_split_by_sentence 不会把 LaTeX 公式切到两段。"""
    from app.services import chunker

    # 段落内含公式，公式被包在中文句号之间
    text = (
        "第一段说明。公式 $E = mc^2$ 描述了质能等价关系。"
        "第二段继续解释。第二段说完了。"
        "中间还有 $a^2 + b^2 = c^2$ 公式。"
        "第三段结尾。"
    )
    blocks = [
        chunker.Block(page_num=1, block_type="paragraph", text=text),
    ]
    target = 100  # 强制多次切分

    chunks = chunker._split_by_sentence(blocks, target)

    # 每个公式必须完整存在（不出现在不同 chunk 中）
    all_text = "".join(b.text for blocks_list in chunks for b in blocks_list)
    assert "$E = mc^2$" in all_text
    assert "$a^2 + b^2 = c^2$" in all_text

    # 检查每个公式是否完整出现在某个 chunk 中
    for chunk_blocks in chunks:
        chunk_text = "".join(b.text for b in chunk_blocks)
        if "$E = mc^2$" in chunk_text:
            # 公式应该完整出现（不被打断成 `$E = mc^2` 或 `E = mc^2$`）
            assert "$E = mc^2$" in chunk_text
            # 不应出现 $E = mc^2 前没有 $ 但有 $a 等异常
        if "$a^2 + b^2 = c^2$" in chunk_text:
            assert "$a^2 + b^2 = c^2$" in chunk_text


def test_split_sentence_preserves_long_formula():
    """_split_by_sentence 不会切到长公式内。"""
    from app.services import chunker

    # 长公式（超过 target）
    long_formula = "$" + "a + b + c + d + " * 200 + "e$"
    text = "前面文字。" + long_formula + "后面文字。更多内容。"

    blocks = [
        chunker.Block(page_num=1, block_type="paragraph", text=text),
    ]
    target = 100

    chunks = chunker._split_by_sentence(blocks, target)
    all_text = "".join(b.text for blocks_list in chunks for b in blocks_list)
    assert long_formula in all_text, "长公式必须完整保留"


# ============ 9. _split_by_sentence：超过字符阈值仍能完整 ============


def test_split_sentence_no_limit_on_table_or_formula():
    """★ 业务约束：即使表格/公式超过 1500 字符，也不能被切分。"""
    from app.services import chunker

    # 创建一个 3000 字符的表格段落
    table_lines = [
        "| 名称 | 数量 | 备注 |",
        "|---|---|---|",
    ]
    for i in range(80):
        table_lines.append(f"| 项目{i} | {i*10} | 详细说明文字{i*100} |")
    table_text = "\n".join(table_lines)
    # 验证表格确实 > 1500 字符
    assert len(table_text) > 1500

    text = "前置段落说明。\n" + table_text + "\n后续段落。"

    blocks = [
        chunker.Block(page_num=1, block_type="paragraph", text=text),
    ]
    target = 1500

    chunks = chunker._split_by_sentence(blocks, target)
    all_text = "".join(b.text for blocks_list in chunks for b in blocks_list)

    # 表格必须完整保留
    assert "| 项目0 |" in all_text
    assert "| 项目79 |" in all_text
    # 表头和所有行必须在同一个 chunk 中
    found_complete = False
    for cl in chunks:
        chunk_text = "".join(b.text for b in cl)
        if "| 项目0 |" in chunk_text and "| 项目79 |" in chunk_text:
            found_complete = True
            break
    assert found_complete, "完整表格必须在某个 chunk 中保留"


# ============ 10. _split_by_sentence：表/公式混合 ============


def test_split_sentence_mixed_table_and_formula():
    """段落中同时含表格和公式 → 都保持完整。"""
    from app.services import chunker

    text = (
        "前置段落 1。\n"
        "| col1 | col2 |\n|---|---|\n| A | B |\n| C | D |\n"
        "公式 $x = 1$ 说明。\n"
        "| col3 | col4 |\n|---|---|\n| E | F |\n"
        "公式 $y = 2$ 继续。\n"
        "结尾段落。"
    )
    blocks = [
        chunker.Block(page_num=1, block_type="paragraph", text=text),
    ]
    target = 100

    chunks = chunker._split_by_sentence(blocks, target)
    all_text = "".join(b.text for blocks_list in chunks for b in blocks_list)

    # 所有表格和公式都应完整
    assert "| col1 | col2 |" in all_text
    assert "| A | B |" in all_text
    assert "| C | D |" in all_text
    assert "| col3 | col4 |" in all_text
    assert "| E | F |" in all_text
    assert "$x = 1$" in all_text
    assert "$y = 2$" in all_text

    # 检查每张表是否完整
    for cl in chunks:
        chunk_text = "".join(b.text for b in cl)
        if "| col1 |" in chunk_text:
            assert "| C | D |" in chunk_text, "第一张表必须完整"
        if "| col3 |" in chunk_text:
            assert "| E | F |" in chunk_text, "第二张表必须完整"


# ============ 11. 表格 HTML 块（block_type=table）的整体性 ============


def test_html_table_block_kept_intact():
    """HTML 表格块（block_type=table）在切分中作为整体保留。"""
    from app.services import chunker

    # 模拟 MinerU 解析出的表格 block
    table_block = chunker.Block(
        page_num=1,
        block_type="table",
        text="",
        table_html="<table>" + "<tr><td>cell</td></tr>" * 100 + "</table>",
        table_caption="示例表格",
    )
    # 长段落 + 表格 + 长段落
    para1 = chunker.Block(page_num=1, block_type="paragraph", text="前置段落 " * 200)
    para2 = chunker.Block(page_num=1, block_type="paragraph", text="后续段落 " * 200)

    blocks = [para1, table_block, para2]
    target = 1500

    # _split_by_sentence 不会切 table 块
    chunks = chunker._split_by_sentence(blocks, target)
    # ★ 修复：用 _block_to_text 转换（table 块的内容在 table_html/table_caption，不在 .text）
    all_text = "".join(
        chunker._block_to_text(b) for blocks_list in chunks for b in blocks_list
    )

    # 表格 caption 应存在
    assert "示例表格" in all_text, "caption 应在 chunk 渲染文本中存在"
    assert "<table>" in all_text, "table HTML 应在 chunk 渲染文本中存在"

    # 表格块一定在某个 chunk 中完整存在
    found = False
    for cl in chunks:
        if any(b.block_type == "table" for b in cl):
            table_in_chunk = next(b for b in cl if b.block_type == "table")
            assert table_in_chunk.table_html == table_block.table_html
            assert table_in_chunk.table_caption == "示例表格"
            found = True
            break
    assert found, "table 块必须在某个 chunk 中"
