"""GET /api/manifest?limit=&offset="""

from __future__ import annotations

from fastapi import APIRouter, Query

from app.config import settings
from app.models.schemas import ManifestPage
from app.services import manifest_store

router = APIRouter(tags=["manifest"])


@router.get("/manifest", response_model=ManifestPage)
def get_manifest(
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
) -> ManifestPage:
    """分页读取 manifest 全表。"""
    rows_dict = manifest_store.load(settings.manifest_path)
    all_rows = list(rows_dict.values())
    # 按 update_time 倒序，缺失视为空串（稳定排序）
    all_rows.sort(
        key=lambda r: (r.update_time or "", r.filename or ""), reverse=True
    )
    total = len(all_rows)
    page_rows = all_rows[offset : offset + limit]
    return ManifestPage(total=total, limit=limit, offset=offset, rows=page_rows)
