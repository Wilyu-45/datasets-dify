"""租户鉴权依赖（★ 2026-09 租户隔离）。

两种身份：
- 租户：请求头 `X-API-Key: rt-xxx`（tenants 表签发）。缺失/无效时视为
  default 租户——存量匿名调用行为完全不变，default 租户的数据即全局数据。
- 管理员：请求头 `X-Admin-Key: <RAG_ADMIN_API_KEY>`，仅保护 /api/tenants
  管理接口；RAG_ADMIN_API_KEY 未配置时管理接口整体 403（未开启管理面）。

业务接口按需挂 `require_tenant`；对不想暴露给租户的接口（如 cleanup、
config 写接口），挂 `require_admin` 或保持匿名（default 全权）——由各
路由自行决定，本模块只提供依赖。
"""

from __future__ import annotations

from fastapi import Header, HTTPException

from app.config import settings
from app.services import tenant_store
from app.services.tenant_store import DEFAULT_TENANT_ID, Tenant


async def require_tenant(
    x_api_key: str = Header(default=None, alias="X-API-Key"),
) -> Tenant:
    """业务接口的租户身份依赖。

    - 无 X-API-Key 头 → default 租户（匿名 = 全局，存量行为）
    - 带了 key 但无效/被禁用 → 401（防止误以为自己是 default）
    """
    if not x_api_key:
        return Tenant({"tenant_id": DEFAULT_TENANT_ID, "name": "默认租户（匿名）"})
    tenant = tenant_store.verify_api_key(x_api_key)
    if tenant is None:
        raise HTTPException(status_code=401, detail="无效或已禁用的租户 API Key")
    return tenant


async def require_admin(
    x_admin_key: str = Header(default=None, alias="X-Admin-Key"),
) -> None:
    """管理员接口依赖。RAG_ADMIN_API_KEY 未配置 → 403（管理面未开启）。"""
    if not settings.admin_api_key:
        raise HTTPException(
            status_code=403,
            detail="管理接口未启用：请先在 backend/.env 配置 RAG_ADMIN_API_KEY",
        )
    if not x_admin_key or x_admin_key != settings.admin_api_key:
        raise HTTPException(status_code=401, detail="管理员密钥无效")
