"""配置方案（Profile）存储与运行时应用。

★ 2026-08 新增「配置中心」：
    用户需要在网页里配置「知识库 ID + 切分策略 + 全部切分参数」，
    并支持多套配置方案，激活其中一套作为当前配置。
    上传处理时使用所选（默认当前激活）配置方案。

数据持久化：data/configs/profiles.json
    {
      "profiles": [
        {"id": "...", "name": "默认配置", "created_at": "...", "updated_at": "...", "config": {...}}
      ],
      "active_profile_id": "..."
    }

config 的 key 与 Settings 属性名一一对应（chunk_target_chars / chunk_strategy /
dify_dataset_id 等），运行时用 contextmanager 临时覆盖 settings 属性并恢复，
这样 pipeline 各阶段（chunker / dify_ingest 直接读 settings）无需改动即可使用所选配置。
"""

from __future__ import annotations

import logging
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from app.config import Settings, settings

log = logging.getLogger("ragsystem.config_store")

# data/configs/ 下保存 profiles.json
CONFIGS_DIRNAME = "configs"
PROFILES_FILENAME = "profiles.json"

# 所有可配置字段定义（key 必须与 Settings 属性名一致）。
# 前端据此动态渲染表单；新增字段只需在这里补一项。
#
# strategies：该字段生效于哪些切分策略（列表形式）；通用字段（知识库 ID /
# 切分策略本身）不填 strategies，表示所有策略都适用。
# 前端在选择不同切分策略时，只显示与当前策略相关的配置项。
PROFILE_FIELDS: List[Dict[str, Any]] = [
    {
        "key": "dify_dataset_id",
        "label": "知识库 ID",
        "type": "select_dataset",
        "default": "",
        "description": "Dify 目标知识库 ID（可在知识库下拉中选择）",
    },
    {
        "key": "chunk_strategy",
        "label": "切分策略",
        "type": "select_strategy",
        "default": "structure",
        "description": "structure / recursive / fixed / sentence / semantic / parent_child / late_chunking / llm",
    },
    {
        "key": "chunk_target_chars",
        "label": "目标字符数",
        "type": "int",
        "min": 100,
        "max": 10000,
        "default": 1500,
        "description": "单个 chunk 的目标字符上限（cutrule.md 主阈值）",
        "strategies": ["structure", "recursive", "semantic", "late_chunking"],
    },
    {
        "key": "chunk_split_target",
        "label": "二次切分目标字符",
        "type": "int",
        "min": 100,
        "max": 5000,
        "default": 1200,
        "description": "句号/分号二次切分（或句子策略）时，每个子块目标字符",
        "strategies": ["structure", "recursive", "sentence"],
    },
    {
        "key": "chunk_overlap",
        "label": "二次切分重叠字符",
        "type": "int",
        "min": 0,
        "max": 500,
        "default": 100,
        "description": "二次切分相邻子块的重叠字符数（滑动窗口重叠）",
        "strategies": ["structure"],
    },
    {
        "key": "chunk_hard_limit",
        "label": "硬上限",
        "type": "int",
        "min": 100,
        "max": 20000,
        "default": 1800,
        "description": "超过该值强制切分，防止极端长段落",
        "strategies": ["structure", "recursive"],
    },
    {
        "key": "chunk_appendix_threshold",
        "label": "附录合并阈值",
        "type": "int",
        "min": 0,
        "max": 5000,
        "default": 1500,
        "description": "附录贪心合并阈值（≤该值的附录并入正文）",
        "strategies": ["structure"],
    },
    {
        "key": "chunk_max_images_per_segment",
        "label": "单段最大图片数",
        "type": "int",
        "min": 1,
        "max": 50,
        "default": 10,
        "description": "与 Dify 附件上限对齐，避免 add_segments 报错",
        "strategies": ["structure", "recursive", "parent_child"],
    },
    {
        "key": "chunk_table_row_threshold",
        "label": "表格行数阈值",
        "type": "int",
        "min": 1,
        "max": 100,
        "default": 20,
        "description": "行数超过该阈值的表格自动拆分为多段",
        "strategies": [
            "structure",
            "recursive",
            "fixed",
            "sentence",
            "semantic",
            "parent_child",
            "late_chunking",
        ],
    },
    {
        "key": "chunk_table_max_chars",
        "label": "表格字符兜底",
        "type": "int",
        "min": 100,
        "max": 20000,
        "default": 5000,
        "description": "表格可见文本超过该值（即使行数很少）也按行拆分",
        "strategies": [
            "structure",
            "recursive",
            "fixed",
            "sentence",
            "semantic",
            "parent_child",
            "late_chunking",
        ],
    },
    {
        "key": "chunk_fixed_size_chars",
        "label": "固定切分长度",
        "type": "int",
        "min": 100,
        "max": 5000,
        "default": 800,
        "description": "fixed 策略：单块目标字符数",
        "strategies": ["fixed"],
    },
    {
        "key": "chunk_fixed_overlap_chars",
        "label": "固定切分重叠",
        "type": "int",
        "min": 0,
        "max": 500,
        "default": 100,
        "description": "fixed 策略：相邻块重叠字符数",
        "strategies": ["fixed"],
    },
    {
        "key": "chunk_semantic_threshold",
        "label": "语义切分阈值",
        "type": "float",
        "min": 0,
        "max": 1,
        "step": 0.01,
        "default": 0.78,
        "description": "相邻句子/文档向量相似度低于该值视为语义转折 → 切分",
        "strategies": ["semantic", "late_chunking"],
    },
    {
        "key": "chunk_parent_size_chars",
        "label": "父块字符数",
        "type": "int",
        "min": 100,
        "max": 10000,
        "default": 1500,
        "description": "parent_child 策略：父块（上下文）目标字符数",
        "strategies": ["parent_child"],
    },
    {
        "key": "chunk_child_size_chars",
        "label": "子块字符数",
        "type": "int",
        "min": 50,
        "max": 2000,
        "default": 400,
        "description": "parent_child 策略：子块（检索单元）目标字符数",
        "strategies": ["parent_child"],
    },
    {
        "key": "chunk_llm_enabled",
        "label": "LLM 切分开关",
        "type": "bool",
        "default": False,
        "description": "启用 LLM 切分（成本高、速度慢，仅用于小规模高质量文档）",
        "strategies": ["llm"],
    },
    {
        "key": "chunk_llm_chunk_prompt",
        "label": "LLM 切分提示词",
        "type": "str",
        "default": "",
        "description": "LLM 切分时发送给模型的提示词（要求模型只输出切分后的段落 JSON 数组）",
        "strategies": ["llm"],
    },
    {
        "key": "llm_api_base_url",
        "label": "LLM API 地址",
        "type": "str",
        "default": "",
        "description": "llm 策略调用的模型 API 地址（OpenAI 兼容 Chat Completions），如 https://api.openai.com/v1、https://api.deepseek.com/v1",
        "strategies": ["llm"],
    },
    {
        "key": "llm_api_key",
        "label": "LLM API Key",
        "type": "str",
        "default": "",
        "description": "调用大模型接口的鉴权 API Key",
        "strategies": ["llm"],
    },
    {
        "key": "llm_model",
        "label": "LLM 模型名",
        "type": "str",
        "default": "",
        "description": "llm 策略使用的模型名，如 gpt-4o-mini / deepseek-chat / qwen-plus",
        "strategies": ["llm"],
    },
    {
        "key": "chunk_embedding_api_url",
        "label": "自定义 Embedding 地址",
        "type": "str",
        "default": "",
        "description": "semantic / late_chunking 策略用；留空则走 Dify /embeddings",
        "strategies": ["semantic", "late_chunking"],
    },
    {
        "key": "chunk_embedding_api_key",
        "label": "自定义 Embedding Key",
        "type": "str",
        "default": "",
        "description": "与上面的 Embedding 地址配套（OpenAI 兼容）",
        "strategies": ["semantic", "late_chunking"],
    },
]


