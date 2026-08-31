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

# webscrape_task 表：网站抓取任务（2026-08 新增）。
# 每次「网站抓取页 → 选配置 → 抓取」生成一个任务，内容先落在 data/webscrape/{id}/ 临时区，
# 人为预览确认（content/attachment 逐项勾选）后，确认接口再把选中项落到 parsed//pending/ 并触发流水线。
# 设计要点：
# - items 为 JSONB：每个 URL 一项，含 kind(content=网页正文/attachment=附件文件)、临时路径、
#   标题/文件名/字符数/大小、确认标记与最终入库状态（confirmed/error），预览正文不落库（读文件）。
# - profile_id/profile_name/site_url 为抓取时的配置快照；确认时可能更换配置，记录在 confirm_profile。
WEBSCRAPE_TASK_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS webscrape_tasks (
    id               TEXT PRIMARY KEY,
    created_at       TEXT,
    updated_at       TEXT,
    profile_id       TEXT,
    profile_name     TEXT,
    site_url         TEXT,
    status           TEXT,
    confirm_time     TEXT,
    confirm_profile  TEXT,
    items            JSONB
);
CREATE INDEX IF NOT EXISTS idx_webscrape_tasks_time ON webscrape_tasks (created_at);
"""

# process_config_log 表：每次实际触发处理（上传入库 / 流水线）时，
# 记录当时实际生效的配置快照（配置方案 ID/名称 + 全部配置项 JSONB + 知识库 ID + 切分策略），
# 用于事后追溯「这批文档当时是用什么配置切分/入库的」。
# 注：dataset_id / chunk_strategy 为独立列（快照中的关键字段，便于直接查询比对）。
PROCESS_CONFIG_LOG_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS process_config_log (
    id            BIGSERIAL PRIMARY KEY,
    run_time      TEXT,
    source        TEXT,
    profile_id    TEXT,
    profile_name  TEXT,
    dataset_id    TEXT,
    chunk_strategy TEXT,
    config        JSONB,
    target_stems  JSONB,
    status        TEXT,
    error         TEXT,
    duration_ms   INTEGER
);
CREATE INDEX IF NOT EXISTS idx_process_config_log_time ON process_config_log (run_time);
-- 旧版本已建表时补列（幂等）
ALTER TABLE process_config_log ADD COLUMN IF NOT EXISTS dataset_id TEXT;
ALTER TABLE process_config_log ADD COLUMN IF NOT EXISTS chunk_strategy TEXT;
"""

# webscrape_records 表：网站抓取入库台账（★ 2026-08-31 新增，独立于文档上传的 manifest）。
# 网页抓取的每一条内容确认入库时逐条落一行：源 URL / 递归层级 / 落地文件 /
# 目标知识库 / 所用配置；流水线（解析→切分→入库）完成后回填产物与状态。
# 注：manifest 表仍会登记同名行（流水线以 manifest 为工作队列，机制需要），
# 本表才是网站抓取入库的业务台账；按 (task_id, url) 幂等。
WEBSCRAPE_RECORD_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS webscrape_records (
    id            BIGSERIAL PRIMARY KEY,
    task_id       TEXT NOT NULL,
    url           TEXT NOT NULL,
    title         TEXT,
    kind          TEXT,
    depth         INTEGER NOT NULL DEFAULT 0,
    filename      TEXT,
    stem          TEXT,
    dataset_id    TEXT,
    dataset_name  TEXT,
    profile_id    TEXT,
    profile_name  TEXT,
    status        TEXT,
    parse         TEXT,
    chunks        TEXT,
    dify_doc_id   TEXT,
    error_msg     TEXT,
    created_at    TEXT,
    updated_at    TEXT,
    UNIQUE (task_id, url)
);
CREATE INDEX IF NOT EXISTS idx_webscrape_records_time ON webscrape_records (created_at);
"""

INIT_SQL = (
    MANIFEST_TABLE_SQL
    + "\n"
    + DOC_METADATA_TABLE_SQL
    + "\n"
    + PROCESS_CONFIG_LOG_TABLE_SQL
    + "\n"
    + WEBSCRAPE_TASK_TABLE_SQL
    + "\n"
    + WEBSCRAPE_RECORD_TABLE_SQL
)


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
    log.info("数据库表结构已就绪（manifest / doc_metadata / process_config_log）")


def close_pool() -> None:
    """关闭连接池（进程退出时调用）。"""
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None
        log.info("PostgreSQL 连接池已关闭")
