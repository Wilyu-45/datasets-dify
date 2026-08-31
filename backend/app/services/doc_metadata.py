"""文档元数据管理（doc_metadata 表 → Dify Metadata API）。

历史：文档元数据原先以 Excel（doc_metadata.xlsx，含英文字段名首行 + 中文列名第二行）
存储，现已统一迁移到 PostgreSQL 的 doc_metadata 表（表结构见 app.db.DOC_METADATA_TABLE_SQL）。

在 Dify 入库时：
    1. 确保知识库中存在对应的元数据字段（首次运行时自动创建）
    2. 文档入库成功后，批量设置该文档的元数据值

★ 2026-08-31：元数据两个来源合并后推送 Dify（「导入元数据到 Dify」按钮）：
    1. doc_metadata 表（11 个 Dify 专用字段，前端 manifest 行「元数据」抽屉编辑）
    2. manifest 表用户填写列（序号 / 一二级分类 / 关键词 / 适用科室 / 校对 / 处理备注）
    同名字段（effective_date）doc_metadata 行有值优先，否则回落 manifest 列。

doc_metadata 表字段定义：
    doc_type_primary    → string  (类型-一级)
    doc_type_secondary  → string  (类型-二级)
    topic_primary       → string  (主题-一级)
    topic_secondary     → string  (主题-二级)
    core_summary        → string  (核心内容摘要)
    entity_label        → string  (实体标签)
    attribute_label     → string  (属性标签)
    applicable_scenarios→ string  (适用科室)
    effective_date      → string  (生效日期 — 用 string 兼容 "无" / "2009-06-01" 混合格式)
    priority            → number  (优先级)
    status              → string  (现行/废止/...)
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, Optional

from app import db
from app.config import settings

log = logging.getLogger("ragsystem.doc_metadata")


# ============ 字段定义 ============

# 英文字段名 → Dify 元数据 type
# 注意：effective_date 用 string 而非 time，因为用户数据里有 "无" 这种非日期值
METADATA_FIELD_DEFS: Dict[str, str] = {
    "doc_type_primary": "string",
    "doc_type_secondary": "string",
    "topic_primary": "string",
    "topic_secondary": "string",
    "core_summary": "string",
    "entity_label": "string",
    "attribute_label": "string",
    "applicable_scenarios": "string",
    "effective_date": "string",
    "priority": "number",
    "status": "string",
}

# doc_metadata 表的列（filename 为主键，其余与 METADATA_FIELD_DEFS 对应）
DOC_METADATA_FIELDS = list(METADATA_FIELD_DEFS.keys())

# manifest 表用户填写列 → Dify 元数据 type（★ 2026-08-31 与 doc_metadata 字段一并推送）。
# effective_date 两表同名同义：共用上方 doc_metadata 的 effective_date 字段，不重复定义。
MANIFEST_METADATA_FIELD_DEFS: Dict[str, str] = {
    "seq": "number",
    "category_l1": "string",
    "category_l2": "string",
    "keywords": "string",
    "department": "string",
    "verified": "string",
    "process_note": "string",
}

# 推送到 Dify 的全部元数据字段 = doc_metadata 表字段 + manifest 用户填写列
ALL_METADATA_FIELD_DEFS: Dict[str, str] = {
    **METADATA_FIELD_DEFS,
    **MANIFEST_METADATA_FIELD_DEFS,
}


# ============ PostgreSQL 读取 / 写入 ============


def load_doc_metadata(excel_path: Optional[Path] = None) -> Dict[str, Dict[str, Any]]:
    """读取文档元数据（PostgreSQL doc_metadata 表）。

    Returns:
        {stem: {field_name: value, ...}, ...}
        stem = 文件名（不含后缀），field_name = 英文字段名
    """
    with db.get_conn() as conn:
        cur = conn.execute(
            f"SELECT filename, {', '.join(DOC_METADATA_FIELDS)} FROM doc_metadata"
        )
        records = cur.fetchall()

    result: Dict[str, Dict[str, Any]] = {}
    for rec in records:
        stem = (rec.get("filename") or "").strip()
        if not stem:
            continue
        row_data = _normalize_row(rec)
        if row_data:
            result[stem] = row_data

    log.info("文档元数据加载完成: docs=%d", len(result))
    return result


def _normalize_row(rec: Dict[str, Any]) -> Dict[str, Any]:
    """把一行查询结果规整为 {field: value}（空值剔除，number 字段转 float）。"""
    row_data: Dict[str, Any] = {}
    for field in DOC_METADATA_FIELDS:
        val = rec.get(field)
        if val is None or str(val).strip() == "":
            continue
        if METADATA_FIELD_DEFS[field] == "number":
            try:
                row_data[field] = float(val) if not isinstance(val, (int, float)) else val
            except (TypeError, ValueError):
                continue
        else:
            row_data[field] = str(val).strip()
    return row_data


def get_doc_metadata(stem: str) -> Dict[str, Any]:
    """读取单个文档的元数据行（doc_metadata 表）。

    Returns:
        {field_name: value, ...}（不存在或全空时返回 {}）
    """
    stem = (stem or "").strip()
    if not stem:
        return {}
    with db.get_conn() as conn:
        cur = conn.execute(
            f"SELECT {', '.join(DOC_METADATA_FIELDS)} FROM doc_metadata WHERE filename = %(stem)s",
            {"stem": stem},
        )
        rec = cur.fetchone()
    if not rec:
        return {}
    return _normalize_row(rec)


def save_doc_metadata(rows: Dict[str, Dict[str, Any]]) -> int:
    """批量写入文档元数据（upsert，按 filename 主键）。

    Args:
        rows: {stem: {field_name: value, ...}, ...}

    Returns:
        写入行数
    """
    if not rows:
        return 0

    columns = ["filename"] + DOC_METADATA_FIELDS
    sql = f"""
        INSERT INTO doc_metadata ({", ".join(columns)})
        VALUES ({", ".join("%(" + c + ")s" for c in columns)})
        ON CONFLICT (filename) DO UPDATE SET
            {", ".join(f"{c} = EXCLUDED.{c}" for c in DOC_METADATA_FIELDS)}
    """
    params = []
    for stem, values in rows.items():
        param = {"filename": stem}
        for field in DOC_METADATA_FIELDS:
            val = values.get(field)
            if val is None or str(val).strip() == "":
                param[field] = None
            else:
                param[field] = val
        params.append(param)

    with db.get_conn() as conn:
        # psycopg3 的 executemany 在 Cursor 上（Connection 没有此方法）
        with conn.cursor() as cur:
            cur.executemany(sql, params)
        conn.commit()
    log.info("文档元数据已写入: docs=%d", len(params))
    return len(params)


# ============ Dify 元数据字段同步 ============


def ensure_metadata_fields(client: Any) -> Dict[str, Dict[str, Any]]:
    """确保 Dify 知识库中存在所有元数据字段（doc_metadata 字段 + manifest 用户列）。

    已存在的字段跳过（不重复创建），缺失的自动创建。
    返回 {field_name: Dify 字段 dict（含 id/name/type）}。
    """
    field_map: Dict[str, Dict[str, Any]] = {}
    existing = client.list_metadata_fields()  # DifyClient 已解包为 list[dict]
    existing_by_name = {f.get("name"): f for f in (existing or [])}

    for name, field_type in ALL_METADATA_FIELD_DEFS.items():
        if name in existing_by_name:
            field_map[name] = existing_by_name[name]
            continue
        created = client.create_metadata_field(name=name, type_=field_type)
        # 兼容 Dify 返回裸字段对象或 {"doc_metadata": {...}} 包装两种形态
        if isinstance(created, dict) and "id" not in created and isinstance(created.get("doc_metadata"), dict):
            created = created["doc_metadata"]
        field_map[name] = created
        log.info("已创建 Dify 元数据字段: %s (%s)", name, field_type)

    return field_map


def build_merged_metadata(
    manifest_row: Any,
    doc_meta_row: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """合并单个文档的两处元数据来源（★ 2026-08-31「导入元数据到 Dify」）。

    优先级：doc_metadata 表行（高）> manifest 用户填写列（低）；
    同名字段（effective_date）doc_metadata 行有值时优先，否则回落 manifest 列。

    Args:
        manifest_row: manifest 行（ManifestRow 或 dict；无行时传 None）
        doc_meta_row: doc_metadata 表行 {field: value}（无行时传 None/空 dict）
    """

    def _m(field: str) -> Any:
        if manifest_row is None:
            return None
        if isinstance(manifest_row, dict):
            return manifest_row.get(field)
        return getattr(manifest_row, field, None)

    values: Dict[str, Any] = {}
    # manifest 用户列先入（低优先级）
    for field in MANIFEST_METADATA_FIELD_DEFS:
        v = _m(field)
        if v is None or str(v).strip() == "":
            continue
        if field == "seq":
            try:
                values[field] = int(v)
            except (TypeError, ValueError):
                continue
        else:
            values[field] = str(v).strip()
    # doc_metadata 行覆盖 / 补充（高优先级）
    for field, v in (doc_meta_row or {}).items():
        if v is None or str(v).strip() == "":
            continue
        values[field] = v
    return values


def build_metadata_operation(
    document_id: str,
    metadata_values: Dict[str, Any],
    field_map: Dict[str, Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """为单个 Dify 文档构建 batch_update_document_metadata 的 operation 项。

    Args:
        document_id: Dify 文档 ID（manifest.dify_doc_id）
        metadata_values: {field_name: value}（build_merged_metadata 的产物）
        field_map: {field_name: Dify 元数据字段 dict}（ensure_metadata_fields 的产物）

    Returns:
        Dify 端约定的 operation：
            {"document_id": ..., "metadata_list": [{"id","name","value"}, ...],
             "partial_update": True}
        无可写字段时返回 None。
    """
    metadata_list = []
    for field, value in metadata_values.items():
        meta_field = field_map.get(field)
        if meta_field is None or value is None or str(value).strip() == "":
            continue
        metadata_list.append(
            {"id": meta_field["id"], "name": meta_field["name"], "value": value}
        )
    if not metadata_list:
        return None
    return {
        "document_id": document_id,
        "metadata_list": metadata_list,
        "partial_update": True,
    }
