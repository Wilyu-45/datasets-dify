"""数据库持久化层（manifest / doc_metadata），支持 PostgreSQL / MySQL 双方言。

历史：这两个数据集原先以 Excel（manifest.xlsx / doc_metadata.xlsx）存储，
现已统一迁移到关系数据库。本地开发默认 PostgreSQL（psycopg）；
生产环境可用 RAG_DB_TYPE=mysql 切换到 MySQL（pymysql），
通过本模块的方言适配层屏蔽底层差异，对外接口保持不变。

用法：
    from app import db
    db.init_db()          # 幂等建表（应用启动时调用）
    with db.get_conn() as conn:
        conn.execute("SELECT ...")
        conn.commit()
    db.close_pool()       # 进程退出时调用

配置项见 app.config.Settings（RAG_DB_TYPE / RAG_PG_* / RAG_MYSQL_*）。

方言差异处理：
- PostgreSQL：TEXT / BIGSERIAL / JSONB / DOUBLE PRECISION / ON CONFLICT ...
- MySQL：VARCHAR 主键与索引列 / BIGINT AUTO_INCREMENT / JSON / DOUBLE，
  索引与补列用 information_schema 探测（MySQL 不支持 CREATE INDEX / ADD COLUMN
  的 IF NOT EXISTS 语法）。
"""

from __future__ import annotations

import logging
import queue
import threading
from typing import Any

from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

# pyright: reportImplicitRelativeImport=false
# 后端进程始终以 backend/ 为运行根，`from app.config import settings` 是标准绝对导入；
# 类型检查器按 backend.app.db 嵌套包解析时才会误报为隐式相对导入。
from app.config import settings

log = logging.getLogger("ragsystem.db")

# 全局连接池：PostgreSQL 用 psycopg_pool.ConnectionPool；
# MySQL 用本模块的 _MySQLPool。二者都支持 get_conn() 返回的上下文管理器语义。
_pool: Any = None

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
# ★ 2026-09 新增：page_time=抓取内容本身在网站上的更新时间（从页面 meta/正文提取，
#   附件无可识别时间则留空）；content_hash=本次抓取内容的指纹（网页=正文 MD5，
#   附件=文件字节 MD5）。下次抓取到同一 URL 时比对指纹即可判断「网站有没有更新」。
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
    page_time     TEXT,
    content_hash  TEXT,
    created_at    TEXT,
    updated_at    TEXT,
    UNIQUE (task_id, url)
);
CREATE INDEX IF NOT EXISTS idx_webscrape_records_time ON webscrape_records (created_at);
CREATE INDEX IF NOT EXISTS idx_webscrape_records_url ON webscrape_records (url);
"""

# webscrape_records 旧表补列（★ 2026-09：页面更新时间 + 内容指纹；幂等）
WEBSCRAPE_RECORD_ALTER_SQL = """
ALTER TABLE webscrape_records ADD COLUMN IF NOT EXISTS page_time TEXT;
ALTER TABLE webscrape_records ADD COLUMN IF NOT EXISTS content_hash TEXT;
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
    + "\n"
    + WEBSCRAPE_RECORD_ALTER_SQL
)


# ============ MySQL 方言 DDL（RAG_DB_TYPE=mysql 时生效） ============
# 与上方 PG DDL 表结构一一对应，差异点：
# - 主键/被索引列：TEXT -> VARCHAR（InnoDB 索引键长限制，utf8mb4 单键 ≤3072 字节）
# - BIGSERIAL -> BIGINT AUTO_INCREMENT；JSONB -> JSON；DOUBLE PRECISION -> DOUBLE
# - CREATE INDEX / ADD COLUMN 不支持 IF NOT EXISTS，改由 init 时 information_schema 探测
_MYSQL_MANIFEST = """
CREATE TABLE IF NOT EXISTS manifest (
    filename        VARCHAR(255) PRIMARY KEY,
    seq             INT,
    category_l1     TEXT,
    category_l2     TEXT,
    keywords        TEXT,
    department      TEXT,
    effective_date  TEXT,
    import_status   TEXT,
    process_status  TEXT,
    verified        TEXT,
    process_note    TEXT,
    status          VARCHAR(64),
    md5             TEXT,
    create_time     TEXT,
    update_time     TEXT,
    error_msg       TEXT,
    parse           TEXT,
    chunks          TEXT,
    dify_doc_id     TEXT,
    dify_status     TEXT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
"""

_MYSQL_DOC_METADATA = """
CREATE TABLE IF NOT EXISTS doc_metadata (
    filename             VARCHAR(255) PRIMARY KEY,
    doc_type_primary     TEXT,
    doc_type_secondary   TEXT,
    topic_primary        TEXT,
    topic_secondary      TEXT,
    core_summary         TEXT,
    entity_label         TEXT,
    attribute_label      TEXT,
    applicable_scenarios TEXT,
    effective_date       TEXT,
    priority             DOUBLE,
    status               TEXT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
"""

