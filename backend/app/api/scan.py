"""POST /api/scan  — plan.md §3.1 入口"""

from __future__ import annotations

import logging

from fastapi import APIRouter

from app.models.schemas import ScanReport, ScanRequest
from app.services import scanner

router = APIRouter(tags=["scan"])
log = logging.getLogger("ragsystem.api.scan")


@router.post("/scan", response_model=ScanReport)
def post_scan(body: ScanRequest | None = None) -> ScanReport:
    """执行 §3.1 扫描。`dry_run=true` 不移动文件、不写 manifest；`force=true` 强制重扫。"""
    body = body or ScanRequest()
    dry = bool(body.dry_run)
    force = bool(body.force)
    log.info(
        "api /scan called",
        extra={"step": "api", "status": "scan", "dry_run": dry, "force": force},
    )
    return scanner.scan_and_stage(dry_run=dry, force=force)
