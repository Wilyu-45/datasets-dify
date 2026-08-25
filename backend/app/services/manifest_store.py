"""manifest 存储：PostgreSQL 实现（原 Excel manifest.xlsx 已废弃）。

历史：manifest 原先存储在 data/manifest.xlsx（openpyxl 读写 + 损坏备份），
现已统一迁移到 PostgreSQL 的 manifest 表（表结构见 app.db.MANIFEST_TABLE_SQL）。

对外 API 与旧版保持兼容，调用方无需改动：
    find_manifest_file(data_dir) / ensure_exists(path) / bootstrap(data_dir)
    load(path) / upsert(path, row) / bulk_upsert(path, rows)
    ensure_columns(path) / now_iso()
传入的 path 参数已无实际意义（数据存于 PostgreSQL），仅保留以兼容既有调用。
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from app import db
from app.config import settings
from app.models.schemas import ManifestRow

log = logging.getLogger("ragsystem.services.manifest_store")

# ============ 列定义（与 manifest 表 20 列对应） ============

HEADERS_ZH = [
    "序号", "文件名", "一级分类", "二级分类", "关键词", "部门", "生效日期",
    "导入状态", "处理状态", "已核对", "处理备注", "状态", "MD5",
    "创建时间", "更新时间", "错误信息", "解析", "切块", "dify文档ID", "dify状态",
]

# 系统自动维护的列（不要求用户在 Excel 里提供，现由数据库管理）
SYSTEM_HEADERS_ZH = [
    "导入状态", "处理状态", "已核对", "处理备注", "状态", "MD5",
    "创建时间", "更新时间", "错误信息", "解析", "切块", "dify文档ID", "dify状态",
]
PARSE_HEADER_ZH = "解析"
CHUNKS_HEADER_ZH = "切块"
DIFY_HEADERS_ZH = ["dify文档ID", "dify状态"]
USER_HEADERS_ZH = [
    "文件名", "一级分类", "二级分类", "关键词", "部门", "生效日期",
]

HEADER_TO_FIELD = {
    "序号": "seq",
    "文件名": "filename",
    "一级分类": "category_l1",
    "二级分类": "category_l2",
    "关键词": "keywords",
    "部门": "department",
    "生效日期": "effective_date",
    "导入状态": "import_status",
    "处理状态": "process_status",
    "已核对": "verified",
    "处理备注": "process_note",
    "状态": "status",
    "MD5": "md5",
    "创建时间": "create_time",
    "更新时间": "update_time",
    "错误信息": "error_msg",
    "解析": "parse",
    "切块": "chunks",
    "dify文档ID": "dify_doc_id",
    "dify状态": "dify_status",
}
FIELD_TO_HEADER = {v: k for k, v in HEADER_TO_FIELD.items()}

# manifest 表的列（顺序固定，INSERT / UPDATE 共用）
MANIFEST_FIELDS: List[str] = [
    "seq", "filename", "category_l1", "category_l2", "keywords",
    "department", "effective_date", "import_status", "process_status",
    "verified", "process_note", "status", "md5", "create_time",
    "update_time", "error_msg", "parse", "chunks", "dify_doc_id", "dify_status",
]

_UPSERT_SQL = f"""
INSERT INTO manifest ({", ".join(MANIFEST_FIELDS)})
VALUES ({", ".join("%(" + f + ")s" for f in MANIFEST_FIELDS)})
ON CONFLICT (filename) DO UPDATE SET
    {", ".join(f"{f} = EXCLUDED.{f}" for f in MANIFEST_FIELDS)}
"""

# 写操作串行化，避免并发覆盖（替代原 Excel 写锁语义）
_write_lock = threading.RLock()


def now_iso() -> str:
    """当前时间（UTC），格式与旧版保持一致。"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


# ============ 兼容旧 API（path 参数保留但忽略） ============


