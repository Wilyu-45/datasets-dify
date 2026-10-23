"""解析产物列表接口的健壮性与统计口径回归（★ 2026-10 第二轮）。

上一轮把 api/chunk.py 的列表接口改成了「单遍 + 容错」，但 api/parse.py 的
两个同类接口被漏掉了，仍然：
  - list_parsed：对每个目录调 4 次 rglob/glob，且 sum(... if f.is_file())
    里单个不可访文件（权限/断链）会直接抛错 → 整页 500；
  - list_parsed_files：把 x.is_file() 放进 sorted 的 key，同样会抛错。

本轮把容错遍历收口到 pathguard.iter_files_tolerant 并让 parse.py 复用。
测试确保：统计口径不变（image 只算 .png/.jpg、md/json 只看顶层）+ 坏文件不再 500。
"""

from __future__ import annotations

import types
from pathlib import Path


def _touch(path: Path, content: str) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return len(content)


def _make_parsed_root(root: Path) -> int:
    """构造一棵解析产物目录树，返回「属于某个文档目录的文件」总字节数。"""
    total = 0
    # doc_a：顶层 md / json 各一，图片散落在顶层与子目录
    total += _touch(root / "doc_a" / "content.md", "M" * 12)
    total += _touch(root / "doc_a" / "meta.json", "{}")
    total += _touch(root / "doc_a" / "img1.png", "p" * 5)
    total += _touch(root / "doc_a" / "img2.jpg", "j" * 7)
    total += _touch(root / "doc_a" / "images" / "nested.png", "n" * 3)
    # .jpeg 不在旧口径（只算 *.png / *.jpg）内，必须不计入 image_count
    total += _touch(root / "doc_a" / "photo.jpeg", "should-not-count-as-image")
    # 顶层子目录里还有一个非图片文件，计入 file_count 但不计入 image_count
    total += _touch(root / "doc_a" / "images" / "readme.txt", "t")

    # doc_b：只有一篇 md
    total += _touch(root / "doc_b" / "only.md", "B" * 1)
    # 顶层散落文件（非目录），应被整个忽略
    _touch(root / "stray.txt", "ignore me")
    return total


class TestListParsed:
    def _patch(self, monkeypatch, root):
        from app.api import parse as parse_api

        monkeypatch.setattr(parse_api, "settings",
                            types.SimpleNamespace(parsed_dir=root))
        return parse_api

    def test_counts_and_top_level_md_json(self, tmp_path, monkeypatch):
        parse_api = self._patch(monkeypatch, tmp_path)
        _make_parsed_root(tmp_path)

        items = {d["stem"]: d for d in parse_api.list_parsed()}

        assert set(items) == {"doc_a", "doc_b"}
        a = items["doc_a"]
        assert a["image_count"] == 3                 # png + jpg + nested.png（.jpeg 不算）
        assert a["file_count"] == 7                   # doc_a 目录树内全部文件
        assert a["md"] is not None and a["md"].endswith("content.md")
        assert a["json"] is not None and a["json"].endswith("meta.json")
        b = items["doc_b"]
        assert b["image_count"] == 0
        assert b["file_count"] == 1
        assert b["json"] is None

    def test_total_size_sums_document_files(self, tmp_path, monkeypatch):
        parse_api = self._patch(monkeypatch, tmp_path)
        total_written = _make_parsed_root(tmp_path)

        listed = sum(d["total_size"] for d in parse_api.list_parsed())
        # stray.txt 不属于任何文档目录，不计入
        assert listed == total_written

    def test_survives_unreadable_file(self, tmp_path, monkeypatch):
        """权限/断链抛出的非 ENOENT OSError 不能把列表页打成 500。"""
        parse_api = self._patch(monkeypatch, tmp_path)
        _make_parsed_root(tmp_path)

        real_stat = Path.stat

        def flaky(self, **kwargs):
            if self.name == "content.md":
                raise OSError("权限不足")  # 不带 errno，is_file() 不会吞掉
            return real_stat(self, **kwargs)

        monkeypatch.setattr(Path, "stat", flaky)

        items = {d["stem"]: d for d in parse_api.list_parsed()}
        # doc_a 的 content.md 被跳过（因此 md 变 None），但接口不抛异常
        assert items["doc_a"]["md"] is None
        assert items["doc_b"]["file_count"] == 1

    def test_missing_root_returns_empty(self, tmp_path, monkeypatch):
        parse_api = self._patch(monkeypatch, tmp_path / "nope")
        assert parse_api.list_parsed() == []


class TestListParsedFiles:
    def _patch(self, monkeypatch, root):
        from app.api import parse as parse_api

        monkeypatch.setattr(parse_api, "settings",
                            types.SimpleNamespace(parsed_dir=root))
        return parse_api

    def test_lists_only_files_sorted_by_name(self, tmp_path, monkeypatch):
        doc = tmp_path / "doc_a"
        (doc / "images").mkdir(parents=True)
        _touch(doc / "content.md", "x")
        _touch(doc / "images" / "b.png", "yy")
        _touch(doc / "images" / "a.jpg", "zzz")

        parse_api = self._patch(monkeypatch, tmp_path)
        files = parse_api.list_parsed_files("doc_a")

        # 只出文件、按名排序（不再因 is_file() 进 sort key 而崩）
        assert [f["name"] for f in files] == ["a.jpg", "b.png", "content.md"]
        assert {f["rel_path"] for f in files} == {
            "content.md", "images/a.jpg", "images/b.png"
        }

    def test_survives_vanished_file(self, tmp_path, monkeypatch):
        """清理任务并发删文件时，预览列表接口不应 500。"""
        doc = tmp_path / "doc_a"
        (doc / "images").mkdir(parents=True)
        _touch(doc / "content.md", "keep")
        ghost = doc / "images" / "gone.png"
        _touch(ghost, "bye")

        parse_api = self._patch(monkeypatch, tmp_path)

        real_stat = Path.stat

        def flaky_stat(self, **kwargs):
            if self.name == "gone.png":
                raise OSError("文件已被清理任务删除")
            return real_stat(self, **kwargs)

        monkeypatch.setattr(Path, "stat", flaky_stat)

        files = parse_api.list_parsed_files("doc_a")
        assert [f["name"] for f in files] == ["content.md"]


class TestPathguardIterFilesTolerant:
    def test_returns_files_sorted_with_sizes(self, tmp_path):
        from app.services import pathguard

        _touch(tmp_path / "b.md", "123")
        _touch(tmp_path / "sub" / "a.txt", "4567")

        got = pathguard.iter_files_tolerant(tmp_path)
        names = [f.name for f, _ in got]
        assert names == ["a.txt", "b.md"]
        assert dict((f.name, s) for f, s in got) == {"a.txt": 4, "b.md": 3}

    def test_skips_unreadable_entry(self, tmp_path, monkeypatch):
        from app.services import pathguard

        keep = _touch(tmp_path / "keep.md", "abc")
        _touch(tmp_path / "gone.md", "xyz")

        real_stat = Path.stat

        def flaky(self, **kwargs):
            if self.name == "gone.md":
                raise OSError("gone")
            return real_stat(self, **kwargs)

        monkeypatch.setattr(Path, "stat", flaky)

        got = pathguard.iter_files_tolerant(tmp_path)
        assert [f.name for f, _ in got] == ["keep.md"]
        assert got[0][1] == keep
