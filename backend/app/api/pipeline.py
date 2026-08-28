"""plan.md §3 一键流水线 API：
- POST /api/pipeline/run    一键执行 scan → parse → chunk → dify/upload 全流程
- POST /api/pipeline/dry    dry-run 模式（不实际写盘 / 不调外部 API）
- GET  /api/pipeline/status  返回最近一次流水线的状态（可选，预留）

设计要点：
- 默认全流程跑通，body 为空也接受
- 每个子步骤可独立 enabled/dry_run/force
- 出错可配 stop_on_error 决定是否中断
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.services import config_run_log, config_store
from app.services.pipeline import PipelineReport, PipelineRequest, PipelineStep, run_pipeline

router = APIRouter(tags=["pipeline"])
log = logging.getLogger("ragsystem.api.pipeline")


class PipelineStepIn(BaseModel):
    """单个步骤的入参。"""

    enabled: bool = True
    dry_run: bool = False
    force: bool = False  # chunk / dify 阶段：是否强制重做
    strategy: Optional[str] = None  # chunk 阶段：切分策略（structure/fixed/semantic/parent_child 等）


class PipelineRunRequest(BaseModel):
    """一键流水线的入参。"""

    scan: PipelineStepIn = Field(default_factory=lambda: PipelineStepIn())
    parse: PipelineStepIn = Field(default_factory=lambda: PipelineStepIn())
    chunk: PipelineStepIn = Field(default_factory=lambda: PipelineStepIn())
    dify: PipelineStepIn = Field(default_factory=lambda: PipelineStepIn())
    stop_on_error: bool = False
    # ★ 2026-08 配置中心：指定配置方案 ID；空 → 使用当前激活方案（都没有则用 .env 默认）
    profile_id: Optional[str] = None


def _to_step(in_step: PipelineStepIn) -> PipelineStep:
    return PipelineStep(
        enabled=in_step.enabled,
        dry_run=in_step.dry_run,
        force=in_step.force,
        strategy=in_step.strategy,
    )


@router.post("/pipeline/run", response_model=Dict[str, Any])
def post_pipeline_run(body: Optional[PipelineRunRequest] = None) -> Dict[str, Any]:
    """一键执行：scan → parse → chunk → dify/upload 全流程。

    Body 可空（空 = 全部跑，默认参数）。

    ★ 2026-08 配置中心：与上传入库一致，处理前应用配置方案（显式 profile_id > 当前激活方案），
    保证流水线真正使用的知识库 ID / 切分策略与配置中心展示一致；运行结束后把实际生效的
    配置快照写入 process_config_log 表。未配置任何方案时沿用 .env 默认并照样记录。
    """
    body = body or PipelineRunRequest()
    log.info(
        "api /pipeline/run called: scan=%s parse=%s chunk=%s dify=%s stop_on_error=%s",
        body.scan.enabled, body.parse.enabled, body.chunk.enabled, body.dify.enabled, body.stop_on_error,
        extra={"step": "api", "status": "pipeline_run"},
    )
    # 显式 profile_id 不存在 → 404；两者都没有 → 用 .env 默认（profile=None）
    profile = None
    if body.profile_id:
        profile = config_store.get_profile(body.profile_id)
        if not profile:
            raise HTTPException(status_code=404, detail=f"配置方案不存在：{body.profile_id}")
    else:
        profile = config_store.get_active_profile()
    try:
        req = PipelineRequest(
            scan=_to_step(body.scan),
            parse=_to_step(body.parse),
            chunk=_to_step(body.chunk),
            dify=_to_step(body.dify),
            stop_on_error=body.stop_on_error,
        )
        with config_store.apply_config(profile["config"] if profile else None):
            report: PipelineReport = run_pipeline(req)
            # ★ 配置追溯：在 apply_config 生效范围内抓快照，记录的就是本次真正用到的配置
            run_config = config_run_log.snapshot_settings_config()
        config_run_log.record_run(
            source=config_run_log.SOURCE_PIPELINE_API,
            profile=profile,
            config=run_config,
            target_stems=req.target_stems,
            status=report.status,
            error=report.error,
            duration_ms=report.duration_ms,
        )
        return report.to_dict()
    except Exception as e:  # noqa: BLE001
        log.exception("pipeline 接口异常", extra={"step": "api", "error_msg": str(e)})
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.post("/pipeline/dry", response_model=Dict[str, Any])
def post_pipeline_dry() -> Dict[str, Any]:
    """dry-run 模式：全部步骤 dry_run=True，不调外部 API、不实际写盘。

    用于一键"预演"——查看每个阶段会处理多少文件 / 哪些跳过。
    """
    log.info("api /pipeline/dry called", extra={"step": "api", "status": "pipeline_dry"})
    try:
        req = PipelineRequest(
            scan=PipelineStep(enabled=True, dry_run=True),
            parse=PipelineStep(enabled=True, dry_run=True),
            chunk=PipelineStep(enabled=True, dry_run=True),
            dify=PipelineStep(enabled=True, dry_run=True),
            stop_on_error=False,
        )
        report = run_pipeline(req)
        return report.to_dict()
    except Exception as e:  # noqa: BLE001
        log.exception("pipeline dry 接口异常", extra={"step": "api", "error_msg": str(e)})
        raise HTTPException(status_code=500, detail=str(e)) from e
