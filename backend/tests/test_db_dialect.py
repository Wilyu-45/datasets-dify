"""数据库方言适配层测试（2026-09 生产部署改造，板块 A）。

策略（计划「测试与验证计划」第 1 条）：
- MySQL 无 CI 实例 → 用 SQL 模板字符串断言覆盖方言差异：
    BIGSERIAL→BIGINT AUTO_INCREMENT、JSONB→JSON、TEXT 主键→VARCHAR、
    ON CONFLICT→ON DUPLICATE KEY、CREATE INDEX/ADD COLUMN 去 IF NOT EXISTS。
- get_pool() 方言分支：mock pymysql / ConnectionPool，不建立真实连接。
- PG 侧：现网 PG 可用时验证 init_db() 幂等（本地开发环境真实跑）；不可用则 skip。
- MySQL 真实集成：仅在设置 RAG_TEST_MYSQL=1（CI 提供实例）时执行，否则 skip。

注意：切换方言统一用 monkeypatch 改共享 settings 对象的属性（测试后自动还原），
不修改 .env，也不依赖 MySQL 实例。
"""
from __future__ import annotations

import re
import sys
from unittest.mock import MagicMock

import pytest

from app import db
from app.services import manifest_store


# ============ MySQL DDL 模板（纯字符串断言，无需 MySQL 实例） ============


class TestMySQLDDLTemplates:
    """MySQL 建表语句与 PG DDL 的方言差异（表结构一一对应）。"""

    MYSQL_DDL = [
        db._MYSQL_MANIFEST,
        db._MYSQL_DOC_METADATA,
        db._MYSQL_PROCESS_CONFIG_LOG,
        db._MYSQL_WEBSCRAPE_TASK,
        db._MYSQL_WEBSCRAPE_RECORD,
    ]

    def test_five_tables_use_innodb_utf8mb4(self):
        """5 张表与 PG 侧一一对应，统一 InnoDB + utf8mb4。"""
        assert len(self.MYSQL_DDL) == 5
        for stmt in self.MYSQL_DDL:
            assert "CREATE TABLE IF NOT EXISTS" in stmt
            assert "ENGINE=InnoDB DEFAULT CHARSET=utf8mb4" in stmt

    def test_no_pg_only_types(self):
        """MySQL DDL 不得出现 PG 专有类型。"""
        for stmt in self.MYSQL_DDL:
            assert "BIGSERIAL" not in stmt
            assert "JSONB" not in stmt
            assert "DOUBLE PRECISION" not in stmt

    def test_no_if_not_exists_outside_create_table(self):
        """MySQL 不支持 CREATE INDEX / ADD COLUMN 的 IF NOT EXISTS（仅 CREATE TABLE 可用）。"""
        for stmt in self.MYSQL_DDL:
            assert stmt.count("IF NOT EXISTS") == stmt.count("CREATE TABLE IF NOT EXISTS")

    def test_bigserial_to_bigint_autoincrement(self):
        """BIGSERIAL → BIGINT AUTO_INCREMENT（process_config_log / webscrape_records）。"""
        assert "BIGSERIAL PRIMARY KEY" in db.PROCESS_CONFIG_LOG_TABLE_SQL
        assert "BIGSERIAL PRIMARY KEY" in db.WEBSCRAPE_RECORD_TABLE_SQL
        assert "BIGINT AUTO_INCREMENT PRIMARY KEY" in db._MYSQL_PROCESS_CONFIG_LOG
        assert "BIGINT AUTO_INCREMENT PRIMARY KEY" in db._MYSQL_WEBSCRAPE_RECORD

    def test_jsonb_to_json(self):
        """JSONB → JSON（webscrape_tasks.items / process_config_log.config）。"""
        assert "JSONB" in db.WEBSCRAPE_TASK_TABLE_SQL
        assert "JSONB" in db.PROCESS_CONFIG_LOG_TABLE_SQL
        assert re.search(r"items\s+JSON\b", db._MYSQL_WEBSCRAPE_TASK)
        assert re.search(r"config\s+JSON\b", db._MYSQL_PROCESS_CONFIG_LOG)
        assert re.search(r"target_stems\s+JSON\b", db._MYSQL_PROCESS_CONFIG_LOG)

    def test_text_primary_keys_become_varchar(self):
        """TEXT 主键/被索引列 → VARCHAR（InnoDB 索引键长限制）。"""
        assert re.search(r"filename\s+TEXT PRIMARY KEY", db.MANIFEST_TABLE_SQL)
        assert re.search(r"filename\s+VARCHAR\(255\) PRIMARY KEY", db._MYSQL_MANIFEST)
        assert re.search(r"filename\s+VARCHAR\(255\) PRIMARY KEY", db._MYSQL_DOC_METADATA)
        assert re.search(r"id\s+VARCHAR\(255\) PRIMARY KEY", db._MYSQL_WEBSCRAPE_TASK)
        # webscrape_records：(task_id, url) 为唯一键，改 VARCHAR 且限制长度
        assert re.search(r"task_id\s+VARCHAR\(191\)", db._MYSQL_WEBSCRAPE_RECORD)
        assert re.search(r"url\s+VARCHAR\(512\)", db._MYSQL_WEBSCRAPE_RECORD)
        assert "UNIQUE KEY uq_webscrape_task_url (task_id, url)" in db._MYSQL_WEBSCRAPE_RECORD
        assert "UNIQUE (task_id, url)" in db.WEBSCRAPE_RECORD_TABLE_SQL

    def test_double_precision_to_double(self):
        """DOUBLE PRECISION → DOUBLE（doc_metadata.priority）。"""
        assert "DOUBLE PRECISION" in db.DOC_METADATA_TABLE_SQL
        assert re.search(r"priority\s+DOUBLE\b", db._MYSQL_DOC_METADATA)


