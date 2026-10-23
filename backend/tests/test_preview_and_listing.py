"""Excel 预览句柄释放 + 切分产物列表统计的回归测试（★ 2026-10）。

两块都是排查中发现的真实缺陷：
1. `file_preview.xlsx_to_html` 只在成功路径 `wb.close()`，中途报错时
   read_only 工作簿的文件句柄一直不释放——Windows 下源 .xlsx 被锁死，
   清理/重解析/覆盖写入全部失败；
2. `api/chunk.list_chunks` 对同一目录走 3~4 次 rglob，改为单遍统计后
   必须保证口径不变（chunk 数只算顶层 chunk_*.md，图片数算 images/ 全树）。
"""

from __future__ import annotations

import types
from pathlib import Path

import pytest


class _FakeSheet:
    def __init__(self, title: str, fail: bool = False) -> None:
        self.title = title
        self._fail = fail

    def iter_rows(self, values_only: bool = True):  # noqa: ARG002
        yield ["列A", "列B"]
        yield ["值1", "值2"]
        if self._fail:
            raise RuntimeError("工作簿中途读取失败")


class _FakeWB:
    def __init__(self, sheets) -> None:
        self.worksheets = sheets
        self.closed = 0

    def close(self) -> None:
        self.closed += 1


def _patch_load_workbook(monkeypatch, wb):
    openpyxl = pytest.importorskip("openpyxl")

    monkeypatch.setattr(openpyxl, "load_workbook", lambda *a, **k: wb)
    return wb


class TestXlsxPreviewReleasesHandle:
    def test_closes_workbook_on_success(self, monkeypatch):
        from app.services import file_preview

        wb = _patch_load_workbook(monkeypatch, _FakeWB([_FakeSheet("S1")]))
        html = file_preview.xlsx_to_html(Path("dummy.xlsx"))

        assert wb.closed == 1
        assert "列A" in html

    def test_closes_workbook_when_reading_raises(self, monkeypatch):
        """核心回归：读取中途抛异常也要 close（原来会泄漏句柄锁住文件）。"""
        from app.services import file_preview

        wb = _patch_load_workbook(monkeypatch, _FakeWB([_FakeSheet("S1", fail=True)]))
        html = file_preview.xlsx_to_html(Path("dummy.xlsx"))

        assert wb.closed == 1
        assert "无法解析" in html  # 降级为提示页，不抛给前端

    def test_closes_when_load_workbook_itself_raises(self, monkeypatch):
        from app.services import file_preview

        openpyxl = pytest.importorskip("openpyxl")

        def boom(*a, **k):
            raise ValueError("文件已损坏")

        monkeypatch.setattr(openpyxl, "load_workbook", boom)
        html = file_preview.xlsx_to_html(Path("dummy.xlsx"))

        assert "无法解析" in html


def _touch(path: Path, content: str) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return len(content)


class TestListChunksCounting:
    @pytest.fixture()
    def chunks_root(self, tmp_path, monkeypatch):
        from app.api import chunk as chunk_api

        root = tmp_path / "chunks"
        total = 0
        # 顶层 chunk_*.md 才算 chunk
        total += _touch(root / "doc_a" / "chunk_001_概览.md", "A" * 10)
        total += _touch(root / "doc_a" / "chunk_002_细节.md", "B" * 20)
        # metadata 不是 chunk（名字也以 chunk_ 开头，必须靠后缀区分）
        total += _touch(root / "doc_a" / "chunk_metadata.json", "{}")
        # images 目录递归计数；藏在 images 里的 .md 不能算 chunk
        total += _touch(root / "doc_a" / "images" / "img1.png", "x" * 5)
        total += _touch(root / "doc_a" / "images" / "sub" / "img2.png", "y" * 7)
        total += _touch(root / "doc_a" / "images" / "chunk_009_假.md", "z" * 3)
        # 第二个文档 + 一个非目录条目（应被忽略）
        total += _touch(root / "doc_b" / "chunk_001_唯一.md", "C" * 1)
        _touch(root / "stray.txt", "ignore me")

        monkeypatch.setattr(chunk_api, "settings",
                            types.SimpleNamespace(chunks_dir=root))
        return root, total

    def test_counts_match_previous_multi_pass_semantics(self, chunks_root):
        from app.api import chunk as chunk_api

        items = {s.stem: s for s in chunk_api.list_chunks()}

        assert set(items) == {"doc_a", "doc_b"}
        a = items["doc_a"]
        assert a.chunk_count == 2          # 只有顶层两个 chunk_*.md
        assert a.image_count == 3          # images/ 全树文件数
        assert a.file_count == 6           # 目录内全部文件
        assert items["doc_b"].chunk_count == 1
        assert items["doc_b"].image_count == 0

    def test_total_size_sums_all_files(self, chunks_root):
        from app.api import chunk as chunk_api

        _, total_written = chunks_root
        listed = sum(s.total_size for s in chunk_api.list_chunks())
        # total_written 只累加了各子目录里的文件；stray.txt 不属于任何文档目录
        assert listed == total_written

    def test_survives_unreadable_file(self, chunks_root, monkeypatch):
        """权限/断链抛出的非 ENOENT OSError 不能把列表页打成 500。"""
        from app.api import chunk as chunk_api

        root, total_written = chunks_root
        real_stat = Path.stat

        def flaky(self, **kwargs):
            if self.name.endswith(".md") and self.parent == root / "doc_a":
                raise OSError("权限不足")  # 不带 errno，is_file() 不会吞掉
            return real_stat(self, **kwargs)

        monkeypatch.setattr(Path, "stat", flaky)

        items = {s.stem: s for s in chunk_api.list_chunks()}
        # doc_a 的两个 chunk 被跳过，其余统计仍然可用，且不抛异常
        assert items["doc_a"].chunk_count == 0
        assert items["doc_a"].image_count == 3
        assert items["doc_b"].chunk_count == 1

    def test_missing_root_returns_empty(self, tmp_path, monkeypatch):
        from app.api import chunk as chunk_api

        monkeypatch.setattr(
            chunk_api, "settings",
            types.SimpleNamespace(chunks_dir=tmp_path / "nope"),
        )
        assert chunk_api.list_chunks() == []


class TestListChunkFilesSurvivesVanishedFile:
    def test_skips_file_that_disappears_before_stat(self, tmp_path, monkeypatch):
        """清理任务并发删文件时，列表接口不应 500。"""
        from app.api import chunk as chunk_api

        doc = tmp_path / "doc_a"
        (doc / "images").mkdir(parents=True)
        (doc / "chunk_001_a.md").write_text("keep", encoding="utf-8")
        ghost = doc / "images" / "gone.png"
        ghost.write_text("bye", encoding="utf-8")

        monkeypatch.setattr(chunk_api, "settings",
                            types.SimpleNamespace(chunks_dir=tmp_path))

        real_stat = Path.stat

        def flaky_stat(self, **kwargs):
            if self.name == "gone.png":
                raise OSError("文件已被清理任务删除")
            return real_stat(self, **kwargs)

        monkeypatch.setattr(Path, "stat", flaky_stat)

        files = chunk_api.list_chunk_files("doc_a")

        assert [f.name for f in files] == ["chunk_001_a.md"]