def _default_config() -> Dict[str, Any]:
    """从当前 settings 取值生成默认 config（字段缺失时用 PROFILE_FIELDS 的 default 兜底）。"""
    cfg: Dict[str, Any] = {}
    for field in PROFILE_FIELDS:
        key = field["key"]
        cfg[key] = getattr(settings, key, field.get("default"))
    return cfg


def _now_iso() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _profiles_file() -> Path:
    return settings.data_root / CONFIGS_DIRNAME / PROFILES_FILENAME


def _ensure_profiles_file() -> None:
    """确保 profiles.json 存在；不存在则用当前 settings 生成一个默认配置方案并激活。"""
    path = _profiles_file()
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    default_profile = {
        "id": uuid.uuid4().hex,
        "name": "默认配置",
        "created_at": _now_iso(),
        "updated_at": _now_iso(),
        "config": _default_config(),
    }
    data = {"profiles": [default_profile], "active_profile_id": default_profile["id"]}
    _write(data)
    log.info("config_store: 已生成默认配置方案 id=%s", default_profile["id"])


def _write(data: Dict[str, Any]) -> None:
    import json

    path = _profiles_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fp:
        json.dump(data, fp, ensure_ascii=False, indent=2)


def _load() -> Dict[str, Any]:
    import json

    _ensure_profiles_file()
    path = _profiles_file()
    try:
        with path.open("r", encoding="utf-8") as fp:
            return json.load(fp)
    except Exception as e:  # noqa: BLE001
        log.exception("config_store: 读取 profiles.json 失败，重置为默认: %s", e)
        default_profile = {
            "id": uuid.uuid4().hex,
            "name": "默认配置",
            "created_at": _now_iso(),
            "updated_at": _now_iso(),
            "config": _default_config(),
        }
        data = {"profiles": [default_profile], "active_profile_id": default_profile["id"]}
        _write(data)
        return data


