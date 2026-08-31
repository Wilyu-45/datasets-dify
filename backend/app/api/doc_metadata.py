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

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.services import doc_metadata

router = APIRouter(tags=["doc-metadata"])
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
def list_doc_metadata() -> Dict[str, Any]:
    """全量文档元数据（{stem: {field: value}}，无行的 stem 不出现）。"""
    rows = doc_metadata.load_doc_metadata()
    return {"total": len(rows), "rows": rows}


@router.get("/doc-metadata/{stem}")
def get_doc_metadata(stem: str) -> Dict[str, Any]:
    """单个文档的元数据行（不存在时返回空对象，前端表单据此渲染空值）。"""
    return doc_metadata.get_doc_metadata(stem)


@router.put("/doc-metadata/{stem}")
def put_doc_metadata(stem: str, body: DocMetadataUpdate) -> Dict[str, Any]:
    """保存（upsert）单个文档的元数据行。

    表单全量提交：显式传入的空字符串 / None 会清空对应字段（整行覆盖语义）。
    """
    stem = stem.strip()
    if not stem:
        raise HTTPException(status_code=400, detail="stem 不能为空")
    fields = {k: v for k, v in body.model_dump().items()}
    doc_metadata.save_doc_metadata({stem: fields})
    row = doc_metadata.get_doc_metadata(stem)
    log.info("doc-metadata saved: stem=%s fields=%d", stem, len(row))
    return row
