"""backend/app/services/env_store.py 的回归测试（★ 2026-10）。

关注点：.env 是应用唯一配置来源，写坏 = 服务起不来，所以必须验证
「失败时原文件不动」「注释与顺序保留」「并发不丢更新」这三件事。

注意：本仓库多个测试 fixture 会 `del sys.modules["app.*"]`，因此所有
被测模块都在测试函数内 import，避免拿到 stale 副本。
"""

from __future__ import annotations

import threading

import pytest


def _read(path):
    return path.read_text(encoding="utf-8")


class TestSetEnvValue:
    def test_replaces_in_place_and_keeps_comments(self, tmp_path):
        from app.services import env_store

        env = tmp_path / ".env"
        env.write_text(
            "# 切分策略\n"
            "RAG_CHUNK_STRATEGY=structure\n"
            "RAG_DB_TYPE=mysql\n"
            "\n"
            "# 下一段\n"
            "RAG_CLEANUP_ENABLED=true\n",
            encoding="utf-8",
        )

        env_store.set_env_value("RAG_CHUNK_STRATEGY", "semantic", path=env)

        text = _read(env)
        assert "RAG_CHUNK_STRATEGY=semantic" in text
        # 其他键、注释、顺序都不许动
        assert text.splitlines()[0] == "# 切分策略"
        assert "RAG_DB_TYPE=mysql" in text
        assert "RAG_CLEANUP_ENABLED=true" in text
        assert text.count("RAG_CHUNK_STRATEGY") == 1

    def test_appends_when_key_absent(self, tmp_path):
        from app.services import env_store

        env = tmp_path / ".env"
        env.write_text("RAG_A=1\n", encoding="utf-8")

        env_store.set_env_value("RAG_B", "2", path=env)

        assert _read(env) == "RAG_A=1\nRAG_B=2\n"

    def test_creates_missing_file(self, tmp_path):
        from app.services import env_store

        env = tmp_path / "sub" / ".env"

        env_store.set_env_value("RAG_NEW", "v", path=env)

        assert _read(env) == "RAG_NEW=v\n"

    def test_replaces_all_duplicate_lines(self, tmp_path):
        """手工编辑过 .env 时同一键可能出现多行，必须全部替换，否则重启读到旧值。"""
        from app.services import env_store

        env = tmp_path / ".env"
        env.write_text(
            "RAG_CHUNK_STRATEGY=structure\nRAG_X=1\nRAG_CHUNK_STRATEGY=fixed\n",
            encoding="utf-8",
        )

        env_store.set_env_value("RAG_CHUNK_STRATEGY", "parent_child", path=env)

        text = _read(env)
        assert text.count("RAG_CHUNK_STRATEGY") == 2
        assert "RAG_CHUNK_STRATEGY=structure" not in text
        assert "RAG_CHUNK_STRATEGY=fixed" not in text

    def test_tolerates_bom_and_export_prefix(self, tmp_path):
        from app.services import env_store

        env = tmp_path / ".env"
        env.write_text("\ufeffRAG_DB_TYPE=postgres\nexport RAG_LOG_LEVEL=info\n",
                       encoding="utf-8")

        env_store.set_env_value("RAG_DB_TYPE", "mysql", path=env)
        env_store.set_env_value("RAG_LOG_LEVEL", "debug", path=env)

        text = _read(env)
        # 带 BOM 的首行仍要认得出来，否则会被当成“键不存在”而重复追加
        assert text.count("RAG_DB_TYPE") == 1
        assert "RAG_DB_TYPE=mysql" in text
        assert text.count("RAG_LOG_LEVEL") == 1
        assert "export RAG_LOG_LEVEL=debug" in text

    def test_rejects_bad_key_and_newline_value(self, tmp_path):
        from app.services import env_store

        env = tmp_path / ".env"
        env.write_text("RAG_A=1\n", encoding="utf-8")

        for bad_key in ("", "RAG A", "1RAG", "RAG-A", "RAG=x"):
            with pytest.raises(ValueError):
                env_store.set_env_value(bad_key, "v", path=env)
        # 换行会把一行配置撕成两行，破坏整个文件
        with pytest.raises(ValueError):
            env_store.set_env_value("RAG_A", "x\nRAG_EVIL=1", path=env)
        with pytest.raises(ValueError):
            env_store.set_env_value("RAG_A", "x\rY", path=env)

        assert _read(env) == "RAG_A=1\n"


