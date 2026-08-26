"""pytest 全局配置：把 backend/ 加进 sys.path，并提供 PG manifest 表保护。"""
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))


@pytest.fixture(autouse=True)
def _pg_manifest_guard():
    """任何测试前后保护 PostgreSQL manifest 表：测试结束后恢复原内容。

    背景：manifest 存于 PostgreSQL（全局共享），而测试的 data_root 已隔离到
    tmp_path。若测试直接写 manifest 表且不清理，会污染开发库并让测试间
    互相干扰（残留行被后续「处理所有」类测试读到）。
    """
    from app.services import manifest_store

    try:
        manifest_store.bootstrap()
    except Exception:  # noqa: BLE001  PG 不可用（如无数据库环境），跳过保护
        yield
        return
    saved = list(manifest_store.load().values())
    manifest_store.clear()
    yield
    manifest_store.clear()
    if saved:
        manifest_store.bulk_upsert(saved)