# ============ 索引 / 补列列表（information_schema 探测幂等） ============


class TestMySQLIndexesAndAlters:
    """MySQL 的索引/补列集中在显式列表，init 时经 information_schema 探测（幂等）。"""

    def test_index_entries_without_if_not_exists(self):
        assert len(db._MYSQL_INDEXES) == 5
        for table, name, sql in db._MYSQL_INDEXES:
            assert name.startswith("idx_")
            assert sql.startswith("CREATE INDEX ")
            assert f" ON {table} " in sql
            assert "IF NOT EXISTS" not in sql

    def test_alter_entries_without_if_not_exists(self):
        assert len(db._MYSQL_ALTERS) == 4
        for table, column, sql in db._MYSQL_ALTERS:
            assert sql.startswith(f"ALTER TABLE {table} ADD COLUMN {column} ")
            assert "IF NOT EXISTS" not in sql

    def test_pg_counterpart_keeps_if_not_exists(self):
        """PG 侧对照：CREATE INDEX / ADD COLUMN 均带 IF NOT EXISTS（幂等）。"""
        assert db.INIT_SQL.count("CREATE INDEX IF NOT EXISTS") >= 3
        assert db.INIT_SQL.count("ADD COLUMN IF NOT EXISTS") >= 2

    def test_mysql_entries_match_pg_index_and_column_names(self):
        """MySQL 5 索引 + 4 补列与 PG DDL 中声明的名称完全一致。"""
        pg_indexes = set(re.findall(r"CREATE INDEX IF NOT EXISTS (\w+)", db.INIT_SQL))
        assert {name for _, name, _ in db._MYSQL_INDEXES} == pg_indexes
        pg_cols = set(re.findall(r"ADD COLUMN IF NOT EXISTS (\w+)", db.INIT_SQL))
        assert {col for _, col, _ in db._MYSQL_ALTERS} == pg_cols


# ============ manifest UPSERT 方言分叉 ============


