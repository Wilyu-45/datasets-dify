"""chunk_strategies 多策略切分引擎测试。

覆盖全部 8 种策略（structure/recursive/fixed/sentence/semantic/
parent_child/late_chunking/llm）的基本行为与降级路径，以及
config_store 字段→策略归属元数据的一致性。
"""
import pytest

from app.config import settings
from app.services import chunk_strategies, config_store
from app.services.chunker import Block, Region, chunk_body


def _para(text: str, page: int = 1) -> Block:
    return Block(page_num=page, block_type="paragraph", text=text)


def _title(text: str, level: int = 1, page: int = 1) -> Block:
    return Block(page_num=page, block_type="title", level=level, text=text)


def _body_region(blocks) -> Region:
    return Region("body", "第一章", blocks)


@pytest.fixture(autouse=True)
def _chunk_params(monkeypatch):
    """统一给足各策略参数，避免测试间默认值差异。"""
    monkeypatch.setattr(settings, "chunk_target_chars", 1500)
    monkeypatch.setattr(settings, "chunk_split_target", 1200)
    monkeypatch.setattr(settings, "chunk_overlap", 100)
    monkeypatch.setattr(settings, "chunk_hard_limit", 1800)
    monkeypatch.setattr(settings, "chunk_max_images_per_segment", 10)
    monkeypatch.setattr(settings, "chunk_fixed_size_chars", 800)
    monkeypatch.setattr(settings, "chunk_fixed_overlap_chars", 100)
    monkeypatch.setattr(settings, "chunk_semantic_threshold", 0.78)
    monkeypatch.setattr(settings, "chunk_parent_size_chars", 1500)
    monkeypatch.setattr(settings, "chunk_child_size_chars", 400)
    monkeypatch.setattr(settings, "chunk_llm_enabled", False)
    monkeypatch.setattr(settings, "chunk_embedding_api_url", "")
    monkeypatch.setattr(settings, "chunk_embedding_api_key", "")
    monkeypatch.setattr(settings, "dify_api_key", "")
    monkeypatch.setattr(settings, "dify_app_api_key", "")
    yield


# ============================================================
# normalize_strategy
# ============================================================


def test_normalize_strategy_valid_and_fallback():
    assert chunk_strategies.normalize_strategy(None) == "structure"
    assert chunk_strategies.normalize_strategy("") == "structure"
    assert chunk_strategies.normalize_strategy("unknown") == "structure"
    assert chunk_strategies.normalize_strategy("fixed") == "fixed"
    assert chunk_strategies.normalize_strategy("parent-child") == "parent_child"
    assert chunk_strategies.normalize_strategy("  LLM ") == "llm"
    for meta in chunk_strategies.STRATEGY_META:
        assert chunk_strategies.normalize_strategy(meta["key"]) == meta["key"]


# ============================================================
# fixed：固定长度切分
# ============================================================


def test_fixed_strategy_hard_splits(monkeypatch):
    monkeypatch.setattr(settings, "chunk_fixed_size_chars", 300)
    monkeypatch.setattr(settings, "chunk_fixed_overlap_chars", 0)
    # 8 段 × 100 字 = 800 字 → 300/300/200 共 3 块
    blocks = [_para("字" * 100) for _ in range(8)]
    chunks = chunk_strategies.chunk_region_with_strategy(_body_region(blocks), "fixed")
    assert [c.char_count for c in chunks] == [300, 300, 200]
    assert all(c.chunk_type == "body" for c in chunks)


def test_fixed_strategy_with_overlap(monkeypatch):
    monkeypatch.setattr(settings, "chunk_fixed_size_chars", 200)
    monkeypatch.setattr(settings, "chunk_fixed_overlap_chars", 50)
    # 3 段 × 100 字 = 300 字 → 200/100；第二块开头应带上重叠文本
    blocks = [_para("字" * 100) for _ in range(3)]
    chunks = chunk_strategies.chunk_region_with_strategy(_body_region(blocks), "fixed")
    assert len(chunks) == 2
    assert chunks[1].char_count > 100  # 100 原文 + 重叠


