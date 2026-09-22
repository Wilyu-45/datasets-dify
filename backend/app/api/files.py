"""GET /api/files?dir=input|pending"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import List, Literal

from fastapi import APIRouter, Depends, HTTPException
from typing import Literal

from app.api.auth import require_tenant
from app.config import settings
from app.models.schemas import FileItem
from app.services import manifest_store
from app.services.tenant_store import DEFAULT_TENANT_ID, Tenant

router = APIRouter(tags=["files"])


@router.get("/files", response_model=List[FileItem])
def list_files(
    dir: Literal["input", "pending"],
    tenant: Tenant = Depends(require_tenant),
) -> List[FileItem]:
    tid = tenant.tenant_id or DEFAULT_TENANT_ID
    if dir == "input":
        target = settings.input_dir if tid == DEFAULT_TENANT_ID else settings.input_dir_of(tid)
    else:
        target = settings.pending_dir_of(tid)

    if not target.exists():
        return []

    # 读 manifest，filename → status（命名租户只关联自己的行）
    manifest = {}
    try:
        manifest = manifest_store.load(tenant_id=None if tid == DEFAULT_TENANT_ID else tid)
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
