"""路径参数防目录逃逸（★ 2026-10 潜在 bug 排查）。

回归背景：parse / chunk 的列表与预览接口用
`str(target).startswith(str(root))` 判断 stem 是否越界，
字符串前缀匹配会被**同前缀的兄弟目录**绕过（data/parsed_backup 以 data/parsed 开头），
现统一收口到 services.pathguard.resolve_within（Path.is_relative_to）。

策略：pathguard 做纯单元断言；接口层直接调用路由函数（FastAPI 路由函数即普通
函数），避免起 TestClient 依赖 PG / Dify 配置。
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
from fastapi import HTTPException

logging.disable(logging.CRITICAL)


# ============ pathguard 单元行为 ============


def test_resolve_within_allows_normal_stem(tmp_path: Path):
    from app.services import pathguard

    root = tmp_path / "parsed"
    (root / "doc1").mkdir(parents=True)

    got = pathguard.resolve_within(root, "doc1")
    assert got == (root / "doc1").resolve()


@pytest.mark.parametrize(
    "bad",
    [
        "../evil",                              # 直接上跳
        "a/../../evil",                         # 先下再上跳
        "sub/../../../evil",                    # 多层上跳（跳出 tmp_path）
        str(Path("..") / ".." / "etc" / "passwd"),   # 向上探到系统目录
    ],
)
def test_resolve_within_rejects_escape(tmp_path: Path, bad: str):
    from app.services import pathguard

    root = tmp_path / "parsed"
    root.mkdir(parents=True)

    with pytest.raises(ValueError):
        pathguard.resolve_within(root, bad)


def test_resolve_within_rejects_absolute_path_outside_root(tmp_path: Path):
    """绝对路径（不在 root 内）必须拒绝。"""
    from app.services import pathguard

    root = tmp_path / "parsed"
    root.mkdir(parents=True)
    outside = tmp_path / "somewhere_else"

    with pytest.raises(ValueError):
        pathguard.resolve_within(root, str(outside))


def test_resolve_within_rejects_sibling_prefix_dir(tmp_path: Path):
    """★ 核心回归：同前缀兄弟目录不能被当成「在 root 之内」。

    data/parsed_backup 的字符串表示以 data/parsed 开头，旧实现
    `str(target).startswith(str(root))` 会放行；Path.is_relative_to 比较的是
    路径段，因此必须拒绝。
    """
    from app.services import pathguard

    root = tmp_path / "parsed"
    sibling = tmp_path / "parsed_backup"
    root.mkdir()
    (sibling / "secret").mkdir(parents=True)

    # 旧写法的判断条件在这里成立（说明旧实现确实会放行）
    legacy_passes = str((sibling / "secret").resolve()).startswith(str(root.resolve()))
    assert legacy_passes, "该用例的前提：同前缀兄弟目录能骗过 startswith"

    with pytest.raises(ValueError):
        pathguard.resolve_within(root, "../parsed_backup/secret")


# ============ 接口层：非法输入 → 400 ============


@pytest.fixture()
def chunk_api(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """把 app.api.chunk 的 settings 指到隔离目录（不动 backend/.env）。"""
    from app import config as cfg_mod

    settings = cfg_mod.Settings(data_root=tmp_path / "data")
    settings.ensure_dirs()
    import app.api.chunk as chunk_mod

    monkeypatch.setattr(chunk_mod, "settings", settings)
    return chunk_mod, settings


def test_chunk_helper_rejects_escape(chunk_api):
    chunk_mod, settings = chunk_api

    with pytest.raises(HTTPException) as exc:
        chunk_mod._resolve_within(settings.chunks_dir, "../chunks_backup/x")
    assert exc.value.status_code == 400

    # 接口层：“../..” 类 stem 直接 400，不会去读切分目录之外的内容
    with pytest.raises(HTTPException) as exc2:
        chunk_mod.list_chunk_files("../../etc")
    assert exc2.value.status_code == 400


def test_chunk_preview_rejects_glob_metachars(chunk_api):
    """chunk_id 会被拼进 glob 模式，`*` / `[` 之类必须挡掉。"""
    chunk_mod, settings = chunk_api
    doc = settings.chunks_dir / "doc1"
    doc.mkdir(parents=True)
    (doc / "chunk_001_intro.md").write_text("x", encoding="utf-8")

    # 正常 id 能读到内容
    got = chunk_mod.preview_chunk("doc1", "chunk_001")
    assert got["content"] == "x"

    for bad in ("chunk_*", "chunk_[0-9]", "chunk_001/../chunk_002"):
        with pytest.raises(HTTPException) as exc:
            chunk_mod.preview_chunk("doc1", bad)
        assert exc.value.status_code == 400, f"应拒绝 chunk_id={bad!r}"


# ============ 接口层：parse 列表 ============


def test_parse_files_rejects_escape(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from app import config as cfg_mod
    import app.api.parse as parse_mod

    settings = cfg_mod.Settings(data_root=tmp_path / "data")
    settings.ensure_dirs()
    monkeypatch.setattr(parse_mod, "settings", settings)

    with pytest.raises(HTTPException) as exc:
        parse_mod.list_parsed_files("../parsed_backup/x")
    assert exc.value.status_code == 400
