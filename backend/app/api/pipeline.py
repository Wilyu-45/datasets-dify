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
    """
    body = body or PipelineRunRequest()
    log.info(
        "api /pipeline/run called: scan=%s parse=%s chunk=%s dify=%s stop_on_error=%s",
        body.scan.enabled, body.parse.enabled, body.chunk.enabled, body.dify.enabled, body.stop_on_error,
        extra={"step": "api", "status": "pipeline_run"},
    )
    try:
        req = PipelineRequest(
            scan=_to_step(body.scan),
            parse=_to_step(body.parse),
            chunk=_to_step(body.chunk),
            dify=_to_step(body.dify),
            stop_on_error=body.stop_on_error,
        )
        report: PipelineReport = run_pipeline(req)
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
