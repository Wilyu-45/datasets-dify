"""plan.md §3.3 API:
- POST /api/chunk                触发自定义切分
- GET  /api/chunks               列出 data/chunks/ 下的所有切分目录
- GET  /api/chunks/{stem}/files  列出某文档切分产物的所有文件
- GET  /api/chunks/{stem}/chunks 列出某文档的所有 chunk（带元数据）
- GET  /api/chunks/{stem}/preview/{chunk_id}  返回某个 chunk 的内容
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, HTTPException

from app.config import settings
from app.models.schemas import (
    ChunkFile,
    ChunkMeta,
    ChunkReport,
    ChunkRequest,
    ChunkStrategyListResponse,
    ChunkStrategyOption,
    ChunkSummary,
)
from app.services import chunker, chunk_strategies

router = APIRouter(tags=["chunk"])
log = logging.getLogger("ragsystem.api.chunk")


@router.post("/chunk", response_model=ChunkReport)
def post_chunk(body: Optional[ChunkRequest] = None) -> ChunkReport:
    """执行 §3.3 切分。`dry_run=true` 不实际写盘。`force=true` 强制重切。"""
    body = body or ChunkRequest()
    log.info(
        "api /chunk called",
        extra={"step": "api", "status": "chunk", "dry_run": body.dry_run, "force": body.force,
               "target_stems": body.target_stems, "strategy": body.strategy},
    )
    try:
        return chunker.chunk_parsed(
            dry_run=body.dry_run,
            force=body.force,
            target_stems=body.target_stems,  # ★ 2026-08-07：传递 target_stems
            strategy=body.strategy,          # ★ 2026-08-24：多策略切分
        )
    except Exception as e:  # noqa: BLE001
        log.exception("chunk 接口异常", extra={"step": "api", "error_msg": str(e)})
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.get("/chunk/strategies", response_model=ChunkStrategyListResponse)
def list_chunk_strategies() -> ChunkStrategyListResponse:
    """返回支持的切分策略列表（供前端下拉选择）。"""
    metas = chunk_strategies.list_strategies()
    default = settings.chunk_strategy or "structure"
    return ChunkStrategyListResponse(
        strategies=[
            ChunkStrategyOption(
                key=m["key"],
                name=m["name"],
                desc=m["desc"],
                default=m.get("default", False),
            )
            for m in metas
        ],
        default=default,
    )


@router.get("/chunks", response_model=List[ChunkSummary])
def list_chunks() -> List[ChunkSummary]:
    """列出 data/chunks/ 下的所有切分目录（一文档一文件夹）。"""
    root = settings.chunks_dir
    if not root.exists():
        return []
    items: List[ChunkSummary] = []
    for p in sorted(root.iterdir(), key=lambda x: x.name.lower()):
        if not p.is_dir():
            continue
        chunks = list(p.glob("chunk_*.md"))
        images = list((p / "images").rglob("*")) if (p / "images").exists() else []
        images = [f for f in images if f.is_file()]
        total_size = sum(f.stat().st_size for f in p.rglob("*") if f.is_file())
        items.append(
            ChunkSummary(
                stem=p.name,
                dir=str(p.resolve()),
                chunk_count=len(chunks),
                image_count=len(images),
                total_size=total_size,
                file_count=sum(1 for f in p.rglob("*") if f.is_file()),
            )
        )
    return items


@router.get("/chunks/{stem}/files", response_model=List[ChunkFile])
def list_chunk_files(stem: str) -> List[ChunkFile]:
    """列出某文档切分产物的所有文件。"""
    target = (settings.chunks_dir / stem).resolve()
    if not str(target).startswith(str(settings.chunks_dir.resolve())):
        raise HTTPException(status_code=400, detail="非法 stem")
    if not target.is_dir():
        raise HTTPException(status_code=404, detail=f"chunks/{stem} 不存在")
    out: List[ChunkFile] = []
    for f in sorted(target.rglob("*"), key=lambda x: (x.is_file(), x.name.lower())):
        if not f.is_file():
            continue
        rel = f.relative_to(target)
        rel_str = str(rel).replace("\\", "/")
        ext = f.suffix.lower()
        if rel_str.startswith("images/") or ext in (".jpg", ".jpeg", ".png", ".webp"):
            kind = "image"
        elif ext == ".md":
            kind = "chunk"
        elif rel_str == "chunk_metadata.json":
            kind = "metadata"
        else:
            kind = "other"
        out.append(
            ChunkFile(
                name=f.name,
                rel_path=rel_str,
                size=f.stat().st_size,
                ext=ext,
                kind=kind,
            )
        )
    return out


@router.get("/chunks/{stem}/chunks", response_model=List[ChunkMeta])
def list_chunk_meta(stem: str) -> List[ChunkMeta]:
    """读取 chunk_metadata.json，返回所有 chunk 的元数据。"""
    target = (settings.chunks_dir / stem).resolve()
    if not str(target).startswith(str(settings.chunks_dir.resolve())):
        raise HTTPException(status_code=400, detail="非法 stem")
    meta_path = target / "chunk_metadata.json"
    if not meta_path.is_file():
        raise HTTPException(status_code=404, detail=f"chunks/{stem}/chunk_metadata.json 不存在")
    try:
        payload = json.loads(meta_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=500, detail=f"chunk_metadata.json 解析失败: {e}") from e
    items = payload.get("chunks") or []
    strategy = payload.get("strategy") or "structure"
    out: List[ChunkMeta] = []
    for it in items:
        out.append(
            ChunkMeta(
                chunk_id=it.get("chunk_id") or "",
                file_name=it.get("file_name") or "",
                title_path=it.get("title_path") or "",
                chunk_type=it.get("chunk_type") or "body",
                char_count=it.get("char_count") or 0,
                image_refs=it.get("image_refs") or [],
                is_split=it.get("is_split") or False,
                strategy=it.get("strategy") or strategy,
                parent_id=it.get("parent_id"),
            )
        )
    return out


@router.get("/chunks/{stem}/preview/{chunk_id}")
def preview_chunk(stem: str, chunk_id: str) -> dict:
    """返回某个 chunk 的内容（用于前端预览）。"""
    target = (settings.chunks_dir / stem).resolve()
    if not str(target).startswith(str(settings.chunks_dir.resolve())):
        raise HTTPException(status_code=400, detail="非法 stem")
    # 防止路径逃逸
    if "/" in chunk_id or "\\" in chunk_id or ".." in chunk_id:
        raise HTTPException(status_code=400, detail="非法 chunk_id")
    matches = list(target.glob(f"{chunk_id}_*.md"))
    if not matches:
        raise HTTPException(status_code=404, detail=f"chunk {chunk_id} 不存在")
    f = matches[0]
    return {
        "stem": stem,
        "chunk_id": chunk_id,
        "file_name": f.name,
        "content": f.read_text(encoding="utf-8"),
    }
