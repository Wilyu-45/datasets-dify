"""plan.md §3.2 API:
- POST /api/parse                 触发 MinerU 解析
- GET  /api/parsed                列出 data/parsed/ 下的所有解析目录
- GET  /api/parsed/{stem}/files   列出某文档解析结果中的所有文件

（解析进度查询不在本模块：由 api/parse_progress.py 提供 GET /api/parse/progress）
"""

from __future__ import annotations

import logging
from typing import List

from fastapi import APIRouter, Depends, HTTPException

from app.api.auth import require_default_tenant
from app.config import settings
from app.models.schemas import ParseReport, ParseRequest
from app.services import parser, pathguard

router = APIRouter(tags=["parse"], dependencies=[Depends(require_default_tenant)])
log = logging.getLogger("ragsystem.api.parse")


@router.post("/parse", response_model=ParseReport)
def post_parse(body: ParseRequest | None = None) -> ParseReport:
    """执行 §3.2 解析。`dry_run=true` 不调 API、不写 manifest；`force=true` 强制重解析。"""
    body = body or ParseRequest()
    dry = bool(body.dry_run)
    force = bool(body.force)
    log.info(
        "api /parse called",
        extra={"step": "api", "status": "parse", "dry_run": dry, "force": force},
    )
    try:
        return parser.parse_pending(dry_run=dry, force=force)
    except Exception as e:  # noqa: BLE001
        log.exception("parse 接口异常", extra={"step": "api", "error_msg": str(e)})
        raise HTTPException(status_code=500, detail=str(e)) from e


# ★ 2026-10：这里原本也有一条 GET /parse/progress，与 api/parse_progress.py 的
# 同路径路由重复（FastAPI 报 Duplicate Operation ID，后注册的覆盖前面的）。
# 两个实现读的是不同存储，只靠 include_router 顺序才“碰巧”返回正确数据；
# 现只保留 parse_progress.py 那一条（它读全局 tracker，与 mineru_client 写入端一致）。


@router.get("/parsed")
def list_parsed() -> List[dict]:
    """列出 data/parsed/ 下的所有解析目录（一文档一文件夹）。

    ★ 2026-10：与 api/chunk.py 的 list_chunks 对齐——一个目录只走一遍文件树。
    原先对同一个目录调了 4 次 rglob/glob（md / json / images / total_size / file_count
    各一遍），文档多、图片多时很慢；且 ``sum(... if f.is_file())`` 中单个文件在
    stat 前被清理任务删掉或因权限/断链不可访问时会直接抛错，整页 500。
    现在一次遍历同时算出各字段，is_file/stat 均包 try，口径不变。
    """
    parsed = settings.parsed_dir
    if not parsed.exists():
        return []
    items: List[dict] = []
    for p in sorted(parsed.iterdir(), key=lambda x: x.name.lower()):
        try:
            if not p.is_dir():
                continue
        except OSError:  # noqa: BLE001 目录不可访（权限/断链），跳过
            continue
        md = js = None
        image_count = file_count = 0
        total_size = 0
        for f in p.rglob("*"):
            try:
                if not f.is_file():
                    continue
                size = f.stat().st_size
            except OSError:  # noqa: BLE001 文件刚被删/不可访，跳过
                continue
            file_count += 1
            total_size += size
            ext = f.suffix.lower()
            if ext in (".png", ".jpg"):
                image_count += 1
            if f.parent == p:  # md / json 只看顶层（与旧实现 glob 口径一致）
                if ext == ".md" and md is None:
                    md = f
                elif ext == ".json" and js is None:
                    js = f
        items.append(
            {
                "stem": p.name,
                "dir": str(p.resolve()),
                "md": str(md.resolve()) if md else None,
                "json": str(js.resolve()) if js else None,
                "image_count": image_count,
                "total_size": total_size,
                "file_count": file_count,
            }
        )
    return items


@router.get("/parsed/{stem}/files")
def list_parsed_files(stem: str) -> List[dict]:
    """列出某文档解析结果中的所有文件（用于前端预览）。"""
    # 防止路径逃逸（★ 2026-10：原先用 str.startswith 判前缀，会被同前缀的
    # 兄弟目录如 data/parsed_backup 绕过，改用 Path.is_relative_to）
    try:
        target = pathguard.resolve_within(settings.parsed_dir, stem)
    except ValueError as e:
        raise HTTPException(status_code=400, detail="非法 stem") from e
    if not target.is_dir():
        raise HTTPException(status_code=404, detail=f"parsed/{stem} 不存在")
    # ★ 2026-10：与 chunk.py 一致走共享容错遍历。原实现把 x.is_file() 放在
    # sort key 里，单个不可访文件（权限/断链）会让 sorted() 直接抛错→整页 500。
    out: List[dict] = []
    for f, size in pathguard.iter_files_tolerant(target):
        rel = f.relative_to(target)
        out.append(
            {
                "name": f.name,
                "rel_path": str(rel).replace("\\", "/"),
                "size": size,
                "ext": f.suffix.lower(),
            }
        )
    return out
