"""租户管理 API（管理员专用，★ 2026-09 租户隔离）。

全部接口要求请求头 `X-Admin-Key: <RAG_ADMIN_API_KEY>`。
租户 API Key 明文仅在创建（POST /tenants）与轮换（POST .../rotate-key）
的响应中出现一次，库中只存 sha256。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.api.auth import require_admin
from app.services import tenant_store

router = APIRouter(prefix="/tenants", tags=["tenants"])


class TenantCreate(BaseModel):
    tenant_id: str
    name: str = ""
    dify_dataset_id: str


class TenantUpdate(BaseModel):
    name: str | None = None
    dify_dataset_id: str | None = None
    # active / disabled
    status: str | None = None


@router.post("", status_code=201)
def create_tenant(body: TenantCreate, _: None = Depends(require_admin)) -> dict:
    try:
        tenant, api_key = tenant_store.create_tenant(
            tenant_id=body.tenant_id,
            name=body.name or None,
            dify_dataset_id=body.dify_dataset_id,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"tenant": tenant.to_public_dict(), "api_key": api_key}


@router.get("")
def list_tenants(_: None = Depends(require_admin)) -> dict:
    return {"tenants": [t.to_public_dict() for t in tenant_store.list_tenants()]}


@router.get("/{tenant_id}")
def get_tenant(tenant_id: str, _: None = Depends(require_admin)) -> dict:
    tenant = tenant_store.get_tenant(tenant_id)
    if tenant is None:
        raise HTTPException(status_code=404, detail="租户不存在")
    return tenant.to_public_dict()


@router.patch("/{tenant_id}")
def update_tenant(tenant_id: str, body: TenantUpdate,
                  _: None = Depends(require_admin)) -> dict:
    if tenant_store.get_tenant(tenant_id) is None:
        raise HTTPException(status_code=404, detail="租户不存在")
    if body.status not in (None, "active", "disabled"):
        raise HTTPException(status_code=400, detail="status 只允许 active / disabled")
    tenant = tenant_store.update_tenant(tenant_id, body.model_dump(exclude_none=True))
    return tenant.to_public_dict()


@router.post("/{tenant_id}/rotate-key")
def rotate_key(tenant_id: str, _: None = Depends(require_admin)) -> dict:
    tenant, api_key = tenant_store.rotate_api_key(tenant_id)
    if tenant is None:
        raise HTTPException(status_code=404, detail="租户不存在")
    return {"tenant": tenant.to_public_dict(), "api_key": api_key}


@router.delete("/{tenant_id}", status_code=204)
def delete_tenant(tenant_id: str, _: None = Depends(require_admin)) -> None:
    try:
        deleted = tenant_store.delete_tenant(tenant_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not deleted:
        raise HTTPException(status_code=404, detail="租户不存在")
