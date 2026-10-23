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
        """TEXT 主键/被索引列 → VARCHAR（InnoDB 索引键长限制）。

        ★ 2026-09 租户隔离：manifest / doc_metadata 主键改为复合
        (tenant_id, filename)，filename 不再单独作主键。
        """
        for pg_sql, mysql_sql in (
            (db.MANIFEST_TABLE_SQL, db._MYSQL_MANIFEST),
            (db.DOC_METADATA_TABLE_SQL, db._MYSQL_DOC_METADATA),
        ):
            assert "PRIMARY KEY (tenant_id, filename)" in pg_sql
            assert "PRIMARY KEY (tenant_id, filename)" in mysql_sql
            assert re.search(r"filename\s+TEXT\b", pg_sql)
            assert re.search(r"filename\s+VARCHAR\(255\)", mysql_sql)
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
        assert len(db._MYSQL_INDEXES) == 6
        for table, name, sql in db._MYSQL_INDEXES:
            assert name.startswith("idx_")
            assert sql.startswith("CREATE INDEX ")
            assert f" ON {table} " in sql
            assert "IF NOT EXISTS" not in sql

    def test_alter_entries_without_if_not_exists(self):
        assert len(db._MYSQL_ALTERS) == 8
        for table, column, sql in db._MYSQL_ALTERS:
            assert sql.startswith(f"ALTER TABLE {table} ADD COLUMN {column} ")
            assert "IF NOT EXISTS" not in sql

    def test_pg_counterpart_keeps_if_not_exists(self):
        """PG 侧对照：CREATE INDEX / ADD COLUMN 均带 IF NOT EXISTS（幂等）。"""
        assert db.INIT_SQL.count("CREATE INDEX IF NOT EXISTS") >= 3
        assert db.INIT_SQL.count("ADD COLUMN IF NOT EXISTS") >= 2

    def test_mysql_entries_match_pg_index_and_column_names(self):
        """MySQL 6 索引 + 8 补列与 PG DDL 中声明的名称完全一致。"""
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
        assert "ON CONFLICT (tenant_id, filename) DO UPDATE" in sql
        # filename 是主键列，不出现在 UPDATE SET 中
        assert "EXCLUDED.seq" in sql
        assert "EXCLUDED.filename" not in sql
        assert "ON DUPLICATE KEY" not in sql

    def test_mysql_uses_on_duplicate_key(self, monkeypatch):
        monkeypatch.setattr(manifest_store.settings, "rag_db_type", "mysql")
        sql = manifest_store._upsert_sql()
        assert "ON DUPLICATE KEY UPDATE" in sql
        assert "seq = VALUES(seq)" in sql
        assert "filename = VALUES(filename)" not in sql
        assert "ON CONFLICT" not in sql

    def test_both_templates_cover_all_fields_with_named_placeholders(self):
        """两个模板均覆盖 21 列（20 列 + tenant_id），且用 %(field)s 具名占位。"""
        assert len(manifest_store.MANIFEST_FIELDS) == 21
        for sql in (manifest_store._UPSERT_SQL, manifest_store._UPSERT_SQL_MYSQL):
            for field in manifest_store.MANIFEST_FIELDS:
                assert f"%({field})s" in sql


# ============ 运行期 SQL 方言（★ 2026-10 潜在 bug 排查新增） ============


