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
    from app.api import auth

    # 无头 → default 租户（匿名兼容）
    t = auth.require_tenant(x_api_key=None)
    assert t.tenant_id == "default"
    # 有效 key → 对应租户
    _tenant, api_key = tenant_store.create_tenant(_tid("auth"), dify_dataset_id="ds-1")
    t = auth.require_tenant(x_api_key=api_key)
    assert t.tenant_id == _tid("auth")
    # 非法 key → HTTPException 401
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        auth.require_tenant(x_api_key="rt-invalid")
    assert exc.value.status_code == 401


def test_require_admin_dependency(monkeypatch):
    from fastapi import HTTPException

    from app.api import auth

    monkeypatch.setattr(auth.settings, "admin_api_key", "")
    with pytest.raises(HTTPException) as exc:
        auth.require_admin(x_admin_key="whatever")
    assert exc.value.status_code == 403  # 未配置 = 管理面关闭

    monkeypatch.setattr(auth.settings, "admin_api_key", "adm-secret")
    with pytest.raises(HTTPException) as exc:
        auth.require_admin(x_admin_key="wrong")
    assert exc.value.status_code == 401
    assert auth.require_admin(x_admin_key="adm-secret") is None


def test_auth_dependencies_are_sync_callables():
    """三个鉴权依赖必须是普通 def（不是 async def）。

    require_tenant 每次都要查 tenants 表：写成 async 会把这段阻塞 IO 压在
    事件循环上（单 worker 下拖死所有并发请求）。FastAPI 对同步依赖会自动
    放线程池，所以“同步”才是正确形态；本用例防止无意改回 async。
    """
    import asyncio
    import inspect

    from app.api import auth

    for name in ("require_tenant", "require_admin", "require_default_tenant"):
        fn = getattr(auth, name)
        assert inspect.isfunction(fn), f"{name} 不是普通函数"
        assert not asyncio.iscoroutinefunction(fn), f"{name} 不能是 async def"


# ============ 全局接口凭据开关（★ 2026-10） ============


class TestRequireDefaultTenantGuard:
    """`RAG_REQUIRE_ADMIN_FOR_GLOBAL`：匿名不再等于全局权限。

    背景：`require_tenant` 把无 X-API-Key 的请求当作 default 租户，而
    cleanup / scan / parse / chunk / pipeline / webscrape / config 这些能删数据、
    改配置的全局接口都挂的是 `require_default_tenant` —— 只要服务端口可达
    就能全权调用。开关默认关（存量行为不变），开后必须出示 X-Admin-Key。
    """

    _DEFAULT = Tenant({"tenant_id": "default", "name": "默认租户"})

    def _call(self, tenant=None, x_admin_key=None):
        from app.api import auth

        return auth.require_default_tenant(
            tenant=tenant or self._DEFAULT, x_admin_key=x_admin_key
        )

    def test_switch_off_keeps_anonymous_access(self, monkeypatch):
        from fastapi import HTTPException

        from app.api import auth

        monkeypatch.setattr(auth.settings, "require_admin_for_global", False)
        monkeypatch.setattr(auth.settings, "admin_api_key", "")
        # 存量行为：匿名 + 无管理密钥照样可用
        assert self._call().tenant_id == "default"
        # 命名租户依旧 403（与开关无关）
        with pytest.raises(HTTPException) as exc:
            self._call(tenant=Tenant({"tenant_id": "acme"}))
        assert exc.value.status_code == 403

    def test_switch_on_requires_admin_key(self, monkeypatch):
        from fastapi import HTTPException

        from app.api import auth

        monkeypatch.setattr(auth.settings, "require_admin_for_global", True)
        monkeypatch.setattr(auth.settings, "admin_api_key", "adm-secret")

        for missing in (None, ""):
            with pytest.raises(HTTPException) as exc:
                self._call(x_admin_key=missing)
            assert exc.value.status_code == 401  # 匿名/空值 → 要求凭据

        with pytest.raises(HTTPException) as exc:
            self._call(x_admin_key="wrong")
        assert exc.value.status_code == 401

        assert self._call(x_admin_key="adm-secret").tenant_id == "default"

    def test_switch_on_without_key_configured_fails_closed(self, monkeypatch):
        """开关开了但没配密钥：全部 403，不能反而退化成无校验。"""
        from fastapi import HTTPException

        from app.api import auth

        monkeypatch.setattr(auth.settings, "require_admin_for_global", True)
        monkeypatch.setattr(auth.settings, "admin_api_key", "")

        with pytest.raises(HTTPException) as exc:
            self._call(x_admin_key="anything")
        assert exc.value.status_code == 403
        assert "RAG_ADMIN_API_KEY" in exc.value.detail

    def test_named_tenant_still_403_before_credential_check(self, monkeypatch):
        """越权优先级不变：命名租户带对管理密钥也是 403（不是 default）。"""
        from fastapi import HTTPException

        from app.api import auth

        monkeypatch.setattr(auth.settings, "require_admin_for_global", True)
        monkeypatch.setattr(auth.settings, "admin_api_key", "adm-secret")

        with pytest.raises(HTTPException) as exc:
            self._call(tenant=Tenant({"tenant_id": "acme"}), x_admin_key="adm-secret")
        assert exc.value.status_code == 403

    def test_non_ascii_server_key_fails_closed_not_crash(self, monkeypatch):
        """服务端密钥含非 ASCII 字符时只能 401，不能让 compare_digest 抛 TypeError。

        HTTP 头本身只能是 ASCII，这种配置下任何客户端都传不进来，
        因此至少要保证是“拒绝”而不是 500（main.py 启动时会另打一条 error）。
        """
        from fastapi import HTTPException

        from app.api import auth

        monkeypatch.setattr(auth.settings, "require_admin_for_global", True)
        monkeypatch.setattr(auth.settings, "admin_api_key", "密钥-x")

        with pytest.raises(HTTPException) as exc:
            self._call(x_admin_key="x")
        assert exc.value.status_code == 401


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
