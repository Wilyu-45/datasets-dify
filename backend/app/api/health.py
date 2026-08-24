"""GET /api/health"""

from __future__ import annotations

import logging

from fastapi import APIRouter

from app.config import settings
from app.models.schemas import HealthInfo, MinerUHealthInfo
from app.services.mineru_client import MinerUClient

log = logging.getLogger("ragsystem.api.health")
router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthInfo)
def health() -> HealthInfo:
    # MinerU API 健康检查（调 /health 端点，10s 超时）
    mineru_info = None
    try:
        client = MinerUClient()
        result = client.health_check()
        mineru_info = MinerUHealthInfo(**result)
    except Exception as e:  # noqa: BLE001
        log.warning("mineru 健康检查异常: %s", e)
        mineru_info = MinerUHealthInfo(
            healthy=False, status="error", detail=str(e)
        )

    return HealthInfo(
        version=settings.app_version,
        data_root=str(settings.data_root),
        manifest_exists=settings.manifest_path.exists(),
        mineru=mineru_info,
    )
