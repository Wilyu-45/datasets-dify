"""PostgreSQL 持久化层（manifest / doc_metadata）。

历史：这两个数据集原先以 Excel（manifest.xlsx / doc_metadata.xlsx）存储，
现已统一迁移到 PostgreSQL，由本模块统一管理连接与表结构。

用法：
    from app import db
    db.init_db()          # 幂等建表（应用启动时调用）
    with db.get_conn() as conn:
        conn.execute("SELECT ...")
        conn.commit()
    db.close_pool()       # 进程退出时调用

配置项见 app.config.Settings（RAG_PG_*）。
"""

from __future__ import annotations

import logging
from typing import Optional

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from app.config import settings

log = logging.getLogger("ragsystem.db")

_pool: Optional[ConnectionPool] = None

# ============ 表结构（幂等 DDL） ============

# manifest 表：对应原 manifest.xlsx 的全部 20 列（filename 为主键）。
MANIFEST_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS manifest (
    filename        TEXT PRIMARY KEY,
    seq             INTEGER,
    category_l1     TEXT,
    category_l2     TEXT,
    keywords        TEXT,
    department      TEXT,
    effective_date  TEXT,
    import_status   TEXT,
    process_status  TEXT,
    verified        TEXT,
    process_note    TEXT,
    status          TEXT,
    md5             TEXT,
    create_time     TEXT,
    update_time     TEXT,
    error_msg       TEXT,
    parse           TEXT,
    chunks          TEXT,
    dify_doc_id     TEXT,
    dify_status     TEXT
);
CREATE INDEX IF NOT EXISTS idx_manifest_status ON manifest (status);
"""

# doc_metadata 表：对应原 doc_metadata.xlsx（filename 为文件 stem，不含后缀）。
DOC_METADATA_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS doc_metadata (
    filename             TEXT PRIMARY KEY,
    doc_type_primary     TEXT,
    doc_type_secondary   TEXT,
    topic_primary        TEXT,
    topic_secondary      TEXT,
    core_summary         TEXT,
    entity_label         TEXT,
    attribute_label      TEXT,
    applicable_scenarios TEXT,
    effective_date       TEXT,
    priority             DOUBLE PRECISION,
    status               TEXT
);
"""

INIT_SQL = MANIFEST_TABLE_SQL + "\n" + DOC_METADATA_TABLE_SQL


def get_pool() -> ConnectionPool:
    """获取全局连接池（惰性创建）。"""
    global _pool
    if _pool is None:
        dsn = psycopg.conninfo.make_conninfo(
            host=settings.pg_host,
            port=settings.pg_port,
            dbname=settings.pg_dbname,
            user=settings.pg_user,
            password=settings.pg_password,
        )
        _pool = ConnectionPool(
            conninfo=dsn,
            min_size=settings.pg_pool_min,
            max_size=settings.pg_pool_max,
            kwargs={"row_factory": dict_row},
            open=True,
            timeout=settings.pg_pool_timeout,
        )
        log.info(
            "PostgreSQL 连接池已就绪: %s:%s/%s",
            settings.pg_host, settings.pg_port, settings.pg_dbname,
        )
    return _pool


def get_conn() -> psycopg.Connection:
    """从连接池获取一个连接（配合 with 使用，退出时归还连接）。"""
    return get_pool().connection()


def init_db() -> None:
    """幂等创建所有表。应用启动时调用。"""
    with get_conn() as conn:
        conn.execute(INIT_SQL)
        conn.commit()
    log.info("数据库表结构已就绪（manifest / doc_metadata）")


def close_pool() -> None:
    """关闭连接池（进程退出时调用）。"""
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None
        log.info("PostgreSQL 连接池已关闭")