_MYSQL_PROCESS_CONFIG_LOG = """
CREATE TABLE IF NOT EXISTS process_config_log (
    id            BIGINT AUTO_INCREMENT PRIMARY KEY,
    run_time      VARCHAR(64),
    source        TEXT,
    profile_id    TEXT,
    profile_name  TEXT,
    dataset_id    TEXT,
    chunk_strategy TEXT,
    config        JSON,
    target_stems  JSON,
    status        TEXT,
    error         TEXT,
    duration_ms   INT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
"""

_MYSQL_WEBSCRAPE_TASK = """
CREATE TABLE IF NOT EXISTS webscrape_tasks (
    id               VARCHAR(255) PRIMARY KEY,
    created_at       VARCHAR(64),
    updated_at       TEXT,
    profile_id       TEXT,
    profile_name     TEXT,
    site_url         TEXT,
    status           TEXT,
    confirm_time     TEXT,
    confirm_profile  TEXT,
    items            JSON
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
"""

_MYSQL_WEBSCRAPE_RECORD = """
CREATE TABLE IF NOT EXISTS webscrape_records (
    id            BIGINT AUTO_INCREMENT PRIMARY KEY,
    task_id       VARCHAR(191) NOT NULL,
    url           VARCHAR(512) NOT NULL,
    title         TEXT,
    kind          TEXT,
    depth         INT NOT NULL DEFAULT 0,
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
    page_time     TEXT,
    content_hash  TEXT,
    created_at    VARCHAR(64),
    updated_at    TEXT,
    UNIQUE KEY uq_webscrape_task_url (task_id, url)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
"""

# 索引（table, index_name, create_index_sql）——init 时按 information_schema 探测后按需创建
_MYSQL_INDEXES = [
    ("manifest", "idx_manifest_status", "CREATE INDEX idx_manifest_status ON manifest (status)"),
    ("webscrape_tasks", "idx_webscrape_tasks_time", "CREATE INDEX idx_webscrape_tasks_time ON webscrape_tasks (created_at)"),
    ("process_config_log", "idx_process_config_log_time", "CREATE INDEX idx_process_config_log_time ON process_config_log (run_time)"),
    ("webscrape_records", "idx_webscrape_records_time", "CREATE INDEX idx_webscrape_records_time ON webscrape_records (created_at)"),
    ("webscrape_records", "idx_webscrape_records_url", "CREATE INDEX idx_webscrape_records_url ON webscrape_records (url)"),
]

# 补列（table, column, add_column_sql）——旧库升级时按 information_schema 探测后按需 ADD
_MYSQL_ALTERS = [
    ("process_config_log", "dataset_id", "ALTER TABLE process_config_log ADD COLUMN dataset_id TEXT"),
    ("process_config_log", "chunk_strategy", "ALTER TABLE process_config_log ADD COLUMN chunk_strategy TEXT"),
    ("webscrape_records", "page_time", "ALTER TABLE webscrape_records ADD COLUMN page_time TEXT"),
    ("webscrape_records", "content_hash", "ALTER TABLE webscrape_records ADD COLUMN content_hash TEXT"),
]


class _MySQLConnection:
    """把 pymysql 连接封装成与 psycopg PoolConnection 兼容的最小接口。

    仅实现本代码库实际用到的方法：execute / commit / rollback，
    并支持 with 语句（异常时回滚，退出时归还连接池；显式 commit 由调用方负责，
    与 psycopg PoolConnection 的语义保持一致）。execute 返回的 cursor
    支持 fetchone / fetchall（DictCursor 返回 dict 行）。
    """

    def __init__(self, pool: "_MySQLPool", raw: Any) -> None:
        self._pool = pool
        self._raw = raw

    def execute(self, sql: str, params: Any = None) -> Any:
        cursor = self._raw.cursor()
        if params is not None:
            cursor.execute(sql, params)
        else:
            cursor.execute(sql)
        return cursor

    def commit(self) -> None:
        self._raw.commit()

    def rollback(self) -> None:
        self._raw.rollback()

    def __enter__(self) -> "_MySQLConnection":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            if exc_type is not None:
                self.rollback()
        finally:
            self._pool.release(self)


