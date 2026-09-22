"""租户存储与鉴权支撑（★ 2026-09 租户隔离）。

数据模型（tenants 表）：
    tenant_id       租户标识（小写字母/数字/短横线，如 "acme"）
    name            显示名
    dify_dataset_id 该租户绑定的 Dify 知识库（dataset）
    api_key_hash    租户 API Key 的 sha256（明文仅在创建/轮换时返回一次）
    status          active / disabled

内置 default 租户：映射全局 settings.dify_dataset_id，对应存量数据与
未隔离目录布局；default 不参与 key 鉴权（匿名请求即视为 default 租户），
因此存量调用与既有测试完全不受影响。
"""

from __future__ import annotations

import hashlib
import logging
import re
import secrets
import threading
from typing import Dict, List, Optional

from app import db
from app.config import settings

log = logging.getLogger("ragsystem.services.tenant_store")

DEFAULT_TENANT_ID = "default"

_TENANT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")

_write_lock = threading.RLock()


def hash_api_key(api_key: str) -> str:
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()


def generate_api_key() -> str:
    """生成 rt- 前缀的租户 API Key（256 bit 熵）。"""
    return "rt-" + secrets.token_urlsafe(30)


def validate_tenant_id(tenant_id: str) -> bool:
    return bool(_TENANT_ID_RE.match(tenant_id or ""))


class Tenant:
    """租户记录（dict 的轻量包装，便于 IDE 提示）。"""

    def __init__(self, data: Dict) -> None:
        self.data = data

    @property
    def tenant_id(self) -> str:
        return self.data.get("tenant_id") or ""

    @property
    def name(self) -> Optional[str]:
        return self.data.get("name")

    @property
    def dify_dataset_id(self) -> Optional[str]:
        return self.data.get("dify_dataset_id")

    @property
    def status(self) -> str:
        return self.data.get("status") or "active"

    @property
    def is_active(self) -> bool:
        return self.status == "active"

    def to_public_dict(self) -> Dict:
        """对外展示（不含 key 哈希）。"""
        return {k: v for k, v in self.data.items() if k != "api_key_hash"}


def create_tenant(
    tenant_id: str,
    name: Optional[str] = None,
    dify_dataset_id: Optional[str] = None,
) -> tuple:
    """创建租户。返回 (Tenant, api_key明文)。api_key 明文仅此一次返回。

    Raises:
        ValueError: tenant_id 非法 / 已存在 / dataset_id 为空
    """
    tenant_id = (tenant_id or "").strip().lower()
    if not validate_tenant_id(tenant_id):
        raise ValueError("tenant_id 只允许小写字母、数字、短横线，最长 64 字符")
    if tenant_id == DEFAULT_TENANT_ID:
        raise ValueError("default 是内置租户标识，不能重复创建")
    if not (dify_dataset_id or "").strip():
        raise ValueError("必须绑定 dify_dataset_id（该租户的知识库）")
    api_key = generate_api_key()
    from app.services.manifest_store import now_iso as _now  # 复用同一时间格式

    with _write_lock:
        if get_tenant(tenant_id) is not None:
            raise ValueError(f"租户 {tenant_id} 已存在")
        with db.get_conn() as conn:
            conn.execute(
                "INSERT INTO tenants (tenant_id, name, dify_dataset_id, api_key_hash,"
                " status, create_time, update_time) VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (tenant_id, name, dify_dataset_id.strip(),
                 hash_api_key(api_key), "active", _now(), _now()),
            )
            conn.commit()
    log.info("租户已创建: %s → dataset %s", tenant_id, dify_dataset_id)
    return get_tenant(tenant_id), api_key


def get_tenant(tenant_id: str) -> Optional[Tenant]:
    with db.get_conn() as conn:
        cur = conn.execute(
            "SELECT * FROM tenants WHERE tenant_id = %s", (tenant_id,)
        )
        rec = cur.fetchone()
    return Tenant(rec) if rec else None


def list_tenants() -> List[Tenant]:
    with db.get_conn() as conn:
        cur = conn.execute("SELECT * FROM tenants ORDER BY tenant_id")
        return [Tenant(r) for r in cur.fetchall()]


def update_tenant(tenant_id: str, fields: Dict) -> Optional[Tenant]:
    """更新租户的 name / dify_dataset_id / status。"""
    allowed = {k: v for k, v in fields.items()
               if k in ("name", "dify_dataset_id", "status") and v is not None}
    if not allowed:
        return get_tenant(tenant_id)
    from app.services.manifest_store import now_iso as _now

    sets = ", ".join(f"{k} = %s" for k in allowed)
    params = list(allowed.values()) + [_now(), tenant_id]
    with _write_lock:
        with db.get_conn() as conn:
            conn.execute(
                f"UPDATE tenants SET {sets}, update_time = %s WHERE tenant_id = %s",
                params,
            )
            conn.commit()
    return get_tenant(tenant_id)


def rotate_api_key(tenant_id: str) -> tuple:
    """轮换租户 API Key。返回 (Tenant, 新明文 key)。旧 key 立即失效。"""
    tenant = get_tenant(tenant_id)
    if tenant is None:
        return None, ""
    api_key = generate_api_key()
    from app.services.manifest_store import now_iso as _now

    with _write_lock:
        with db.get_conn() as conn:
            conn.execute(
                "UPDATE tenants SET api_key_hash = %s, update_time = %s WHERE tenant_id = %s",
                (hash_api_key(api_key), _now(), tenant_id),
            )
            conn.commit()
    return get_tenant(tenant_id), api_key


def delete_tenant(tenant_id: str) -> bool:
    """删除租户（default 不可删）。其 manifest/doc_metadata 行保留但成为孤儿数据。"""
    if tenant_id == DEFAULT_TENANT_ID:
        raise ValueError("default 租户不可删除")
    with _write_lock:
        with db.get_conn() as conn:
            cur = conn.execute("DELETE FROM tenants WHERE tenant_id = %s", (tenant_id,))
            conn.commit()
    return cur.rowcount > 0 if hasattr(cur, "rowcount") else True


def verify_api_key(api_key: Optional[str]) -> Optional[Tenant]:
    """按 API Key 查租户；无效/禁用返回 None。default 租户无 key（匿名即 default）。"""
    if not api_key:
        return None
    h = hash_api_key(api_key)
    with db.get_conn() as conn:
        cur = conn.execute(
            "SELECT * FROM tenants WHERE api_key_hash = %s AND status = 'active'",
            (h,),
        )
        rec = cur.fetchone()
    return Tenant(rec) if rec else None


def bootstrap_default_tenant() -> None:
    """启动时确保 default 租户存在，绑定全局 dataset（幂等）。"""
    if get_tenant(DEFAULT_TENANT_ID) is not None:
        return
    from app.services.manifest_store import now_iso as _now

    with _write_lock:
        with db.get_conn() as conn:
            conn.execute(
                "INSERT INTO tenants (tenant_id, name, dify_dataset_id, api_key_hash,"
                " status, create_time, update_time) VALUES (%s, %s, %s, %s, %s, %s, %s)"
                " ON CONFLICT (tenant_id) DO NOTHING"
                if settings.rag_db_type != "mysql"
                else "INSERT IGNORE INTO tenants (tenant_id, name, dify_dataset_id,"
                " api_key_hash, status, create_time, update_time)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (DEFAULT_TENANT_ID, "默认租户（全局）", settings.dify_dataset_id,
                 None, "active", _now(), _now()),
            )
            conn.commit()
    log.info("default 租户已就绪（dataset=%s）", settings.dify_dataset_id)
