"""解析进度：单一事实来源与路由不重复（★ 2026-10）。

排查发现两个耦合问题：
1. `api/parse.py` 与 `api/parse_progress.py` 都注册了 GET /api/parse/progress
   （FastAPI 报 Duplicate Operation ID），后 include 的覆盖前面的；
2. 两条实现读的是不同存储——`parser._parse_progress` 与
   `services.parse_progress` 的全局 tracker。MinerU 路径写 tracker，本地解析
   （.html/.xlsx）只写自己那个字典，于是前端进度条对这些文件永远空的。

修法：`parser._set_progress` 统一写全局 tracker；删掉被覆盖的那条重复路由。
"""

from __future__ import annotations

import asyncio
import warnings


def _unique_key(prefix: str) -> str:
    import time
    import uuid

    return f"_probe_{prefix}_{uuid.uuid4().hex[:8]}_{int(time.time() * 1000)}.html"


def test_local_parse_progress_is_visible_through_shared_store():
    """_set_progress 写的进度必须能被同一个 tracker 读到（原实现读写两个字典）。"""
    from app.api import parse_progress as progress_api
    from app.services import parser

    fname = _unique_key("local")
    parser._set_progress(fname, 40, "本地解析中...", "parsing")

    snapshot = asyncio.run(progress_api.get_parse_progress())

    assert fname in snapshot, "本地解析的进度没有出现在接口返回里"
    assert snapshot[fname]["progress"] == 40
    assert snapshot[fname]["status"] == "parsing"

    # 收尾：done 状态会被 cleanup_old 按时间清掉，这里直接摘掉避免污染其他用例
    from app.services.parse_progress import get_tracker

    get_tracker().clear(fname)


def test_done_entries_are_dropped_after_cleanup():
    """tracker 的 30 秒保留期是清理机制，不能被改回“永不清理”的裸字典。"""
    from app.api import parse_progress as progress_api
    from app.services import parser
    from app.services.parse_progress import get_tracker

    fname = _unique_key("done")
    parser._set_progress(fname, 100, "解析完成(本地)", "done")

    tracker = get_tracker()
    tracker._progress[fname]["update_time"] = 0  # 伪造成很久以前
    snapshot = asyncio.run(progress_api.get_parse_progress())

    assert fname not in snapshot
    assert tracker.get(fname) is None


def test_no_duplicate_operation_ids_in_openapi():
    """整个应用不允许出现重复注册的同路径同方法路由。

    重复注册时 FastAPI 只会告警并让后注册的覆盖前者，接口“看起来正常”，
    但真正的处理器取决于 include_router 顺序——属于典型的隐性行为漂移。
    """
    from app.main import app

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        app.openapi_schema = None
        schema = app.openapi()

    dup = [str(w.message) for w in caught if "Duplicate Operation ID" in str(w.message)]
    assert dup == [], f"存在重复注册的路由：{dup}"

    # 进度接口只剩一条，且方法/路径正确
    assert "/api/parse/progress" in schema["paths"]
    assert set(schema["paths"]["/api/parse/progress"].keys()) == {"get"}


def test_progress_router_is_guarded_as_global():
    """进度接口内容含全量文件名，属于全局信息，必须挂 require_default_tenant。"""
    from app.api import auth
    from app.api import parse_progress as progress_api

    guards = [d.dependency for d in progress_api.router.dependencies]
    assert auth.require_default_tenant in guards, (
        f"/api/parse/progress 丢了全局接口守卫，当前守卫：{guards}"
    )