class TestRuntimeSQLDialect:
    """建表 DDL 做过双方言，**运行期 SQL** 也曾漏：

    - webscrape_records / webscrape_tasks 的 upsert 用 ON CONFLICT ... EXCLUDED
    - fetch_latest_ingested_by_url 用 DISTINCT ON + `= ANY(%s::text[])`
    - config_run_log / webscraper 的 JSON 列直接塞 psycopg 的 Jsonb 对象

    这些在 MySQL 下分别报语法错 / TypeError；而 record_run 内部吞异常，
    表现为「配置追溯记录静默全丢」。本地测试库是 PG，故从未暴露 ——
    本类用字符串 + captured-SQL 断言把修复固化为回归（无需真实 MySQL）。

    ★ 注意：部分测试文件的 fixture 会 `del sys.modules["app.*"]`，使得本文件顶部
    import 的 db 成为** stale 副本**；而被测服务在函数内 `from app import db`
    拿到的是重新导入的新模块。因此下面统一用 _db() 取当前模块再打补丁，
    否则 monkeypatch 打在旧模块上、真实 PG 会被调用（报 ON DUPLICATE 语法错）。
    """

    @staticmethod
    def _db():
        import app.db as db_mod

        return db_mod

    # ---- webscrape_records：由列清单生成，两条语句必须结构一致 ----

    def test_webscrape_upsert_templates_are_dialect_specific(self):
        from app.services import webscrape_store as ws

        assert "ON CONFLICT (task_id, url) DO UPDATE SET" in ws._UPSERT_SQL_PG
        assert "EXCLUDED.status" in ws._UPSERT_SQL_PG
        assert "ON DUPLICATE KEY" not in ws._UPSERT_SQL_PG

        assert "ON DUPLICATE KEY UPDATE" in ws._UPSERT_SQL_MYSQL
        assert "status = VALUES(status)" in ws._UPSERT_SQL_MYSQL
        assert "ON CONFLICT" not in ws._UPSERT_SQL_MYSQL
        assert "EXCLUDED." not in ws._UPSERT_SQL_MYSQL

    def test_webscrape_upsert_templates_share_columns_and_placeholders(self):
        """同套列名/具名占位符（漏列会报 Unknown column 或 KeyError）。"""
        from app.services import webscrape_store as ws

        assert len(ws._UPSERT_COLUMNS) == len(ws._UPSERT_UPDATE_COLUMNS) + 3
        for sql in (ws._UPSERT_SQL_PG, ws._UPSERT_SQL_MYSQL):
            for col in ws._UPSERT_COLUMNS:
                assert f"%({col})s" in sql, f"占位符缺 {col}"

    def test_webscrape_upsert_keeps_first_created_at(self):
        """task_id/url 是幂等键、created_at 保留首插时间：三者不进冲突更新。"""
        from app.services import webscrape_store as ws

        for sql in (ws._UPSERT_SQL_PG, ws._UPSERT_SQL_MYSQL):
            conflict_tail = sql.split("DO UPDATE SET" if "ON CONFLICT" in sql
                                    else "ON DUPLICATE KEY UPDATE")[-1]
            for col in ("created_at", "task_id", "url"):
                assert f"{col} = " not in conflict_tail, f"{col} 不该被覆盖"

    def test_upsert_record_selects_statement_by_dialect(self, monkeypatch):
        from app.services import webscrape_store as ws

        executed = []
        conn = MagicMock()
        conn.execute.side_effect = lambda sql, params: executed.append((sql, params))
        ctx = MagicMock()
        ctx.__enter__.return_value = conn
        monkeypatch.setattr(self._db(), "get_conn", lambda: ctx)

        for db_type, expect_sql, forbid in (
            ("mysql", ws._UPSERT_SQL_MYSQL, ws._UPSERT_SQL_PG),
            ("postgres", ws._UPSERT_SQL_PG, ws._UPSERT_SQL_MYSQL),
        ):
            monkeypatch.setattr(ws.settings, "rag_db_type", db_type)
            executed.clear()
            ws.upsert_record("t1", "https://a/b", title="x")
            assert executed[0][0] == expect_sql
            assert executed[0][0] != forbid
            assert executed[0][1]["task_id"] == "t1"
            conn.commit.assert_called()

    # ---- fetch_latest_ingested_by_url：PG 独有的 DISTINCT ON / ::text[] ----

    def test_fetch_latest_uses_portable_in_clause(self, monkeypatch):
        from app.services import webscrape_store as ws

        captured = {}

        class _Cur:
            def fetchall(self):
                return []

        def fake_execute(sql, params=None):
            captured["sql"] = sql
            captured["params"] = params
            return _Cur()

        conn = MagicMock()
        conn.execute.side_effect = fake_execute
        ctx = MagicMock()
        ctx.__enter__.return_value = conn
        monkeypatch.setattr(self._db(), "get_conn", lambda: ctx)

        urls = ["https://a/1", "https://a/2", "  ", "https://a/3"]
        ws.fetch_latest_ingested_by_url(urls)

        sql = captured["sql"]
        assert "DISTINCT ON" not in sql.upper()
        assert "::text[]" not in sql
        assert "= ANY(" not in sql
        assert sql.count("%(u") == 3                     # 空白 URL 被过滤
        assert "ORDER BY url, id DESC" in sql
        assert captured["params"] == {"u0": "https://a/1", "u1": "https://a/2",
                                       "u2": "https://a/3"}

    def test_fetch_latest_returns_newest_row_per_url(self, monkeypatch):
        """每个 URL 取 id 最大（最新）一条：由 ORDER BY id DESC + 首次出现判定。"""
        from app.services import webscrape_store as ws

        rows = [
            {"url": "https://a/1", "page_time": "2026-09-02", "content_hash": "b",
             "dataset_name": "KB", "created_at": "2026-09-02 10:00:00"},
            {"url": "https://a/1", "page_time": "2026-08-01", "content_hash": "a",
             "dataset_name": "KB", "created_at": "2026-08-01 10:00:00"},
        ]

        class _Cur:
            def fetchall(self):
                return rows

        conn = MagicMock()
        conn.execute.return_value = _Cur()
        ctx = MagicMock()
        ctx.__enter__.return_value = conn
        monkeypatch.setattr(self._db(), "get_conn", lambda: ctx)

        got = ws.fetch_latest_ingested_by_url(["https://a/1"])
        assert got["https://a/1"]["content_hash"] == "b"

    def test_fetch_latest_empty_input_skips_query(self, monkeypatch):
        from app.services import webscrape_store as ws

        conn = MagicMock()
        ctx = MagicMock()
        ctx.__enter__.return_value = conn
        monkeypatch.setattr(self._db(), "get_conn", lambda: ctx)

        assert ws.fetch_latest_ingested_by_url([]) == {}
        assert ws.fetch_latest_ingested_by_url(None) == {}
        conn.execute.assert_not_called()

    # ---- webscrape_tasks：Jsonb + ON CONFLICT ----

    def test_save_task_uses_json_string_on_mysql(self, monkeypatch):
        from app.services import webscraper

        executed = []
        conn = MagicMock()
        conn.execute.side_effect = lambda sql, params: executed.append((sql, params))
        ctx = MagicMock()
        ctx.__enter__.return_value = conn
        monkeypatch.setattr(self._db(), "get_conn", lambda: ctx)
        monkeypatch.setattr(webscraper.settings, "rag_db_type", "mysql")

        task = {"id": "t1", "created_at": "x", "items": [{"url": "https://a/1", "标题": "中文"}]}
        webscraper.save_task(task)

        sql, row = executed[0]
        assert "ON DUPLICATE KEY UPDATE" in sql
        assert "ON CONFLICT" not in sql and "EXCLUDED." not in sql
        assert isinstance(row["items"], str)          # pymysql 不能转义 Jsonb
        assert "中文" in row["items"]                  # ensure_ascii=False

    def test_save_task_still_uses_jsonb_on_postgres(self, monkeypatch):
        import json
        from psycopg.types.json import Jsonb
        from app.services import webscraper

        executed = []
        conn = MagicMock()
        conn.execute.side_effect = lambda sql, params: executed.append((sql, params))
        ctx = MagicMock()
        ctx.__enter__.return_value = conn
        monkeypatch.setattr(self._db(), "get_conn", lambda: ctx)
        monkeypatch.setattr(webscraper.settings, "rag_db_type", "postgres")

        webscraper.save_task({"id": "t1", "items": [{"url": "https://a/1"}]})

        sql, row = executed[0]
        assert "ON CONFLICT (id) DO UPDATE SET" in sql
        assert "ON DUPLICATE KEY" not in sql
        assert isinstance(row["items"], Jsonb)


