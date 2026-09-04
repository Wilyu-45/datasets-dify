"""多策略文档切分引擎（chunk_strategies.py）

融合业界主流的多种切分策略，供 chunker.chunk_document 按配置/请求选择：

+----------------+--------------------------------------------------------------+
| 策略 key       | 说明                                                         |
+================+==============================================================+
| structure      | 基于文档结构切分（默认）。利用标题层级 + 贪心合并 + 句号二次  |
|                | 切分，是系统默认且最成熟的策略（即原 chunk_body/附录逻辑）。  |
+----------------+--------------------------------------------------------------+
| recursive      | 递归切分。优先按段落（块）边界贪心合并，超长再按句子边界二次 |
|                | 切分，等价于"段落 → 句子"的递归分隔符层级，语义完整性与块    |
|                | 大小之间平衡较好。                                           |
+----------------+--------------------------------------------------------------+
| fixed          | 固定长度切分。按固定字符数硬性切割（可配 overlap），实现简单  |
|                | 速度快，适合作为基准线（Baseline）；配合特殊内容保护避免切断  |
|                | 表格/公式。                                                  |
+----------------+--------------------------------------------------------------+
| sentence       | 句子级切分。以句号/问号/分号等句末标点为分割点，保留最自然    |
|                | 的语义边界，适合普通文章、报告、FAQ。                        |
+----------------+--------------------------------------------------------------+
| semantic       | 语义切分。利用 Embedding 计算句子间语义相似度，在主题发生转   |
|                | 变的地方切分（需 Dify /embeddings/text-embedding，不可用时    |
|                | 自动降级为句子级切分）。                                     |
+----------------+--------------------------------------------------------------+
| parent_child   | 父-子切分。先切出较大的"父块"（完整上下文），再细分为较小的   |
|                | "子块"（精准检索单元）。子块用于检索，父块提供上下文，        |
|                | metadata 通过 parent_id 关联。                               |
+----------------+--------------------------------------------------------------+
| late_chunking  | 晚切分。先对整段文本生成文档级 Embedding（感知全局上下文），  |
|                | 再据此判断主题相关度、切分（需 Dify embedding，不可用时       |
|                | 自动降级为句子级切分）。                                     |
+----------------+--------------------------------------------------------------+
| llm            | LLM 切分。调用大模型自主决定切分点（成本高、默认关闭，需      |
|                | 配置 chunk_llm_enabled=true 且 Dify App API Key）。            |
+----------------+--------------------------------------------------------------+

设计原则：
  - 策略只影响 body / appendix 区域；cover/toc/preface/reference/single 保持原逻辑。
  - 所有策略都保留"特殊内容保护"（Markdown 表格 / LaTeX 公式不可被切断）。
  - semantic / late_chunking 依赖 embedding 服务，失败时自动降级。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

import requests

from app.config import settings

# 避免循环导入：chunker.py 在函数内部惰性导入本模块，
# 本模块在模块级 import chunker 是安全的（chunker 先加载完成）。
from app.services import chunker as _chunker
from app.services.chunker import (
    Block,
    Chunk,
    Region,
    _blocks_chars,
    _blocks_image_count,
    _extract_tables_from_blocks,
    _find_no_split_zones,
    _greedy_merge_groups,
    _make_table_chunks,
    _render_chunk_body,
    _segment_text_by_zones,
    _split_by_sentence,
    _truncate_overlap_to_safe,
    chunk_appendix,
    chunk_body,
    chunk_reference,
    chunk_simple,
)

log = logging.getLogger("ragsystem.chunk_strategies")

# ============================================================
# 策略元数据（供 API 列表 / 前端下拉 / 文档使用）
# ============================================================

STRATEGY_META: List[Dict[str, str]] = [
    {
        "key": "structure",
        "name": "结构切分",
        "desc": "按标题层级与段落贪心合并，成熟稳健，适合大多数结构化文档",
        "default": True,
    },
    {
        "key": "recursive",
        "name": "递归切分",
        "desc": "段落→句子递归分隔符切分，语义完整性与块大小平衡好",
        "default": False,
    },
    {
        "key": "fixed",
        "name": "固定长度切分",
        "desc": "按固定字符数硬切，简单快速，适合日志/代码或基准测试",
        "default": False,
    },
    {
        "key": "sentence",
        "name": "句子级切分",
        "desc": "按句末标点切分，保留最自然的语义边界",
        "default": False,
    },
    {
        "key": "semantic",
        "name": "语义切分",
        "desc": "基于 Embedding 相似度在主题转变处切分（需 Dify embedding）",
        "default": False,
    },
    {
        "key": "parent_child",
        "name": "父-子切分",
        "desc": "父块提供完整上下文 + 子块精准检索，检索精度与回答丰富性兼得",
        "default": False,
    },
    {
        "key": "late_chunking",
        "name": "晚切分",
        "desc": "先整文 Embedding 再切分，chunk 感知全局上下文（需 Dify embedding）",
        "default": False,
    },
    {
        "key": "llm",
        "name": "LLM 切分",
        "desc": "大模型自主决定切分点，语义最优但成本高（默认关闭）",
        "default": False,
    },
]

_VALID_STRATEGIES = {m["key"] for m in STRATEGY_META}


def normalize_strategy(name: Optional[str]) -> str:
    """校验/归一化策略名；未知或空 → 默认 structure。"""
    if not name:
        return "structure"
    n = name.strip().lower().replace("-", "_")
    return n if n in _VALID_STRATEGIES else "structure"


def list_strategies() -> List[Dict[str, str]]:
    """返回策略元数据列表（供前端下拉）。"""
    return list(STRATEGY_META)


# ============================================================
# 内部模型：句子级原子单元
# ============================================================


@dataclass
class _Atom:
    """切分的最小单元。

    is_atomic=True 表示不可再分割（表格 / 公式 / 标题 / 图片），
    必须整体落入单个 chunk。
    """

    text: str
    page_num: int
    is_atomic: bool
    is_heading: bool = False
    level: int = 0
    block: Optional[Block] = None


# 句末标点（与 chunker._split_by_sentence 的规则保持一致）
_SENT_RE = re.compile(r"[。；!?\n]|(?<![A-Za-z]\.)[.!?](?=\s|$)")


def _split_sentence_text(text: str) -> List[str]:
    """把普通文本按句末标点切成句子（保留标点）。"""
    out: List[str] = []
    cursor = 0
    for m in _SENT_RE.finditer(text):
        out.append(text[cursor : m.end()])
        cursor = m.end()
    if cursor < len(text):
        out.append(text[cursor:])
    return [x for x in out if x.strip()]


def _flatten_atoms(blocks: Sequence[Block]) -> List[_Atom]:
    """把 blocks 展平为句子级原子。

    - paragraph：先按不可分割区（表格/公式）保护，再按句末标点切句子
    - title：保留为标题原子（强制边界）
    - image / table / other：保留为原子
    """
    atoms: List[_Atom] = []
    for b in blocks:
        if b.block_type == "paragraph":
            txt = b.text or ""
            zones = _find_no_split_zones(txt)
            segments = _segment_text_by_zones(txt, zones)
            for is_atomic, seg in segments:
                if is_atomic:
                    atoms.append(
                        _Atom(
                            seg,
                            b.page_num,
                            True,
                            block=Block(page_num=b.page_num, block_type="paragraph", text=seg),
                        )
                    )
                else:
                    for s in _split_sentence_text(seg):
                        atoms.append(
                            _Atom(
                                s,
                                b.page_num,
                                False,
                                block=Block(page_num=b.page_num, block_type="paragraph", text=s),
                            )
                        )
        elif b.block_type == "title":
            atoms.append(
                _Atom(b.text or "", b.page_num, True, is_heading=True, level=b.level or 1, block=b)
            )
        else:
            # image / table / other：不可分割原子
            atoms.append(_Atom("", b.page_num, True, block=b))
    return atoms


def _atoms_chars(atoms: Sequence[_Atom]) -> int:
    """累计原子字符数（不含图片）。"""
    return sum(len(a.text) for a in atoms)


def _render_atom_chunk(
    title_path: str,
    atoms: Sequence[_Atom],
    chunk_type: str = "body",
    is_split: bool = False,
    parent_id: Optional[str] = None,
) -> Chunk:
    """把原子序列渲染为一个 Chunk。"""
    blocks = [a.block for a in atoms if a.block is not None]
    body, image_refs = _render_chunk_body(title_path, blocks, is_split=is_split)
    return Chunk(
        chunk_id="",
        file_name="",
        title_path=title_path,
        chunk_type=chunk_type,
        char_count=_atoms_chars(atoms),
        image_refs=image_refs,
        is_split=is_split,
        body=body,
        parent_id=parent_id,
    )


def _make_plain_chunk(
    title_path: str,
    blocks: Sequence[Block],
    chunk_type: str = "body",
    is_split: bool = False,
    parent_id: Optional[str] = None,
) -> Chunk:
    """把 blocks 渲染为一个 Chunk（供非原子策略复用）。"""
    body, image_refs = _render_chunk_body(title_path, list(blocks), is_split=is_split)
    return Chunk(
        chunk_id="",
        file_name="",
        title_path=title_path,
        chunk_type=chunk_type,
        char_count=_blocks_chars(list(blocks)),
        image_refs=image_refs,
        is_split=is_split,
        body=body,
        parent_id=parent_id,
    )


def _extract_table_chunks(region: Region) -> Tuple[List[Block], List[Chunk]]:
    """把 region.blocks 中的表格抽离为独立 chunk（特殊内容保护）。

    Returns:
        (non_table_blocks, table_chunks)
    """
    non_tables, tables = _extract_tables_from_blocks(region.blocks)
    table_chunks = _make_table_chunks(tables, region.title_path, region.title_path)
    return non_tables, table_chunks


# ============================================================
# 各策略实现
# ============================================================


def _chunk_body_fixed(region: Region) -> List[Chunk]:
    """固定长度切分：按固定字符数硬切，可配 overlap，保护表格/公式。"""
    non_tables, table_chunks = _extract_table_chunks(region)
    atoms = _flatten_atoms(non_tables)
    size = max(50, settings.chunk_fixed_size_chars)
    overlap = max(0, settings.chunk_fixed_overlap_chars)
    max_images = settings.chunk_max_images_per_segment

    groups: List[List[_Atom]] = []
    cur: List[_Atom] = []
    cur_chars = 0
    cur_imgs = 0
    for a in atoms:
        a_len = len(a.text)
        a_img = 1 if (a.block and a.block.block_type == "image" and a.block.image_path) else 0
        # ★ 2026-09：图片超限也触发分组（与字符数阈值并列）
        if cur and (cur_chars + a_len > size or (max_images > 0 and cur_imgs + a_img > max_images)):
            groups.append(cur)
            cur, cur_chars, cur_imgs = [], 0, 0
        cur.append(a)
        cur_chars += a_len
        cur_imgs += a_img
    if cur:
        groups.append(cur)

    # overlap：把上一组末尾的文本复制到下一组开头（不做跨原子截断）
    if overlap > 0 and len(groups) > 1:
        final_groups: List[List[_Atom]] = [groups[0]]
        for i in range(1, len(groups)):
            prev_text = "".join(a.text for a in groups[i - 1])
            ov = _truncate_overlap_to_safe(prev_text, overlap)
            if ov and groups[i]:
                head = _Atom(
                    ov,
                    groups[i][0].page_num,
                    False,
                    block=Block(page_num=groups[i][0].page_num, block_type="paragraph", text=ov),
                )
                groups[i].insert(0, head)
            final_groups.append(groups[i])
        groups = final_groups

    chunks = [_render_atom_chunk(region.title_path, g, "body") for g in groups]
    return table_chunks + chunks


def _chunk_body_sentence(region: Region) -> List[Chunk]:
    """句子级切分：按句末标点切分，保留最自然的语义边界。"""
    non_tables, table_chunks = _extract_table_chunks(region)
    groups = _split_by_sentence(non_tables, settings.chunk_split_target, max_images=settings.chunk_max_images_per_segment)
    chunks = [_make_plain_chunk(region.title_path, g, "body") for g in groups]
    return table_chunks + chunks


def _chunk_body_recursive(region: Region) -> List[Chunk]:
    """递归切分：段落（块）边界 → 句子边界，贪心合并到主阈值。"""
    non_tables, table_chunks = _extract_table_chunks(region)
    # 每个块作为一个单元（标题也作为普通内容参与），优先按块边界合并
    groups = [("", [b]) for b in non_tables]
    merged = _greedy_merge_groups(
        groups,
        settings.chunk_target_chars,
        settings.chunk_max_images_per_segment,
    )
    chunks: List[Chunk] = []
    for _, sub in merged:
        if _blocks_chars(sub) > settings.chunk_hard_limit:
            # 超长：进入更小一级分隔符（句子）
            for g in _split_by_sentence(sub, settings.chunk_split_target, max_images=settings.chunk_max_images_per_segment):
                chunks.append(_make_plain_chunk(region.title_path, g, "body", is_split=True))
        else:
            chunks.append(_make_plain_chunk(region.title_path, sub, "body"))
    return table_chunks + chunks


# ============================================================
# Embedding 客户端（semantic / late_chunking 依赖）
# ============================================================


class EmbeddingClient:
    """Embedding 客户端（语义切分 / 晚切分依赖）。

    端点优先级：
      1) settings.chunk_embedding_api_url — 自定义 embedding 服务（OpenAI 兼容：
         body {"input": [...]} 或 Dify 格式 {"texts": [...], "text_type": "document"}）
      2) 默认 Dify `/embeddings/text-embedding`（Knowledge API Key）

    失败时返回 None，调用方自动降级。
    """

    def __init__(self) -> None:
        base = (settings.dify_api_url or "").rstrip("/")
        self.url = (settings.chunk_embedding_api_url or "").strip() or (
            f"{base}/embeddings/text-embedding" if base else ""
        )
        api_key = (
            (settings.chunk_embedding_api_key or "").strip()
            or settings.dify_api_key
            or ""
        )
        self.headers = {"Authorization": f"Bearer {api_key}"}
        self.timeout = max(15, getattr(settings, "dify_timeout", 60) or 60)
        self.batch_size = 20

    @property
    def available(self) -> bool:
        return bool(self.url) and bool(settings.dify_api_key or settings.chunk_embedding_api_key)

    def embed(self, texts: Sequence[str]) -> Optional[List[List[float]]]:
        """批量求向量。返回 embeddings 列表（与 texts 一一对应）；失败返回 None。"""
        if not self.available or not texts:
            return None
        all_embs: List[List[float]] = []
        try:
            for i in range(0, len(texts), self.batch_size):
                batch = [t for t in texts[i : i + self.batch_size]]
                resp = requests.post(
                    self.url,
                    json={"texts": batch, "text_type": "document"},
                    headers=self.headers,
                    timeout=self.timeout,
                )
                resp.raise_for_status()
                data = resp.json()
                # 兼容 OpenAI 风格返回：data[].embedding
                if isinstance(data, dict) and "data" in data:
                    embs = [d.get("embedding") for d in data["data"] if isinstance(d, dict)]
                else:
                    embs = (data or {}).get("embeddings") or []
                if not embs:
                    return None
                all_embs.extend(embs)
            return all_embs if len(all_embs) == len(texts) else None
        except Exception as e:  # noqa: BLE001
            log.warning("embedding 调用失败（%s），语义切分将降级为句子级", e)
            return None


def _get_embedding_client() -> Optional[EmbeddingClient]:
    client = EmbeddingClient()
    return client if client.available else None


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    """余弦相似度。"""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def _group_atoms_with_breakpoints(
    atoms: Sequence[_Atom],
    breakpoints: set,
    target: int,
    max_images: int = 0,
) -> List[List[_Atom]]:
    """按断裂点 + 目标字符数把原子序列分组。

    - 标题原子强制作为新组的起点
    - breakpoints 是原子索引，表示"该原子之前应切分"
    - 累计字符超过 target 也切分（保证块大小合理）
    - ★ 2026-09：累计图片超过 max_images 也切分（避免单段 10+ 图）
    """
    groups: List[List[_Atom]] = []
    cur: List[_Atom] = []
    cur_chars = 0
    cur_imgs = 0
    for i, a in enumerate(atoms):
        a_img = 1 if (a.block and a.block.block_type == "image" and a.block.image_path) else 0
        force_new = a.is_heading or (i in breakpoints) or (
            cur and cur_chars + len(a.text) > target
        ) or (
            max_images > 0 and cur and cur_imgs + a_img > max_images
        )
        if cur and force_new:
            groups.append(cur)
            cur, cur_chars, cur_imgs = [], 0, 0
        cur.append(a)
        cur_chars += len(a.text)
        cur_imgs += a_img
    if cur:
        groups.append(cur)
    return groups or [list(atoms)]


def _chunk_body_semantic(region: Region) -> List[Chunk]:
    """语义切分：Embedding 相似度低谷处切分。

    不可用（未配置/调用失败）时降级为句子级切分。
    """
    client = _get_embedding_client()
    if client is None:
        log.warning("semantic 策略需要 Dify embedding，未配置 → 降级为 sentence")
        return _chunk_body_sentence(region)

    non_tables, table_chunks = _extract_table_chunks(region)
    atoms = _flatten_atoms(non_tables)
    # 只对普通句子求 embedding；标题/表格等原子作为强制边界
    plain_idx = [i for i, a in enumerate(atoms) if not a.is_atomic]
    plain_texts = [atoms[i].text for i in plain_idx]
    if len(plain_idx) < 2:
        return table_chunks + [_render_atom_chunk(region.title_path, atoms, "body")]

    embs = client.embed(plain_texts)
    if not embs:
        log.warning("semantic 策略 embedding 失败 → 降级为 sentence")
        return _chunk_body_sentence(region)

    # 相邻句子相似度；相似度低于阈值 → 语义断裂点
    threshold = settings.chunk_semantic_threshold
    breakpoints: set = set()
    for k in range(len(plain_idx) - 1):
        sim = _cosine(embs[k], embs[k + 1])
        if sim < threshold:
            # 断裂点位于第 k+1 个普通句子的原子索引处
            breakpoints.add(plain_idx[k + 1])

    groups = _group_atoms_with_breakpoints(atoms, breakpoints, settings.chunk_target_chars, max_images=settings.chunk_max_images_per_segment)
    chunks = [_render_atom_chunk(region.title_path, g, "body") for g in groups]
    return table_chunks + chunks


def _chunk_body_parent_child(region: Region) -> List[Chunk]:
    """父-子切分。

    先按"父块大小"贪心合并出父块（完整上下文），再在父块内按"子块大小"
    细分出子块（精准检索单元）。父块与子块都输出：
      - 子块 chunk_type=body，metadata 记录 parent_id（父块标题路径）
      - 父块 chunk_type=parent，与子块同入 metadata（入库侧可自行取舍）
    """
    non_tables, table_chunks = _extract_table_chunks(region)
    parent_size = max(300, settings.chunk_parent_size_chars)
    child_size = max(100, settings.chunk_child_size_chars)

    groups = [("", [b]) for b in non_tables]
    parents = _greedy_merge_groups(
        groups,
        parent_size,
        settings.chunk_max_images_per_segment,
    )

    chunks: List[Chunk] = []
    for p_idx, (_, p_blocks) in enumerate(parents):
        parent_id = f"{region.title_path} #P{p_idx + 1}"
        # 父块（上下文）
        chunks.append(_make_plain_chunk(region.title_path, p_blocks, "parent"))
        # 子块（检索单元）：父块内按句子切分到子块大小
        for cg in _split_by_sentence(p_blocks, child_size, max_images=settings.chunk_max_images_per_segment):
            chunks.append(
                _make_plain_chunk(region.title_path, cg, "body", parent_id=parent_id)
            )
    return table_chunks + chunks


def _chunk_body_late_chunking(region: Region) -> List[Chunk]:
    """晚切分：先整文 Embedding（文档级上下文），再判断句子主题相关度切分。

    与 semantic 的区别：以"句子 vs 文档向量"的相关度而非相邻相似度判断。
    不可用时降级为句子级切分。
    """
    client = _get_embedding_client()
    if client is None:
        log.warning("late_chunking 策略需要 Dify embedding，未配置 → 降级为 sentence")
        return _chunk_body_sentence(region)

    non_tables, table_chunks = _extract_table_chunks(region)
    atoms = _flatten_atoms(non_tables)
    plain_idx = [i for i, a in enumerate(atoms) if not a.is_atomic]
    plain_texts = [atoms[i].text for i in plain_idx]
    if not plain_texts:
        return table_chunks + [_render_atom_chunk(region.title_path, atoms, "body")]

    # 文档级向量（整段文本一次编码）
    doc_emb = client.embed([" ".join(plain_texts)])
    if not doc_emb:
        log.warning("late_chunking 整文 embedding 失败 → 降级为 sentence")
        return _chunk_body_sentence(region)
    doc_vec = doc_emb[0]

    # 句子级向量
    embs = client.embed(plain_texts)
    if not embs:
        log.warning("late_chunking 句子 embedding 失败 → 降级为 sentence")
        return _chunk_body_sentence(region)

    # 句子与文档主题相关度低于阈值 → 主题偏离，切分
    threshold = settings.chunk_semantic_threshold
    breakpoints: set = set()
    for k, emb in enumerate(embs):
        if _cosine(emb, doc_vec) < threshold:
            breakpoints.add(plain_idx[k])

    groups = _group_atoms_with_breakpoints(atoms, breakpoints, settings.chunk_target_chars, max_images=settings.chunk_max_images_per_segment)
    chunks = [_render_atom_chunk(region.title_path, g, "body") for g in groups]
    return table_chunks + chunks


def _chunk_body_llm(region: Region) -> List[Chunk]:
    """LLM 切分：调用大语言模型（OpenAI 兼容 Chat Completions）让模型返回切分后的段落。

    默认关闭（settings.chunk_llm_enabled=False），开启时需配置
    settings.llm_api_base_url / llm_api_key / llm_model。失败/超时自动降级为结构切分。
    """
    if not settings.chunk_llm_enabled:
        log.warning("llm 策略未启用（chunk_llm_enabled=False）→ 降级为 structure")
        return chunk_body(region)

    base = (settings.llm_api_base_url or "").rstrip("/")
    api_key = (settings.llm_api_key or "").strip()
    model = (settings.llm_model or "").strip()
    if not (base and api_key and model):
        log.warning(
            "llm 策略缺少 llm_api_base_url / llm_api_key / llm_model → 降级为 structure"
        )
        return chunk_body(region)

    chat_url = f"{base}/chat/completions"
    text = "\n".join(
        _block_to_plain_text(b) for b in region.blocks if b.block_type != "title"
    )
    if not text.strip():
        return chunk_body(region)

    prompt = settings.chunk_llm_chunk_prompt
    try:
        resp = requests.post(
            chat_url,
            json={
                "model": model,
                "messages": [
                    {
                        "role": "system",
                        "content": "你是一名文档切分专家，请按要求完成切分并只输出 JSON。",
                    },
                    {"role": "user", "content": f"{prompt}\n\n{text}"},
                ],
                "temperature": 0.2,
            },
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=max(60, settings.dify_timeout or 60),
        )
        resp.raise_for_status()
        data = resp.json()
        answer = (
            ((data.get("choices") or [{}])[0].get("message") or {}).get("content")
            or ""
        )
        # 期望模型返回 JSON 数组（切分后的段落）；兼容 JSON 代码块
        answer_clean = answer.strip()
        if answer_clean.startswith("```"):
            answer_clean = re.sub(r"^```[a-zA-Z]*\s*", "", answer_clean)
            answer_clean = re.sub(r"\s*```$", "", answer_clean)
        parsed = json.loads(answer_clean)
        if not isinstance(parsed, list) or not parsed:
            return chunk_body(region)
        chunks: List[Chunk] = []
        for seg_text in parsed:
            if not isinstance(seg_text, str) or not seg_text.strip():
                continue
            blocks = [
                Block(
                    page_num=region.blocks[0].page_num if region.blocks else 1,
                    block_type="paragraph",
                    text=seg_text.strip(),
                )
            ]
            chunks.append(_make_plain_chunk(region.title_path, blocks, "body"))
        return chunks or chunk_body(region)
    except Exception:  # noqa: BLE001
        log.warning("LLM 切分失败 → 降级为 structure", exc_info=True)
        return chunk_body(region)


def _block_to_plain_text(b: Block) -> str:
    """把 block 转成纯文本（供 LLM 切分）。"""
    if b.block_type == "title":
        return b.text or ""
    if b.block_type == "paragraph":
        return b.text or ""
    if b.block_type == "table":
        return (b.table_caption or "") + " " + _strip_html(b.table_html or "")
    return ""


def _strip_html(html: str) -> str:
    """粗略去掉 HTML 标签（供 LLM 切分的纯文本）。"""
    return re.sub(r"<[^>]+>", "", html)


# ============================================================
# 统一入口
# ============================================================

# 策略 key → body 区域实现（appendix 区域复用同一套策略实现）
_BODY_CHUNKERS = {
    "fixed": _chunk_body_fixed,
    "sentence": _chunk_body_sentence,
    "recursive": _chunk_body_recursive,
    "semantic": _chunk_body_semantic,
    "parent_child": _chunk_body_parent_child,
    "late_chunking": _chunk_body_late_chunking,
    "llm": _chunk_body_llm,
}


def chunk_region_with_strategy(region: Region, strategy: Optional[str]) -> List[Chunk]:
    """按策略切分一个区域。

    - body / appendix：按 strategy 选择实现；structure 走原 chunk_body/chunk_appendix
    - cover / toc / preface / single：原 chunk_simple
    - reference：原 chunk_reference
    """
    s = normalize_strategy(strategy)
    if region.region_type == "body":
        if s == "structure":
            return chunk_body(region)
        impl = _BODY_CHUNKERS.get(s, chunk_body)
        return impl(region)
    if region.region_type == "appendix":
        if s == "structure":
            return chunk_appendix(region)
        impl = _BODY_CHUNKERS.get(s, chunk_appendix)
        return impl(region)
    if region.region_type == "reference":
        return chunk_reference(region)
    return chunk_simple(region)
