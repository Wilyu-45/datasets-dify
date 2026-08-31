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
    """人工校验左栏的单个文档条目（元数据页也复用）。"""

    id: str
    name: str
    indexing_status: str = "waiting"
    enabled: bool = True
    word_count: Optional[int] = None
    created_at: Optional[int] = None
    display_position: Optional[int] = None  # 服务端返回的 position 字段（部分 Dify 版本）
    # ★ 2026-08-31 Dify 端已写入的文档元数据（部分 Dify 版本返回 doc_metadata 字段）
    metadata: Optional[List[Dict[str, Any]]] = None


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
def get_dify_documents(
    page: int = 1,
    limit: int = 50,
    keyword: Optional[str] = None,
    dataset_id: Optional[str] = None,
) -> List[DifyDocumentItem]:
    """列出 Dify 数据集中的所有文档（人工校验左栏 / 元数据页）。

    通过 ``GET /datasets/{id}/documents`` 拉取，兼容 Dify 自托管和云服务。
    ★ 2026-08-31 dataset_id 可选：缺省用当前配置的目标知识库，
    元数据页切换知识库时传入对应 ID。
    """
    if not settings.dify_api_key:
        raise HTTPException(status_code=400, detail="dify_api_key 未配置（backend/.env 的 RAG_DIFY_API_KEY）")
    if not (dataset_id or settings.dify_dataset_id):
        raise HTTPException(status_code=400, detail="dify_dataset_id 未配置（backend/.env 的 RAG_DIFY_DATASET_ID）")
    log.info(
        "api /dify/documents called",
        extra={"step": "api", "status": "list_documents", "page": page, "limit": limit},
    )
    try:
        client = DifyClient(dataset_id=dataset_id or None)
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
                    metadata=d.get("doc_metadata") or None,
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

    target_stems: Optional[List[str]] = None  # None=知识库全部文档，指定则只同步这些（按文档名）
    dataset_id: Optional[str] = None  # 目标知识库 ID（缺省用当前配置 RAG_DIFY_DATASET_ID）


def _list_all_dify_documents(client: DifyClient, max_pages: int = 100) -> List[Dict[str, Any]]:
    """拉取目标知识库的全部文档（分页，每页 100，最多 max_pages 页）。"""
    docs: List[Dict[str, Any]] = []
    page = 1
    while page <= max_pages:
        payload = client.list_documents(page=page, limit=100)
        items = payload.get("data") or []
        docs.extend(items)
        if not payload.get("has_more") or not items:
            break
        page += 1
    return docs


@router.post("/dify/metadata/sync")
def post_sync_metadata(body: Optional[MetadataSyncRequest] = None) -> Dict[str, Any]:
    """把元数据导入 Dify 知识库（★ 2026-08-31 重写：以 Dify 库内文档清单为准）。

    文档清单直接来自目标知识库（分页拉取），按**文档名（stem）**匹配本地元数据：
      1. doc_metadata 表行（元数据页「填写元数据」抽屉保存，11 个字段）
      2. manifest 表用户填写列（历史数据；序号/一级二级分类/关键词/适用科室/校对/处理备注）
    好处：文档 ID 全部来自 Dify 自身（不会因台账里的陈旧 dify_doc_id 报 404）；
    知识库里存在但台账没有的文档（后续迁移场景）同样可同步。
    缺失的 Dify 元数据字段自动创建；批量写入失败时逐篇重试隔离错误（如单篇被删）。
    """
    if not settings.dify_api_key:
        raise HTTPException(status_code=400, detail="dify_api_key 未配置")
    body = body or MetadataSyncRequest()
    log.info("api /dify/metadata/sync called", extra={"target_stems": body.target_stems})
    try:
        client = DifyClient(dataset_id=body.dataset_id or None)
        # 1) 确保字段存在（doc_metadata 字段 + manifest 用户列，缺失自动创建）
        field_map = doc_metadata.ensure_metadata_fields(client)
        # 2) 本地元数据来源：doc_metadata 表 + manifest 用户填写列（按 stem 建索引）
        doc_meta = doc_metadata.load_doc_metadata()
        manifest_by_stem: Dict[str, Any] = {}
        for fname, row in dify_ingest._load_manifest_index().items():
            stem = Path(fname).stem
            if stem not in manifest_by_stem:
                manifest_by_stem[stem] = row
            chunks_clean = (row.chunks or "").replace("\\", "/").strip()
            if chunks_clean and "/" in chunks_clean:
                manifest_by_stem.setdefault(Path(chunks_clean).name, row)
        # 3) 以 Dify 库内文档清单为准，逐文档合并元数据
        docs = _list_all_dify_documents(client)
        operations = []
        for d in docs:
            doc_id = str(d.get("id") or "")
            stem = str(d.get("name") or "").strip()
            if not doc_id or not stem:
                continue
            if body.target_stems and stem not in body.target_stems:
                continue
            values = doc_metadata.build_merged_metadata(
                manifest_by_stem.get(stem), doc_meta.get(stem) or {}
            )
            op = doc_metadata.build_metadata_operation(doc_id, values, field_map)
            if op:
                operations.append(op)
        if not operations:
            return {
                "ok": True,
                "synced": 0,
                "errors": 0,
                "total": 0,
                "skipped": len(docs),
                "message": "知识库内没有匹配到带元数据的文档（先在「元数据」页填写，或清单列里有历史数据）",
            }
        # 4) 批量写入（50 篇/批）；批失败降级为逐篇重试，隔离单篇错误（如刚被删除）
        synced = 0
        failed: List[str] = []
        batch_size = 50

        def _push_single(op: Dict[str, Any]) -> bool:
            try:
                client.batch_update_document_metadata([op])
                return True
            except DifyError as e:
                log.error(
                    "元数据写入失败: document_id=%s err=%s",
                    op.get("document_id"), e,
                )
                return False

        for i in range(0, len(operations), batch_size):
            batch = operations[i : i + batch_size]
            try:
                client.batch_update_document_metadata(batch)
                synced += len(batch)
            except DifyError as e:
                log.warning("元数据批量更新失败，降级逐篇重试: %s", e)
                for op in batch:
                    if _push_single(op):
                        synced += 1
                    else:
                        failed.append(op["document_id"])
        return {
            "ok": not failed,
            "synced": synced,
            "errors": len(failed),
            "total": len(operations),
            "skipped": len(docs) - len(operations),
            "failed_doc_ids": failed,
            "message": "" if not failed else f"{len(failed)} 篇文档写入失败（可能已被删除），详见后端日志",
        }
    except DifyError as e:
        raise HTTPException(status_code=502, detail=f"Dify 调用失败: {e}") from e
    except Exception as e:  # noqa: BLE001
        log.exception("dify metadata sync 异常")
        raise HTTPException(status_code=500, detail=str(e)) from e