# ============ 配置追溯记录的 JSON 列 ============


class TestConfigRunLogJsonParam:
    """record_run 吞异常 + MySQL Jsonb → 追溯记录静默全丢（★ 2026-10 修复）。"""

    @pytest.fixture()
    def captured(self, monkeypatch):
        import app.db as db_mod

        executed = []
        conn = MagicMock()
        conn.execute.side_effect = lambda sql, params: executed.append((sql, params))
        ctx = MagicMock()
        ctx.__enter__.return_value = conn
        monkeypatch.setattr(db_mod, "get_conn", lambda: ctx)
        return executed

    def test_json_param_dialect(self, monkeypatch):
        from psycopg.types.json import Jsonb
        from app.services import config_run_log as crl

        monkeypatch.setattr(crl.settings, "rag_db_type", "mysql")
        got = crl._json_param(["a", "中文"])
        assert isinstance(got, str) and "中文" in got

        monkeypatch.setattr(crl.settings, "rag_db_type", "postgres")
        assert isinstance(crl._json_param(["a"]), Jsonb)

    def test_record_run_row_types_on_mysql(self, captured, monkeypatch):
        from app.services import config_run_log as crl

        monkeypatch.setattr(crl.settings, "rag_db_type", "mysql")
        crl.record_run(
            source=crl.SOURCE_PIPELINE_API,
            config={"llm_api_key": "secret", "chunk_strategy": "structure"},
            target_stems=["doc_a"],
            status="ok",
        )
        _, row = captured[0]
        assert isinstance(row["config"], str) and isinstance(row["target_stems"], str)
        assert "secret" not in row["config"]          # 脱敏仍然生效
        assert row["target_stems"] == '["doc_a"]'

    def test_record_row_keys_match_placeholders(self, captured, monkeypatch):
        """列名与 %(col)s 一一对应（写错名字 pymysql 直接报错，且被吞掉）。"""
        from app.services import config_run_log as crl

        monkeypatch.setattr(crl.settings, "rag_db_type", "mysql")
        crl.record_run(source=crl.SOURCE_UPLOAD_SINGLE, config={}, target_stems=None)
        sql, row = captured[0]
        assert set(re.findall(r"%\((\w+)\)s", sql)) == set(row.keys())
        assert row["target_stems"] == "[]"