class TestAtomicity:
    def test_original_untouched_when_replace_fails(self, tmp_path, monkeypatch):
        import os as _os

        from app.services import env_store

        env = tmp_path / ".env"
        original = "RAG_CHUNK_STRATEGY=structure\nRAG_DB_TYPE=mysql\n"
        env.write_text(original, encoding="utf-8")

        def boom(src, dst):  # noqa: ARG001
            raise OSError("replace failed")

        monkeypatch.setattr(env_store.os, "replace", boom)

        with pytest.raises(OSError):
            env_store.set_env_value("RAG_CHUNK_STRATEGY", "semantic", path=env)

        # 关键：失败后 .env 仍是完整原文件（旧实现是截断式写入，做不到这点）
        assert _read(env) == original
        # 临时文件必须清干净，不能把 .env.tmp.* 留在配置目录里
        leftovers = [p.name for p in tmp_path.iterdir() if ".tmp." in p.name]
        assert leftovers == []
        # 也不会因为“改名备份”出现过 .env 短暂不存在的窗口
        assert env.exists()
        assert _os.fspath(env)

    def test_backup_written_with_previous_content(self, tmp_path):
        from app.services import env_store

        env = tmp_path / ".env"
        env.write_text("RAG_A=1\n", encoding="utf-8")

        env_store.set_env_value("RAG_A", "2", path=env)

        bak = tmp_path / ".env.bak"
        assert bak.is_file()
        assert _read(bak) == "RAG_A=1\n"
        assert _read(env) == "RAG_A=2\n"

    def test_concurrent_writes_keep_both_keys(self, tmp_path):
        """两个配置同时保存（切分策略 + 知识库）不能互相丢更新。"""
        from app.services import env_store

        env = tmp_path / ".env"
        env.write_text("RAG_A=0\n", encoding="utf-8")

        errors: list = []

        def worker(key, value):
            try:
                for _ in range(8):
                    env_store.set_env_value(key, value, path=env)
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        threads = [
            threading.Thread(target=worker, args=("RAG_CHUNK_STRATEGY", "semantic")),
            threading.Thread(target=worker, args=("RAG_DIFY_DATASET_ID", "ds-1")),
            threading.Thread(target=worker, args=("RAG_LOG_LEVEL", "debug")),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []
        text = _read(env)
        assert "RAG_CHUNK_STRATEGY=semantic" in text
        assert "RAG_DIFY_DATASET_ID=ds-1" in text
        assert "RAG_LOG_LEVEL=debug" in text
        assert "RAG_A=0" in text  # 原有键不能被并发写覆盖掉
        # 每个键只应出现一次（读-改-写必须串行，否则会产生重复行）
        for key in ("RAG_CHUNK_STRATEGY", "RAG_DIFY_DATASET_ID", "RAG_LOG_LEVEL"):
            assert text.count(f"{key}=") == 1

    def test_no_temp_file_left_in_data_dir(self, tmp_path):
        from app.services import env_store

        env = tmp_path / ".env"
        env.write_text("RAG_A=1\n", encoding="utf-8")

        env_store.set_env_value("RAG_A", "2", path=env)

        assert sorted(p.name for p in tmp_path.iterdir()) == [".env", ".env.bak"]


class TestEnvFilePath:
    def test_returns_configured_path(self, monkeypatch):
        from app.services import env_store

        # 必须 patch env_store 自己持有的 settings 对象：其他 fixture 会清 sys.modules，
        # 重新 import 得到的 settings 是另一个实例（另一个 Settings 类），
        # model_config 字典并不共用，打到它身上对被测函数不可见。
        monkeypatch.setitem(env_store.settings.model_config, "env_file", "/tmp/x/.env")
        assert env_store.env_file_path() is not None

    def test_none_when_dotenv_disabled(self, monkeypatch, tmp_path):
        from app.services import env_store

        monkeypatch.setitem(env_store.settings.model_config, "env_file", None)
        assert env_store.env_file_path() is None

        with pytest.raises(env_store.EnvWriteError):
            env_store.set_env_value("RAG_A", "1")