def list_profiles() -> List[Dict[str, Any]]:
    return _load().get("profiles", [])


def get_active_profile_id() -> Optional[str]:
    return _load().get("active_profile_id")


def get_profile(profile_id: str) -> Optional[Dict[str, Any]]:
    for p in list_profiles():
        if p.get("id") == profile_id:
            return p
    return None


def get_active_profile() -> Optional[Dict[str, Any]]:
    active_id = get_active_profile_id()
    if not active_id:
        return None
    return get_profile(active_id)


def resolve_profile(profile_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """解析要使用的配置方案：显式 profile_id > 当前激活方案。

    两者都没有时返回 None，由调用方决定行为（通常报错提示先配置）。
    """
    if profile_id:
        return get_profile(profile_id)
    return get_active_profile()


def create_profile(name: str, config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    data = _load()
    now = _now_iso()
    profile = {
        "id": uuid.uuid4().hex,
        "name": name.strip() or f"配置方案 {now}",
        "created_at": now,
        "updated_at": now,
        "config": _merge_config(config or {}),
    }
    data["profiles"].append(profile)
    # 若还没有激活方案，自动激活刚创建的
    if not data.get("active_profile_id"):
        data["active_profile_id"] = profile["id"]
    _write(data)
    return profile


def update_profile(profile_id: str, name: Optional[str], config: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    data = _load()
    for p in data["profiles"]:
        if p.get("id") != profile_id:
            continue
        if name is not None:
            p["name"] = name.strip() or p["name"]
        if config is not None:
            p["config"] = _merge_config(config, base=p.get("config"))
        p["updated_at"] = _now_iso()
        _write(data)
        return p
    return None


def delete_profile(profile_id: str) -> bool:
    data = _load()
    before = len(data["profiles"])
    data["profiles"] = [p for p in data["profiles"] if p.get("id") != profile_id]
    if len(data["profiles"]) == before:
        return False
    if data.get("active_profile_id") == profile_id:
        # 删掉激活方案 → 激活剩余第一个（若还有）
        data["active_profile_id"] = data["profiles"][0]["id"] if data["profiles"] else None
    _write(data)
    return True


def activate_profile(profile_id: str) -> Optional[Dict[str, Any]]:
    profile = get_profile(profile_id)
    if not profile:
        return None
    data = _load()
    data["active_profile_id"] = profile_id
    _write(data)
    return profile


def _merge_config(config: Dict[str, Any], base: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """合并 config：base 为底（默认取 settings 当前值），config 覆盖。仅保留合法字段。"""
    merged = dict(base or _default_config())
    valid_keys = {f["key"] for f in PROFILE_FIELDS}
    for k, v in config.items():
        if k in valid_keys:
            merged[k] = v
    return merged


def get_field_schema() -> List[Dict[str, Any]]:
    """返回字段定义 + 当前 settings 实际值（作为 default）。"""
    fields = []
    for f in PROFILE_FIELDS:
        item = dict(f)
        item["default"] = getattr(settings, f["key"], f.get("default"))
        fields.append(item)
    return fields


@contextmanager
def apply_config(config: Optional[Dict[str, Any]]) -> Iterator[None]:
    """临时应用一份配置到全局 settings，离开 with 块后恢复。

    适用于「上传处理使用所选配置方案」：pipeline 各阶段（chunker / dify_ingest）
    直接读 settings.chunk_* / settings.dify_*，本函数临时覆盖这些属性即可生效。
    恢复逻辑保证即使 pipeline 抛异常也不会污染后续请求。
    """
    if not config:
        yield
        return
    saved: Dict[str, Any] = {}
    for key, value in config.items():
        if not hasattr(settings, key):
            continue
        saved[key] = getattr(settings, key)
        setattr(settings, key, value)
    try:
        yield
    finally:
        for key, value in saved.items():
            setattr(settings, key, value)
