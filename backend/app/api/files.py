"""GET /api/files?dir=input|pending"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import List, Literal

from fastapi import APIRouter, HTTPException

from app.config import settings
from app.models.schemas import FileItem
from app.services import manifest_store

router = APIRouter(tags=["files"])


@router.get("/files", response_model=List[FileItem])
def list_files(dir: Literal["input", "pending"]) -> List[FileItem]:
    if dir == "input":
        target = settings.input_dir
    else:
        target = settings.pending_dir

    if not target.exists():
        return []

    # 读 manifest，filename → status
    manifest = {}
    try:
        manifest = manifest_store.load(settings.manifest_path)
    except Exception:  # noqa: BLE001
        manifest = {}

    items: List[FileItem] = []
    for p in sorted(target.iterdir(), key=lambda x: x.name.lower()):
        if not p.is_file():
            continue
        if p.suffix.lower() not in settings.allowed_extensions:
            continue
        stat = p.stat()
        row = manifest.get(p.name)
        items.append(
            FileItem(
                name=p.name,
                size=stat.st_size,
                mtime=datetime.fromtimestamp(stat.st_mtime),
                md5=row.md5 if row else None,
                status=row.status if row else None,
            )
        )
    return items
