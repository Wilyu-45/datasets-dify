"""配置中心 API（2026-08 新增）。

管理「配置方案（Profile）」：
    - 每个配置方案 = 知识库 ID + 切分策略 + 全部切分参数
    - 支持多套方案，激活其中一套作为「当前配置」
    - 上传处理时使用所选（默认激活）配置方案

端点：
    GET    /api/config/profiles        所有配置方案 + 当前激活 id
    POST   /api/config/profiles        创建方案
    PUT    /api/config/profiles/{id}   更新方案
    DELETE /api/config/profiles/{id}   删除方案
    POST   /api/config/profiles/{id}/activate  激活方案
    GET    /api/config/active          当前激活方案（含字段定义）
    GET    /api/config/schema          可配置字段定义（前端动态渲染表单）
    GET    /api/config/run-logs        处理配置记录（每次实际处理时落库的配置快照）
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.services import config_run_log, config_store

router = APIRouter(prefix="/config", tags=["config"])
log = logging.getLogger("ragsystem.api.config")


class ProfileCreateBody(BaseModel):
    name: str
    config: Optional[Dict[str, Any]] = None


class ProfileUpdateBody(BaseModel):
    name: Optional[str] = None
    config: Optional[Dict[str, Any]] = None


class ProfileOut(BaseModel):
    id: str
    name: str
    created_at: str
    updated_at: str
    config: Dict[str, Any]


class ProfilesResponse(BaseModel):
    profiles: List[ProfileOut]
    active_profile_id: Optional[str] = None


@router.get("/profiles", response_model=ProfilesResponse)
def get_profiles() -> ProfilesResponse:
    profiles = [ProfileOut(**p) for p in config_store.list_profiles()]
    return ProfilesResponse(
        profiles=profiles,
        active_profile_id=config_store.get_active_profile_id(),
    )


@router.post("/profiles", response_model=ProfileOut)
def create_profile(body: ProfileCreateBody) -> ProfileOut:
    profile = config_store.create_profile(body.name, body.config)
    return ProfileOut(**profile)


@router.put("/profiles/{profile_id}", response_model=ProfileOut)
def update_profile(profile_id: str, body: ProfileUpdateBody) -> ProfileOut:
    profile = config_store.update_profile(profile_id, body.name, body.config)
    if not profile:
        raise HTTPException(status_code=404, detail="配置方案不存在")
    return ProfileOut(**profile)


@router.delete("/profiles/{profile_id}")
def delete_profile(profile_id: str) -> Dict[str, Any]:
    ok = config_store.delete_profile(profile_id)
    if not ok:
        raise HTTPException(status_code=404, detail="配置方案不存在")
    return {"ok": True, "active_profile_id": config_store.get_active_profile_id()}


@router.post("/profiles/{profile_id}/activate", response_model=ProfileOut)
def activate_profile(profile_id: str) -> ProfileOut:
    profile = config_store.activate_profile(profile_id)
    if not profile:
        raise HTTPException(status_code=404, detail="配置方案不存在")
    return ProfileOut(**profile)


class ActiveConfigResponse(BaseModel):
    profile: Optional[ProfileOut] = None
    fields: List[Dict[str, Any]]


@router.get("/active", response_model=ActiveConfigResponse)
def get_active_config() -> ActiveConfigResponse:
    """当前激活配置方案 + 字段定义（供前端展示「当前配置」卡片）。"""
    profile = config_store.get_active_profile()
    return ActiveConfigResponse(
        profile=ProfileOut(**profile) if profile else None,
        fields=config_store.get_field_schema(),
    )


class SchemaResponse(BaseModel):
    fields: List[Dict[str, Any]]


@router.get("/schema", response_model=SchemaResponse)
def get_schema() -> SchemaResponse:
    """所有可配置字段定义（含当前 settings 实际值）。前端据此动态渲染表单。"""
    return SchemaResponse(fields=config_store.get_field_schema())


# ============ 处理配置记录（2026-08 新增） ============


class RunConfigLogItem(BaseModel):
    """一条处理配置记录（process_config_log 表的一行）。"""

    id: int
    run_time: Optional[str] = None
    source: Optional[str] = None
    profile_id: Optional[str] = None
    profile_name: Optional[str] = None
    config: Dict[str, Any]
    target_stems: List[str] = []
    status: Optional[str] = None
    error: Optional[str] = None
    duration_ms: Optional[int] = None


class RunConfigLogsResponse(BaseModel):
    total: int
    rows: List[RunConfigLogItem]


@router.get("/run-logs", response_model=RunConfigLogsResponse)
def get_run_config_logs(limit: int = 50) -> RunConfigLogsResponse:
    """最近的处理配置记录（每次实际触发处理时落库的配置快照，按时间倒序）。

    用途：追溯「这批文档当时是用哪个配置方案 / 哪些切分参数处理入库的」。
    API Key 类字段（llm_api_key / chunk_embedding_api_key）已脱敏。
    """
    rows = config_run_log.list_runs(limit=limit)
    items = []
    for r in rows:
        cfg = r.get("config") or {}
        stems = r.get("target_stems") or []
        items.append(
            RunConfigLogItem(
                id=int(r.get("id") or 0),
                run_time=r.get("run_time"),
                source=r.get("source"),
                profile_id=r.get("profile_id"),
                profile_name=r.get("profile_name"),
                config=cfg if isinstance(cfg, dict) else {},
                target_stems=stems if isinstance(stems, list) else [],
                status=r.get("status"),
                error=r.get("error"),
                duration_ms=r.get("duration_ms"),
            )
        )
    return RunConfigLogsResponse(total=len(items), rows=items)