# ============================================================
# sentence：句子级切分
# ============================================================


def test_sentence_strategy_merges_when_target_large(monkeypatch):
    monkeypatch.setattr(settings, "chunk_split_target", 1200)
    monkeypatch.setattr(settings, "chunk_overlap", 0)
    text = "第一句话。第二句话。第三句话。第四句话。"
    chunks = chunk_strategies.chunk_region_with_strategy(
        _body_region([_para(text)]), "sentence"
    )
    assert len(chunks) == 1
    assert chunks[0].char_count == len(text)


def test_sentence_strategy_splits_by_sentence(monkeypatch):
    monkeypatch.setattr(settings, "chunk_split_target", 10)
    monkeypatch.setattr(settings, "chunk_overlap", 0)
    text = "第一句话。第二句话。第三句话。"
    chunks = chunk_strategies.chunk_region_with_strategy(
        _body_region([_para(text)]), "sentence"
    )
    assert len(chunks) >= 2
    joined = "".join(c.body for c in chunks)
    for s in ("第一句话。", "第二句话。", "第三句话。"):
        assert s in joined


# ============================================================
# recursive：递归切分（段落 → 句子）
# ============================================================


def test_recursive_strategy_greedy_merge(monkeypatch):
    monkeypatch.setattr(settings, "chunk_target_chars", 500)
    monkeypatch.setattr(settings, "chunk_hard_limit", 800)
    monkeypatch.setattr(settings, "chunk_split_target", 200)
    # 6 段 × 100 字 = 600 字 → 500 + 100 = 2 块
    blocks = [_para("字" * 100) for _ in range(6)]
    chunks = chunk_strategies.chunk_region_with_strategy(_body_region(blocks), "recursive")
    assert len(chunks) == 2
    assert [c.char_count for c in chunks] == [500, 100]


def test_recursive_strategy_oversize_goes_to_sentence(monkeypatch):
    monkeypatch.setattr(settings, "chunk_target_chars", 500)
    monkeypatch.setattr(settings, "chunk_hard_limit", 400)
    monkeypatch.setattr(settings, "chunk_split_target", 100)
    monkeypatch.setattr(settings, "chunk_overlap", 0)
    # 单段 500 字 > hard_limit 400 → 按句子目标 100 二次切分 → 5 块
    blocks = [_para("字。" * 250)]
    chunks = chunk_strategies.chunk_region_with_strategy(_body_region(blocks), "recursive")
    assert len(chunks) == 5
    assert all(c.is_split for c in chunks)


# ============================================================
# semantic / late_chunking：无 embedding 时降级为 sentence
# ============================================================


def _multi_sentence_region() -> Region:
    text = "".join(f"第{i}句话。" for i in range(8))
    return _body_region([_para(text)])


def test_semantic_strategy_falls_back_to_sentence(monkeypatch):
    monkeypatch.setattr(settings, "chunk_embedding_api_url", "")
    monkeypatch.setattr(settings, "dify_api_key", "")
    region = _multi_sentence_region()
    sem_chunks = chunk_strategies.chunk_region_with_strategy(region, "semantic")
    sent_chunks = chunk_strategies.chunk_region_with_strategy(region, "sentence")
    assert [c.body for c in sem_chunks] == [c.body for c in sent_chunks]


def test_late_chunking_strategy_falls_back_to_sentence(monkeypatch):
    monkeypatch.setattr(settings, "chunk_embedding_api_url", "")
    monkeypatch.setattr(settings, "dify_api_key", "")
    region = _multi_sentence_region()
    lc_chunks = chunk_strategies.chunk_region_with_strategy(region, "late_chunking")
    sent_chunks = chunk_strategies.chunk_region_with_strategy(region, "sentence")
    assert [c.body for c in lc_chunks] == [c.body for c in sent_chunks]


# ============================================================
# parent_child：父-子切分
# ============================================================


