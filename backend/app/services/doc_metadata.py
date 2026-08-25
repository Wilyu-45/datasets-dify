"""文档元数据管理（doc_metadata 表 → Dify Metadata API）。

历史：文档元数据原先以 Excel（doc_metadata.xlsx，含英文字段名首行 + 中文列名第二行）
存储，现已统一迁移到 PostgreSQL 的 doc_metadata 表（表结构见 app.db.DOC_METADATA_TABLE_SQL）。

在 Dify 入库时：
    1. 确保知识库中存在对应的元数据字段（首次运行时自动创建）
    2. 文档入库成功后，批量设置该文档的元数据值

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
        if row_data:
            result[stem] = row_data

    log.info("文档元数据加载完成: docs=%d", len(result))
    return result


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
        conn.executemany(sql, params)
        conn.commit()
    log.info("文档元数据已写入: docs=%d", len(params))
    return len(params)


# ============ Dify 元数据字段同步 ============


def ensure_metadata_fields(client: Any) -> Dict[str, Dict[str, Any]]:
    """确保 Dify 知识库中存在所有元数据字段。

    已存在的字段跳过（不重复创建），缺失的自动创建。
    """
    field_map: Dict[str, Dict[str, Any]] = {}
    existing = client.list_metadata_fields()
    existing_by_name = {f.get("name"): f for f in existing.get("data", [])}

    for name, field_type in METADATA_FIELD_DEFS.items():
        if name in existing_by_name:
            field_map[name] = existing_by_name[name]
            continue
        created = client.create_metadata_field(name=name, type=field_type)
        field_map[name] = created
        log.info("已创建 Dify 元数据字段: %s (%s)", name, field_type)

    return field_map


def build_metadata_operation(
    stem: str,
    doc_metadata_row: Dict[str, Any],
    field_map: Dict[str, Dict[str, Any]],
) -> Dict[str, Any]:
    """为单个文档构建 metadata batch update 的 operation_data 项。

    Args:
        stem: 文档文件名（不含后缀）
        doc_metadata_row: {field_name: value}（来自 doc_metadata 表）
        field_map: {field_name: Dify metadata field dict}
    """
    op_data: Dict[str, Any] = {}
    for field, value in doc_metadata_row.items():
        meta_field = field_map.get(field)
        if meta_field is None:
            continue
        op_data[meta_field["name"]] = value

    return {
        "doc_metadata_name": stem,
        "operation_data": op_data,
    }
