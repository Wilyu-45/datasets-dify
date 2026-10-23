"""网站抓取入库台账（webscrape_records 表，2026-08-31 新增）。

★ 需求：网页抓取的每一条内容入库时都要在数据库里有记录，且与
「文档上传处理」用的 manifest 表分开 —— 本表是网站抓取专用的入库台账：

    确认入库时（api/webscrape.py confirm）
        → 每落地成功一项 upsert 一行（task_id + url 幂等）：
          源 URL / 标题 / 类型(content|attachment) / 递归层级 / 落地文件名 /
          目标知识库 / 确认所用配置，status = landed
    流水线（解析→切分→入库）完成后
        → 按 stem 回填 parse / chunks / dify_doc_id / error_msg，
          status = parsed / ingested / error

注意：manifest 表仍会同步登记同名行 —— 流水线（parse_pending /
chunk / dify）以 manifest 为工作队列，这是机制需要；本表才是
网站抓取入库的业务台账，二者通过 filename/stem 关联可互相查证。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

from app.config import settings

log = logging.getLogger("ragsystem.webscrape_store")

# 台账状态机：landed(已落地待流水线) → parsed(已解析) → ingested(已入库) / error(失败)
STATUS_LANDED = "landed"
STATUS_PARSED = "parsed"
STATUS_INGESTED = "ingested"
STATUS_ERROR = "error"


def _now() -> str:
    import time
    return time.strftime("%Y-%m-%d %H:%M:%S")


# upsert 语句按方言分两套（★ 2026-10 修复 MySQL 生产兼容）：
#   PostgreSQL：INSERT ... ON CONFLICT (task_id, url) DO UPDATE SET x = EXCLUDED.x
#   MySQL     ：INSERT ... ON DUPLICATE KEY UPDATE x = VALUES(x)（依赖
#               UNIQUE KEY uq_webscrape_task_url，见 app.db._MYSQL_WEBSCRAPE_RECORD）
# 列名与占位符完全一致，仅冲突子句不同，故由同一份列清单生成。
_UPSERT_COLUMNS = [
    "task_id", "url", "title", "kind", "depth", "filename", "stem",
    "dataset_id", "dataset_name", "profile_id", "profile_name",
    "page_time", "content_hash", "status", "error_msg", "created_at", "updated_at",
]
# 冲突时覆盖的列：task_id/url 是幂等键，created_at 保留首次插入时间
_UPSERT_UPDATE_COLUMNS = [
    c for c in _UPSERT_COLUMNS if c not in ("task_id", "url", "created_at")
]
_UPSERT_INSERT_VALUES = ", ".join(f"%({c})s" for c in _UPSERT_COLUMNS)

_UPSERT_SQL_PG = (
    f"INSERT INTO webscrape_records ({', '.join(_UPSERT_COLUMNS)}) "
    f"VALUES ({_UPSERT_INSERT_VALUES}) "
    f"ON CONFLICT (task_id, url) DO UPDATE SET "
    + ", ".join(f"{c} = EXCLUDED.{c}" for c in _UPSERT_UPDATE_COLUMNS)
)

_UPSERT_SQL_MYSQL = (
    f"INSERT INTO webscrape_records ({', '.join(_UPSERT_COLUMNS)}) "
    f"VALUES ({_UPSERT_INSERT_VALUES}) "
    f"ON DUPLICATE KEY UPDATE "
    + ", ".join(f"{c} = VALUES({c})" for c in _UPSERT_UPDATE_COLUMNS)
)


def upsert_record(
    task_id: str,
    url: str,
    *,
    title: str = "",
    kind: str = "content",
    depth: int = 0,
    filename: str = "",
    stem: str = "",
    dataset_id: str = "",
    dataset_name: str = "",
    profile_id: str = "",
    profile_name: str = "",
    page_time: str = "",
    content_hash: str = "",
    status: str = STATUS_LANDED,
    error_msg: str = "",
) -> None:
    """插入或更新一条入库记录（按 task_id + url 幂等，确认入库时调用）。

    ★ 2026-09：page_time=抓取内容在网站上的更新时间；content_hash=内容指纹
    （网页=正文 MD5，附件=文件字节 MD5）——下次抓取同一 URL 时用于判断“是否更新”。
    """
    from app import db

    now = _now()
    row = {
        "task_id": task_id,
        "url": url,
        "title": title,
        "kind": kind,
        "depth": int(depth or 0),
        "filename": filename,
        "stem": stem,
        "dataset_id": dataset_id,
        "dataset_name": dataset_name,
        "profile_id": profile_id,
        "profile_name": profile_name,
        "page_time": page_time or "",
        "content_hash": content_hash or "",
        "status": status,
        "error_msg": error_msg,
        "created_at": now,
        "updated_at": now,
    }
    with db.get_conn() as conn:
        conn.execute(
            _UPSERT_SQL_MYSQL if settings.rag_db_type == "mysql" else _UPSERT_SQL_PG,
            row,
        )
        conn.commit()


def update_pipeline_result(
    stems: List[str],
    results: Dict[str, Dict[str, Any]],
) -> None:
    """流水线完成后按 stem 批量回填产物与状态。

    Args:
        stems: 本批处理的 stem 列表
        results: {stem: {status, parse, chunks, dify_doc_id, error_msg}}
    """
    from app import db

    now = _now()
    updated = 0
    with db.get_conn() as conn:
        for stem in stems:
            r = results.get(stem)
            if not r:
                continue
            conn.execute(
                """
                UPDATE webscrape_records SET
                    status = %(status)s,
                    parse = COALESCE(NULLIF(%(parse)s, ''), parse),
                    chunks = COALESCE(NULLIF(%(chunks)s, ''), chunks),
                    dify_doc_id = COALESCE(NULLIF(%(dify_doc_id)s, ''), dify_doc_id),
                    error_msg = %(error_msg)s,
                    updated_at = %(now)s
                WHERE stem = %(stem)s
                """,
                {
                    "stem": stem,
                    "status": r.get("status") or STATUS_LANDED,
                    "parse": r.get("parse") or "",
                    "chunks": r.get("chunks") or "",
                    "dify_doc_id": r.get("dify_doc_id") or "",
                    "error_msg": r.get("error_msg") or "",
                    "now": now,
                },
            )
            updated += 1
        conn.commit()
    if updated:
        log.info("webscrape 台账已回填流水线结果: %d 条", updated)


def list_records(limit: int = 100) -> List[Dict[str, Any]]:
    """按时间倒序列出最近的入库台账记录。"""
    from app import db

    limit = max(1, min(int(limit), 500))
    with db.get_conn() as conn:
        cur = conn.execute(
            "SELECT id, task_id, url, title, kind, depth, filename, stem, "
            "dataset_id, dataset_name, profile_id, profile_name, status, "
            "parse, chunks, dify_doc_id, error_msg, page_time, content_hash, "
            "created_at, updated_at "
            "FROM webscrape_records ORDER BY id DESC LIMIT %s",
            (limit,),
        )
        rows = cur.fetchall()
    return [dict(r) for r in rows]


def fetch_latest_ingested_by_url(
    urls: List[str],
) -> Dict[str, Dict[str, Any]]:
    """批量取各 URL「最近一次成功入库」的台账记录（★ 2026-09 更新检测用）。

    成功入库判定：status=ingested 或已回填 dify_doc_id（含 2026-08-31 前后
    两版回填记录）。每个 URL 取 id 最大（最新）的一条。

    Returns:
        {url: {"page_time", "content_hash", "dataset_name", "created_at", ...}}
        某 URL 从未成功入库过则不在返回里。
    """
    urls = [u for u in (urls or []) if u and u.strip()]
    if not urls:
        return {}
    from app import db

    # ★ 2026-10 修复（MySQL 生产兼容）：原写法用 PostgreSQL 独有的
    #   `SELECT DISTINCT ON (url)` 与 `url = ANY(%s::text[])`，MySQL 下直接语法报错。
    #   改为两库通用的 IN + ORDER BY url, id DESC，「每个 URL 取最新一条」
    #   在 Python 侧完成（首次出现即该 URL 的 id 最大行）。
    placeholders = ", ".join(f"%(u{i})s" for i in range(len(urls)))
    params: Dict[str, Any] = {f"u{i}": u for i, u in enumerate(urls)}
    with db.get_conn() as conn:
        cur = conn.execute(
            "SELECT url, page_time, content_hash, dataset_name, created_at "
            "FROM webscrape_records "
            f"WHERE url IN ({placeholders}) "
            "AND (status = 'ingested' OR (dify_doc_id IS NOT NULL AND dify_doc_id <> '')) "
            "ORDER BY url, id DESC",
            params,
        )
        rows = cur.fetchall()
    latest: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        url = r["url"]
        if url not in latest:
            latest[url] = dict(r)
    return latest