def test_parent_child_strategy(monkeypatch):
    monkeypatch.setattr(settings, "chunk_parent_size_chars", 1500)
    monkeypatch.setattr(settings, "chunk_child_size_chars", 300)
    monkeypatch.setattr(settings, "chunk_overlap", 0)
    # 8 段 × 100 字 = 800 字 → 1 个父块；子块按 300 细分 → 3 块
    blocks = [_para("字" * 100) for _ in range(8)]
    chunks = chunk_strategies.chunk_region_with_strategy(_body_region(blocks), "parent_child")
    parents = [c for c in chunks if c.chunk_type == "parent"]
    children = [c for c in chunks if c.chunk_type == "body"]
    assert len(parents) == 1
    assert len(children) == 3
    for c in children:
        assert c.parent_id
        assert c.parent_id == parents[0].title_path + " #P1"
        assert c.char_count <= 400  # 子块不超过目标 + 余量


# ============================================================
# llm：未启用时降级为 structure
# ============================================================


def test_llm_strategy_disabled_falls_back_to_structure(monkeypatch):
    monkeypatch.setattr(settings, "chunk_llm_enabled", False)
    region = _body_region([_para("字" * 1000), _title("1.1 小节", 2), _para("字" * 800)])
    chunks = chunk_strategies.chunk_region_with_strategy(region, "llm")
    ref = chunk_body(region)
    assert [c.body for c in chunks] == [c.body for c in ref]


# ============================================================
# 区域类型分流 & 全策略冒烟
# ============================================================


def test_cover_region_uses_chunk_simple():
    region = Region("cover", "封面", [Block(page_num=1, block_type="paragraph", text="封面文字")])
    chunks = chunk_strategies.chunk_region_with_strategy(region, "fixed")
    assert chunks and chunks[0].chunk_type == "cover"


def test_reference_region_uses_chunk_reference():
    region = Region("reference", "参考文献", [_para("张三. 某书. 出版社, 2020.")])
    chunks = chunk_strategies.chunk_region_with_strategy(region, "sentence")
    assert chunks and chunks[0].chunk_type == "reference"


@pytest.mark.parametrize("strategy", [m["key"] for m in chunk_strategies.STRATEGY_META])
def test_all_strategies_smoke(strategy):
    blocks = [
        _title("1.1 小节", 2),
        _para("第一句话。第二句话。" * 3),
        _para("字" * 80),
    ]
    chunks = chunk_strategies.chunk_region_with_strategy(_body_region(blocks), strategy)
    assert chunks, f"策略 {strategy} 应产生 chunk"


# ============================================================
# config_store 字段 → 策略归属元数据
# ============================================================


def test_profile_fields_strategies_are_valid():
    valid = {m["key"] for m in chunk_strategies.STRATEGY_META}
    keys = {f["key"] for f in config_store.PROFILE_FIELDS}
    for f in config_store.PROFILE_FIELDS:
        for s in f.get("strategies", []):
            assert s in valid, f"字段 {f['key']} 的 strategies 含非法策略 {s}"
    assert "dify_dataset_id" in keys
    assert "chunk_strategy" in keys
    assert "chunk_llm_chunk_prompt" in keys  # llm 提示词已加入字段定义


def test_every_strategy_has_own_fields():
    field_keys_by_strategy: dict = {}
    for f in config_store.PROFILE_FIELDS:
        for s in f.get("strategies", []):
            field_keys_by_strategy.setdefault(s, set()).add(f["key"])
    for m in chunk_strategies.STRATEGY_META:
        s = m["key"]
        if s == "structure":
            continue  # structure 是默认策略，其专属字段较多
        assert field_keys_by_strategy.get(s), f"策略 {s} 没有任何专属配置字段"


def test_field_schema_exposes_strategies():
    fields = config_store.get_field_schema()
    by_key = {f["key"]: f for f in fields}
    assert by_key["chunk_target_chars"]["strategies"] == [
        "structure",
        "recursive",
        "semantic",
        "late_chunking",
    ]
    assert by_key["chunk_fixed_size_chars"]["strategies"] == ["fixed"]
    assert by_key["chunk_llm_chunk_prompt"]["strategies"] == ["llm"]