# ============ MySQL 连接封装接口完整性 ============


class TestMySQLConnectionInterface:
    """psycopg3 的 executemany 只在 Cursor 上：doc_metadata.save_doc_metadata 走
    `with conn.cursor() as cur`，封装类缺 cursor() 会让该接口在 MySQL 下报错。"""

    def _conn(self):
        import app.db as db_mod

        raw = MagicMock()
        return db_mod._MySQLConnection(MagicMock(), raw), raw

    def test_cursor_delegates_to_raw(self):
        conn, raw = self._conn()
        assert conn.cursor() is raw.cursor.return_value
        raw.cursor.assert_called()

    def test_implements_only_used_methods(self):
        """本仓库对连接对象只用了这 4 个方法 + with 协议。"""
        import app.db as db_mod

        for name in ("execute", "commit", "rollback", "cursor", "__enter__", "__exit__"):
            assert hasattr(db_mod._MySQLConnection, name), name

    def test_describe_target_by_dialect(self, monkeypatch):
        """启动日志不能写死 postgresql://（MySQL 部署会误导运维）。"""
        import app.db as db_mod

        monkeypatch.setattr(db_mod.settings, "rag_db_type", "mysql")
        assert db_mod.describe_target().startswith("mysql://")
        monkeypatch.setattr(db_mod.settings, "rag_db_type", "postgres")
        assert db_mod.describe_target().startswith("postgresql://")


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