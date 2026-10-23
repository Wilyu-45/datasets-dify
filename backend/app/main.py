"""FastAPI 入口。"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app import __version__
from app.api import files as files_api
from app.api import health as health_api
from app.api import manifest as manifest_api
from app.api import parse as parse_api
from app.api import parse_progress as parse_progress_api  # ★ 2026-08-07
from app.api import scan as scan_api
from app.api import chunk as chunk_api
from app.api import cleanup as cleanup_api  # ★ 2026-09 生产部署改造：手动文件清理
from app.api import config_api
from app.api import dify as dify_api
from app.api import doc_metadata as doc_metadata_api  # ★ 2026-08-31 文档元数据编辑
from app.api import pipeline as pipeline_api
from app.api import upload as upload_api
from app.api import webscrape as webscrape_api  # ★ 2026-08: 网站抓取
from app.api import tenants as tenants_api  # ★ 2026-09: 租户管理（管理员）
from app.services import tenant_store  # ★ 2026-09: 租户隔离
from app import db
from app.config import settings
from app.logging_config import setup as setup_logging
from app.services import manifest_store
from app.services.cleanup import run_cleanup

log = logging.getLogger("ragsystem.main")


async def _cleanup_loop(interval_hours: int) -> None:
    """周期性文件清理后台任务（2026-09 生产部署改造，计划 C3）。

    每 interval_hours 小时跑一轮 run_cleanup（阻塞 IO 放to_thread）。
    启动后先等一个周期再清，避免与启动期的解析/切分任务争抢。
    cancel 时干净退出（定时任务与 shutdown 配合）。
    """
    interval_sec = interval_hours * 3600
    log.info(
        "cleanup 定时任务已启动",
        extra={"step": "cleanup_timer", "status": "ok", "interval_hours": interval_hours},
    )
    try:
        while True:
            await asyncio.sleep(interval_sec)
            try:
                # run_cleanup 同步且持锁，放子线程避免阻塞事件循环
                report = await asyncio.to_thread(run_cleanup)
                log.info(
                    "cleanup 定时执行完成",
                    extra={
                        "step": "cleanup_timer",
                        "status": "ok" if not report.errors else "partial",
                        "removed_files": report.removed_files,
                        "removed_dirs": report.removed_dirs,
                    },
                )
            except Exception:  # noqa: BLE001
                # 单轮失败不终止定时器，等下一周期
                log.exception("cleanup 定时任务单轮执行异常（已忽略，等下一周期）")
    except asyncio.CancelledError:
        log.info("cleanup 定时任务已取消", extra={"step": "cleanup_timer", "status": "cancelled"})
        raise


@asynccontextmanager
async def lifespan(app: FastAPI):  # noqa: ARG001
    """启动时初始化数据目录、数据库表结构（manifest / doc_metadata）与日志。

    方言由 settings.rag_db_type 决定（本地 postgres / 生产 mysql）。
    """
    setup_logging(
        logs_dir=settings.logs_dir,
        level=settings.log_level,
        retention_days=settings.log_retention_days,
    )
    settings.ensure_dirs()
    # 初始化数据库表结构（manifest / doc_metadata 持久化层，方言跟随 rag_db_type）
    manifest_store.bootstrap()
    # ★ 2026-09 租户隔离：确保 default 租户存在（绑定全局 dataset，幂等）
    tenant_store.bootstrap_default_tenant()
    log.info(
        "app started",
        extra={
            "step": "startup",
            "status": "ok",
            "data_root": str(settings.data_root),
            "manifest": f"{db.describe_target()}#manifest",
        },
    )
    # ★ 2026-10 全局接口凭据开关：两种“看似坏了”的状态都在启动时说明白，
    #   避免运维只能从一堆 401/403 反推配置。
    if settings.require_admin_for_global and not settings.admin_api_key:
        log.error(
            "已开启 RAG_REQUIRE_ADMIN_FOR_GLOBAL 但未配置 RAG_ADMIN_API_KEY："
            "清理/扫描/解析/切分/流水线/网站抓取/配置写等全局接口将全部 403",
            extra={"step": "startup", "status": "admin_guard_misconfigured"},
        )
    elif settings.require_admin_for_global:
        log.info(
            "全局管理接口已要求 X-Admin-Key 凭据（匿名请求 401）",
            extra={"step": "startup", "status": "admin_guard_on"},
        )
        # HTTP 头只能是 ASCII，密钥里混进中文/全角字符时任何客户端都传不进来，
        # 只会留下一排无法解释的 401，因此启动时直接报出来。
        if not settings.admin_api_key.isascii():
            log.error(
                "RAG_ADMIN_API_KEY 含非 ASCII 字符：X-Admin-Key 请求头只能传 ASCII，"
                "全局接口将永远返回 401，请改为字母数字密钥",
                extra={"step": "startup", "status": "admin_key_non_ascii"},
            )
    else:
        log.warning(
            "全局管理接口当前匿名可调（请确保服务只在内网/反代后面）；"
            "如需收紧：RAG_REQUIRE_ADMIN_FOR_GLOBAL=true + RAG_ADMIN_API_KEY",
            extra={"step": "startup", "status": "admin_guard_off"},
        )
    # ★ 2026-09 生产部署改造：按配置启动周期性文件清理后台任务
    cleanup_task: asyncio.Task | None = None
    if settings.cleanup_enabled and settings.cleanup_schedule_hours > 0:
        cleanup_task = asyncio.create_task(_cleanup_loop(settings.cleanup_schedule_hours))
    else:
        log.info(
            "cleanup 定时任务未启用",
            extra={
                "step": "startup",
                "status": "skipped",
                "enabled": settings.cleanup_enabled,
                "schedule_hours": settings.cleanup_schedule_hours,
            },
        )
    yield
    if cleanup_task is not None:
        cleanup_task.cancel()
        try:
            await cleanup_task
        except asyncio.CancelledError:
            pass
    db.close_pool()
    log.info("app stopped", extra={"step": "shutdown", "status": "ok"})


app = FastAPI(
    title=settings.app_name,
    version=__version__,
    description="plan.md §3.1 文件读取与状态管理 + §3.2 MinerU 解析 + §3.3 自定义切分 + Web 框架骨架",
    lifespan=lifespan,
)

# CORS：允许跨域来源，见 backend/.env 的 RAG_CORS_ORIGINS（JSON 数组）
app.add_middleware(
    CORSMiddleware,
    allow_origins=list(settings.cors_origins),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 业务路由（先注册，确保 /api/* 优先于静态）
app.include_router(health_api.router, prefix="/api")
app.include_router(files_api.router, prefix="/api")
app.include_router(manifest_api.router, prefix="/api")
app.include_router(scan_api.router, prefix="/api")
app.include_router(parse_api.router, prefix="/api")
app.include_router(parse_progress_api.router, prefix="/api")  # ★ 2026-08-07
app.include_router(chunk_api.router, prefix="/api")
app.include_router(cleanup_api.router, prefix="/api")  # ★ 2026-09 手动清理
app.include_router(config_api.router, prefix="/api")
app.include_router(dify_api.router, prefix="/api")
app.include_router(doc_metadata_api.router, prefix="/api")  # ★ 2026-08-31 文档元数据编辑
app.include_router(pipeline_api.router, prefix="/api")
app.include_router(upload_api.router, prefix="/api")
app.include_router(webscrape_api.router, prefix="/api")  # ★ 2026-08: 网站抓取
app.include_router(tenants_api.router, prefix="/api")  # ★ 2026-09: 租户管理（X-Admin-Key）


# 图片静态托管：把 data/output/ 暴露为 /static/output/*
# 用途：Dify 知识库无法直接 /files/upload 时（Dify 0.x 某些部署的权限限制），
# 把 chunk 里的 images/xxx.jpg 替换为 {public_base_url}/static/output/{stem}/images/xxx.jpg，
# Dify 索引时会从我们的公网地址（ngrok / OSS）拉取并内嵌图片。
# 挂载顺序：必须在 frontend 之前，否则被 catch-all 吞掉。
_OUTPUT_DIR = settings.output_dir
if _OUTPUT_DIR.exists():
    app.mount(
        "/static/output",
        StaticFiles(directory=str(_OUTPUT_DIR), html=False),
        name="output_static",
    )
    log.info(
        "mounted output dir as /static/output",
        extra={"step": "startup", "status": "static", "path": str(_OUTPUT_DIR)},
    )


# 生产模式：若 RAG_SERVE_FRONTEND 为 True 且 frontend/dist 存在，则挂载为静态站点
# ★ 前后端分离部署时（RAG_SERVE_FRONTEND=false），此挂载将被跳过
# （Nginx 分别代理前端页面与后端 API）
_DIST = Path(__file__).resolve().parents[2] / "frontend" / "dist"
if settings.serve_frontend and _DIST.is_dir():
    app.mount("/", StaticFiles(directory=str(_DIST), html=True), name="frontend")
    log.info("mounted frontend dist", extra={"dist": str(_DIST)})
