"""FastAPI 入口。"""

from __future__ import annotations

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
from app.api import dify as dify_api
from app.api import pipeline as pipeline_api
from app.api import upload as upload_api
from app.config import settings
from app.logging_config import setup as setup_logging
from app.services import manifest_store

log = logging.getLogger("ragsystem.main")


@asynccontextmanager
async def lifespan(app: FastAPI):  # noqa: ARG001
    """启动时初始化目录、manifest（含自动补列）、日志。"""
    setup_logging(
        logs_dir=settings.logs_dir,
        level=settings.log_level,
        retention_days=settings.log_retention_days,
    )
    settings.ensure_dirs()
    # bootstrap：找到用户的 manifest.xlsx，必要时追加缺失的系统列
    manifest_path = manifest_store.bootstrap(settings.data_root)
    log.info(
        "app started",
        extra={
            "step": "startup",
            "status": "ok",
            "data_root": str(settings.data_root),
            "manifest": str(manifest_path),
        },
    )
    yield
    log.info("app stopped", extra={"step": "shutdown", "status": "ok"})


app = FastAPI(
    title=settings.app_name,
    version=__version__,
    description="plan.md §3.1 文件读取与状态管理 + §3.2 MinerU 解析 + §3.3 自定义切分 + Web 框架骨架",
    lifespan=lifespan,
)

# CORS：开发期 Vite 5173 + 自托管 8000
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:8000",
        "http://127.0.0.1:8000",
    ],
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
app.include_router(dify_api.router, prefix="/api")
app.include_router(pipeline_api.router, prefix="/api")
app.include_router(upload_api.router, prefix="/api")


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


# 生产模式：若 frontend/dist 存在，则挂载为静态站点
_DIST = Path(__file__).resolve().parents[2] / "frontend" / "dist"
if _DIST.is_dir():
    app.mount("/", StaticFiles(directory=str(_DIST), html=True), name="frontend")
    log.info("mounted frontend dist", extra={"dist": str(_DIST)})
