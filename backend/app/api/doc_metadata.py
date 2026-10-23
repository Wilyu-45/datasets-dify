"""文档元数据（doc_metadata 表）读写 API（★ 2026-08-31 新增）。

前端在 manifest 台账行上打开「元数据」抽屉，可编辑 doc_metadata 表的全部 11 个字段
（此前前端无处填写这些字段，少于数据库表结构），保存后通过
POST /api/dify/metadata/sync 一键导入 Dify 知识库（manifest 用户填写列一并推送）。

- GET  /api/doc-metadata           全表 {stem: {field: value}}
- GET  /api/doc-metadata/{stem}    单行（不存在返回空对象，前端表单直接渲染）
- PUT  /api/doc-metadata/{stem}    upsert 单行（表单全量提交，空值=清空该字段）
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.api.auth import require_tenant
from app.services import doc_metadata
from app.services.tenant_store import Tenant

router = APIRouter(
    tags=["doc-metadata"],
    dependencies=[Depends(require_tenant)],
)
log = logging.getLogger("ragsystem.api.doc_metadata")


class DocMetadataUpdate(BaseModel):
    """PUT /api/doc-metadata/{stem} 入参：doc_metadata 表 11 个字段（均可选）。"""

    doc_type_primary: Optional[str] = None
    doc_type_secondary: Optional[str] = None
    topic_primary: Optional[str] = None
    topic_secondary: Optional[str] = None
    core_summary: Optional[str] = None
    entity_label: Optional[str] = None
    attribute_label: Optional[str] = None
    applicable_scenarios: Optional[str] = None
    effective_date: Optional[str] = None
    priority: Optional[float] = None
    status: Optional[str] = None


@router.get("/doc-metadata")
def list_doc_metadata(tenant: Tenant = Depends(require_tenant)) -> Dict[str, Any]:
    """文档元数据（{stem: {field: value}}，无行的 stem 不出现）。

    ★ 2026-10 修复（租户语义）：只看本租户自己的行（default 也只看 default），
    与 POST /api/dify/metadata/sync 的取数口径保持一致——原先 default 返回全表，
    命名租户的同名行会出现在列表里，但同步时又不会被推送，用户改完看不到效果。
    """
    rows = doc_metadata.load_doc_metadata(tenant_id=tenant.tenant_id)
    return {"total": len(rows), "rows": rows}


@router.get("/doc-metadata/{stem}")
def get_doc_metadata(
    stem: str, tenant: Tenant = Depends(require_tenant)
) -> Dict[str, Any]:
    """单个文档的元数据行（不存在时返回空对象，前端表单据此渲染空值）。"""
    return doc_metadata.get_doc_metadata(stem, tenant_id=tenant.tenant_id)


@router.put("/doc-metadata/{stem}")
def put_doc_metadata(
    stem: str, body: DocMetadataUpdate, tenant: Tenant = Depends(require_tenant)
) -> Dict[str, Any]:
    """保存（upsert）单个文档的元数据行。

    表单全量提交：显式传入的空字符串 / None 会清空对应字段（整行覆盖语义）。
    """
    stem = stem.strip()
    if not stem:
        raise HTTPException(status_code=400, detail="stem 不能为空")
    fields = {k: v for k, v in body.model_dump().items()}
    doc_metadata.save_doc_metadata({stem: fields}, tenant_id=tenant.tenant_id)
    # ★ 2026-10 修复：回读必须用写入时同一个 tenant_id。原先 default 传 None，
    #   而 filename 已不再是主键（同名行可属不同租户），不带租户条件时
    #   返回哪一行由数据库决定，可能把别的租户的同名行回显给本租户。
    row = doc_metadata.get_doc_metadata(stem, tenant_id=tenant.tenant_id)
    log.info(
        "doc-metadata saved: tenant=%s stem=%s fields=%d",
        tenant.tenant_id, stem, len(row),
    )
    return row
