"""plan.md §3.4 Dify 入库 API:
- GET  /api/dify/config         查看当前 Dify 配置
- POST /api/dify/config         切换目标知识库（写回 backend/.env 持久化）
- GET  /api/dify/datasets       列出当前 API Key 可见的知识库（供用户选择目标知识库）
- GET  /api/dify/test           测试 Dify 连通性（验证 API Key / dataset）
- POST /api/dify/upload         把 data/chunks/ 下的所有文档目录入库到 Dify
- GET  /api/dify/documents              列出 Dify 数据集下的所有文档（人工校验左栏）
- GET  /api/dify/documents/{doc}/segments  列出某文档的所有分段（人工校验中栏）
- POST /api/dify/documents/{doc}/segments/{seg}  更新单个分段（人工校验保存）
- GET  /api/dify/metadata/fields        列出知识库元数据字段
- POST /api/dify/metadata/init-fields   初始化文档元数据字段（自动创建缺失字段）
- POST /api/dify/metadata/sync          从 doc_metadata 同步文档元数据到已入库的 Dify 文档
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.config import settings, REPO_ROOT
from app.models.schemas import (
    DifyConfigInfo,
    DifyTestResult,
    DifyUploadReport,
    DifyUploadRequest,
    DifyDatasetItem,
    DifyConfigUpdate,
)
from app.services import dify_ingest
from app.services.dify_uploader import DifyClient, DifyError
from app.services import doc_metadata

router = APIRouter(tags=["dify"])
log = logging.getLogger("ragsystem.api.dify")


# ============ §3.5 人工校验相关 schema ============


class DifyDocumentItem(BaseModel):
    """人工校验左栏的单个文档条目。"""

    id: str
    name: str
    indexing_status: str = "waiting"
    enabled: bool = True
    word_count: Optional[int] = None
    created_at: Optional[int] = None
    display_position: Optional[int] = None  # 服务端返回的 position 字段（部分 Dify 版本）


class DifySegmentItem(BaseModel):
    """人工校验中栏的单个分段条目。"""

    id: str
    document_id: str
    position: int
    content: str
    word_count: int = 0
    tokens: int = 0
    status: str = "completed"
    enabled: bool = True
    attachments: List[Dict[str, Any]] = []


class DifySegmentUpdateRequest(BaseModel):
    """人工校验保存按钮的入参。"""

    content: Optional[str] = None
    enabled: Optional[bool] = None


@router.get("/dify/config", response_model=DifyConfigInfo)
def get_dify_config() -> DifyConfigInfo:
    """返回当前 Dify 配置（API Key 仅返回是否已配置，不回显明文）。"""
    chunk_dirs: List = []
    output_dirs: List = []
    if settings.chunks_dir.exists():
        chunk_dirs = [p for p in settings.chunks_dir.iterdir() if p.is_dir()]
    if settings.output_dir.exists():
        output_dirs = [p for p in settings.output_dir.iterdir() if p.is_dir()]
    return DifyConfigInfo(
        api_url=settings.dify_api_url,
        dataset_id=settings.dify_dataset_id,
        has_api_key=bool(settings.dify_api_key),
        indexing_technique=settings.dify_indexing_technique,
        doc_form=settings.dify_doc_form,
        chunks_dir=str(settings.chunks_dir),
        output_dir=str(settings.output_dir),
        chunk_dir_count=len(chunk_dirs),
        output_dir_count=len(output_dirs),
    )


@router.get("/dify/test")
def get_dify_test() -> DifyTestResult:
    """测试 Dify 连通性：GET /datasets/{id}。

    用途：在「执行入库」之前快速验证：
    - API URL 可达（DNS / 网络 / 端口）
    - API Key 有效（不会被 401 拒绝）
    - Dataset ID 存在（不会被 404 拒绝）

    失败时 result.ok=False、result.error 含可操作的修复建议
    （如 401 → "请重新生成 dataset- 开头的 Key"）。
    """
    if not settings.dify_api_key:
        raise HTTPException(
            status_code=400,
            detail="dify_api_key 未配置（backend/.env 的 RAG_DIFY_API_KEY）",
        )
    if not settings.dify_dataset_id:
        raise HTTPException(
            status_code=400,
            detail="dify_dataset_id 未配置（backend/.env 的 RAG_DIFY_DATASET_ID）",
        )
    client = DifyClient()
    payload = client.test_connection()
    log.info(
        "dify connectivity test",
        extra={
            "step": "dify",
            "status": "test",
            "ok": payload.get("ok"),
            "error_code": payload.get("error_code"),
            "elapsed_ms": payload.get("elapsed_ms"),
        },
    )
    return DifyTestResult(**payload)


@router.get("/dify/datasets", response_model=List[DifyDatasetItem])
def get_dify_datasets() -> List[DifyDatasetItem]:
    """列出当前 API Key 可见的知识库，供用户选择目标知识库。

    Dify 分页 limit 上限 100，这里最多拉取 5 页（500 个知识库）。
    """
    if not settings.dify_api_key:
        raise HTTPException(status_code=400, detail="dify_api_key 未配置（backend/.env 的 RAG_DIFY_API_KEY）")
    log.info("api /dify/datasets called", extra={"step": "api", "status": "list_datasets"})
    try:
        client = DifyClient()
        out: List[DifyDatasetItem] = []
        page = 1
        while page <= 5:
            payload = client.list_datasets(page=page, limit=100)
            items = payload.get("data") or []
            for d in items:
                out.append(
                    DifyDatasetItem(
                        id=d.get("id", "") or "",
                        name=d.get("name", "") or "",
                        description=d.get("description", "") or "",
                        permission=d.get("permission", "only_me") or "only_me",
                        indexing_technique=d.get("indexing_technique", "") or "",
                        document_count=int(d.get("document_count") or 0),
                        created_at=d.get("created_at"),
                    )
                )
            if not payload.get("has_more") or not items:
                break
            page += 1
        return out
    except DifyError as e:  # noqa: BLE001
        log.exception("dify datasets 接口异常", extra={"step": "api", "error_msg": str(e)})
        raise HTTPException(status_code=502, detail=f"Dify 调用失败: {e}") from e
    except Exception as e:  # noqa: BLE001
        log.exception("dify datasets 接口未捕获异常", extra={"step": "api", "error_msg": str(e)})
        raise HTTPException(status_code=500, detail=str(e)) from e


def _persist_env(key: str, value: str) -> None:
    """把 ``key=value`` 写回 backend/.env（不存在则追加），UTF-8 无 BOM。"""
    env_file = REPO_ROOT / "backend" / ".env"
    lines = env_file.read_text(encoding="utf-8").splitlines() if env_file.exists() else []
    replaced = False
    out: List[str] = []
    for line in lines:
        if line.strip().startswith(f"{key}="):
            out.append(f"{key}={value}")
            replaced = True
        else:
            out.append(line)
    if not replaced:
        out.append(f"{key}={value}")
    env_file.write_text("\n".join(out) + "\n", encoding="utf-8")


@router.post("/dify/config", response_model=DifyConfigInfo)
def post_dify_config(body: Optional[DifyConfigUpdate] = None) -> DifyConfigInfo:
    """更新 Dify 运行配置：切换目标知识库。

    Body:
        - dataset_id: 新的知识库 ID（立即生效，并写回 backend/.env 持久化，重启后仍生效）
    """
    body = body or DifyConfigUpdate()
    if body.dataset_id:
        new_id = body.dataset_id.strip()
        if not new_id:
            raise HTTPException(status_code=400, detail="dataset_id 不能为空")
        old_id = settings.dify_dataset_id
        settings.dify_dataset_id = new_id
        try:
            _persist_env("RAG_DIFY_DATASET_ID", new_id)
        except Exception as e:  # noqa: BLE001
            log.warning("持久化 RAG_DIFY_DATASET_ID 到 backend/.env 失败: %s", e)
        log.info(
            "dify dataset_id 已切换",
            extra={"step": "api", "old": old_id, "new": new_id},
        )
    return get_dify_config()


@router.post("/dify/upload", response_model=DifyUploadReport)
def post_dify_upload(body: Optional[DifyUploadRequest] = None) -> DifyUploadReport:
    """执行 §3.4 Dify 入库。

    Body:
        - dry_run: bool = False  不实际调用 Dify
        - force:   bool = False  强制重传（默认跳过 manifest dify_status=done 的行）
        - target_stems: Optional[List[str]] = None  ★ 2026-08-07：指定文件列表
    """
    body = body or DifyUploadRequest()
    log.info(
        "api /dify/upload called",
        extra={"step": "api", "status": "dify_upload", "dry_run": body.dry_run, "force": body.force,
               "target_stems": body.target_stems},
    )
    # 预检：API Key / dataset_id 是否配置（dry_run 也可预检以友好提示）
    if not body.dry_run:
        if not settings.dify_api_key:
            raise HTTPException(status_code=400, detail="dify_api_key 未配置（backend/.env 的 RAG_DIFY_API_KEY）")
        if not settings.dify_dataset_id:
            raise HTTPException(status_code=400, detail="dify_dataset_id 未配置（backend/.env 的 RAG_DIFY_DATASET_ID）")
    try:
        return dify_ingest.upload_all_docs(
            dry_run=body.dry_run,
            force=body.force,
            target_stems=body.target_stems,  # ★ 2026-08-07：传递 target_stems
        )
    except DifyError as e:  # noqa: BLE001
        log.exception("dify upload 接口异常", extra={"step": "api", "error_msg": str(e)})
        raise HTTPException(status_code=502, detail=f"Dify 调用失败: {e}") from e
    except Exception as e:  # noqa: BLE001
        log.exception("dify upload 接口未捕获异常", extra={"step": "api", "error_msg": str(e)})
        raise HTTPException(status_code=500, detail=str(e)) from e


# ============ §3.5 人工校验相关端点 ============


@router.get("/dify/documents", response_model=List[DifyDocumentItem])
def get_dify_documents(page: int = 1, limit: int = 50, keyword: Optional[str] = None) -> List[DifyDocumentItem]:
    """列出 Dify 数据集中的所有文档（人工校验左栏）。

    通过 ``GET /datasets/{id}/documents`` 拉取，兼容 Dify 自托管和云服务。
    """
    if not settings.dify_api_key:
        raise HTTPException(status_code=400, detail="dify_api_key 未配置（backend/.env 的 RAG_DIFY_API_KEY）")
    if not settings.dify_dataset_id:
        raise HTTPException(status_code=400, detail="dify_dataset_id 未配置（backend/.env 的 RAG_DIFY_DATASET_ID）")
    log.info(
        "api /dify/documents called",
        extra={"step": "api", "status": "list_documents", "page": page, "limit": limit},
    )
    try:
        client = DifyClient()
        payload = client.list_documents(page=page, limit=limit, keyword=keyword)
        items = payload.get("data") or []
        out: List[DifyDocumentItem] = []
        for d in items:
            out.append(
                DifyDocumentItem(
                    id=d.get("id", ""),
                    name=d.get("name", ""),
                    indexing_status=d.get("indexing_status", "waiting"),
                    enabled=bool(d.get("enabled", True)),
                    word_count=d.get("word_count"),
                    created_at=d.get("created_at"),
                    display_position=d.get("position"),
                )
            )
        return out
    except DifyError as e:  # noqa: BLE001
        log.exception("dify documents 接口异常", extra={"step": "api", "error_msg": str(e)})
        raise HTTPException(status_code=502, detail=f"Dify 调用失败: {e}") from e
    except Exception as e:  # noqa: BLE001
        log.exception("dify documents 接口未捕获异常", extra={"step": "api", "error_msg": str(e)})
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.get("/dify/documents/{doc_id}/segments", response_model=List[DifySegmentItem])
def get_dify_document_segments(
    doc_id: str,
    keyword: Optional[str] = None,
    status: Optional[str] = None,
) -> List[DifySegmentItem]:
    """列出某 Dify 文档的所有分段（人工校验中栏）。

    透传 Dify 服务端的 keyword/status 过滤。
    """
    if not settings.dify_api_key:
        raise HTTPException(status_code=400, detail="dify_api_key 未配置（backend/.env 的 RAG_DIFY_API_KEY）")
    log.info(
        "api /dify/documents/{doc_id}/segments called",
        extra={"step": "api", "status": "list_segments", "document_id": doc_id},
    )
    try:
        client = DifyClient()
        items = client.list_segments(doc_id, keyword=keyword, status=status)
        out: List[DifySegmentItem] = []
        for i, seg in enumerate(items):
            out.append(
                DifySegmentItem(
                    id=seg.get("id", ""),
                    document_id=seg.get("document_id", doc_id),
                    position=int(seg.get("position") or (i + 1)),
                    content=seg.get("content", "") or "",
                    word_count=int(seg.get("word_count") or 0),
                    tokens=int(seg.get("tokens") or 0),
                    status=seg.get("status", "completed"),
                    enabled=bool(seg.get("enabled", True)),
                    attachments=seg.get("attachments") or [],
                )
            )
        return out
    except DifyError as e:  # noqa: BLE001
        log.exception("dify segments 接口异常", extra={"step": "api", "error_msg": str(e)})
        raise HTTPException(status_code=502, detail=f"Dify 调用失败: {e}") from e
    except Exception as e:  # noqa: BLE001
        log.exception("dify segments 接口未捕获异常", extra={"step": "api", "error_msg": str(e)})
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.post("/dify/documents/{doc_id}/segments/{seg_id}", response_model=Dict[str, Any])
def post_update_dify_segment(
    doc_id: str,
    seg_id: str,
    body: DifySegmentUpdateRequest,
) -> Dict[str, Any]:
    """更新单个 Dify 分段（人工校验保存按钮）。

    转发到 ``DifyClient.update_segment()``，保留 update_segment 关于
    attachment_ids 的所有行为（含 2026-07-31 修复）。
    """
    if not settings.dify_api_key:
        raise HTTPException(status_code=400, detail="dify_api_key 未配置（backend/.env 的 RAG_DIFY_API_KEY）")
    if body.content is None and body.enabled is None:
        raise HTTPException(
            status_code=400,
            detail="body 至少需要包含 content 或 enabled 之一",
        )
    log.info(
        "api /dify/documents/{doc_id}/segments/{seg_id} called",
        extra={
            "step": "api",
            "status": "update_segment",
            "document_id": doc_id,
            "segment_id": seg_id,
            "has_content": body.content is not None,
            "has_enabled": body.enabled is not None,
        },
    )
    try:
        client = DifyClient()
        return client.update_segment(
            document_id=doc_id,
            segment_id=seg_id,
            content=body.content,
            enabled=body.enabled,
        )
    except DifyError as e:  # noqa: BLE001
        log.exception("dify update segment 接口异常", extra={"step": "api", "error_msg": str(e)})
        raise HTTPException(status_code=502, detail=f"Dify 调用失败: {e}") from e
    except Exception as e:  # noqa: BLE001
        log.exception("dify update segment 接口未捕获异常", extra={"step": "api", "error_msg": str(e)})
        raise HTTPException(status_code=500, detail=str(e)) from e


# ============ 文档元数据相关端点 ============


@router.get("/dify/metadata/fields")
def get_dify_metadata_fields() -> Dict[str, Any]:
    """列出知识库所有元数据字段（含内置 + 自定义）。"""
    if not settings.dify_api_key:
        raise HTTPException(status_code=400, detail="dify_api_key 未配置")
    try:
        client = DifyClient()
        fields = client.list_metadata_fields()
        return {"fields": fields, "defined_fields": list(doc_metadata.METADATA_FIELD_DEFS.keys())}
    except DifyError as e:
        raise HTTPException(status_code=502, detail=f"Dify 调用失败: {e}") from e


@router.post("/dify/metadata/init-fields")
def post_init_metadata_fields() -> Dict[str, Any]:
    """初始化文档元数据字段（自动创建缺失的字段到 Dify 知识库）。"""
    if not settings.dify_api_key:
        raise HTTPException(status_code=400, detail="dify_api_key 未配置")
    try:
        client = DifyClient()
        field_map = doc_metadata.ensure_metadata_fields(client)
        return {
            "ok": True,
            "fields": field_map,
            "total": len(field_map),
        }
    except DifyError as e:
        raise HTTPException(status_code=502, detail=f"Dify 调用失败: {e}") from e


class MetadataSyncRequest(BaseModel):
    """POST /api/dify/metadata/sync 的入参。"""
    target_stems: Optional[List[str]] = None  # None=全部已入库文档，指定则只同步这些


@router.post("/dify/metadata/sync")
def post_sync_metadata(body: Optional[MetadataSyncRequest] = None) -> Dict[str, Any]:
    """从 Excel 同步文档元数据到已入库的 Dify 文档。

    用于：
    1. 首次导入元数据（文档已入库但无元数据）
    2. Excel 更新后重新同步
    """
    if not settings.dify_api_key:
        raise HTTPException(status_code=400, detail="dify_api_key 未配置")
    body = body or MetadataSyncRequest()
    log.info("api /dify/metadata/sync called", extra={"target_stems": body.target_stems})
    try:
        client = DifyClient()
        # 1) 确保字段存在
        field_map = doc_metadata.ensure_metadata_fields(client)
        # 2) 加载 Excel
        doc_meta = doc_metadata.load_doc_metadata()
        if not doc_meta:
            return {"ok": True, "synced": 0, "message": "Excel 无数据或文件不存在"}
        # 3) 获取已入库的文档列表（通过 manifest）
        manifest = dify_ingest._load_manifest_index()
        operations = []
        for fname, row in manifest.items():
            if not row.dify_doc_id or (row.dify_status or "") != "done":
                continue
            stem = Path(row.chunks or "").name if row.chunks else Path(fname).stem
            if body.target_stems and stem not in body.target_stems:
                continue
            op = doc_metadata.build_metadata_operation(
                row.dify_doc_id, stem, field_map, doc_meta,
            )
            if op:
                operations.append(op)
        if not operations:
            return {"ok": True, "synced": 0, "message": "无匹配的已入库文档或无对应元数据"}
        # 4) 批量更新（每批最多 50 个文档）
        synced = 0
        errors = 0
        batch_size = 50
        for i in range(0, len(operations), batch_size):
            batch = operations[i : i + batch_size]
            try:
                client.batch_update_document_metadata(batch)
                synced += len(batch)
            except DifyError as e:
                log.error("元数据批量更新失败: %s", e)
                errors += len(batch)
        return {"ok": errors == 0, "synced": synced, "errors": errors, "total": len(operations)}
    except DifyError as e:
        raise HTTPException(status_code=502, detail=f"Dify 调用失败: {e}") from e
    except Exception as e:  # noqa: BLE001
        log.exception("dify metadata sync 异常")
        raise HTTPException(status_code=500, detail=str(e)) from e