class _MySQLPool:
    """基于 queue 的极简同步连接池（min/max/timeout 语义对齐 PG 池）。

    pymysql 无官方连接池，这里用最少的标准库原语实现：
    - 预创建 min_size 条连接放入空闲队列；
    - 取连接优先复用空闲，其次在 max_size 内新建，达到上限则带超时等待；
    - 归还前 rollback 重置事务，close 时关闭全部连接。
    """

    def __init__(self, min_size: int, max_size: int, timeout: float, **connect_kwargs: Any) -> None:
        import pymysql  # 惰性导入：postgres 环境无需安装 pymysql
        self._pymysql = pymysql
        self._min = max(1, int(min_size))
        self._max = max(self._min, int(max_size))
        self._timeout = float(timeout)
        self._connect_kwargs = connect_kwargs
        self._idle: "queue.Queue[Any]" = queue.Queue()
        self._size = 0
        self._lock = threading.Lock()
        self._closed = False
        for _ in range(self._min):
            self._size += 1
            self._idle.put(self._new_connection())

    def _new_connection(self) -> Any:
        return self._pymysql.connect(
            cursorclass=self._pymysql.cursors.DictCursor,
            autocommit=False,
            charset="utf8mb4",
            **self._connect_kwargs,
        )

    def connect(self) -> _MySQLConnection:
        # 1) 优先复用空闲连接
        try:
            raw = self._idle.get_nowait()
            self._ensure_alive(raw)
            return _MySQLConnection(self, raw)
        except queue.Empty:
            pass
        # 2) 未达上限则新建
        can_grow = False
        with self._lock:
            can_grow = self._size < self._max
            if can_grow:
                self._size += 1
        if can_grow:
            return _MySQLConnection(self, self._new_connection())
        # 3) 达到上限，带超时等待空闲连接
        try:
            raw = self._idle.get(timeout=self._timeout)
        except queue.Empty:
            raise TimeoutError(f"MySQL 连接池获取超时（{self._timeout:g}s，max={self._max}）")
        self._ensure_alive(raw)
        return _MySQLConnection(self, raw)

    def release(self, wrapped: _MySQLConnection) -> None:
        raw = wrapped._raw
        try:
            raw.rollback()
        except Exception:  # noqa: BLE001
            pass
        with self._lock:
            closed = self._closed
        if closed:
            with self._lock:
                self._size -= 1
            self._safe_close(raw)
            return
        self._idle.put(raw)

    def _ensure_alive(self, raw: Any) -> None:
        try:
            raw.ping(reconnect=True)
        except Exception:  # noqa: BLE001
            self._safe_close(raw)
            raise

    def _safe_close(self, raw: Any) -> None:
        try:
            raw.close()
        except Exception:  # noqa: BLE001
            pass

    def close(self) -> None:
        with self._lock:
            self._closed = True
        while True:
            try:
                raw = self._idle.get_nowait()
            except queue.Empty:
                break
            self._safe_close(raw)


def _mysql_index_exists(conn: Any, table: str, index_name: str) -> bool:
    cur = conn.execute(
        "SELECT 1 FROM information_schema.statistics "
        "WHERE table_schema = DATABASE() AND table_name = %s AND index_name = %s LIMIT 1",
        (table, index_name),
    )
    return cur.fetchone() is not None


def _mysql_column_exists(conn: Any, table: str, column: str) -> bool:
    cur = conn.execute(
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_schema = DATABASE() AND table_name = %s AND column_name = %s LIMIT 1",
        (table, column),
    )
    return cur.fetchone() is not None


def _init_mysql(conn: Any) -> None:
    """幂等建表（MySQL）：建表 + 按 information_schema 探测补索引/补列。"""
    for stmt in (
        _MYSQL_MANIFEST,
        _MYSQL_DOC_METADATA,
        _MYSQL_PROCESS_CONFIG_LOG,
        _MYSQL_WEBSCRAPE_TASK,
        _MYSQL_WEBSCRAPE_RECORD,
    ):
        conn.execute(stmt)
    for table, name, stmt in _MYSQL_INDEXES:
        if not _mysql_index_exists(conn, table, name):
            conn.execute(stmt)
    for table, column, stmt in _MYSQL_ALTERS:
        if not _mysql_column_exists(conn, table, column):
            conn.execute(stmt)
    conn.commit()


def get_pool() -> Any:
    """获取全局连接池（惰性创建），按 settings.rag_db_type 选择方言。"""
    global _pool
    if _pool is not None:
        return _pool
    if settings.rag_db_type == "mysql":
        _pool = _MySQLPool(
            min_size=settings.mysql_pool_min,
            max_size=settings.mysql_pool_max,
            timeout=settings.mysql_pool_timeout,
            host=settings.mysql_host,
            port=settings.mysql_port,
            user=settings.mysql_user,
            password=settings.mysql_password,
            database=settings.mysql_dbname,
        )
        log.info(
            "MySQL 连接池已就绪: %s:%s/%s",
            settings.mysql_host, settings.mysql_port, settings.mysql_dbname,
        )
    else:
        dsn = make_conninfo(
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


def get_conn() -> Any:
    """从连接池获取一个连接（配合 with 使用，退出时归还连接）。

    - PostgreSQL：返回 psycopg_pool 的 PoolConnection（上下文管理器）。
    - MySQL：返回 _MySQLConnection（上下文管理器，语义对齐）。
    """
    pool = get_pool()
    if isinstance(pool, _MySQLPool):
        return pool.connect()
    return pool.connection()


def init_db() -> None:
    """幂等创建所有表。应用启动时调用。按方言执行对应 DDL。"""
    with get_conn() as conn:
        if settings.rag_db_type == "mysql":
            _init_mysql(conn)
        else:
            _ = conn.execute(INIT_SQL)  # 建表结果通过 commit/日志反馈，返回值无需使用
    log.info("数据库表结构已就绪（manifest / doc_metadata / process_config_log）")


def close_pool() -> None:
    """关闭连接池（进程退出时调用）。"""
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None
        log.info("数据库连接池已关闭")
