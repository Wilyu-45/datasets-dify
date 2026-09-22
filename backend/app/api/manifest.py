"""GET /api/manifest?limit=&offset=  +  PATCH /api/manifest/{filename}（web 端编辑清单元数据）"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query

from app.api.auth import require_tenant
from app.models.schemas import ManifestPage, ManifestRow, ManifestUpdate
from app.services import manifest_store
from app.services.tenant_store import DEFAULT_TENANT_ID, Tenant

router = APIRouter(tags=["manifest"])


@router.get("/manifest", response_model=ManifestPage)
def get_manifest(
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    tenant: Tenant = Depends(require_tenant),
) -> ManifestPage:
    """分页读取 manifest 全表（命名租户只见自己的行，default 见全部）。"""
    tid = None if tenant.tenant_id == DEFAULT_TENANT_ID else tenant.tenant_id
    rows_dict = manifest_store.load(tenant_id=tid)
    all_rows = list(rows_dict.values())
    # 按 update_time 倒序，缺失视为空串（稳定排序）
    all_rows.sort(
        key=lambda r: (r.update_time or "", r.filename or ""), reverse=True
    )
    total = len(all_rows)
    page_rows = all_rows[offset : offset + limit]
    return ManifestPage(total=total, limit=limit, offset=offset, rows=page_rows)


@router.patch("/manifest/{filename}", response_model=ManifestRow)
def update_manifest_row(
    filename: str,
    body: ManifestUpdate,
    tenant: Tenant = Depends(require_tenant),
) -> ManifestRow:
    """web 端编辑清单元数据（替代原 Excel 填列）。

    仅更新显式传入的字段（PATCH 语义），其余列保持不变。
    """
    tid = None if tenant.tenant_id == DEFAULT_TENANT_ID else tenant.tenant_id
    fields = {
        k: v
        for k, v in body.model_dump().items()
        if k in body.model_fields_set and v is not None
    }
    row = manifest_store.update_fields(filename, fields, tenant_id=tid)
    if row is None:
        raise HTTPException(status_code=404, detail=f"清单中不存在「{filename}」")
    return row
