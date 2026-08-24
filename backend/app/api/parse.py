"""plan.md §3.2 API:
- POST /api/parse                 触发 MinerU 解析
- GET  /api/parse/progress        查询解析进度（实时轮询）
- GET  /api/parsed                列出 data/parsed/ 下的所有解析目录
- GET  /api/parsed/{stem}/files   列出某文档解析结果中的所有文件
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Dict, List

from fastapi import APIRouter, HTTPException

from app.config import settings
from app.models.schemas import ParseReport, ParseRequest
from app.services import parser

router = APIRouter(tags=["parse"])
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


@router.get("/parse/progress")
def get_parse_progress() -> Dict[str, dict]:
    """★ 2026-08-08：查询解析进度（供前端轮询）。"""
    return parser.get_parse_progress()


@router.get("/parsed")
def list_parsed() -> List[dict]:
    """列出 data/parsed/ 下的所有解析目录（一文档一文件夹）。"""
    parsed = settings.parsed_dir
    if not parsed.exists():
        return []
    items: List[dict] = []
    for p in sorted(parsed.iterdir(), key=lambda x: x.name.lower()):
        if not p.is_dir():
            continue
        # 找 .md / .json
        md = next(iter(p.glob("*.md")), None)
        js = next(iter(p.glob("*.json")), None)
        images = list(p.rglob("*.png")) + list(p.rglob("*.jpg"))
        total_size = sum(f.stat().st_size for f in p.rglob("*") if f.is_file())
        items.append(
            {
                "stem": p.name,
                "dir": str(p.resolve()),
                "md": str(md.resolve()) if md else None,
                "json": str(js.resolve()) if js else None,
                "image_count": len(images),
                "total_size": total_size,
                "file_count": sum(1 for f in p.rglob("*") if f.is_file()),
            }
        )
    return items


@router.get("/parsed/{stem}/files")
def list_parsed_files(stem: str) -> List[dict]:
    """列出某文档解析结果中的所有文件（用于前端预览）。"""
    target = (settings.parsed_dir / stem).resolve()
    # 防止路径逃逸
    if not str(target).startswith(str(settings.parsed_dir.resolve())):
        raise HTTPException(status_code=400, detail="非法 stem")
    if not target.is_dir():
        raise HTTPException(status_code=404, detail=f"parsed/{stem} 不存在")
    out: List[dict] = []
    for f in sorted(target.rglob("*"), key=lambda x: (x.is_file(), x.name.lower())):
        if not f.is_file():
            continue
        rel = f.relative_to(target)
        out.append(
            {
                "name": f.name,
                "rel_path": str(rel).replace("\\", "/"),
                "size": f.stat().st_size,
                "ext": f.suffix.lower(),
            }
        )
    return out