def find_manifest_file(data_dir: Optional[Path] = None) -> Path:
    """（已废弃）返回数据根目录。manifest 已存储于 PostgreSQL manifest 表。"""
    return Path(data_dir) if data_dir else settings.data_root


def ensure_exists(path: Optional[Path] = None) -> None:
    """确保 manifest 表存在（幂等）。"""
    db.init_db()


def bootstrap(data_dir: Optional[Path] = None) -> Path:
    """初始化 manifest 存储（幂等建表）。返回 data_dir 以兼容旧签名。"""
    db.init_db()
    return Path(data_dir) if data_dir else settings.data_root


def ensure_columns(path: Optional[Path] = None) -> Tuple[bool, List[str]]:
    """PG 表结构固定，无需补列。返回 (changed=False, missing=[])。"""
    return False, []


# ============ 读写 ============


def _row_to_dict(row: ManifestRow) -> Dict:
    data = {}
    for field in MANIFEST_FIELDS:
        data[field] = getattr(row, field, None)
    return data


def _fill_timestamps(data: Dict) -> None:
    now = now_iso()
    if not data.get("create_time"):
        data["create_time"] = now
    if not data.get("update_time"):
        data["update_time"] = now


def load(path: Optional[Path] = None) -> Dict[str, ManifestRow]:
    """读取 manifest 全表，返回 {filename: ManifestRow}。"""
    with db.get_conn() as conn:
        cur = conn.execute("SELECT * FROM manifest")
        records = cur.fetchall()
    result: Dict[str, ManifestRow] = {}
    for rec in records:
        row = ManifestRow(**rec)
        result[row.filename] = row
    return result


def fetch(filename: str) -> Optional[ManifestRow]:
    """按文件名查询单行。"""
    with db.get_conn() as conn:
        cur = conn.execute("SELECT * FROM manifest WHERE filename = %s", (filename,))
        rec = cur.fetchone()
    return ManifestRow(**rec) if rec else None


def upsert(path: Optional[Path] = None, row: Optional[ManifestRow] = None, **overrides) -> None:
    """插入或更新单行（兼容 upsert(path, row) 与 upsert(row) 两种调用）。"""
    if isinstance(path, ManifestRow) and row is None:
        row = path
    if row is None:
        raise ValueError("upsert() 需要 row 参数")
    data = _row_to_dict(row)
    data.update(overrides)
    _fill_timestamps(data)
    with _write_lock:
        with db.get_conn() as conn:
            conn.execute(_UPSERT_SQL, data)
            conn.commit()


def bulk_upsert(path: Optional[Path] = None, rows: Optional[Iterable[ManifestRow]] = None) -> None:
    """批量插入或更新（兼容 bulk_upsert(path, rows) 与 bulk_upsert(rows)）。"""
    if isinstance(path, (list, tuple, set)) and rows is None:
        rows = path
    rows = list(rows or [])
    if not rows:
        return
    params = []
    for row in rows:
        data = _row_to_dict(row)
        _fill_timestamps(data)
        params.append(data)
    with _write_lock:
        with db.get_conn() as conn:
            conn.executemany(_UPSERT_SQL, params)
            conn.commit()


def delete(filename: str) -> None:
    """按文件名删除一行。"""
    with _write_lock:
        with db.get_conn() as conn:
            conn.execute("DELETE FROM manifest WHERE filename = %s", (filename,))
            conn.commit()


def count() -> int:
    """manifest 总行数。"""
    with db.get_conn() as conn:
        cur = conn.execute("SELECT COUNT(*) AS c FROM manifest")
        return int(cur.fetchone()["c"])


def clear() -> None:
    """清空 manifest 表所有行（测试隔离 / 重置用）。"""
    with _write_lock:
        with db.get_conn() as conn:
            conn.execute("DELETE FROM manifest")
            conn.commit()


def exists() -> bool:
    """manifest 表是否存在。"""
    with db.get_conn() as conn:
        cur = conn.execute(
            "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
            "WHERE table_name = 'manifest') AS e"
        )
        return bool(cur.fetchone()["e"])