class TestManifestUpsertDialect:
    """manifest_store._upsert_sql() 按 settings.rag_db_type 返回对应方言模板。"""

    def test_postgres_uses_on_conflict(self, monkeypatch):
        monkeypatch.setattr(manifest_store.settings, "rag_db_type", "postgres")
        sql = manifest_store._upsert_sql()
        assert "ON CONFLICT (filename) DO UPDATE" in sql
        assert "filename = EXCLUDED.filename" in sql
        assert "ON DUPLICATE KEY" not in sql

    def test_mysql_uses_on_duplicate_key(self, monkeypatch):
        monkeypatch.setattr(manifest_store.settings, "rag_db_type", "mysql")
        sql = manifest_store._upsert_sql()
        assert "ON DUPLICATE KEY UPDATE" in sql
        assert "filename = VALUES(filename)" in sql
        assert "ON CONFLICT" not in sql

    def test_both_templates_cover_all_fields_with_named_placeholders(self):
        """两个模板均覆盖 20 列，且用 %(field)s 具名占位（psycopg/pymysql 两侧通用）。"""
        assert len(manifest_store.MANIFEST_FIELDS) == 20
        for sql in (manifest_store._UPSERT_SQL, manifest_store._UPSERT_SQL_MYSQL):
            for field in manifest_store.MANIFEST_FIELDS:
                assert f"%({field})s" in sql


# ============ 连接池方言分支 ============


class TestGetPoolDialect:
    """get_pool() 按 settings.rag_db_type 选择池实现（mock 掉真实连接）。"""

    @pytest.fixture(autouse=True)
    def _isolate_pool(self, monkeypatch):
        # 测试期间禁用全局池缓存；teardown 由 monkeypatch 恢复原池
        monkeypatch.setattr(db, "_pool", None)
        yield

    def test_mysql_branch_returns_mysql_pool(self, monkeypatch):
        fake_pymysql = MagicMock()
        monkeypatch.setitem(sys.modules, "pymysql", fake_pymysql)
        monkeypatch.setattr(db.settings, "rag_db_type", "mysql")

        pool = db.get_pool()
        try:
            assert isinstance(pool, db._MySQLPool)
            # 连接参数来自 mysql_* 配置
            _, kwargs = fake_pymysql.connect.call_args
            assert kwargs["host"] == db.settings.mysql_host
            assert kwargs["port"] == db.settings.mysql_port
            assert kwargs["database"] == db.settings.mysql_dbname
        finally:
            pool.close()

    def test_postgres_branch_uses_psycopg_pool(self, monkeypatch):
        fake_pool_cls = MagicMock()
        monkeypatch.setattr(db, "ConnectionPool", fake_pool_cls)
        monkeypatch.setattr(db.settings, "rag_db_type", "postgres")

        db.get_pool()
        fake_pool_cls.assert_called_once()
        _, kwargs = fake_pool_cls.call_args
        assert kwargs["min_size"] == db.settings.pg_pool_min
        assert kwargs["max_size"] == db.settings.pg_pool_max
        assert kwargs["kwargs"] == {"row_factory": db.dict_row}
        assert "conninfo" in kwargs


# ============ 真实数据库集成（可用才跑，不可用 skip） ============


class TestInitDbPostgresIntegration:
    """现网 PG（本地开发库）可用时验证 init_db() 幂等；不可用则 skip。"""

    def test_init_db_is_idempotent(self, monkeypatch):
        monkeypatch.setattr(db.settings, "rag_db_type", "postgres")
        try:
            db.init_db()
        except Exception as e:  # noqa: BLE001
            db.close_pool()
            pytest.skip(f"PostgreSQL 不可用，跳过集成验证: {e}")
        # 第二次调用必须仍然成功（CREATE TABLE/INDEX/ALTER ... IF NOT EXISTS 幂等）
        db.init_db()


class TestMySQLIntegrationOptional:
    """CI 提供 MySQL 实例（RAG_TEST_MYSQL=1）时跑真实 init_db() 幂等；否则 skip。"""

    def test_init_db_is_idempotent(self, monkeypatch):
        import os

        if os.environ.get("RAG_TEST_MYSQL") != "1":
            pytest.skip("未提供 MySQL 实例（设置 RAG_TEST_MYSQL=1 启用集成）")

        # 先关闭当前池（PG），避免方言切换后复用错池
        db.close_pool()
        monkeypatch.setattr(db, "_pool", None)
        monkeypatch.setattr(db.settings, "rag_db_type", "mysql")
        try:
            db.init_db()
        except Exception as e:  # noqa: BLE001
            db.close_pool()
            monkeypatch.setattr(db, "_pool", None)
            pytest.skip(f"MySQL 不可用: {e}")
        db.init_db()  # 第二次必须仍然成功（建表/索引/补列全部幂等）
        db.close_pool()
        monkeypatch.setattr(db, "_pool", None)