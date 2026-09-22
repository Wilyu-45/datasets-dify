"""租户隔离（★ 2026-09）：tenant_store CRUD / auth 依赖 / manifest 数据隔离。

说明：tests 运行环境连本地 PG（与 conftest 的 manifest 保护同一实例），
tenants 表用唯一前缀 + 结束清理，避免污染。
"""

from __future__ import annotations

import pytest

from app.services import manifest_store, tenant_store
from app.services.tenant_store import Tenant, validate_tenant_id


_T_PREFIX = "selftest_tenant_"


@pytest.fixture()
def _cleanup_tenants():
    yield
    for t in tenant_store.list_tenants():
        if t.tenant_id.startswith(_T_PREFIX):
            tenant_store.delete_tenant(t.tenant_id)


def _tid(suffix: str) -> str:
    return f"{_T_PREFIX}{suffix}"


# ============ tenant_id 校验 ============


def test_validate_tenant_id_accepts_lowercase_and_digits():
    assert validate_tenant_id("acme")
    assert validate_tenant_id("a1-b_c")


def test_validate_tenant_id_rejects_bad_input():
    assert not validate_tenant_id("")
    assert not validate_tenant_id("UPPER")
    assert not validate_tenant_id("-leading")
    assert not validate_tenant_id("空格 x")
    assert not validate_tenant_id("x" * 65)


# ============ CRUD ============


@pytest.mark.usefixtures("_cleanup_tenants")
def test_create_and_get_tenant():
    tenant, api_key = tenant_store.create_tenant(
        _tid("basic"), name="基础测试", dify_dataset_id="ds-111"
    )
    assert api_key.startswith("rt-")
    assert tenant.tenant_id == _tid("basic")
    assert tenant.dify_dataset_id == "ds-111"
    assert tenant.is_active
    # 明文 key 不落库（只存哈希）
    got = tenant_store.get_tenant(_tid("basic"))
    assert got.to_public_dict().get("api_key_hash") is None
    assert got.data["api_key_hash"] == tenant_store.hash_api_key(api_key)


@pytest.mark.usefixtures("_cleanup_tenants")
def test_create_tenant_rejects_duplicate_default_and_missing_dataset():
    tenant_store.create_tenant(_tid("dup"), dify_dataset_id="ds-1")
    with pytest.raises(ValueError):
        tenant_store.create_tenant(_tid("dup"), dify_dataset_id="ds-2")
    with pytest.raises(ValueError):
        tenant_store.create_tenant("default", dify_dataset_id="ds-3")
    with pytest.raises(ValueError):
        tenant_store.create_tenant(_tid("nods"), dify_dataset_id="  ")
    with pytest.raises(ValueError):
        tenant_store.create_tenant("Bad ID", dify_dataset_id="ds-4")


@pytest.mark.usefixtures("_cleanup_tenants")
def test_update_tenant_fields():
    tenant_store.create_tenant(_tid("upd"), dify_dataset_id="ds-old")
    updated = tenant_store.update_tenant(
        _tid("upd"), {"dify_dataset_id": "ds-new", "status": "disabled"}
    )
    assert updated.dify_dataset_id == "ds-new"
    assert updated.status == "disabled"


@pytest.mark.usefixtures("_cleanup_tenants")
def test_rotate_api_key_invalidates_old():
    _t, old_key = tenant_store.create_tenant(_tid("rot"), dify_dataset_id="ds-1")
    assert tenant_store.verify_api_key(old_key).tenant_id == _tid("rot")
    _t2, new_key = tenant_store.rotate_api_key(_tid("rot"))
    assert new_key != old_key
    assert tenant_store.verify_api_key(old_key) is None
    assert tenant_store.verify_api_key(new_key).tenant_id == _tid("rot")


@pytest.mark.usefixtures("_cleanup_tenants")
def test_delete_tenant_and_default_protection():
    tenant_store.create_tenant(_tid("del"), dify_dataset_id="ds-1")
    assert tenant_store.delete_tenant(_tid("del"))
    assert tenant_store.get_tenant(_tid("del")) is None
    with pytest.raises(ValueError):
        tenant_store.delete_tenant("default")


