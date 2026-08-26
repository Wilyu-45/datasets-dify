"""plan.md §3.3 — 自定义文档切分策略（核心）。

实现要点（依据 cutrule.md + cutstrategy.md）：
    1. 输入：data/parsed/{stem}/ 下的 MinerU 产物
       - {stem}_content_list_v2.json  ← 主解析数据（标题层级、页码、图片）
       - {stem}.md                      ← 文本内容（备用，含图片语法）
       - images/                        ← 图片文件夹
    2. 输出：data/chunks/{stem}/
       - chunk_NNN_{slug}.md   ← 每个切分文件
       - chunk_metadata.json   ← 全部 chunk 的元数据
       - images/               ← 引用的图片（去重拷贝）
    3. 切分流程：
       a) 把 v2 扁平化为有序 block 列表
       b) 区域划分：封面 / 目录 / 前言 / 正文 / 附录 / 参考文献
       c) 各区域独立切分：
          - 封面/目录/前言/参考文献：整体为 1 段（>1500 时按段切）
          - 正文：1级 → 贪心合并 2级（≤1500）→ 贪心合并 3级（≤1500）→ 句号切
          - 附录：贪心合并（≤1500）
       d) 每个 chunk 内容开头添加完整标题路径（一级 > 二级 > 三级）
       e) 图片内联保留（`![](images/xxx.jpg)`）
    4. 层级兜底：v2 没有 title 类型时，按文本模式识别：
       - "第X章" → level-1
       - "第X条" → level-2
       - "附录 X" → appendix
       - "参考文献" → reference
    5. manifest 更新：成功 → chunks 列写入 chunks/{stem}，status=chunked

★ 2026-07-31 图片超限切分（cutrule.md 3.5 / 4.3）：
    背景：用户 WST 809 文档 11+ 张图在同段，Dify add_segments 报 400
    `Exceeded maximum attachment limit of 10`。
    解决：在贪心合并时增加"图片数 ≤ settings.chunk_max_images_per_segment"
    维度（默认 10），与 Dify 端 SINGLE_CHUNK_ATTACHMENT_LIMIT 对齐。
    - _greedy_merge_groups 新增 max_images 入参
    - chunk_body / chunk_appendix 统一把 max_images=settings.chunk_max_images_per_segment
      传下去
    - 合并条件：chars + next_chars ≤ threshold AND images + next_images ≤ max_images
    - 任一条件不满足 → 落盘当前 buffer，新组开新段
    - 单组就超 max_images：原样保留（不强行拆图，避免与"图片不独立成段"冲突）
    - 旧有 chunk 数量 + 字符阈值语义不变

启动约束：
    - 服务启动不调本函数；仅用户点击「切分」按钮时由 /api/chunk 触发。
    - 幂等：chunks 列非空 → 跳过（除非 force=true）。

★ 2026-07-31 v2 解析严重缺失自动 fallback：
    背景：MinerU 服务端在某些 PDF（GBK-EUC-H CMap / 扫描件 OCR 失败）上
    返回 v2 文本极少（< 50 字符）。chunker 检测到 trivial 时不再直接报错，
    而是自动调 pdf_fallback.maybe_fallback_after_mineru_failure 重解析
    （前提是 manifest 能找到原 PDF：pending/ 或 input/）。
    - 重解析成功且产物 ≥ _PARSE_QUALITY_MIN_TEXT_CHARS → 继续切分
    - 重解析失败或仍 trivial → 报错（CHUNK_FAILED + manifest error）
    - 仅对 .pdf 后缀触发 fallback；其它格式直接报错
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import time
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from app.config import settings
from app.models.schemas import (
    ChunkAction,
    ChunkActionRecord,
    ChunkMeta,
    ChunkReport,
    ManifestRow,
)
from app.services import manifest_store

log = logging.getLogger("ragsystem.chunker")


# ============================================================
# 数据结构
# ============================================================


@dataclass
class Block:
    """扁平化后的单个 block。"""

    page_num: int                # 1-based 页码
    block_type: str              # title / paragraph / image / header / footer / table / other
    level: Optional[int] = None  # 仅 title 有
    text: str = ""               # 纯文本（title/paragraph/table），或 image_caption
    image_path: Optional[str] = None  # 仅 image 有，原始相对路径
    image_caption: Optional[str] = None
    table_html: Optional[str] = None  # 仅 table 有，HTML 字符串（保留版式）
    table_caption: Optional[str] = None  # 仅 table 有
    table_footnote: Optional[str] = None  # 仅 table 有（caption 下方追加的注脚文本）
    table_image_path: Optional[str] = None  # 仅 table 有：当 table 是图片型时记录 images/xxx 路径
    table_type: Optional[str] = None  # 仅 table 有：simple_table / complex_table
    raw: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Region:
    """切分前的区域（封面/目录/前言/正文/附录/参考文献）。"""

    region_type: str             # cover / toc / preface / body / appendix / reference
    title_path: str              # 该区域的标题路径
    blocks: List[Block] = field(default_factory=list)
    # ★ 2026-08-06：region 在原 blocks 列表中的起始索引，用于按文档原始顺序输出
    # （中文文档：cover→toc→preface→body→appendix→reference；
    #  英文论文：cover→body→preface→reference，preface 可能在 body 之后）
    start_idx: int = 0


@dataclass
class Chunk:
    """切分后的一个 chunk。"""

    chunk_id: str                # chunk_001
    file_name: str               # chunk_001_封面.md
    title_path: str              # 完整标题路径（用于内容开头和元数据）
    chunk_type: str              # cover / toc / preface / body / appendix / reference / single
    char_count: int              # 字符数（不计占位符）
    image_refs: List[str] = field(default_factory=list)  # 相对路径 ["images/xxx.jpg"]
    is_split: bool = False       # 是否为超长二次切分
    body: str = ""               # 实际写入文件的 markdown 内容
    # 内部用：父级标题路径（一级），用于多级合并时构造 title_path
    _level1: str = ""
    _level2: str = ""
    # ★ 2026-08-13 表格独立成段
    table_name: Optional[str] = None   # 表名（如 "表2"、"表 2 流程图"）
    table_type: Optional[str] = None   # "table" / "table_part"
    # ★ 2026-08-24 多策略切分：父-子切分时记录父块标识
    parent_id: Optional[str] = None    # 父块标识（parent_child 策略），子块指向其父块


# ============================================================
# 文件定位
# ============================================================


def _find_first(root: Path, glob_pat: str, recursive: bool = True) -> Optional[Path]:
    """在 root 下找第一个匹配的文件。"""
    if not root.exists():
        return None
    matches = list(root.rglob(glob_pat) if recursive else root.glob(glob_pat))
    return matches[0] if matches else None


def locate_parsed_files(parsed_dir: Path) -> Tuple[Optional[Path], Optional[Path], Optional[Path]]:
    """定位 (v2_json, md_file, images_dir)。

    parsed_dir 通常是 data/parsed/{stem}/，其下会有一个或多个子目录
    （如 office/、hybrid_auto/）存放实际产物。

    Returns:
        (v2_path, md_path, images_dir) — 任一可能为 None
    """
    v2 = _find_first(parsed_dir, "*_content_list_v2.json")
    md = _find_first(parsed_dir, "*.md")
    images = _find_first(parsed_dir, "images", recursive=True)
    return v2, md, images


# ============================================================
# v2 解析与扁平化
# ============================================================


def _extract_runs_text(runs: Any) -> str:
    """从 paragraph_content / title_content / image_caption 等 run 数组中拼出文本。

    ★ 2026-08-04：识别 equation_inline 类型的 run（v2 把行内公式单独渲染为
    `{"type": "equation_inline", "content": "x^2+y^2"}`），用 `$...$` 包裹保留为
    LaTeX 行内公式；其它类型（text / hyperlink）维持原 content 拼接。
    """
    if not runs:
        return ""
    if isinstance(runs, str):
        return runs
    if not isinstance(runs, list):
        return str(runs)
    parts: List[str] = []
    for run in runs:
        if isinstance(run, dict):
            rtype = run.get("type")
            c = run.get("content") or run.get("text") or ""
            if not c:
                continue
            if rtype == "equation_inline":
                # 包裹为 LaTeX 行内公式
                parts.append(f"${c}$")
            else:
                parts.append(str(c))
        elif isinstance(run, str):
            parts.append(run)
    return "".join(parts)


def load_v2_blocks(v2_path: Path) -> List[Block]:
    """加载 v2 json → 扁平化的 Block 列表（按页序，丢弃 header/footer/page_number）。

    对于纯 paragraph 文档（v2 没有显式 title 块），
    把匹配「第X章」「第X条」「附录 X」「参考文献」「前言」「目录」
    等模式的 paragraph 提升为虚拟 title 块。
    """
    if not v2_path or not v2_path.exists():
        return []
    with open(v2_path, "r", encoding="utf-8") as fp:
        pages = json.load(fp)

    blocks: List[Block] = []
    if not isinstance(pages, list):
        return blocks

    # 预扫描：是否含任何显式 title 块
    has_explicit_title = any(
        isinstance(p, list) and any(isinstance(b, dict) and b.get("type") == "title" for b in p)
        for p in pages
    )

    for page_idx, page in enumerate(pages, start=1):
        if not isinstance(page, list):
            continue
        for raw in page:
            if not isinstance(raw, dict):
                continue
            t = raw.get("type")
            content = raw.get("content") or {}

            if t == "title":
                level = content.get("level")
                txt = _extract_runs_text(content.get("title_content"))
                blocks.append(
                    Block(
                        page_num=page_idx,
                        block_type="title",
                        level=level if isinstance(level, int) else None,
                        text=txt.strip(),
                        raw=raw,
                    )
                )
            elif t == "paragraph":
                txt = _extract_runs_text(content.get("paragraph_content"))
                if not txt.strip():
                    continue
                # 兜底提升：把匹配 region/level 模式的 paragraph 升级为 title。
                # 不再受 has_explicit_title 守卫限制——v2 中部分文档（如 WST 809）
                # 既有显式 title（封面/目录/前言），又在正文里把 4.x.y 渲染为 paragraph，
                # 附录 A/C/D/E/G/H/I/J 的"附 录 X"也可能是 paragraph。需要始终尝试升级。
                promoted = _maybe_promote_to_title(txt)
                if promoted is not None:
                    promoted.page_num = page_idx
                    promoted.raw = raw
                    blocks.append(promoted)
                    continue
                blocks.append(
                    Block(
                        page_num=page_idx,
                        block_type="paragraph",
                        text=txt.strip(),
                        raw=raw,
                    )
                )
            elif t == "image":
                img_src = (content.get("image_source") or {}).get("path") or ""
                cap = _extract_runs_text(content.get("image_caption"))
                blocks.append(
                    Block(
                        page_num=page_idx,
                        block_type="image",
                        image_path=img_src or None,
                        image_caption=cap.strip() or None,
                        raw=raw,
                    )
                )
            elif t == "table":
                # ★ 2026-08-04：表格块处理（v2 解析后的关键结构）
                # MinerU 把表格渲染为 `{"type":"table","content":{"html":..., "table_caption":...,
                #   "table_footnote":[...], "image_source":{...}(图片型表格才有),
                #   "table_type": "simple_table|complex_table", "table_nest_level": 1+}}`
                # 旧代码完全忽略 table 块 → 表格内容 100% 丢失，附件/名单类文档 chunk 残缺。
                # 处理：
                #   1) 优先用 html 字符串（含 <p> 段落 + colspan/rowspan）保留版式
                #   2) 若同时存在 image_source（图片型表格）→ 走 image 渲染 + 收集 image_refs
                #   3) 拼 caption + footnote 拼到表格上方/下方作为辅助文本
                table_html = (content.get("html") or content.get("table_body") or "").strip()
                cap = _extract_runs_text(content.get("table_caption"))
                fn = _extract_runs_text(content.get("table_footnote"))
                img_src = (content.get("image_source") or {}).get("path") or ""
                table_type = content.get("table_type") or None
                # 仅当 html 为空且有图片时走图片型表格分支
                if not table_html and img_src:
                    blocks.append(
                        Block(
                            page_num=page_idx,
                            block_type="image",
                            image_path=img_src or None,
                            image_caption=(cap.strip() or fn.strip() or None),
                            raw=raw,
                        )
                    )
                else:
                    blocks.append(
                        Block(
                            page_num=page_idx,
                            block_type="table",
                            text=cap.strip() or "",
                            table_html=table_html or None,
                            table_caption=cap.strip() or None,
                            table_footnote=fn.strip() or None,
                            table_image_path=img_src or None,
                            table_type=table_type,
                            raw=raw,
                        )
                    )
            elif t == "list":
                # ★ 关键：MinerU v2 把目次页的 TOC 条目（"1 范围.....", "附录 A ...", 等）
                # 渲染为 list 块，每个 list_items 元素是一行。旧代码完全忽略 list 块，
                # 导致目录 chunk 被截断成只剩 "## 目 次" 一行。
                # 这里把 list 展开为多个 Block：
                #   - 尝试把 item 文本升级为 title（章节号/附录号等）
                #   - 否则作为 paragraph
                items = content.get("list_items") or []
                if not isinstance(items, list):
                    continue
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    item_text = _extract_runs_text(item.get("item_content"))
                    if not item_text.strip():
                        continue
                    # 尝试升级为 title（与 paragraph 处理一致）
                    promoted = _maybe_promote_to_title(item_text)
                    if promoted is not None:
                        promoted.page_num = page_idx
                        promoted.raw = raw
                        blocks.append(promoted)
                    else:
                        blocks.append(
                            Block(
                                page_num=page_idx,
                                block_type="paragraph",
                                text=item_text.strip(),
                                raw=raw,
                            )
                        )
            # 其它（header/footer/page_number）丢弃
    return blocks


def _maybe_promote_to_title(txt: str) -> Optional[Block]:
    """把匹配 region/level 模式的 paragraph 升级为 title。返回 None 表示保持 paragraph。"""
    s = txt.strip()
    if not s:
        return None
    # 区域标题优先
    if RE_TOC.match(s):
        return Block(page_num=0, block_type="title", level=1, text=s)
    if RE_PREFACE.match(s):
        return Block(page_num=0, block_type="title", level=1, text=s)
    if RE_REFERENCE.match(s):
        return Block(page_num=0, block_type="title", level=1, text=s)
    if RE_APPENDIX.match(s):
        return Block(page_num=0, block_type="title", level=1, text=s)
    # 章节标题
    if RE_CHAPTER.match(s):
        return Block(page_num=0, block_type="title", level=1, text=s)
    # ★ 2026-08-12：中文数字序号标题 "一、xxx" → level-1
    if RE_CN_NUMERAL_L1.match(s):
        return Block(page_num=0, block_type="title", level=1, text=s)
    # 条款标题
    if RE_ARTICLE.match(s):
        return Block(page_num=0, block_type="title", level=2, text=s)
    # 数字标题（"1 范围" / "4.1 基本原则" / "4.2.1 在入口..."）
    m = RE_NUMERIC_TITLE.match(s)
    if m:
        num_part = m.group(1)
        # 防御 1：4 位数字开头很可能是年份（2024），不是章节号
        if len(num_part.split(".")[0]) >= 4:
            return None
        # 防御 2：数字后面紧跟"年/月/日"是日期，不是标题
        # 例如 "2024 年 11 月 12 日"、"11 月 12 日"
        rest = s[m.end():]
        if (
            re.match(r"^\s*年", rest)
            or re.match(r"^\s*月", rest)
            or re.match(r"^\s*日", rest)
        ):
            return None
        depth = num_part.count(".")
        # depth=0 → level 1, depth=1 → level 2, depth=2 → level 3
        return Block(page_num=0, block_type="title", level=min(depth + 1, 3), text=s)
    return None


# ============================================================
# 层级识别（v2 无 title 时的兜底）
# ============================================================

# "第X章" / "第X部分" / "第一章" 等 → level-1
RE_CHAPTER = re.compile(r"^第\s*[一二三四五六七八九十百千零0-9]+\s*[章部分]")
# ★ 2026-08-12：中文数字序号标题 "一、xxx" / "二、xxx" / "十、xxx" → level-1
# 常见于中国政府公文、标准规范等文档，如 "一、门诊设置" "二、病区设置"
# 限制：长度 ≤ 40（与 _is_chapter_like 一致），后面接非空内容
RE_CN_NUMERAL_L1 = re.compile(
    r"^[一二三四五六七八九十百]+\s*[、\uff0c]\s*\S"
)
# "第X条" / "第一条" 等 → level-2
RE_ARTICLE = re.compile(r"^第\s*[一二三四五六七八九十百千零0-9]+\s*条")
# "附录 A" / "附录A" / "附 录 A" / "附录 1" / "附录 一" / "附 录 B（资料性）xxx"
# 注意：附录标题后可能跟 "（资料性）xxx" 等说明文字
RE_APPENDIX = re.compile(
    r"^附\s*录\s*"
    r"(?:[A-J]|[一二三四五六七八九十百千0-9]+|（[一二三四五六七八九十百千0-9]+）|\([A-J0-9]+\))"
)
# "参考文献" / "参考书目" / "Reference"
RE_REFERENCE = re.compile(r"^\s*(?:参考文献|参考书目|参考资料|Reference)", re.IGNORECASE)
# "前言" / "前 言" / "序" / "序 言"
RE_PREFACE = re.compile(r"^\s*(?:前\s*言|序\s*言|序\s*$)\s*$")
# "目录" / "目 录" / "目次"
RE_TOC = re.compile(r"^\s*(?:目\s*录|目\s*次)\s*$")
# "X" / "X.Y" / "X.Y.Z" 形式 — 顶层 X 视为 level-1，X.Y 视为 level-2，X.Y.Z 视为 level-3
# `(?:\s|$)` 兼容"4.1"和"4.1 基本"两种形态（注意不能用 look-ahead `(?=[\s\.])`，
# 否则贪婪匹配会回退到只剩 "X"，因为 ".X" 后面是数字 \d 不属于 [\s\.]）
RE_NUMERIC_TITLE = re.compile(r"^(\d+(?:\.\d+){0,2})(?:\s|$)")

# ★ 2026-08-06：英文论文/标准文档的标题识别
# 论文常见独立 L1 章节（用于 region 划分和 body 起点识别）
RE_ENGLISH_L1_HEADING = re.compile(
    r"^\s*(?:ABSTRACT|INTRODUCTION|CONCLUSION[S]?|DISCUSSION|"
    r"METHOD[S]?|RESULT[S]?|MATERIAL[S]?\s+AND\s+METHOD[S]?|"
    r"BACKGROUND|OBJECTIVE[S]?|PURPOSE|AIM[S]?|"
    r"REFERENCES?|BIBLIOGRAPHY|ACKNOWLEDGEMENT[S]?|ACKNOWLEDGMENT[S]?|"
    r"APPENDIX[A-Z]?|CONTENTS|TABLE\s+OF\s+CONTENTS|LIST\s+OF\s+FIGURES|"
    r"LIST\s+OF\s+TABLES|ABBREVIATIONS|GLOSSARY|"
    r"PREFACE|FORWARD|PROLOGUE|EXECUTIVE\s+SUMMARY|"
    r"RECOMMENDATION[S]?|KEY\s+RECOMMENDATION[S]?|"
    r"SCOPE|NORMATIVE\s+REFERENCES?|TERMS\s+AND\s+DEFINITIONS|"
    r"DEFINITIONS|ABBREVIATIONS\s+AND\s+ACRONYMS)\s*$",
    re.IGNORECASE,
)
# Paper header / 水印类标题（v2 偶尔会把页眉当 title），不作为 L1 章节识别
RE_PAPER_HEADER = re.compile(
    r"^\s*(?:Accepted\s+Manuscript|Original\s+Article|Review\s+Article|"
    r"Case\s+Report|Brief\s+Communication|Letter\s+to\s+the\s+Editor|"
    r"ACCEPTED\s+MANUSCRIPT|Manuscript|PREPRINT|"
    r"Page\s+\d+|©\s*\d{4}|Copyright|Confidential|"
    r"Draft|Working\s+Paper|Technical\s+Report)\s*$",
    re.IGNORECASE,
)
# 整行的"Reference: XXX" 编号（学术论文首页 PII/Reference 编号），不是参考文献区域
RE_REFERENCE_NUMBER = re.compile(
    r"^\s*(?:Reference|Ref|PII|DOI|Article\s+ID|Article\s+No)[ #:.][\s\S]*$",
    re.IGNORECASE,
)


def _infer_block_level(block: Block) -> Optional[int]:
    """当 v2 没有显式 level 时，根据文本推断 level 1~3。

    返回 None 表示非标题。
    """
    if block.block_type != "title":
        return None
    t = block.text.strip()
    if not t:
        return None
    if RE_CHAPTER.match(t):
        return 1
    # ★ 2026-08-12：中文数字序号标题 "一、xxx" → level-1
    if RE_CN_NUMERAL_L1.match(t):
        return 1
    if RE_ARTICLE.match(t):
        return 2
    m = RE_NUMERIC_TITLE.match(t)
    if m:
        depth = m.group(1).count(".")
        # 1.2.3 → depth=2 → level=3
        return min(depth + 1, 3)
    return None


def _looks_like_l1_title(text: str) -> bool:
    """判断 title 文本是否真的像 L1 章节标题。

    2026-08-06 修复：v2 常把"1 xxx"形式的"长文子项"误标为 L1，
    但实际上它们是 L3（"X.Y.Z"）的子项 1) 2) ...
    典型反例：
      - "1 总则"（3 字符）→ L1 ✓
      - "8 污水处理站"（7 字符）→ L1 ✓
      - "1 门诊、病房病人的排泄物、分泌物就地消毒处理后，方可排入污水处理站。"（37 字符）→ 子项 ✗
      - "2 当流程为重力自排式时,污水量应按最大小时污水量计算;"（22 字符含","）→ 子项 ✗
    经验阈值：
      1) L1 章节标题通常 <= 30 字符
      2) L1 章节标题不含句子级标点（句号、逗号、分号、冒号）
         —— 真正的 L1 标题是章节名（短、无标点），子项描述才有标点。
    ★ 2026-08-12：中文数字序号标题 "一、xxx" 例外——"、" 是序号分隔符，
         不是句子级标点。若匹配 RE_CN_NUMERAL_L1，直接返回 True。
    """
    s = (text or "").strip()
    if not s:
        return False
    # ★ 2026-08-12：中文数字序号标题 "一、xxx" 直接通过（长度限制 40，与 _is_chapter_like 一致）
    if RE_CN_NUMERAL_L1.match(s) and len(s) <= 40:
        return True
    if len(s) > 30:
        return False
    # ★ 2026-08-13：数字编号章节标题例外。
    # 标准文档中 L1 标题常含中文顿号（如 "8 标志、包装、运输和贮存"），
    # 但下面的标点检查会把 "、" 当作句子级标点排除，导致两个 L1 章节合并。
    # 例外条件：以纯整数编号开头（"8 xxx"、"12 xxx"），不含小数点（排除 "8.1 xxx"），
    # 且长度 ≤ 30（已在上面检查）。这样 "8.1 xxx" 仍走 L2 路径。
    _num_prefix = s.split(None, 1)[0] if s.split() else ""
    if _num_prefix.isdigit() and "." not in _num_prefix:
        return True
    # 含句子级标点（中文/英文）的不是章节标题
    if re.search(r"[;\u3002\uff0c\uff1b\uff1a\u3001!?,:;.]", s):
        return False
    return True


def _effective_level(b: Block) -> Optional[int]:
    """统一获取 title 的有效 level。

    优先级：文本模式推断 > v2 标注。
    根因：原代码 `b.level or _infer_block_level(b)` 在 v2 给出非 None level 时永远走 v2，
    但 v2 常把"1 范围"/"4.1 基本原则"统一标为 level=2（结构噪音），
    而 cutrule.md 期望"1 范围"是 level 1、"4.1"是 level 2。
    改用本函数：先按文本模式推断，推断失败才回退到 v2 标注。
    """
    if b.block_type != "title":
        return None
    inferred = _infer_block_level(b)
    if inferred is not None:
        return inferred
    return b.level


# ============================================================
# 区域划分
# ============================================================


def _classify_title(t: str) -> str:
    """根据标题文本判断它属于哪个区域。

    ★ 2026-08-06：增加英文论文标题识别。
    - 整行的 "REFERENCES" / "BIBLIOGRAPHY" → reference_start
    - 整行的 "ACKNOWLEDGMENT[S]" / "PREFACE" → preface_start
    - 整行的 "APPENDIX X" → appendix_start
    - 整行的 "ABSTRACT" / "INTRODUCTION" / "METHODS" / "RESULTS" /
      "DISCUSSION" / "CONCLUSION" → 不作区域判断，body 区域内的 L1
    - "Reference: XXX" / "PII: XXX" 编号行 → 不作任何区域触发（避免误识别）
    """
    s = t.strip()
    if not s:
        return ""
    # "Reference: xxx" / "PII: xxx" / "DOI: xxx" 编号行（不是参考文献区域）
    if RE_REFERENCE_NUMBER.match(s):
        return ""
    if RE_APPENDIX.match(s):
        return "appendix_start"
    if RE_REFERENCE.match(s):
        return "reference_start"
    if RE_PREFACE.match(s):
        return "preface_start"
    if RE_TOC.match(s):
        return "toc_start"
    # 英文论文：整行的 REFERENCES / BIBLIOGRAPHY
    if re.match(r"^\s*(?:REFERENCES?|BIBLIOGRAPHY)\s*$", s, re.IGNORECASE):
        return "reference_start"
    # 英文论文：整行的 ACKNOWLEDGMENT[S] / PREFACE → preface
    if re.match(
        r"^\s*(?:ACKNOWLEDGEMENT[S]?|ACKNOWLEDGMENT[S]?|PREFACE|FOREWORD|PROLOGUE)\s*$",
        s, re.IGNORECASE,
    ):
        return "preface_start"
    # 英文论文：整行的 APPENDIX X
    if re.match(r"^\s*APPENDIX\s+[A-Z0-9]+\s*$", s, re.IGNORECASE):
        return "appendix_start"
    return ""


# ============================================================
# 解析质量检测：识别 MinerU 解析严重缺失的情况
# ============================================================

# 触发"解析失败"告警的阈值
_PARSE_QUALITY_MIN_BLOCKS = 3          # v2 总块数（含 page_number/header）低于此值
_PARSE_QUALITY_MIN_TITLE_OR_PARA = 1   # 至少 1 个 title 或 paragraph 块
_PARSE_QUALITY_MIN_TEXT_CHARS = 50     # 真实文本字符数（去除空白）


def _collect_v2_text_chars(v2_path: Path) -> Tuple[int, int, int]:
    """统计 v2 文件中的块数和文本字符数。

    Returns:
        (total_blocks, title_or_para_blocks, text_chars)
    """
    try:
        data = json.loads(v2_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return 0, 0, 0
    if not isinstance(data, list):
        return 0, 0, 0
    total = 0
    title_or_para = 0
    text_chars = 0
    for page in data:
        if not isinstance(page, list):
            continue
        for blk in page:
            total += 1
            if not isinstance(blk, dict):
                continue
            btype = blk.get("type", "")
            content = blk.get("content") or {}
            if btype in ("title", "paragraph"):
                title_or_para += 1
            # 收集所有文本字段
            if isinstance(content, dict):
                for key in ("title_content", "paragraph_content", "text"):
                    for run in content.get(key) or []:
                        if isinstance(run, dict):
                            rtype = run.get("type")
                            text = run.get("content") or run.get("text") or ""
                            # ★ 2026-08-04：equation_inline 公式也计入（公式是有效内容，不是噪音）
                            text_chars += len(text) if text else 0
                # ★ 2026-08-04：表格 HTML 也要计入（防止"全文就一个表格"被误判 trivial）
                # ★ 2026-08-04 bugfix：_strip_html_tags 返回 str，不能 int += str，必须取 len
                if btype == "table":
                    html = content.get("html") or content.get("table_body") or ""
                    if html:
                        text_chars += len(_strip_html_tags(html))
    return total, title_or_para, text_chars


def _is_parse_content_trivial(parsed_dir: Path) -> Tuple[bool, str]:
    """检测 MinerU 解析产物是否严重缺失（如扫描件 OCR 失败、纯图片 PDF 等）。

    Returns:
        (is_trivial, reason)
    """
    # 1) 优先检查 v2 文件
    v2_files = list(parsed_dir.rglob("*_content_list_v2.json"))
    if v2_files:
        v2_path = v2_files[0]
        total, tp, chars = _collect_v2_text_chars(v2_path)
        if total < _PARSE_QUALITY_MIN_BLOCKS:
            return True, f"v2 块数过少（{total} 个），MinerU 可能解析失败"
        if tp < _PARSE_QUALITY_MIN_TITLE_OR_PARA:
            return True, f"v2 中无 title/paragraph 块（共 {total} 个块），MinerU 可能解析失败"
        if chars < _PARSE_QUALITY_MIN_TEXT_CHARS:
            return True, f"v2 提取的文本过少（{chars} 字符），MinerU 可能解析失败"

    # 2) 兜底检查 .md 文件
    md_files = list(parsed_dir.rglob("*.md"))
    if md_files:
        try:
            text = md_files[0].read_text(encoding="utf-8")
            cleaned = text.strip()
            if len(cleaned) < _PARSE_QUALITY_MIN_TEXT_CHARS:
                return True, f".md 文件过短（{len(cleaned)} 字符），MinerU 可能解析失败"
        except OSError:
            pass
    else:
        # 既没有 v2 也没有 md → 解析产物不完整
        return True, "未找到 v2 或 .md 文件，解析产物不完整"

    return False, ""


# ============================================================
# ★ 2026-07-31：v2 解析严重缺失时自动 fallback 重解析
# 背景：MinerU 服务端在某些 PDF（GBK-EUC-H CMap / 扫描件 OCR 失败）上
# 返回 v2 文本极少（< 50 字符）。chunker 不再直接报错，而是：
#   1) 在 pending/ 或 input/ 下找原 PDF
#   2) 调 pdf_fallback.maybe_fallback_after_mineru_failure 重解析
#   3) 重解析后重新检测 v2；通过则继续切分，否则报错
# ============================================================


def _find_source_pdf(stem: str, manifest_filename: Optional[str] = None) -> Optional[Path]:
    """在 pending_dir / input_dir 下找原 PDF。

    优先级：
    1) pending/{stem}.pdf
    2) input/{stem}.pdf
    3) pending/*.{pdf} 中 stem 前缀匹配的（防 stem 截断）

    Returns:
        找到的 PDF 路径；都找不到返回 None
    """
    candidates: List[Path] = []
    # 1) 直匹配
    for d in (settings.pending_dir, settings.input_dir):
        if not d.exists():
            continue
        for ext in (".pdf", ".PDF"):
            p = d / f"{stem}{ext}"
            if p.is_file():
                return p
    # 2) 前缀匹配（防 stem 被截断 / 含特殊字符）
    for d in (settings.pending_dir, settings.input_dir):
        if not d.exists():
            continue
        for p in d.iterdir():
            if p.suffix.lower() == ".pdf" and p.stem.startswith(stem[: max(4, len(stem) // 2)]):
                candidates.append(p)
    if candidates:
        return candidates[0]
    return None


def _try_pdf_fallback_for_trivial_parse(
    parse_path: Path,
    stem: str,
) -> Tuple[bool, str]:
    """v2 解析严重缺失时，自动用 pdf_fallback 重解析。

    Args:
        parse_path: 当前解析产物目录（如 data/parsed/{stem}/）
        stem: 文档 stem（用于在 pending/ 或 input/ 找原 PDF）

    Returns:
        (ok, reason)
        - ok=True: 重解析成功（或不需要重解析），可继续切分
        - ok=False: 重解析失败或不需要重解析，reason 含失败原因
    """
    from app.services import pdf_fallback
    from app.services.mineru_client import MinerUClient

    src_pdf = _find_source_pdf(stem)
    if src_pdf is None:
        return False, "未找到原 PDF，无法触发 fallback 重解析（请手动重跑解析）"

    if not pdf_fallback.is_pymupdf_available():
        return False, "PyMuPDF 未安装，无法触发 fallback（pip install pymupdf）"

    log.warning(
        "chunker: v2 解析严重缺失，自动触发 pdf_fallback 重解析: %s (stem=%s)",
        src_pdf.name, stem,
    )
    try:
        client = MinerUClient()
    except Exception as e:  # noqa: BLE001
        return False, f"无法构造 MinerUClient，跳过 fallback: {e}"

    try:
        fallback_result = pdf_fallback.maybe_fallback_after_mineru_failure(
            src_pdf, parse_path, client=client
        )
    except Exception as e:  # noqa: BLE001
        return False, f"pdf_fallback 调用异常: {e}"

    if fallback_result is None:
        return False, "pdf_fallback 三级 Tier 全部失败"

    # 重新检测产物
    is_trivial2, reason2 = _is_parse_content_trivial(parse_path)
    if is_trivial2:
        return False, f"pdf_fallback 完成后仍 trivial: {reason2}"

    log.info(
        "chunker: pdf_fallback 重解析成功: %s (used=%s, backend=%s)",
        src_pdf.name, fallback_result.used_fallback, fallback_result.backend,
    )
    return True, f"pdf_fallback 重解析成功（backend={fallback_result.backend}）"


# ============================================================
# 2026-08-06 added: TOC entry detection (X title ... (page))
# 典型场景：GB/CECS/CJJ 等标准 PDF 的目录页通常有 8+ 个这种条目（每个一行），
# 之前 toc_end 直接用 preface_idx / body_start_idx 截断，导致目录条目被误归入
# body 区，进而被 chunk_body 视为 L1 标题生成"空壳" chunk。
# 识别模式：行首是"X"或"X.Y"（章节号），中间是标题，末尾是省略号 + 数字（页码）。
RE_TOC_ENTRY = re.compile(
    r"^\s*"
    r"\d+(?:\.\d+){0,2}"          # 章节号：1 / 1.1 / 1.1.1
    r"\s+\S.*?"                     # 标题文本（至少 1 个非空字符）
    r"[\u2026\.\u00b7]{2,}"        # …… 省略号
    r".*?"                            # …… 后到页码之间可能有空格或 "("
    r"\d+"                           # 页码（数字）
    r"\)?\s*$"                      # 可选 ")" + 行尾
)


def _is_toc_like_text(text: str) -> bool:
    """判断文本是否像目录条目（含省略号 + 页码）。

    比 RE_TOC_ENTRY 更宽松：只要求含省略号 + 行末数字（可带括号），
    不要求行首必须是数字章节号（这样 "本规范用词说明 …… (17)"、
    "附:条文说明 …… (19)" 这类 paragraph 类型的目录条目也能识别）。
    """
    if not text:
        return False
    if not re.search(r"[\u2026\.\u00b7]{2,}", text):
        return False
    if not re.search(r"\d+\)?\s*$", text):
        return False
    return True


def _extend_region_for_toc_entries(blocks, start: int, initial_end: int) -> int:
    """从 start+1 开始扫描，延伸直到遇到非"目录条目"的 block。

    关键：
      1) 即使 initial_end <= start+1（没有可延伸空间），也继续扫描直到
         遇到非目录条目为止——这是修复 body_start_idx 误指 TOC 条目的关键。
      2) title 和 paragraph 都接受（paragraph 类型常见于"本规范用词说明"等
         非数字开头的特殊目录条目）。
      3) 遇到 image / table / footer 等其他类型立即停止。
    """
    end = initial_end
    i = start + 1
    while i < len(blocks):
        b = blocks[i]
        text = b.text if isinstance(b.text, str) else ""
        if b.block_type in ("title", "paragraph") and _is_toc_like_text(text):
            end = i + 1
            i += 1
        else:
            break
    return end


def classify_regions(blocks: List[Block]) -> List[Region]:
    """把 blocks 划分到 封面/目录/前言/正文/附录/参考文献 区域。

    关键规则：按文档中出现顺序确定边界（先出现的区域先 cut）。
        1. cover  = 从 0 到第一个 toc_start / preface_start
        2. toc    = toc_start title → 下一个 preface_start / body_start
        3. preface = preface_start title → 下一个 body_start
        4. body   = 第一个「body 起点」之后到第一个 appendix_start
        5. appendix = appendix_start title → 第一个 reference_start
        6. reference = reference_start title → 末尾

    body 起点定义为（按优先级）：
        a) preface 之后的第一个 level-1 标题
        b) 没有 preface 时，第一个 level-1 中明显是「章节」（如「1 范围」/「第一章」）
           而非文档名
        c) 兜底：preface 之后第一个 title 块（任意 level）
        d) 兜底：第一个 level-1
        e) 兜底：第一个 title 块

    没有识别到任何区域时，整段视为 single。
    """
    if not blocks:
        return []

    # 1) 找区域边界（appendix/reference 一律取最前的）
    toc_idx = preface_idx = appendix_idx = reference_idx = -1
    # 2026-08-06: 记录"重复出现的封面标题"——GB/CECS 等标准的"条文说明"独立成册，
    # 附录封面会重复主封面的"中国工程建设标准化协会标准"+"文档名"。
    # 第二次出现这些标题的位置 = appendix 区域起点。
    seen_cover_titles: set = set()
    in_body_started = False
    for i, b in enumerate(blocks):
        if b.block_type != "title":
            continue
        text = b.text.strip()
        # 第二个出现的"封面标题"作为 appendix 起点（前提是必须先进入 body）
        if (
            text in seen_cover_titles
            and appendix_idx == -1
            and in_body_started
        ):
            appendix_idx = i
        elif text.startswith("中国工程建设标准化协会") or text in (
            "中国工程建设标准化协会标准",
        ):
            seen_cover_titles.add(text)
        kind = _classify_title(b.text)
        if kind == "toc_start" and toc_idx == -1:
            toc_idx = i
        elif kind == "preface_start" and preface_idx == -1:
            preface_idx = i
        elif kind == "appendix_start" and appendix_idx == -1:
            appendix_idx = i
        elif kind == "reference_start" and reference_idx == -1:
            reference_idx = i
        # body 起点 = max(preface_end, toc_end) 之后
        if (
            preface_idx >= 0
            and i > preface_idx
            and b.text not in seen_cover_titles
        ):
            in_body_started = True

    # 1.5) 找「可能的 body 起点」候选（不过滤 cover 标题），
    # 用于在没有 toc/preface 的文档里推断 cover_end（典型场景：医院感染暴发报告
    # 这种"封面+直接第一章"结构，前面没有目录/前言）。
    def _is_chapter_like(t: str) -> bool:
        s = t.strip()
        if not s:
            return False
        # 2026-08-06: 长文本不是章节标题。
        # 典型反例：1.0.8 的子项 1) "1 门诊、病房病人的排泄物、分泌物就地消毒处理后，
        # 方可排入污水处理站。" 51 字符，被误识别为 L1 标题生成空壳 chunk。
        # 章节标题通常 <= 60 字符（如 "1 总则" / "8 污水处理站" / "1.0.1 ..."）。
        # 英文论文章节标题如 "Abstract" / "Introduction" / "Methods" 都很短（<= 30 字符），
        # 但也允许类似 "American College of Surgeons and Surgical Infection Society: ..." 这样的
        # 长文档主标题作为 L1 识别（论文首页标题）。
        if len(s) > 60:
            return False
        # 2026-08-06: 跳过 paper header / 水印类标题。
        if RE_PAPER_HEADER.match(s):
            return False
        if RE_CHAPTER.match(s):
            return True
        if RE_ARTICLE.match(s):
            return True
        if re.match(r"^\d+\s+\S", s):
            return True
        if re.match(r"^\d+\.\S*", s):
            return True
        if RE_ENGLISH_L1_HEADING.match(s):
            return True
        # ★ 2026-08-06：中文章节号识别（“一/二/三/.../十” + 顿号/逗号）
        # 背景：用户上传的“安宁疗护中心管理规范”“护理中心管理规范”等中文规范文档，
        # 章节标题是“## 一、机构管理”“## 二、质量管理”“## 六、管理”这类中文顿号形式，
        # 旧代码不识别，会导致整份文档被识别为 cover 区域后按句号切成 N 段“封面 (part N)”。
        # 限制：
        #   - 必须以“X、”或“X，”开头（顿号/中文逗号都是正式章节号）
        #   - 后面接非空内容（避免“X、下面是具体内容...”这种长段落被误识别）
        #   - 长度 ≤ 40 字符（避开被解析成单一行的“## 六、管理 + 整段正文”这种超长伪标题；
        #     真实章节标题如“## 一、机构管理” 6 字符远低于 40）
        if re.match(r"^[一二三四五六七八九十]+[、，]\s*\S", s) and len(s) <= 40:
            return True
        return False

    body_start_candidate = -1
    # 在有 preface 时，body 起点就是 preface 之后第一个 level-1
    if preface_idx >= 0:
        for i in range(preface_idx + 1, len(blocks)):
            b = blocks[i]
            if b.block_type == "title" and _effective_level(b) == 1:
                body_start_candidate = i
                break
    # 没有 preface 时，body 起点是第一个 chapter-like 的 level-1
    if body_start_candidate == -1:
        for i, b in enumerate(blocks):
            if (
                b.block_type == "title"
                and _effective_level(b) == 1
                and _is_chapter_like(b.text)
            ):
                body_start_candidate = i
                break
    # 兜底：第一个 level-1（**仅在文档中存在 chapter-like 标题时启用**，
    # 避免单页通知文档把封面通知标题误识别为 body 起点）
    if body_start_candidate == -1:
        if any(
            b.block_type == "title" and _is_chapter_like(b.text) for b in blocks
        ):
            for i, b in enumerate(blocks):
                # ★ 2026-08-06：必须加 _is_chapter_like 过滤，否则会选到
                # "Accepted Manuscript" / "Reference: ACS 8512" 之类的非章节标题。
                if b.block_type == "title" and _effective_level(b) == 1 and _is_chapter_like(b.text):
                    body_start_candidate = i
                    break
    # 兜底：第一个 title（同上条件）
    if body_start_candidate == -1:
        if any(
            b.block_type == "title" and _is_chapter_like(b.text) for b in blocks
        ):
            for i, b in enumerate(blocks):
                if b.block_type == "title" and _is_chapter_like(b.text):
                    body_start_candidate = i
                    break
    # ★ 2026-08-06：英文论文 fallback ——
    # v2 常把所有章节标为 level-2，所以"第一个 chapter-like 的 level-1"永远找不到。
    # 这时取第一个 chapter-like 的标题（任意 level）作为 body 起点。
    # 已用 _is_chapter_like 过滤掉 paper header 和超长文本。
    # ★ 重要：必须跳过区域标题（REFERENCE / APPENDIX / PREFACE / TOC）。
    # 典型反例：ACS／SIS 论文的 "REFERENCES" 标题（页 26）在所有 L2 章节里也是
    # "chapter-like"——如果直接选它作为 body 起点，会把整篇 references 放进 body。
    if body_start_candidate == -1:
        for i, b in enumerate(blocks):
            if b.block_type == "title" and _is_chapter_like(b.text) and not _classify_title(b.text):
                body_start_candidate = i
                break

    # 1.6) 用 toc_idx / preface_idx / body_start_candidate 计算 cover 边界，
    # 再从 cover 区域收集 title 集合用于排除封面重复（如 WST 809 p4 重复了 p1 文档名）。
    _cover_end_for_filter = min(
        [x for x in (toc_idx, preface_idx, body_start_candidate) if x >= 0]
        or [len(blocks)]
    )
    cover_title_texts = {
        b.text for b in blocks[:_cover_end_for_filter] if b.block_type == "title"
    }

    body_start_idx = -1
    # ★ 2026-08-06：优先用 body_start_candidate（已在上面计算，包含 paper header
    # 过滤和区域标题过滤）。它是 cover 边界计算的正确输入。
    if body_start_candidate >= 0:
        # 还需要排除 cover 区域里已经出现的标题
        for i in range(body_start_candidate, len(blocks)):
            b = blocks[i]
            if b.block_type == "title" and b.text not in cover_title_texts:
                body_start_idx = i
                break
    # a) preface 之后的第一个 level-1（排除 cover 已出现的标题）
    if body_start_idx == -1 and preface_idx >= 0:
        for i in range(preface_idx + 1, len(blocks)):
            b = blocks[i]
            if b.block_type == "title" and _effective_level(b) == 1 and b.text not in cover_title_texts:
                body_start_idx = i
                break
    # b) 没有 preface：第一个 level-1 且像章节
    if body_start_idx == -1 and preface_idx == -1:
        for i, b in enumerate(blocks):
            if (
                b.block_type == "title"
                and _effective_level(b) == 1
                and _is_chapter_like(b.text)
                and b.text not in cover_title_texts
            ):
                body_start_idx = i
                break
    # c) 兜底：preface 之后第一个 title（任意 level），但排除 cover 标题
    # ★ 2026-08-06：同时跳过区域标题和 paper header，避免 "REFERENCES" 被误选。
    if body_start_idx == -1 and preface_idx >= 0:
        for i in range(preface_idx + 1, len(blocks)):
            b = blocks[i]
            if (
                b.block_type == "title"
                and b.text not in cover_title_texts
                and not _classify_title(b.text)
                and not RE_PAPER_HEADER.match(b.text)
            ):
                body_start_idx = i
                break
    # d) 兜底：第一个 level-1
    if body_start_idx == -1:
        for i, b in enumerate(blocks):
            if (
                b.block_type == "title"
                and _effective_level(b) == 1
                and b.text not in cover_title_texts
                and not _classify_title(b.text)
                and not RE_PAPER_HEADER.match(b.text)
            ):
                body_start_idx = i
                break
    # e) 兜底：第一个 chapter-like 的 title
    if body_start_idx == -1:
        for i, b in enumerate(blocks):
            if (
                b.block_type == "title"
                and b.text not in cover_title_texts
                and _is_chapter_like(b.text)
                and not _classify_title(b.text)
                and not RE_PAPER_HEADER.match(b.text)
            ):
                body_start_idx = i
                break

    # 3) 按边界切分
    regions: List[Region] = []
    # cover: 0 → min(toc_idx, preface_idx, body_start_idx, appendix_idx, reference_idx)
    # ★ 2026-08-06：必须包含 body_start_idx，否则 cover 区域会一路延伸到
    # preface/appendix 之后，丢失 body 起点。
    # 典型场景：英文论文 body 起点 "PURPOSE" (block 37) 出现在 preface 起点
    # "ACKNOWLEDGMENT" (block 120) 之前，cover_end 必须用 body_start_idx=37 截断。
    cover_end_candidates = [x for x in (toc_idx, preface_idx, body_start_idx, appendix_idx, reference_idx) if x >= 0]
    cover_end = min(cover_end_candidates) if cover_end_candidates else len(blocks)
    if cover_end > 0:
        regions.append(Region("cover", "封面", blocks[:cover_end], start_idx=0))

    # 目录
    if toc_idx >= 0:
        toc_end = preface_idx if preface_idx > toc_idx else (
            body_start_idx if body_start_idx > toc_idx else len(blocks)
        )
        # 2026-08-06: 扩展 toc_end 以包含目录页的"X 标题 ... (页码)"条目
        toc_end = _extend_region_for_toc_entries(blocks, toc_idx, toc_end)
        # 2026-08-06: 如果 body_start_idx 落在 toc 区域里，向后调整。
        # 典型场景：body_start_idx 选了 "1 总则 …… (1)" 这个 TOC 条目，
        # extend 之后 toc 区域扩大了，body 起点必须同步后移。
        if 0 <= body_start_idx < toc_end:
            body_start_idx = toc_end
        # ★ 2026-08-06：用 toc 区域首个 title block 的实际文本作为 region title
        # （中英文兼容：英文论文的 TOC 标题可能是 "CONTENTS" / "TABLE OF CONTENTS"）
        toc_title = blocks[toc_idx].text if blocks[toc_idx].block_type == "title" else "目录"
        regions.append(Region("toc", toc_title, blocks[toc_idx:toc_end], start_idx=toc_idx))

    # 前言
    if preface_idx >= 0:
        # 2026-08-06: pf_end 必须用 toc_idx 截断（不是 body_start_idx）。
        # 否则 toc 区域会重复出现在 preface + body 里。
        if toc_idx > preface_idx:
            pf_end = toc_idx
        elif body_start_idx > preface_idx:
            pf_end = body_start_idx
        else:
            # ★ 2026-08-06：preface 之后的 region 优先级：
            # appendix > reference > 文档末尾。
            # body 在 preface 之前不算（论文的 body 在 ACKNOWLEDGMENT 之前）。
            # 不再用 len(blocks)，否则 "REFERENCES" + tables 都会被吸进 preface。
            next_after_preface_candidates = [x for x in (appendix_idx, reference_idx) if x > preface_idx]
            pf_end = min(next_after_preface_candidates) if next_after_preface_candidates else len(blocks)
        # ★ 2026-08-06：用 preface 区域首个 title block 的实际文本（英文论文可能是 "PREFACE"）
        pf_title = blocks[preface_idx].text if blocks[preface_idx].block_type == "title" else "前言"
        regions.append(Region("preface", pf_title, blocks[preface_idx:pf_end], start_idx=preface_idx))

    # 正文
    if body_start_idx >= 0:
        # ★ 2026-08-06：body 之后的 region 优先级：appendix > reference > preface > 末尾。
        # 中文标准场景：body → appendix → reference
        # 英文论文场景：body (PURPOSE~DISCUSSION) → preface (ACKNOWLEDGMENT) → reference
        # 不能只看 appendix / reference，必须把所有后续 region 都考虑进去。
        body_end_candidates = [x for x in (appendix_idx, reference_idx, preface_idx) if x > body_start_idx]
        body_end = min(body_end_candidates) if body_end_candidates else len(blocks)
        regions.append(Region("body", "", blocks[body_start_idx:body_end], start_idx=body_start_idx))

    # 附录
    if appendix_idx >= 0:
        ap_end = reference_idx if reference_idx > appendix_idx else len(blocks)
        # 2026-08-06: 扩展 appendix_end 以包含附录目录的"X 标题 ... (页码)"条目
        ap_end = _extend_region_for_toc_entries(blocks, appendix_idx, ap_end)
        # ★ 2026-08-06：用 appendix 区域首个 title block 的实际文本（"附录 A" / "APPENDIX A"）
        ap_title = blocks[appendix_idx].text if blocks[appendix_idx].block_type == "title" else "附录"
        regions.append(Region("appendix", ap_title, blocks[appendix_idx:ap_end], start_idx=appendix_idx))

    # 参考文献
    if reference_idx >= 0:
        # ★ 2026-08-06：用 reference 区域首个 title block 的实际文本
        # （英文论文是 "REFERENCES" / "BIBLIOGRAPHY"，中文是 "参考文献"）
        ref_title = blocks[reference_idx].text if blocks[reference_idx].block_type == "title" else "参考文献"
        regions.append(Region("reference", ref_title, blocks[reference_idx:], start_idx=reference_idx))

    # 兜底：什么都没识别到 → 整段作为 single
    if not regions and blocks:
        regions.append(Region("single", "正文", blocks, start_idx=0))

    # ★ 收尾：过滤 preface 区域中与 cover 重复的 title 块。
    #   典型场景：WST 809 之类标准的 PDF，正文首页（或第二页）会重复文档主标题
    #   （如 "基层医疗卫生机构功能单元视觉设计标准"），旧逻辑把它留在 preface 区域里，
    #   导致前言 chunk 末尾出现一条 # 标题。用户期望这条重复应被剔除
    #   —— 主标题属于 cover，body 段另起新 chunk 时不带这条重复。
    if cover_title_texts and len(regions) >= 2:
        cover_region = regions[0]
        if cover_region.region_type == "cover":
            # 在 preface 区域（如果存在）中过滤
            for r in regions[1:]:
                if r.region_type == "preface":
                    r.blocks = [
                        b
                        for b in r.blocks
                        if not (b.block_type == "title" and b.text in cover_title_texts)
                    ]

    return regions


# ============================================================
# Chunk 内容渲染
# ============================================================


def _slugify(text: str, max_len: int = 40) -> str:
    """把标题转成文件名安全的 slug。"""
    # 只保留中日韩英数与常见符号
    s = re.sub(r"[\\/:*?\"<>|\r\n\t]+", "_", text).strip()
    s = re.sub(r"\s+", "_", s)
    if not s:
        s = "chunk"
    if len(s) > max_len:
        s = s[:max_len].rstrip("_")
    return s


# ★ 2026-08-04：HTML 标签剥离（仅用于 _blocks_chars 估算表格字符数）
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_HTML_ENTITIES = {
    "&nbsp;": " ",
    "&amp;": "&",
    "&lt;": "<",
    "&gt;": ">",
    "&quot;": '"',
    "&apos;": "'",
    "&#39;": "'",
}


def _strip_html_tags(html: str) -> str:
    """粗略地把 HTML 标签剥掉，得到可见文本（用于字符数估算 / 切分阈值判断）。"""
    if not html:
        return ""
    # 先解常见 HTML 实体
    s = html
    for ent, ch in _HTML_ENTITIES.items():
        s = s.replace(ent, ch)
    s = _HTML_TAG_RE.sub("", s)
    s = re.sub(r"\s+", " ", s)
    return s.strip()


# ============================================================
# ★ 2026-08-04：切分不可分割区检测
# ============================================================
#
# 业务背景：用户文档中常含「大表格」或「长公式」，切分时不能拆开：
#   - Markdown 表格（`| col | col |\n|---|---|\n...`）：切到中间会破坏列对齐
#   - LaTeX 行内公式（`$...$`）：切到中间会破坏公式完整性
#   - LaTeX 块级公式（`$$...$$`）：同上行内公式
#   - HTML 表格（`<table>...</table>`）：MinerU 解析后通常转为 block_type="table"，
#     不会再进入 _split_by_sentence 流程，但仍可能嵌在 paragraph 文本中
#
# 解决：把 paragraph 文本中所有"不可分割区"找出来，
# _split_by_sentence 在选切分点时跳过这些区内的句末符号（。；!?\n）。

# 匹配 Markdown 表格行（以 | 开头或包含 |----| 等对齐行）
_RE_MD_TABLE_LINE = re.compile(
    r"(?:^|\n)(?:\|[^\n]*\|[ \t]*\n)+"  # 多行表格
    r"(?:\|?[\s:|-]+\|?[ \t]*\n)"  # 分隔行
    r"(?:\|[^\n]*\|[ \t]*\n?)+"  # 后续数据行
)
# 简化版：匹配含 | 且有对齐行的"疑似表格"
_RE_MD_TABLE_INLINE = re.compile(
    r"(?:^|\n)(\|[^\n]+\|)\s*\n(\|?[\s:|-]+\|?)\s*\n((?:\|[^\n]+\|\s*\n?)+)"
)

# LaTeX 行内公式：$...$（不跨行）
_RE_LATEX_INLINE = re.compile(r"\$+([^\$\n]+?)\$+")
# LaTeX 块级公式：$$...$$（可跨行）
_RE_LATEX_BLOCK = re.compile(r"\$\$(.+?)\$\$", re.DOTALL)


def _find_no_split_zones(text: str) -> List[Tuple[int, int]]:
    """找出 text 中所有"切分时不可分割"的区段。

    返回：[(start, end), ...] 列表（end exclusive）
    包含：
      - Markdown 表格区段（含表头、分隔行、数据行）
      - LaTeX 行内公式 $...$
      - LaTeX 块级公式 $$...$$

    调用方在选择切分点时应跳过这些区段内的句末符号。
    """
    zones: List[Tuple[int, int]] = []

    # 1) Markdown 表格
    for m in _RE_MD_TABLE_INLINE.finditer(text):
        zones.append((m.start(), m.end()))

    # 2) LaTeX 块级公式 $$...$$
    for m in _RE_LATEX_BLOCK.finditer(text):
        zones.append((m.start(), m.end()))

    # 3) LaTeX 行内公式 $...$（不含块级公式内部）
    block_zones = [(z[0], z[1]) for z in zones if "$$" in text[z[0]:z[1]]]
    for m in _RE_LATEX_INLINE.finditer(text):
        s, e = m.start(), m.end()
        # 若此行内公式被某个块级公式覆盖，则跳过
        if any(bs <= s and e <= be for bs, be in block_zones):
            continue
        zones.append((s, e))

    # 排序 + 合并重叠
    zones.sort()
    merged: List[Tuple[int, int]] = []
    for s, e in zones:
        if merged and s <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))
    return merged


def _is_in_no_split_zone(pos: int, zones: List[Tuple[int, int]]) -> bool:
    """判断 pos 位置是否处于不可分割区内。"""
    for s, e in zones:
        if s <= pos < e:
            return True
        if pos < s:
            return False
    return False


def _adjust_split_to_safe_position(
    text: str, target_pos: int, zones: List[Tuple[int, int]]
) -> int:
    """调整 target_pos 到最近的"安全"切分点（不在不可分割区内）。

    策略：
      - 若 target_pos 本身在 no_split_zone 外 → 返回 target_pos
      - 否则：向后找到 no_split_zone 结束的位置（最坏情况）
              或向前找到 zone 开始前的最近句末符号（更紧凑）
      - 二者择近：优先向前找最近的句末符号（保持块大小合理）
    """
    if not zones or not _is_in_no_split_zone(target_pos, zones):
        return target_pos

    # 找到 target_pos 所在的 zone
    zone_end = target_pos
    for s, e in zones:
        if s <= target_pos < e:
            zone_end = e
            break

    # 向前找最近的句末符号（优先级：\n > 。 > ； > ! > ?）
    sentence_end_chars = ["\n", "。", "；", "!", "?"]
    for back_pos in range(target_pos - 1, -1, -1):
        if _is_in_no_split_zone(back_pos, zones):
            continue
        if text[back_pos] in sentence_end_chars:
            return back_pos + 1  # 切分点在句末符号之后

    # 找不到合适的向前切分点 → 切到 zone 结束（保证完整性）
    return zone_end


def _block_to_text(b: Block) -> str:
    """把 block 转成 markdown 文本片段。

    ★ 2026-08-04：新增 table 分支
        - 普通表格：保留 HTML 字符串（Markdown 原生支持 <table>，Dify 服务端按 markdown 渲染时
          会保留表格版式），caption 拼在表格上方、footnote 拼在下方作为辅助文本
        - 图片型表格：退化为 `![](images/xxx.jpg)` 语法（同时由 _render_chunk_body 收集到 image_refs）
    """
    if b.block_type == "title":
        level = b.level if isinstance(b.level, int) and 1 <= b.level <= 6 else 2
        return f"{'#' * level} {b.text}\n"
    if b.block_type == "image":
        if b.image_path:
            # cutrule.md §5.1：保留 MD 原生图片语法 `![](images/xxx.jpg)`，
            # 不要把 caption 塞进 alt 文本（避免 alt 文本过长或变成检索噪音）。
            return f"![]({b.image_path})\n"
        return ""
    if b.block_type == "paragraph":
        return f"{b.text}\n"
    if b.block_type == "table":
        # ★ 表格块渲染：caption（可选）+ HTML 表格 + footnote（可选）
        # HTML 表格是 Markdown 的合法子集，GitHub / Dify / VSCode 等渲染器均能正常显示。
        # 直接保留 html 字符串，放弃"HTML→Markdown table"的转换（合并单元格/嵌套表格容易出错）。
        parts: List[str] = []
        if b.table_caption:
            parts.append(f"**{b.table_caption}**\n\n")
        if b.table_image_path:
            # 退化为图片
            parts.append(f"![]({b.table_image_path})\n")
        if b.table_html:
            # 末尾保留一个空行，避免相邻段落粘连
            parts.append(f"{b.table_html}\n\n")
        if b.table_footnote:
            parts.append(f"\n*{b.table_footnote}*\n")
        return "".join(parts)
    return ""


def _render_chunk_body(chunk_title_path: str, blocks: List[Block], is_split: bool = False) -> Tuple[str, List[str]]:
    """渲染 chunk 的 markdown 内容 + 收集图片引用。

    Returns:
        (body_text, image_refs)
    """
    parts: List[str] = []
    parts.append(f"{chunk_title_path}\n")
    if is_split:
        parts.append("\n")  # 二次切分的 chunk 不重复大段内容开头
    image_refs: List[str] = []
    seen = set()

    for b in blocks:
        if b.block_type == "image" and b.image_path:
            rel = b.image_path  # 已经是相对 v2 所在目录的路径 "images/xxx.jpg"
            image_refs.append(rel)
            seen.add(rel)
        elif b.block_type == "table" and b.table_image_path:
            # ★ 2026-08-04：图片型表格的图片路径也要加入 image_refs，让 dify_ingest 能上传到 Dify
            rel = b.table_image_path
            if rel not in seen:
                image_refs.append(rel)
                seen.add(rel)
        parts.append(_block_to_text(b))
        parts.append("\n")

    return "".join(parts).rstrip() + "\n", image_refs


# ============================================================
# 切分算法
# ============================================================


def _blocks_chars(blocks: Sequence[Block]) -> int:
    """累计字符数（不算标题字符，便于和阈值比较）。

    ★ 2026-08-04：表格块也计入字符数（按其可见文本字符数算）
        - HTML 表格用纯文本长度（剥标签）作为估算字符数
        - 这样超长表格能正确触发贪心合并的切分逻辑，避免一整张大表塞进单段
    """
    total = 0
    for b in blocks:
        if b.block_type == "title":
            total += len(b.text)
        elif b.block_type == "paragraph":
            total += len(b.text)
        elif b.block_type == "image":
            # 图片占位符很小，不计入
            total += 0
        elif b.block_type == "table":
            # 表格按可见文本字符数估算（剥掉 HTML 标签）
            visible = _strip_html_tags(b.table_html or "")
            total += len(visible)
    return total


def _segment_text_by_zones(
    text: str, zones: List[Tuple[int, int]]
) -> List[Tuple[bool, str]]:
    """把 text 按 zones 拆分为 (is_atomic, text_segment) 列表。

    - 普通段（is_atomic=False）可以按句末切分
    - 不可分割段（is_atomic=True，表格/公式）必须整体落入单个 chunk

    Args:
        text: 原始文本
        zones: [(start, end), ...] 不可分割区列表（end exclusive）

    Returns:
        [(is_atomic, text_segment), ...] 交替的段列表
    """
    if not zones:
        return [(False, text)] if text else []
    segments: List[Tuple[bool, str]] = []
    cursor = 0
    for s, e in zones:
        if s > cursor:
            segments.append((False, text[cursor:s]))
        segments.append((True, text[s:e]))
        cursor = e
    if cursor < len(text):
        segments.append((False, text[cursor:]))
    return [seg for seg in segments if seg[1]]


def _truncate_overlap_to_safe(
    overlap_text: str, max_len: int
) -> str:
    """overlap 文本如果包含不可分割区，截断到第一个 zone 之前。

    避免下次切分时把表格/公式的开头/中间当 overlap 引入新 chunk。
    """
    if not overlap_text or len(overlap_text) <= max_len:
        return overlap_text
    truncated = overlap_text[-max_len:]
    zones = _find_no_split_zones(truncated)
    if zones:
        first_zone_start = zones[0][0]
        if first_zone_start > 0:
            truncated = truncated[:first_zone_start]
    return truncated


def _split_by_sentence(blocks: List[Block], target: int) -> List[List[Block]]:
    """规则 3.4 情况二：按句号（。）/分号（；）切分。

    每个子块累计字符数 ≈ target；相邻子块有 overlap 字符（重复上段末尾）。
    仅对 paragraph 类做切分；title/image/table 保留在所属子块。

    ★ 2026-08-04 切分策略增强（不破坏表格/公式完整性）：
      - 切分前先把 paragraph 文本拆分为「普通段」和「不可分割段」
      - 不可分割段（Markdown 表格 / LaTeX 公式）必须整体落入单个 chunk
      - 即使不可分割段超过 target 字符（1500），也不切分（业务约束）
      - 仅对普通段按句末符号切分
    """
    if not blocks:
        return []

    # 把 paragraph 切成 (sentence, page_num)；保留 title/image 作为切分点
    out: List[List[Block]] = []
    cur: List[Block] = []
    cur_chars = 0
    overlap_buffer: List[str] = []  # 上一个段落末尾的若干字符

    def flush() -> None:
        nonlocal cur, cur_chars, overlap_buffer
        if not cur:
            return
        out.append(cur)
        # 取最后一个 paragraph 的最后 chunk_overlap 字符
        last_para_text = ""
        for b in reversed(cur):
            if b.block_type == "paragraph":
                last_para_text = b.text
                break
        if last_para_text and settings.chunk_overlap > 0:
            overlap_text = _truncate_overlap_to_safe(
                last_para_text, settings.chunk_overlap
            )
            overlap_buffer = [overlap_text] if overlap_text else []
        else:
            overlap_buffer = []
        cur = []
        cur_chars = 0

    def _flush_and_start_overlap() -> None:
        """flush 当前 chunk 并在新 chunk 起始加 overlap。"""
        nonlocal cur_chars, overlap_buffer
        flush()
        if overlap_buffer:
            cur.append(
                Block(
                    page_num=cur[0].page_num if cur else 1,
                    block_type="paragraph",
                    text=overlap_buffer[0],
                )
            ) if cur else cur.append(
                Block(
                    page_num=1,
                    block_type="paragraph",
                    text=overlap_buffer[0],
                )
            )
            cur_chars += len(overlap_buffer[0])

    def append_regular_text(text: str, page_num: int) -> None:
        """把一段普通文本追加到当前 chunk（按句末切分）。

        ★ 2026-08-06：句末标点同时支持中英文。
        - 中文：。；！？（含全角分号/问号）
        - 英文：. ? ! 后面跟空白或换行才算句末（避免误切 "e.g." / "i.e." / "Dr." / "Mr." 缩写）
        """
        nonlocal cur, cur_chars
        if not text:
            return
        cursor = 0
        starts_new = False
        for m in re.finditer(r"[。；!?\n]|(?<![A-Za-z]\.)[.!?](?=\s|$)", text):
            # 当前 chunk 加上本句就超 target 时，先 flush
            if cur_chars + m.end() - cursor > target and cur:
                flush()
                if overlap_buffer:
                    cur.append(
                        Block(
                            page_num=page_num,
                            block_type="paragraph",
                            text=overlap_buffer[0],
                        )
                    )
                    cur_chars += len(overlap_buffer[0])
                starts_new = True
            # 累计这一句
            if not cur or starts_new:
                starts_new = False
                cur.append(
                    Block(
                        page_num=page_num,
                        block_type="paragraph",
                        text=text[cursor:m.end()],
                    )
                )
            else:
                cur[-1].text += text[cursor:m.end()]
            cur_chars += m.end() - cursor
            cursor = m.end()
        # 剩余
        if cursor < len(text):
            tail = text[cursor:]
            if cur_chars + len(tail) > target and cur:
                flush()
                if overlap_buffer:
                    cur.append(
                        Block(
                            page_num=page_num,
                            block_type="paragraph",
                            text=overlap_buffer[0],
                        )
                    )
                    cur_chars += len(overlap_buffer[0])
            if cur and cur[-1].block_type == "paragraph":
                cur[-1].text += tail
            else:
                cur.append(
                    Block(page_num=page_num, block_type="paragraph", text=tail)
                )
            cur_chars += len(tail)

    def append_atomic_text(text: str, page_num: int) -> None:
        """把一段不可分割文本（表格/公式）整体追加。

        业务约束：即使超过 target 字符也必须整体保留，不切分。
        """
        nonlocal cur, cur_chars
        if not text:
            return
        if cur and cur[-1].block_type == "paragraph":
            cur[-1].text += text
        else:
            cur.append(
                Block(page_num=page_num, block_type="paragraph", text=text)
            )
        cur_chars += len(text)

    for b in blocks:
        if b.block_type == "paragraph":
            txt = b.text
            no_split_zones = _find_no_split_zones(txt)
            segments = _segment_text_by_zones(txt, no_split_zones)
            for is_atomic, seg_text in segments:
                if is_atomic:
                    append_atomic_text(seg_text, b.page_num)
                else:
                    append_regular_text(seg_text, b.page_num)
        else:
            # title / image / table：跟随当前子块（★ table 块本身就不参与 _split_by_sentence）
            cur.append(b)

    flush()
    return out


def _blocks_image_count(blocks: List[Block]) -> int:
    """统计 blocks 中图片块（block_type=='image' 且 image_path 非空）的数量。

    ★ 2026-07-31：用于图片超限切分（cutrule.md 3.5 / 4.3）。
    不去重文件名 —— 同名图被同一段引用多次时按实际出现次数计算，
    因为每次出现都会作为独立图片引用写进 chunk，对 Dify attachment_ids 的消耗也按次数。
    """
    return sum(1 for b in blocks if b.block_type == "image" and b.image_path)


def _greedy_merge_groups(
    groups: List[Tuple[str, List[Block]]],
    threshold: int,
    max_images: int = 0,
) -> List[Tuple[str, List[Block]]]:
    """对 (title, blocks) 列表做贪心合并：累计字符 + 下一个 ≤ threshold 则合并。

    不会跨越组合并——如果单个 group 超过 threshold，原样保留。

    合并后的 title 用 ` ~ ` 拼接；若超过 3 个，压缩为 `first ~ ... ~ last`。

    ★ 2026-07-31：增加 max_images 维度（cutrule.md 3.5 / 4.3）。
    - max_images <= 0 时：只按字符数合并（兼容旧行为）
    - max_images > 0 时：合并条件加一条「累计图片数 + 下一组图片数 ≤ max_images」
      用于保证单段 attachment_ids ≤ Dify 端 SINGLE_CHUNK_ATTACHMENT_LIMIT
    - 单组就超 max_images：原样保留（不强行拆图，避免与 cutrule.md 5.1 冲突）
    """
    if not groups:
        return []
    out: List[Tuple[str, List[Block]]] = []
    buf_titles: List[str] = []
    buf_blocks: List[Block] = []
    buf_chars = 0
    buf_imgs = 0

    def _render_title(titles: List[str]) -> str:
        # 2026-08-06: 过滤 None（l2 退化时 cur2_title=None 表示"无 L2"）
        titles = [t for t in titles if t]
        if not titles:
            return ""
        if len(titles) == 1:
            return titles[0]
        if len(titles) <= 3:
            return " ~ ".join(titles)
        return f"{titles[0]} ~ ... ~ {titles[-1]}"

    def flush() -> None:
        nonlocal buf_titles, buf_blocks, buf_chars, buf_imgs
        if not buf_blocks:
            return
        merged_title = _render_title(buf_titles)
        out.append((merged_title, buf_blocks))
        buf_titles = []
        buf_blocks = []
        buf_chars = 0
        buf_imgs = 0

    for title, sub_blocks in groups:
        sub_chars = _blocks_chars(sub_blocks)
        sub_imgs = _blocks_image_count(sub_blocks)
        if not buf_blocks:
            buf_titles.append(title)
            buf_blocks.extend(sub_blocks)
            buf_chars = sub_chars
            buf_imgs = sub_imgs
            continue
        # 单个超长 → 单独 flush
        if sub_chars > threshold:
            flush()
            out.append((title, sub_blocks))
            continue
        # ★ 2026-07-31：图片超限切分（max_images > 0 时生效）
        # 触发条件：当前 buffer 加上下一组图片数超过上限。
        # 注意：单组已超 max_images 不在此分支（已经在 buffer 中，单组原样保留）
        if max_images > 0 and buf_imgs + sub_imgs > max_images:
            flush()
            buf_titles.append(title)
            buf_blocks.extend(sub_blocks)
            buf_chars = sub_chars
            buf_imgs = sub_imgs
            continue
        if buf_chars + sub_chars <= threshold:
            buf_titles.append(title)
            buf_blocks.extend(sub_blocks)
            buf_chars += sub_chars
            buf_imgs += sub_imgs
        else:
            flush()
            buf_titles.append(title)
            buf_blocks.extend(sub_blocks)
            buf_chars = sub_chars
            buf_imgs = sub_imgs
    flush()
    return out


# ============================================================
# ★ 2026-08-13 表格独立成段辅助函数
# ============================================================


class _TableRowCounter(HTMLParser):
    """计算 HTML 表格中的 <tr> 数量。"""

    def __init__(self):
        super().__init__()
        self.row_count = 0

    def handle_starttag(self, tag, attrs):
        if tag.lower() == "tr":
            self.row_count += 1


def _count_table_rows(html: str) -> int:
    """计算 HTML 表格中的行数（<tr> 数量）。"""
    parser = _TableRowCounter()
    parser.feed(html or "")
    return parser.row_count


def _split_table_html_by_rows(html: str, max_rows: int) -> List[str]:
    """将大表格 HTML 按行数拆分为多个 HTML 片段。

    每个片段保留 <thead>（如有）+ 对应的 <tbody> 行。
    """
    if max_rows <= 0 or _count_table_rows(html) <= max_rows:
        return [html]

    # 提取各部分
    thead_match = re.search(r"<thead[^>]*>.*?</thead>", html, re.DOTALL | re.IGNORECASE)
    thead = thead_match.group(0) if thead_match else ""
    thead_rows = _count_table_rows(thead) if thead else 0

    # 提取所有 <tr>
    tr_pattern = re.compile(r"(<tr[^>]*>.*?</tr>)", re.DOTALL | re.IGNORECASE)
    all_trs = tr_pattern.findall(html or "")
    if not all_trs:
        return [html]

    # body 行 = 总行数 - thead 行数
    body_trs = all_trs[thead_rows:] if thead_rows > 0 else all_trs
    if not body_trs:
        return [html]

    # 提取 <table> 开始标签（含属性）
    table_open_match = re.match(r"(<table[^>]*>)", html, re.IGNORECASE)
    table_open = table_open_match.group(1) if table_open_match else "<table>"

    # 按 max_rows 拆分 body 行
    parts: List[str] = []
    for i in range(0, len(body_trs), max_rows):
        chunk_trs = body_trs[i:i + max_rows]
        part = table_open + "\n"
        if thead:
            part += thead + "\n"
        part += "\n".join(chunk_trs)
        part += "\n</table>"
        parts.append(part)

    return parts


def _extract_tables_from_blocks(
    blocks: List[Block],
) -> Tuple[List[Block], List[Block]]:
    """将 blocks 分为 (non_table_blocks, table_blocks)，保持各自顺序。"""
    non_table: List[Block] = []
    tables: List[Block] = []
    for b in blocks:
        if b.block_type == "table":
            tables.append(b)
        else:
            non_table.append(b)
    return non_table, tables


def _make_table_chunks(
    table_blocks: List[Block],
    title_path: str,
    l1_title: str,
) -> List[Chunk]:
    """为表格 block 列表创建独立 chunk。

    - 每个表格独立成段
    - 表名标注在内容前
    - 大表格（行数 > chunk_table_row_threshold）自动拆分为多段
    """
    out: List[Chunk] = []
    row_threshold = settings.chunk_table_row_threshold

    for tblock in table_blocks:
        caption = tblock.table_caption or ""
        # 表名：取 caption 的前缀（如 "表2"、"表 2 流程图"）
        table_name = caption.strip() if caption else ""

        # 图片型表格（无 HTML 内容时才走图片分支，优先保留文本利于 RAG 检索）
        if not tblock.table_html and tblock.table_image_path:
            parts = [f"{title_path}\n"]
            if table_name:
                parts.append(f"\n**{table_name}**\n\n")
            parts.append(f"![]({tblock.table_image_path})\n")
            if tblock.table_footnote:
                parts.append(f"\n*{tblock.table_footnote}*\n")
            body = "".join(parts).rstrip() + "\n"
            out.append(Chunk(
                chunk_id="", file_name="", title_path=title_path,
                chunk_type="table", char_count=len(body),
                body=body,
                image_refs=[tblock.table_image_path],
                _level1=l1_title,
                table_name=table_name or None,
                table_type="table",
            ))
            continue

        # HTML 型表格（优先文本渲染，如有图片也保留到 image_refs 供 Dify 附件）
        html = tblock.table_html or ""
        if not html:
            continue
        # 收集图片引用（表格可能同时有 image_source）
        tbl_image_refs: List[str] = []
        if tblock.table_image_path:
            tbl_image_refs.append(tblock.table_image_path)

        row_count = _count_table_rows(html)
        # ★ 2026-08-20：除行数外再按字符数兜底——少数行但超长单元格的表格
        #   （如 WS 628-2 附录 B：3 行 11662 字符）行数阈值不触发，
        #   整表超大会被 Dify 静默丢弃。可见文本超过 chunk_table_max_chars
        #   时按字符预算反推每片最大行数再拆分。
        visible_chars = len(_strip_html_tags(html))
        table_max_chars = settings.chunk_table_max_chars
        need_split = row_count > row_threshold or (
            table_max_chars > 0 and visible_chars > table_max_chars
        )
        if need_split:
            # 大表格拆分
            split_rows = row_threshold
            if row_count <= row_threshold and table_max_chars > 0 and visible_chars > table_max_chars:
                # 行数少但字符超长：让每片 ≈ table_max_chars（至少 1 行）
                split_rows = max(1, int(row_count * table_max_chars / visible_chars))
                if split_rows >= row_count:
                    split_rows = max(1, row_count - 1)
            parts_list = _split_table_html_by_rows(html, split_rows)
            total_parts = len(parts_list)
            for idx, part_html in enumerate(parts_list, start=1):
                part_name = f"{table_name} (part {idx}/{total_parts})" if total_parts > 1 else table_name
                cparts = [f"{title_path}\n"]
                if part_name:
                    cparts.append(f"\n**{part_name}**\n\n")
                cparts.append(f"{part_html}\n")
                if idx == total_parts and tblock.table_footnote:
                    cparts.append(f"\n*{tblock.table_footnote}*\n")
                body = "".join(cparts).rstrip() + "\n"
                out.append(Chunk(
                    chunk_id="", file_name="", title_path=title_path,
                    chunk_type="table", char_count=len(body),
                    body=body, image_refs=list(tbl_image_refs),
                    _level1=l1_title,
                    table_name=part_name or None,
                    table_type="table_part",
                ))
        else:
            # 普通表格
            cparts = [f"{title_path}\n"]
            if table_name:
                cparts.append(f"\n**{table_name}**\n\n")
            cparts.append(f"{html}\n")
            if tblock.table_footnote:
                cparts.append(f"\n*{tblock.table_footnote}*\n")
            body = "".join(cparts).rstrip() + "\n"
            out.append(Chunk(
                chunk_id="", file_name="", title_path=title_path,
                chunk_type="table", char_count=len(body),
                body=body, image_refs=list(tbl_image_refs),
                _level1=l1_title,
                table_name=table_name or None,
                table_type="table",
            ))

    return out


def chunk_body(region: Region) -> List[Chunk]:
    """正文区域切分：1级 → 贪心合并 2级 → 贪心合并 3级 → 句号切。

    遵循 cutrule.md 规则 3.1~3.4。
    """
    blocks = region.blocks
    if not blocks:
        return []
    target = settings.chunk_target_chars

    # 1) 按 level-1 切分（v2 有 level 时直接用；否则按 RE_CHAPTER/RE_NUMERIC_TITLE 推断）
    l1_groups: List[Tuple[str, List[Block], int]] = []  # (title, blocks, page_of_title)
    cur_title = ""
    cur_blocks: List[Block] = []
    cur_page = 1

    def flush_l1() -> None:
        nonlocal cur_title, cur_blocks, cur_page
        if cur_blocks or cur_title:
            l1_groups.append((cur_title, cur_blocks, cur_page))
        cur_title = ""
        cur_blocks = []
        cur_page = 1

    for b in blocks:
        if b.block_type == "title":
            # 2026-08-06 修复：排除 v2 误识别为 L1 的"长文子项"。
            # 这些 title 实际是 L3（"X.Y.Z"）的子项 1) 2) ... 描述，
            # 不应该被当作 L1 章节切分。
            eff_lvl = _effective_level(b)
            is_l1 = (
                (eff_lvl == 1 and _looks_like_l1_title(b.text))
                # ★ 2026-08-06：英文论文 v2 把章节标为 L2（如 "PURPOSE" / "INTRODUCTION"），
                # 但语义是 L1 章节。需要把它们也作为 L1 切分点。
                or (eff_lvl == 2 and RE_ENGLISH_L1_HEADING.match(b.text))
            )
            if is_l1:
                flush_l1()
                cur_title = b.text
                cur_page = b.page_num
                continue
        cur_blocks.append(b)
    flush_l1()

    chunks: List[Chunk] = []
    max_images = settings.chunk_max_images_per_segment
    for l1_title, l1_blocks, l1_page in l1_groups:
        if not l1_blocks and not l1_title:
            continue

        # ★ 2026-08-13：表格独立成段——提取表格 block，单独成 chunk
        remaining_blocks, table_blocks = _extract_tables_from_blocks(l1_blocks)
        tp_for_table = l1_title or "正文"
        table_chunks = _make_table_chunks(table_blocks, tp_for_table, l1_title)

        # 没有文本 block 时只输出表格 chunk
        if not remaining_blocks:
            chunks.extend(table_chunks)
            continue

        total = _blocks_chars(remaining_blocks)
        # ★ 2026-07-31：图片超限切分前置（cutrule.md 3.5）
        # 即使 l1 段总字符 ≤ 1500，但图片数 > max_images，也必须进入
        # l2/l3 拆分段流程（避免单段 10+ 图触发 Dify 400）。
        img_count = _blocks_image_count(remaining_blocks)
        if total <= target and img_count <= max_images:
            # 整体作为 1 段
            chunks.append(
                Chunk(
                    chunk_id="",
                    file_name="",
                    title_path=l1_title or "正文",
                    chunk_type="body",
                    char_count=total,
                    body="",
                    _level1=l1_title,
                )
            )
            chunks[-1].body, chunks[-1].image_refs = _render_chunk_body(
                chunks[-1].title_path,
                remaining_blocks,
            )
            chunks.extend(table_chunks)
            continue

        # 按 level-2 切分
        l2_groups: List[Tuple[str, List[Block]]] = []
        cur2_title: Optional[str] = None  # 2026-08-06: 初始 None，区别于 l1_title
        cur2_blocks: List[Block] = []

        def flush_l2() -> None:
            nonlocal cur2_title, cur2_blocks
            if cur2_blocks:
                # 2026-08-06: 如果 cur2_title 还是 None，说明本组内没有 L2 标题，
                # 这种"无 L2"的组 title 用 l1_title（与 l1_title 拼接时会去重）。
                l2_groups.append((cur2_title, cur2_blocks))
            cur2_blocks = []

        for b in remaining_blocks:
            if b.block_type == "title":
                if _effective_level(b) == 2:
                    flush_l2()
                    cur2_title = b.text
                    continue
            cur2_blocks.append(b)
        flush_l2()
        if not l2_groups:
            # 2026-08-06: 整段作为 1 个 L2 group，cur2_title=None 标记“无 L2”，
            # title_path 拼接时跳过 L2 避免与 L1 重复。
            l2_groups = [(None, remaining_blocks)]

        # 贪心合并 level-2
        # ★ 2026-07-31：传 max_images 让图片超限触发切分（cutrule.md 3.5）
        merged_l2 = _greedy_merge_groups(
            l2_groups, target, max_images=settings.chunk_max_images_per_segment
        )

        for l2_merged_title, l2_blocks_sub in merged_l2:
            sub_total = _blocks_chars(l2_blocks_sub)
            # ★ 2026-07-31：l2 合并后图片超限也需继续走 l3 切分（cutrule.md 3.5）
            sub_imgs = _blocks_image_count(l2_blocks_sub)

            # 2026-08-06: 先扫描 l2 段内是否有 L3 标题，决定后续切分路径。
            # 即使 sub_total <= target，只要存在 L3 标题，也必须按 L3 贪心合并
            # （避免"2 术语"15 条 L3 全塞 1 段、title_path 被压缩成 ~ ~ ~）。
            l3_groups: List[Tuple[str, List[Block]]] = []
            cur3_title = l2_merged_title
            cur3_blocks: List[Block] = []
            has_l3 = False
            for _b in l2_blocks_sub:
                if _b.block_type == "title" and _effective_level(_b) == 3:
                    has_l3 = True
                    break

            if not has_l3 and sub_total <= target and sub_imgs <= max_images:
                # 整体作为 1 段（无 L3 标题，且不超阈值）
                # 2026-08-06: l2_merged_title 为 None 时跳过 L2，避免与 L1 重复
                if l2_merged_title is None or l2_merged_title == l1_title:
                    tp = l1_title or "正文"
                else:
                    tp = f"{l1_title} > {l2_merged_title}" if l1_title else l2_merged_title
                body, refs = _render_chunk_body(tp, l2_blocks_sub)
                chunks.append(
                    Chunk(
                        chunk_id="",
                        file_name="",
                        title_path=tp,
                        chunk_type="body",
                        char_count=sub_total,
                        body=body,
                        image_refs=refs,
                        _level1=l1_title,
                        _level2=l2_merged_title,
                    )
                )
                continue

            # 单个二级组超长 或 存在 L3 标题 → 按三级（如果有）贪心合并，否则按句号切
            l3_groups = []
            cur3_title = l2_merged_title
            cur3_blocks = []
            has_l3 = False

            def flush_l3() -> None:
                nonlocal cur3_title, cur3_blocks
                if cur3_blocks:
                    l3_groups.append((cur3_title, cur3_blocks))
                cur3_blocks = []

            for b in l2_blocks_sub:
                if b.block_type == "title":
                    if _effective_level(b) == 3:
                        has_l3 = True
                        flush_l3()
                        cur3_title = b.text
                        continue
                cur3_blocks.append(b)
            flush_l3()
            if not l3_groups:
                l3_groups = [(l2_merged_title, l2_blocks_sub)]

            if has_l3:
                # ★ 2026-07-31：传 max_images（cutrule.md 3.5）
                merged_l3 = _greedy_merge_groups(
                    l3_groups, target, max_images=settings.chunk_max_images_per_segment
                )
                for l3_t, l3_b in merged_l3:
                    # 2026-08-06: l2_merged_title 为 None 或与 l1_title 重复时跳过 L2
                    if l2_merged_title is None or l2_merged_title == l1_title:
                        tp = (
                            f"{l1_title} > {l3_t}" if l1_title else l3_t
                        )
                    else:
                        tp = (
                            f"{l1_title} > {l2_merged_title} > {l3_t}"
                            if l1_title
                            else f"{l2_merged_title} > {l3_t}"
                        )
                    # ★ 2026-07-31 修复：之前用 `_` 丢了 refs，导致后续
                    # dify_ingest 看不到 image_refs，预览/上传图全丢。
                    body, l3_refs = _render_chunk_body(tp, l3_b)
                    chunks.append(
                        Chunk(
                            chunk_id="",
                            file_name="",
                            title_path=tp,
                            chunk_type="body",
                            char_count=_blocks_chars(l3_b),
                            body=body,
                            image_refs=l3_refs,
                            _level1=l1_title,
                            _level2=l2_merged_title,
                        )
                    )
            else:
                # 按句号切
                sub_chunks_blocks = _split_by_sentence(l2_blocks_sub, settings.chunk_split_target)
                for i, sub_blocks in enumerate(sub_chunks_blocks, start=1):
                    suffix = f" (part {i})" if len(sub_chunks_blocks) > 1 else ""
                    tp = (
                        f"{l1_title} > {l2_merged_title}{suffix}"
                        if l1_title
                        else f"{l2_merged_title}{suffix}"
                    )
                    body, _ = _render_chunk_body(tp, sub_blocks, is_split=len(sub_chunks_blocks) > 1)
                    chunks.append(
                        Chunk(
                            chunk_id="",
                            file_name="",
                            title_path=tp,
                            chunk_type="body",
                            char_count=_blocks_chars(sub_blocks),
                            is_split=len(sub_chunks_blocks) > 1,
                            body=body,
                            _level1=l1_title,
                            _level2=l2_merged_title,
                        )
                    )
            # ★ 2026-08-13：追加当前 L1 组的表格 chunk
            chunks.extend(table_chunks)
    return chunks


def chunk_simple(region: Region) -> List[Chunk]:
    """封面/目录/前言：整体为 1 段（超长按句号切）。"""
    blocks = region.blocks
    if not blocks:
        return []
    title = region.title_path
    tp = title
    total = _blocks_chars(blocks)
    if total <= settings.chunk_target_chars:
        body, refs = _render_chunk_body(tp, blocks)
        return [
            Chunk(
                chunk_id="",
                file_name="",
                title_path=tp,
                chunk_type=region.region_type,
                char_count=total,
                body=body,
                image_refs=refs,
            )
        ]
    # 超长 → 句号切
    sub_blocks_list = _split_by_sentence(blocks, settings.chunk_split_target)
    out: List[Chunk] = []
    for i, sub in enumerate(sub_blocks_list, start=1):
        sub_tp = f"{title} (part {i})" if len(sub_blocks_list) > 1 else title
        body, refs = _render_chunk_body(sub_tp, sub, is_split=len(sub_blocks_list) > 1)
        out.append(
            Chunk(
                chunk_id="",
                file_name="",
                title_path=sub_tp,
                chunk_type=region.region_type,
                char_count=_blocks_chars(sub),
                is_split=len(sub_blocks_list) > 1,
                body=body,
                image_refs=refs,
            )
        )
    return out


def chunk_reference(region: Region) -> List[Chunk]:
    """参考文献区域切分。

    ★ 2026-08-06：按 block 边界贪心合并（每条 reference 一个 block），
    而不是按句号切。理由：
    - v2 把 reference list 的 list_items 展开为独立的 paragraph block（每条一条），
      按句号切会把同一条 reference 切断（每条 reference 末尾是 . 但中间也有缩写 e.g.）
    - 参考文献总长通常 > 1500，按句号切会产生大量碎片（典型：29 段 1100 字符/段）

    切分策略：
    - 每个 block（reference 条目 / table / title）作为一个 group
    - 贪心合并 group：累计字符数 ≤ settings.chunk_target_chars (1500) 且
      图片数 ≤ settings.chunk_max_images_per_segment (10)
    - 单条超 target：原样保留为独立段（不强行拆）
    - 多于 1 段时给 title_path 加 " (part i)" 编号
    - ★ 2026-08-06：用 region.title_path 作为 fallback（保留原文 REFERENCES / 参考文献）
    """
    blocks = region.blocks
    if not blocks:
        return []
    target = settings.chunk_target_chars
    max_images = settings.chunk_max_images_per_segment

    # 用 region 标题作为 fallback（英文论文是 "REFERENCES" / 中文是 "参考文献"）
    default_title = region.title_path or "参考文献"

    # 把每个 block 当一个 group
    groups: List[Tuple[str, List[Block]]] = []
    for b in blocks:
        if b.block_type == "title":
            groups.append((b.text, [b]))
        else:
            groups.append(("", [b]))

    merged = _greedy_merge_groups(groups, target, max_images=max_images)

    out: List[Chunk] = []
    n = len(merged)
    for title, sub in merged:
        if title:
            # group 里包含 REFERENCES / BIBLIOGRAPHY 标题
            tp = title
        elif n == 1:
            tp = default_title
        else:
            tp = f"{default_title} (part {len(out)+1})"
        body, refs = _render_chunk_body(tp, sub, is_split=n > 1)
        out.append(
            Chunk(
                chunk_id="",
                file_name="",
                title_path=tp,
                chunk_type="reference",
                char_count=_blocks_chars(sub),
                is_split=n > 1,
                body=body,
                image_refs=refs,
            )
        )
    return out


def chunk_appendix(region: Region) -> List[Chunk]:
    """附录切分：每个附录独立成段，不贪心合并。

    ★ 2026-08-13：取消贪心合并——附录内容较大且语义独立，
    合并会破坏上下文完整性。改为每个附录单独成段。
    单附录超长时按子标题拆分（保证语义完整），兜底按句号切。
    """
    blocks = region.blocks
    if not blocks:
        return []
    target = settings.chunk_appendix_threshold
    max_images = settings.chunk_max_images_per_segment

    # 按 appendix 标题分组（每个附录一组，不合并）
    groups: List[Tuple[str, List[Block]]] = []
    cur_title = ""
    cur_blocks: List[Block] = []

    def flush() -> None:
        nonlocal cur_title, cur_blocks
        if cur_blocks:
            groups.append((cur_title, cur_blocks))
        cur_title = ""
        cur_blocks = []

    for b in blocks:
        if b.block_type == "title" and (RE_APPENDIX.match(b.text) or _classify_title(b.text) == "appendix_start"):
            flush()
            cur_title = b.text
            continue
        cur_blocks.append(b)
    flush()
    if not groups:
        groups = [(region.title_path, blocks)]

    out: List[Chunk] = []
    for title, sub in groups:
        tp = title or region.title_path
        total = _blocks_chars(sub)
        imgs = _blocks_image_count(sub)

        if total <= target and imgs <= max_images:
            # 整个附录作为 1 段
            body, refs = _render_chunk_body(tp, sub)
            out.append(
                Chunk(
                    chunk_id="", file_name="", title_path=tp,
                    chunk_type="appendix", char_count=total,
                    body=body, image_refs=refs,
                )
            )
        else:
            # 单附录超长——先尝试按内部标题拆分（保证语义完整）
            sub_groups: List[Tuple[str, List[Block]]] = []
            has_sub_title = False
            cur_st = ""
            cur_sb: List[Block] = []

            def flush_sub() -> None:
                nonlocal cur_st, cur_sb
                if cur_sb:
                    sub_groups.append((cur_st, cur_sb))
                cur_st = ""
                cur_sb = []

            for b in sub:
                if b.block_type == "title" and _effective_level(b) >= 2:
                    has_sub_title = True
                    flush_sub()
                    cur_st = b.text
                    continue
                cur_sb.append(b)
            flush_sub()

            if has_sub_title and sub_groups:
                # 按子标题贪心合并（同层级，双约束）
                merged_sub = _greedy_merge_groups(
                    sub_groups, target, max_images=max_images
                )
                for st, sb in merged_sub:
                    sub_tp = f"{tp} > {st}" if st else tp
                    body, refs = _render_chunk_body(sub_tp, sb)
                    out.append(
                        Chunk(
                            chunk_id="", file_name="", title_path=sub_tp,
                            chunk_type="appendix", char_count=_blocks_chars(sb),
                            body=body, image_refs=refs,
                        )
                    )
            else:
                # 无子标题——按句号切（兜底）
                sub_list = _split_by_sentence(sub, settings.chunk_split_target)
                for i, s in enumerate(sub_list, start=1):
                    sub_tp = f"{tp} (part {i})" if len(sub_list) > 1 else tp
                    body, refs = _render_chunk_body(sub_tp, s, is_split=len(sub_list) > 1)
                    out.append(
                        Chunk(
                            chunk_id="", file_name="", title_path=sub_tp,
                            chunk_type="appendix", char_count=_blocks_chars(s),
                            is_split=len(sub_list) > 1,
                            body=body, image_refs=refs,
                        )
                    )
    return out


# ============================================================
# 写盘
# ============================================================


def _copy_referenced_images(
    images_src: Optional[Path],
    chunks_dir: Path,
    refs: Iterable[str],
) -> int:
    """把被引用的图片从 parsed/images/ 拷贝到 chunks/{stem}/images/，去重。

    Returns:
        拷贝成功的图片数。
    """
    if not images_src or not images_src.exists():
        return 0
    target = chunks_dir / "images"
    target.mkdir(parents=True, exist_ok=True)
    seen = set()
    copied = 0
    for rel in refs:
        if not rel:
            continue
        # rel 形如 "images/xxx.jpg" 或纯文件名
        rel_clean = rel.replace("\\", "/")
        if rel_clean in seen:
            continue
        seen.add(rel_clean)

        # 来源：v2 中是相对 v2 所在目录的路径；先在 images_src 直接找，再尝试 rel basename
        candidates = [
            images_src / Path(rel_clean).name,
            images_src / rel_clean,
        ]
        # 兜底：递归找
        if not any(c.exists() for c in candidates):
            matches = list(images_src.rglob(Path(rel_clean).name))
            if matches:
                candidates.append(matches[0])

        for cand in candidates:
            if cand.is_file():
                dst = target / cand.name
                if dst.exists():
                    try:
                        if dst.stat().st_size == cand.stat().st_size:
                            copied += 1
                            break
                    except OSError:
                        pass
                shutil.copy2(cand, dst)
                copied += 1
                break
    return copied


def write_chunks(
    chunks_dir: Path,
    chunks: List[Chunk],
    images_src: Optional[Path],
) -> Tuple[List[Chunk], int, int]:
    """把 chunks 写入 chunks_dir/，分配 chunk_id 与 file_name。

    Returns:
        (chunks, total_image_refs, copied_image_count)
    """
    chunks_dir.mkdir(parents=True, exist_ok=True)
    total_refs: List[str] = []
    for idx, c in enumerate(chunks, start=1):
        c.chunk_id = f"chunk_{idx:03d}"
        slug = _slugify(c.title_path)
        c.file_name = f"{c.chunk_id}_{slug}.md"
        (chunks_dir / c.file_name).write_text(c.body, encoding="utf-8")
        total_refs.extend(c.image_refs)
    copied = _copy_referenced_images(images_src, chunks_dir, total_refs)
    return chunks, len(total_refs), copied


def write_chunk_metadata(
    chunks_dir: Path,
    chunks: List[Chunk],
    doc_stem: str,
    strategy: str = "",
) -> Path:
    """写 chunk_metadata.json。"""
    items: List[Dict[str, Any]] = []
    for c in chunks:
        item: Dict[str, Any] = {
            "chunk_id": c.chunk_id,
            "file_name": c.file_name,
            "title_path": c.title_path,
            "chunk_type": c.chunk_type,
            "char_count": c.char_count,
            "image_refs": c.image_refs,
            "is_split": c.is_split,
        }
        # ★ 2026-08-13：表格独立成段元数据
        if c.chunk_type == "table":
            item["table_name"] = c.table_name
            item["table_type"] = c.table_type
        # ★ 2026-08-24：多策略切分 — 记录父块关联
        if c.parent_id:
            item["parent_id"] = c.parent_id
        items.append(item)
    payload = {
        "doc_stem": doc_stem,
        "chunk_count": len(chunks),
        "strategy": strategy or "structure",
        "chunks": items,
    }
    out = chunks_dir / "chunk_metadata.json"
    out.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return out


# ============================================================
# 主入口：单个文档切分
# ============================================================


@dataclass
class ChunkResult:
    stem: str
    chunks_dir: Path
    chunks: List[Chunk]
    image_count: int
    total_chars: int


def chunk_document(
    parsed_dir: Path,
    chunks_dir: Path,
    strategy: str = "",
) -> ChunkResult:
    """对单个文档执行切分。返回 ChunkResult。

    Args:
        strategy: 切分策略（structure/recursive/fixed/sentence/semantic/
                  parent_child/late_chunking/llm）。空 → 使用配置默认。

    Raises:
        FileNotFoundError: v2 或 md 都不存在
    """
    v2_path, md_path, images_src = locate_parsed_files(parsed_dir)
    if v2_path is None and md_path is None:
        raise FileNotFoundError(f"在 {parsed_dir} 找不到 v2 或 md 文件")

    if v2_path is not None:
        blocks = load_v2_blocks(v2_path)
    else:
        # 极端兜底：没有 v2，只把 md 按整段作为单个 paragraph
        blocks = [
            Block(
                page_num=1,
                block_type="paragraph",
                text=(md_path.read_text(encoding="utf-8") if md_path else "").strip(),
            )
        ]

    regions = classify_regions(blocks)

    # ★ 2026-08-06：按文档原始顺序输出 regions。
    # 英文论文场景：preface (ACKNOWLEDGMENT) 可能出现在 body (PURPOSE~DISCUSSION) 之后，
    # regions 的构造顺序固定为 cover→toc→preface→body→appendix→reference，
    # 会导致 chunk 顺序与文档顺序不一致。这里按 start_idx 排序。
    regions.sort(key=lambda r: r.start_idx)

    # ★ 2026-08-24：多策略切分引擎。非默认策略在 chunk_strategies 内路由；
    # structure（默认）走原有 chunk_body/chunk_appendix/chunk_reference/chunk_simple。
    from app.services.chunk_strategies import chunk_region_with_strategy, normalize_strategy

    strategy = normalize_strategy(strategy)
    all_chunks: List[Chunk] = []
    for r in regions:
        all_chunks.extend(chunk_region_with_strategy(r, strategy))

    stem = parsed_dir.name
    chunks, image_refs_count, copied = write_chunks(chunks_dir, all_chunks, images_src)
    write_chunk_metadata(chunks_dir, chunks, stem, strategy=strategy)
    total_chars = sum(c.char_count for c in chunks)
    return ChunkResult(
        stem=stem,
        chunks_dir=chunks_dir,
        chunks=chunks,
        image_count=copied,
        total_chars=total_chars,
    )


# ============================================================
# 管线入口：遍历 manifest
# ============================================================


def _is_already_chunked(row: ManifestRow) -> bool:
    # ★ 2026-08 修复（manifest status 被倒退）：
    #   chunks 列存在两种合法形式：
    #     1) "{stem}"             → 文件在 data/chunks/{stem}/（仅完成 §3.3 时）
    #     2) "output/{stem}"      → dify 入库成功后，chunks 被移到 data/output/，列被同步更新
    #   之前只看 chunks 非空，路径又只用 settings.chunks_dir / row.chunks 拼，
    #   会把 output/ 形式错拼成 data/chunks/output/{stem}（不存在）→ 误判未切分 →
    #   重新跑切分 → status 从 'done' 倒退成 'chunked'，chunks 列从 'output/...' 倒退成 '{stem}'。
    #   现在把"实际是否已切分"判定改为"对应目录存在且有 chunk_*.md"。
    return _resolve_chunks_dir_from_manifest(row) is not None


def _resolve_chunks_dir_from_manifest(row: ManifestRow) -> Optional[Path]:
    """根据 manifest.row.chunks 解析出当前实际存放切分产物的目录。

    返回：
      - None：chunks 列为空 / 对应目录不存在 / 目录里没有 chunk_*.md
      - Path：当前实际有 chunk_*.md 的目录（data/chunks/... 或 data/output/...）

    支持两种 chunks 列形式：
      - "{stem}"             → 候选 data/chunks/{stem}/
      - "output/{stem}"      → 候选 data/output/{stem}/
    """
    text = (row.chunks or "").strip()
    if not text:
        return None
    text = text.replace("\\", "/").rstrip("/")
    # 去掉 "output/" 前缀（兼容 dify_ingest 写入的形式）
    if text.startswith("output/"):
        stem = text[len("output/"):].strip()
        candidate = settings.output_dir / stem
    else:
        # 旧形式：纯 stem，文件在 data/chunks/{stem}/
        candidate = settings.chunks_dir / text
    if _is_chunks_dir_valid(candidate):
        return candidate
    return None


def _is_chunks_dir_valid(chunks_dir: Path) -> bool:
    if not chunks_dir.is_dir():
        return False
    return any(chunks_dir.glob("chunk_*.md"))


def _strip_parse_qualifier_suffix(stem: str) -> str:
    """去掉 PyMuPDF fallback 在 stem 上追加的诊断后缀。

    示例：
      "济宁...办法(1) [vlm-image-fallback 修复]" → "济宁...办法(1)"
      "name [pymupdf-fallback 修复]"             → "name"
      "name(v2)"                                  → "name"
      "name"                                      → "name"   (无变化)
    """
    # 顺序：先剥 fallback 后缀（最右的 [...]），再剥末尾的 "(N)" 数字编号
    s = re.sub(r"\s*\[[^\]]*fallback[^\]]*\]\s*$", "", stem).strip()
    s = re.sub(r"\s*\[[^\]]*修复\]\s*$", "", s).strip()
    return s


def _resolve_parsed_dir(
    desired: str,
    parsed_root: Path,
) -> Tuple[Optional[Path], Optional[str]]:
    """在 parsed_root 下找实际解析目录，支持 stem 模糊匹配。

    优先级：
      1) 精确路径（绝对或相对）存在 → 直接返回
      2) desired 包含 fallback 诊断后缀 → 去掉后缀后再精确匹配
      3) 不带后缀的 stem 与 parsed_root 下任一子目录同名 → 返回该目录
      4) 不带后缀的 stem 去掉末尾 "(N)" 编号再匹配（处理类似 "name(1)" vs "name"）
      5) 仍找不到 → 返回 (None, 原始 desired) 让上层报错

    Returns:
        (resolved_path, effective_stem)
        - resolved_path=None 表示未找到
        - effective_stem 是去除后缀后的 stem（用于同步 manifest.parse）
    """
    if not desired:
        return None, None
    desired_path = Path(desired)

    # 1) 精确匹配
    if not desired_path.is_absolute():
        candidate = parsed_root / desired_path
    else:
        candidate = desired_path
    if candidate.is_dir():
        return candidate, candidate.name

    # 2) 去掉 fallback 后缀再匹配
    base = _strip_parse_qualifier_suffix(desired_path.name)
    if base != desired_path.name:
        candidate2 = parsed_root / base
        if candidate2.is_dir():
            return candidate2, base

    # 3) 在 parsed_root 下找同名子目录
    if not parsed_root.exists():
        return None, base
    exact_child = parsed_root / base
    if exact_child.is_dir():
        return exact_child, base

    # 4) 去掉末尾 "(N)" 编号再匹配（如 "name(1)" → "name"）
    base_no_num = re.sub(r"\s*\(\d+\)\s*$", "", base).strip()
    if base_no_num and base_no_num != base:
        candidate3 = parsed_root / base_no_num
        if candidate3.is_dir():
            return candidate3, base_no_num

    # 5) stem 前缀匹配：找 parsed_root 下以 base 去括号后开头的目录（处理截断的 stem）
    base_clean = re.sub(r"[\s()\[\]（）【】]+", "", base)
    if base_clean and len(base_clean) >= 6:
        for child in parsed_root.iterdir():
            if not child.is_dir():
                continue
            child_clean = re.sub(r"[\s()\[\]（）【】]+", "", child.name)
            # 双向包含（防前缀误命中）：取较长的前 70% 比较
            shorter, longer = (
                (base_clean, child_clean) if len(base_clean) <= len(child_clean) else (child_clean, base_clean)
            )
            if len(shorter) >= 6 and longer.startswith(shorter[: max(6, int(len(shorter) * 0.7))]):
                return child, child.name

    return None, base


def _write_manifest_chunk_done(
    row: ManifestRow,
    *,
    chunks_text: str,
    sys_status: str,
    err: Optional[str] = None,
) -> None:
    now = manifest_store.now_iso()
    update_kwargs: Dict[str, object] = {
        "filename": row.filename,
        "chunks": chunks_text,
        "update_time": now,
    }
    if sys_status:
        update_kwargs["status"] = sys_status
    update_kwargs["error_msg"] = err
    new_row = row.model_copy(update=update_kwargs)
    manifest_store.upsert(new_row)


def chunk_parsed(
    dry_run: bool = False,
    force: bool = False,
    target_stems: Optional[List[str]] = None,
    strategy: str = "",
) -> ChunkReport:
    """§3.3 主入口：遍历 manifest，对解析成功（parse 列非空）的文档执行切分。

    ★ 2026-08 新增 target_stems 白名单（单文件上传 + 一键入库）：
        - target_stems=None（默认）：处理所有 parse 列非空的行
        - target_stems=[stem1, stem2, ...]：只处理这些 stem 对应的行
          用于「单文件上传 + 一键入库」场景——只切分这一个文件，
          不应该处理 manifest 里其他已解析的文档（那些需要走完整清单流程）。

    ★ 2026-08-24 多策略切分：strategy 指定切分策略
      （structure/recursive/fixed/sentence/semantic/parent_child/late_chunking/llm），
      空 → 使用 settings.chunk_strategy。
    """
    started = time.perf_counter()
    log.info(
        "chunk started",
        extra={"step": "chunk", "status": "start", "dry_run": dry_run, "force": force,
               "target_stems": target_stems, "strategy": strategy},
    )
    settings.ensure_dirs()
    manifest_store.bootstrap(settings.data_root)
    manifest: Dict[str, ManifestRow] = manifest_store.load()

    # ★ target_stems 白名单：转 set 提高查找效率
    target_stem_set: Optional[set] = (
        set(target_stems) if target_stems is not None else None
    )

    actions: List[ChunkActionRecord] = []
    chunked_count = skipped_count = failed_count = 0

    for fname, row in manifest.items():
        t0 = time.perf_counter()

        # ★ 0) target_stems 白名单过滤：白名单外的行直接跳过
        if target_stem_set is not None:
            row_stem = Path(fname).stem
            if row_stem not in target_stem_set:
                continue

        # 1) 必须先有 parse 列（解析未完成 → 跳过）
        if not (row.parse and str(row.parse).strip()):
            log.warning(
                "跳过未解析文档",
                extra={
                    "step": "chunk",
                    "status": "no_parsed",
                    "file_name": fname,
                },
            )
            actions.append(
                ChunkActionRecord(
                    filename=fname,
                    action=ChunkAction.NO_PARSED,
                    error="parse 列为空，请先执行 §3.2 解析",
                    duration_ms=int((time.perf_counter() - t0) * 1000),
                )
            )
            continue

        # 2) 已切分（除非 force）
        #   ★ 2026-08 修复：兼容 "output/{stem}" 形式（dify 入库成功后的 chunks 列），
        #   用 _resolve_chunks_dir_from_manifest 解析实际目录，避免把 done 行倒退成 chunked。
        if not force:
            existing_chunks_dir = _resolve_chunks_dir_from_manifest(row)
            if existing_chunks_dir is not None:
                skipped_count += 1
                actions.append(
                    ChunkActionRecord(
                        filename=fname,
                        action=ChunkAction.SKIPPED_DONE,
                        chunks_dir=str(existing_chunks_dir.resolve()),
                        duration_ms=int((time.perf_counter() - t0) * 1000),
                    )
                )
                continue

        # 3) 定位 parsed_dir
        # row.parse 形如 "data/parsed/xxx" 或 "parsed/xxx" 或 "xxx"（仅 stem）
        # 也可能是 fallback 链路写入的带后缀 stem（"[vlm-image-fallback 修复]"）。
        # 用 _resolve_parsed_dir 做多级回退查找（精确 → 去后缀 → 去编号 → 前缀匹配）。
        parse_text = str(row.parse or "").strip()
        # 若是绝对路径，截出根再回退
        parse_root = settings.parsed_dir
        parse_stem_input = parse_text
        if parse_text:
            pp = Path(parse_text)
            if pp.is_absolute() and str(pp).startswith(str(parse_root.resolve())):
                # 绝对路径下取最后一段作为 stem 输入，其余仍以 parsed_root 为根
                try:
                    rel = pp.relative_to(parse_root.resolve())
                    parse_stem_input = str(rel).replace("\\", "/")
                    parse_root = parse_root.resolve()
                except ValueError:
                    parse_stem_input = pp.name
        resolved_dir, effective_stem = _resolve_parsed_dir(parse_stem_input, parse_root)

        # ★ stem 命中：manifest.parse 与实际目录不一致 → 同步 manifest
        #   场景：用户把文档改名（如加 "(1)" 后缀 / 触发 fallback 后被附加诊断后缀），
        #   但 data/parsed/ 下仍是原始名。同步后 manifest.parse 与 chunks 真正对应的
        #   目录一致，前端列表也准确。
        if resolved_dir is not None and effective_stem and effective_stem != Path(parse_stem_input).name:
            log.info(
                "chunk: parsed_dir stem 模糊匹配",
                extra={
                    "step": "chunk",
                    "status": "stem_matched",
                    "file_name": fname,
                    "desired": parse_text,
                    "resolved": str(resolved_dir),
                    "effective_stem": effective_stem,
                },
            )
            try:
                now = manifest_store.now_iso()
                corrected = row.model_copy(
                    update={
                        "filename": row.filename,
                        "parse": str(resolved_dir.resolve()),
                        "update_time": now,
                    }
                )
                manifest_store.upsert(corrected)
            except Exception:  # noqa: BLE001
                log.exception(
                    "chunk: 同步 manifest.parse 失败（不影响切分）",
                    extra={"step": "chunk", "file_name": fname},
                )

        if resolved_dir is None:
            failed_count += 1
            err = f"解析目录不存在: {parse_text}（也尝试了去掉 fallback 后缀/编号/前缀匹配）"
            actions.append(
                ChunkActionRecord(
                    filename=fname,
                    action=ChunkAction.CHUNK_FAILED,
                    error=err,
                    duration_ms=int((time.perf_counter() - t0) * 1000),
                )
            )
            _write_manifest_chunk_done(
                row,
                chunks_text=f"切分失败 → {err}",
                sys_status="error",
                err=err,
            )
            continue
        parse_path = resolved_dir

        # 4) 切分目标目录
        chunks_dir = settings.chunks_dir / parse_path.name

        # 5) dry_run
        if dry_run:
            chunked_count += 1
            actions.append(
                ChunkActionRecord(
                    filename=fname,
                    action=ChunkAction.DRY_RUN,
                    chunks_dir=str(chunks_dir.resolve()),
                    duration_ms=int((time.perf_counter() - t0) * 1000),
                )
            )
            _write_manifest_chunk_done(
                row,
                chunks_text="试运行-已切分",
                sys_status="chunking",
            )
            continue

        # 6) 实际切分
        try:
            # 6.0) 解析质量预检（仅 .pdf）：识别 MinerU 解析严重缺失的情况
            #   典型场景：扫描件 OCR 失败、纯图片 PDF（v2 只有 page_number/header）。
            #   PyMuPDF fallback 只适用于 PDF；.xlsx/.html 等非 PDF 文档跳过此检查，
            #   避免 office 产物（v2 块少）或本地解析产物（md 短）被误判 trivial 后
            #   走无效的 pdf_fallback 链。
            is_trivial, trivial_reason = False, ""
            if Path(fname).suffix.lower() == ".pdf":
                is_trivial, trivial_reason = _is_parse_content_trivial(parse_path)
            if is_trivial:
                # ★ 2026-07-31：自动 fallback 重解析（不再直接报错）
                # 优先级：v2 严重缺失 → 找原 PDF → pdf_fallback.maybe_fallback_after_mineru_failure
                # 重新检测产物；通过则继续切分，否则报错。
                fb_ok, fb_reason = _try_pdf_fallback_for_trivial_parse(
                    parse_path, parse_path.name
                )
                if fb_ok:
                    log.info(
                        "chunk: v2 trivial → fallback 重解析成功，继续切分",
                        extra={
                            "step": "chunk",
                            "status": "fallback_recovered",
                            "file_name": fname,
                            "parsed_dir": str(parse_path),
                            "fallback_reason": fb_reason,
                        },
                    )
                    # 重新检测确保产物可用
                    is_trivial, trivial_reason = _is_parse_content_trivial(parse_path)
                if is_trivial:
                    failed_count += 1
                    err_text = (
                        f"MinerU 解析内容严重缺失：{trivial_reason}。"
                        f"已自动尝试 pdf_fallback：{fb_reason}。"
                        f"建议检查 PDF 是否需要 OCR，或重新解析。"
                    )
                    log.warning(
                        "chunk skipped: parse content trivial (fallback exhausted)",
                        extra={
                            "step": "chunk",
                            "status": "skipped_parse_incomplete",
                            "file_name": fname,
                            "parsed_dir": str(parse_path),
                            "reason": trivial_reason,
                            "fallback_reason": fb_reason,
                        },
                    )
                    actions.append(
                        ChunkActionRecord(
                            filename=fname,
                            action=ChunkAction.CHUNK_FAILED,
                            error=err_text,
                            duration_ms=int((time.perf_counter() - t0) * 1000),
                        )
                    )
                    _write_manifest_chunk_done(
                        row,
                        chunks_text=f"切分跳过 → {err_text}",
                        sys_status="error",
                        err=err_text,
                    )
                    continue

            # force=true 时，先清空旧 chunks_dir（防遗留文件）
            if force and chunks_dir.exists():
                shutil.rmtree(chunks_dir, ignore_errors=True)
            result = chunk_document(parse_path, chunks_dir, strategy=strategy)
            chunked_count += 1
            # chunks 列只存 stem（不带 chunks/ 前缀），与 parse 列只存绝对路径或相对路径不同
            _write_manifest_chunk_done(
                row,
                chunks_text=parse_path.name,
                sys_status="chunked",
                err=None,
            )
            log.info(
                "chunk ok",
                extra={
                    "step": "chunk",
                    "status": "chunked",
                    "file_name": fname,
                    "chunks_dir": str(result.chunks_dir),
                    "chunk_count": len(result.chunks),
                    "total_chars": result.total_chars,
                    "image_count": result.image_count,
                    "duration_ms": int((time.perf_counter() - t0) * 1000),
                },
            )
            actions.append(
                ChunkActionRecord(
                    filename=fname,
                    action=ChunkAction.CHUNKED,
                    chunks_dir=str(result.chunks_dir.resolve()),
                    chunk_count=len(result.chunks),
                    total_chars=result.total_chars,
                    image_count=result.image_count,
                    duration_ms=int((time.perf_counter() - t0) * 1000),
                )
            )
        except Exception as e:  # noqa: BLE001
            failed_count += 1
            err_text = f"切分失败: {e}"
            log.exception(
                "chunk 失败",
                extra={
                    "step": "chunk",
                    "status": "failed",
                    "file_name": fname,
                    "error_msg": err_text,
                },
            )
            actions.append(
                ChunkActionRecord(
                    filename=fname,
                    action=ChunkAction.CHUNK_FAILED,
                    error=err_text,
                    duration_ms=int((time.perf_counter() - t0) * 1000),
                )
            )
            _write_manifest_chunk_done(
                row,
                chunks_text=f"切分失败 → {e}",
                sys_status="error",
                err=err_text,
            )

    duration_ms = int((time.perf_counter() - started) * 1000)
    report = ChunkReport(
        dry_run=dry_run,
        scanned=chunked_count + skipped_count + failed_count,
        chunked=chunked_count,
        skipped_done=skipped_count,
        failed=failed_count,
        actions=actions,
    )
    log.info(
        "chunk finished",
        extra={
            "step": "chunk",
            "status": "done",
            "duration_ms": duration_ms,
            "chunked": report.chunked,
            "skipped": report.skipped_done,
            "failed": report.failed,
        },
    )
    return report
