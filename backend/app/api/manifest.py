"""GET /api/manifest?limit=&offset=  +  PATCH /api/manifest/{filename}（web 端编辑清单元数据）"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from app.models.schemas import ManifestPage, ManifestRow, ManifestUpdate
from app.services import manifest_store

router = APIRouter(tags=["manifest"])


@router.get("/manifest", response_model=ManifestPage)
def get_manifest(
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
) -> ManifestPage:
    """分页读取 manifest 全表。"""
    rows_dict = manifest_store.load()
    all_rows = list(rows_dict.values())
    # 按 update_time 倒序，缺失视为空串（稳定排序）
    all_rows.sort(
        key=lambda r: (r.update_time or "", r.filename or ""), reverse=True
    )
    total = len(all_rows)
    page_rows = all_rows[offset : offset + limit]
    return ManifestPage(total=total, limit=limit, offset=offset, rows=page_rows)


@router.patch("/manifest/{filename}", response_model=ManifestRow)
def update_manifest_row(filename: str, body: ManifestUpdate) -> ManifestRow:
    """web 端编辑清单元数据（替代原 Excel 填列）。

    仅更新显式传入的字段（PATCH 语义），其余列保持不变。
    """
    fields = {
        k: v
        for k, v in body.model_dump().items()
        if k in body.model_fields_set and v is not None
    }
    row = manifest_store.update_fields(filename, fields)
    if row is None:
        raise HTTPException(status_code=404, detail=f"清单中不存在「{filename}」")
    return row
