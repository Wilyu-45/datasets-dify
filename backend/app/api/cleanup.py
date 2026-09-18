"""POST /api/cleanup + GET /api/cleanup/status — 手动文件清理入口。

2026-09 生产部署改造（计划 C4）：除 lifespan 定时任务外，管理员可
手动触发清理。POST 支持 dry_run 预览；status 返回最近一次执行结果
（定时任务与手动触发共享，见 services.cleanup._last_report）。
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from fastapi import APIRouter

from app.models.schemas import CleanupRequest
from app.services.cleanup import get_last_report, run_cleanup

router = APIRouter(tags=["cleanup"])
log = logging.getLogger("ragsystem.api.cleanup")


@router.post("/cleanup")
def post_cleanup(body: CleanupRequest | None = None) -> Dict[str, Any]:
    """立即执行一轮文件清理，返回报告。dry_run=True 只统计不删除。"""
    body = body or CleanupRequest()
    dry = bool(body.dry_run)
    log.info("api /cleanup called", extra={"step": "api", "status": "cleanup", "dry_run": dry})
    report = run_cleanup(dry_run=dry)
    return report.to_dict()


@router.get("/cleanup/status")
def get_cleanup_status() -> Dict[str, Any]:
    """返回最近一次清理结果；进程启动后从未执行过清理则 last_run=null。"""
    report = get_last_report()
    if report is None:
        return {"last_run": None}
    return {"last_run": report.to_dict()}
