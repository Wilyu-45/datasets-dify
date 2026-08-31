"""处理配置运行记录（process_config_log 表）。

★ 2026-08 配置中心配套：处理时使用的配置信息记录到数据库。

每次实际触发处理（上传单文件/批量入库、重跑入库、/api/pipeline/run）时，
把当时生效的配置快照写入 PostgreSQL `process_config_log` 表，用于事后追溯：
    「这批文档当时是用哪个配置方案、哪些切分参数处理入库的」。

设计要点：
- 配置项以 JSONB 快照落库（不随 profiles.json 后续修改而变化）
- API Key 类字段（llm_api_key / chunk_embedding_api_key）落库前脱敏
- 记录失败只打日志、绝不影响处理主流程（record_run 内部吞异常）
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, List, Optional

import psycopg
from psycopg.types.json import Jsonb

from app import db
from app.config import settings

log = logging.getLogger("ragsystem.config_run_log")

# 落库前需要脱敏的配置项（只记录是否存在，不记录明文）
SECRET_KEYS = {"llm_api_key", "chunk_embedding_api_key"}

_SECRET_MASK = "******"

# 记录来源标识（source 字段取值）
SOURCE_UPLOAD_SINGLE = "upload_single"      # 单文件上传 + 一键入库
SOURCE_UPLOAD_BATCH = "upload_batch"        # 批量上传 + 一键入库
SOURCE_UPLOAD_REINGEST = "upload_reingest"  # 已上传文件单独重跑入库
SOURCE_PIPELINE_API = "pipeline_api"        # /api/pipeline/run 直接触发
SOURCE_WEBSCRAPE = "webscrape"              # 网站抓取 + 一键入库


def snapshot_settings_config() -> Dict[str, Any]:
    """从当前 settings 抽取全部可配置字段值（用于没有配置方案时的快照）。"""
    from app.services.config_store import PROFILE_FIELDS

    return {f["key"]: getattr(settings, f["key"], f.get("default")) for f in PROFILE_FIELDS}


def _mask_secrets(config: Dict[str, Any]) -> Dict[str, Any]:
    masked = dict(config)
    for key in SECRET_KEYS:
        if key in masked and masked[key]:
            masked[key] = _SECRET_MASK
    return masked


def record_run(
    source: str,
    profile: Optional[Dict[str, Any]] = None,
    config: Optional[Dict[str, Any]] = None,
    target_stems: Optional[List[str]] = None,
    status: Optional[str] = None,
    error: Optional[str] = None,
    duration_ms: Optional[int] = None,
) -> None:
    """写入一条处理配置记录。

    Args:
        source: 触发来源（SOURCE_* 常量之一）。
        profile: 使用的配置方案（含 id/name）；没有方案时传 None（仅用于归属标识）。
        config: ★ 实际生效的配置快照。调用方应在处理期间（apply_config 生效范围内）
            用 snapshot_settings_config() 抓取，保证记录的就是 chunker/dify 真正读到的值；
            不传则退化为 profile.config / 当前 settings 快照。
        target_stems: 本批处理的目标文件 stem 列表（可选）。
        status: 流水线总状态（ok/partial/failed）。
        error: 失败信息（可选）。
        duration_ms: 流水线总耗时（毫秒，可选）。

    任何异常都只打日志，不影响处理主流程。
    """
    try:
        eff_config = config
        if eff_config is None and profile:
            eff_config = profile.get("config")
        if eff_config is None:
            eff_config = snapshot_settings_config()
        eff_config = eff_config or {}
        row = {
            "run_time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "source": source,
            "profile_id": (profile or {}).get("id"),
            "profile_name": (profile or {}).get("name"),
            "dataset_id": eff_config.get("dify_dataset_id") or None,
            "chunk_strategy": eff_config.get("chunk_strategy") or None,
            "config": Jsonb(_mask_secrets(eff_config)),
            "target_stems": Jsonb(target_stems or []),
            "status": status,
            "error": error,
            "duration_ms": duration_ms,
        }
        with db.get_conn() as conn:
            conn.execute(
                """
                INSERT INTO process_config_log
                    (run_time, source, profile_id, profile_name, dataset_id,
                     chunk_strategy, config, target_stems, status, error, duration_ms)
                VALUES
                    (%(run_time)s, %(source)s, %(profile_id)s, %(profile_name)s, %(dataset_id)s,
                     %(chunk_strategy)s, %(config)s, %(target_stems)s, %(status)s, %(error)s,
                     %(duration_ms)s)
                """,
                row,
            )
            conn.commit()
        log.info(
            "process_config_log 已记录: source=%s profile=%s dataset=%s strategy=%s stems=%s status=%s",
            source, (profile or {}).get("name"),
            row["dataset_id"], row["chunk_strategy"], target_stems, status,
        )
    except Exception:  # noqa: BLE001
        log.exception("process_config_log 记录失败（不影响处理主流程）")


def list_runs(limit: int = 50) -> List[Dict[str, Any]]:
    """按时间倒序列出最近的处理配置记录。"""
    limit = max(1, min(int(limit), 500))
    with db.get_conn() as conn:
        cur = conn.execute(
            """
            SELECT id, run_time, source, profile_id, profile_name, dataset_id,
                   chunk_strategy, config, target_stems, status, error, duration_ms
            FROM process_config_log
            ORDER BY id DESC
            LIMIT %s
            """,
            (limit,),
        )
        rows = cur.fetchall()
    out: List[Dict[str, Any]] = []
    for r in rows:
        item = dict(r)
        for col in ("config", "target_stems"):
            raw = item.get(col)
            if isinstance(raw, str):
                try:
                    item[col] = json.loads(raw)
                except json.JSONDecodeError:
                    pass
        out.append(item)
    return out
