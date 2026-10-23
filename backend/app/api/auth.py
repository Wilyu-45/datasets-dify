"""租户鉴权依赖（★ 2026-09 租户隔离）。

两种身份：
- 租户：请求头 `X-API-Key: rt-xxx`（tenants 表签发）。缺失/无效时视为
  default 租户——存量匿名调用行为完全不变，default 租户的数据即全局数据。
- 管理员：请求头 `X-Admin-Key: <RAG_ADMIN_API_KEY>`，仅保护 /api/tenants
  管理接口；RAG_ADMIN_API_KEY 未配置时管理接口整体 403（未开启管理面）。

业务接口按需挂 `require_tenant`；对不想暴露给租户的接口（如 cleanup、
config 写接口），挂 `require_admin` 或保持匿名（default 全权）——由各
路由自行决定，本模块只提供依赖。

★ 2026-10 新增开关 `RAG_REQUIRE_ADMIN_FOR_GLOBAL`（默认关）：
    开启后，全局管理类接口（清理/扫描/解析/切分/流水线/网站抓取/配置中
    心写接口）不再接受匿名请求，必须带正确的 X-Admin-Key ——因为「无
    key 即 default」意味着任何能连到端口的人都能删数据、改配置。default
    租户在 tenants 表里没有 API Key，故命名租户的 X-API-Key 也不能用来访
    问这些接口（依旧 403）。

★ 2026-10 执行模型：三个依赖都是同步 `def`（不是 `async def`）。
    require_tenant 要查 tenants 表，是一次完整的数据库往返。FastAPI 对同步
    依赖会放到线程池执行；写成 async 反而会把这段阻塞 IO 压在事件循环上，
    单 worker 部署下所有并发请求（包括前端进度轮询）都会被拖住。
"""

from __future__ import annotations

from hmac import compare_digest as _const_time_equal

from fastapi import Depends, Header, HTTPException

from app.config import settings
from app.services import tenant_store
from app.services.tenant_store import DEFAULT_TENANT_ID, Tenant


def _enc(value: str) -> bytes:
    """compare_digest 只收 bytes（或纯 ASCII str），统一编码以免非 ASCII 密钥抛 TypeError。"""
    return value.encode("utf-8", errors="surrogatepass")


def _admin_key_matches(presented: str) -> bool:
    """常量时间比较管理密钥，避免通过响应时延逐位猜测密钥。"""
    return _const_time_equal(_enc(presented), _enc(settings.admin_api_key))


def require_tenant(
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


def require_admin(
    x_admin_key: str = Header(default=None, alias="X-Admin-Key"),
) -> None:
    """管理员接口依赖。RAG_ADMIN_API_KEY 未配置 → 403（管理面未开启）。"""
    if not settings.admin_api_key:
        raise HTTPException(
            status_code=403,
            detail="管理接口未启用：请先在 backend/.env 配置 RAG_ADMIN_API_KEY",
        )
    if not x_admin_key or not _admin_key_matches(x_admin_key):
        raise HTTPException(status_code=401, detail="管理员密钥无效")


def require_default_tenant(
    tenant: Tenant = Depends(require_tenant),
    x_admin_key: str = Header(default=None, alias="X-Admin-Key"),
) -> Tenant:
    """全局管理类接口依赖：仅 default 租户（匿名调用即 default）可用。

    用于全局配置、目录扫描/解析/切分、网站抓取、清理等不按租户分区的接口，
    防止命名租户越权操作全局资源或其他租户的数据。

    ★ 2026-10：开关 `require_admin_for_global` 打开后，匿名不再等于权限：
    额外要求携带正确 X-Admin-Key；admin_api_key 未配置时 fail-closed（
    全部 403），避免「开关开了但密钥没配」反而变成完全无校验。
    """
    guard = settings.require_admin_for_global
    if tenant.tenant_id != DEFAULT_TENANT_ID:
        raise HTTPException(
            status_code=403,
            detail="该接口为全局管理接口，仅限 default 租户（匿名调用）使用"
            + ("，且需携带 X-Admin-Key" if guard else ""),
        )
    if guard:
        if not settings.admin_api_key:
            raise HTTPException(
                status_code=403,
                detail="已开启 RAG_REQUIRE_ADMIN_FOR_GLOBAL，但 RAG_ADMIN_API_KEY 为空"
                       "（backend/.env 或容器 environment 未配置），全局管理接口暂不可用",
            )
        if not x_admin_key or not _admin_key_matches(x_admin_key):
            raise HTTPException(
                status_code=401,
                detail="全局管理接口需要管理员凭据（请求头 X-Admin-Key）",
            )
    return tenant