# ============ 鉴权 ============


@pytest.mark.usefixtures("_cleanup_tenants")
def test_verify_api_key_disabled_tenant_rejected():
    _t, api_key = tenant_store.create_tenant(_tid("dis"), dify_dataset_id="ds-1")
    assert tenant_store.verify_api_key(api_key) is not None
    tenant_store.update_tenant(_tid("dis"), {"status": "disabled"})
    assert tenant_store.verify_api_key(api_key) is None
    assert tenant_store.verify_api_key(None) is None
    assert tenant_store.verify_api_key("rt-garbage") is None


@pytest.mark.usefixtures("_cleanup_tenants")
def test_require_tenant_dependency(monkeypatch):
    import asyncio

    from app.api import auth

    # 无头 → default 租户（匿名兼容）
    t = asyncio.run(auth.require_tenant(x_api_key=None))
    assert t.tenant_id == "default"
    # 有效 key → 对应租户
    _tenant, api_key = tenant_store.create_tenant(_tid("auth"), dify_dataset_id="ds-1")
    t = asyncio.run(auth.require_tenant(x_api_key=api_key))
    assert t.tenant_id == _tid("auth")
    # 非法 key → HTTPException 401
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        asyncio.run(auth.require_tenant(x_api_key="rt-invalid"))
    assert exc.value.status_code == 401


def test_require_admin_dependency(monkeypatch):
    import asyncio

    from fastapi import HTTPException

    from app.api import auth

    monkeypatch.setattr(auth.settings, "admin_api_key", "")
    with pytest.raises(HTTPException) as exc:
        asyncio.run(auth.require_admin(x_admin_key="whatever"))
    assert exc.value.status_code == 403  # 未配置 = 管理面关闭

    monkeypatch.setattr(auth.settings, "admin_api_key", "adm-secret")
    with pytest.raises(HTTPException) as exc:
        asyncio.run(auth.require_admin(x_admin_key="wrong"))
    assert exc.value.status_code == 401
    assert asyncio.run(auth.require_admin(x_admin_key="adm-secret")) is None


# ============ manifest 数据隔离 ============


@pytest.mark.usefixtures("_cleanup_tenants")
def test_manifest_same_filename_across_tenants_isolated():
    from app.models.schemas import ManifestRow

    tenant_store.create_tenant(_tid("iso"), dify_dataset_id="ds-1")
    t1, t2 = "default", _tid("iso")
    now = manifest_store.now_iso()
    for tid in (t1, t2):
        manifest_store.upsert(row=ManifestRow(
            filename="同名文件.pdf",
            status="new",
            tenant_id=tid,
            create_time=now,
            update_time=now,
        ))

    rows_default = manifest_store.load(tenant_id=t1)
    rows_tenant = manifest_store.load(tenant_id=t2)
    assert rows_default["同名文件.pdf"].tenant_id == "default"
    assert rows_tenant["同名文件.pdf"].tenant_id == _tid("iso")

    # 精确 fetch 按租户过滤
    assert manifest_store.fetch("同名文件.pdf", tenant_id=t2) is not None
    assert manifest_store.fetch("同名文件.pdf", tenant_id="不存在的租户") is None

    # 更新互不影响
    manifest_store.update_fields(
        "同名文件.pdf", {"process_status": "租户A处理中"}, tenant_id=t2
    )
    assert (
        manifest_store.fetch("同名文件.pdf", tenant_id=t1).process_status
        != "租户A处理中"
    )
    assert (
        manifest_store.fetch("同名文件.pdf", tenant_id=t2).process_status
        == "租户A处理中"
    )

    # 清理
    manifest_store.delete("同名文件.pdf", tenant_id=t1)
    manifest_store.delete("同名文件.pdf", tenant_id=t2)


def test_tenant_dir_layout_for_named_and_default():
    from app.config import settings

    assert settings.pending_dir_of("default") == settings.pending_dir
    assert settings.pending_dir_of(None) == settings.pending_dir
    t = settings.pending_dir_of("acme")
    assert t != settings.pending_dir
    assert t.name == "acme" and t.parent == settings.pending_dir
