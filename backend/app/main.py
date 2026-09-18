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
    """启动时初始化数据目录、PostgreSQL 表结构（manifest / doc_metadata）与日志。"""
    setup_logging(
        logs_dir=settings.logs_dir,
        level=settings.log_level,
        retention_days=settings.log_retention_days,
    )
    settings.ensure_dirs()
    # 初始化 PostgreSQL 表结构（manifest / doc_metadata 持久化层）
    manifest_store.bootstrap()
    log.info(
        "app started",
        extra={
            "step": "startup",
            "status": "ok",
            "data_root": str(settings.data_root),
            "manifest": f"postgresql://{settings.pg_host}:{settings.pg_port}/{settings.pg_dbname}#manifest",
        },
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
