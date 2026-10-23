"""一键流水线整体状态聚合（★ 2026-10 第二轮排查）。

修复的问题：
- 旧实现 ``len(errors) == 4`` 才判 failed，但上传/抓取驱动的流水线**默认关闭
  scan**（最多 3 步会跑），于是「启用的步骤全部失败」被误报成 partial；下游
  webscrape 入库回填依赖 status=="failed" 兜底 pipeline_error，就拿不到整体失败信号。
- 所有步骤都禁用时，文档承诺的 skipped 状态从未产生（旧代码会落到 ok）。
- 空跑时 ``all(... if enabled)`` 对空序列返回 True，dry_run 被误报成 True。

策略：只测状态聚合，四个阶段的真实副作用全部 mock 掉。
"""

from __future__ import annotations

from typing import Any, Dict

import pytest

from app.services import pipeline as pl


def _ok_result(name: str) -> Any:
    """返回一个「像各阶段 Report」的占位对象即可——状态逻辑只看是否抛异常。"""

    class _R:
        def model_dump(self) -> Dict[str, Any]:
            return {"name": name}

    return _R()


def _patch_stages(monkeypatch, outcomes: Dict[str, Any]):
    """outcomes[stage] = None 表示成功返回，Exception 实例表示抛出。"""

    def make(stage: str):
        def _fn(*a, **k):
            res = outcomes[stage]
            if isinstance(res, BaseException):
                raise res
            return _ok_result(stage)

        return _fn

    monkeypatch.setattr(pl.scanner, "scan_and_stage", make("scan"))
    monkeypatch.setattr(pl.parser, "parse_pending", make("parse"))
    monkeypatch.setattr(pl.chunker, "chunk_parsed", make("chunk"))
    monkeypatch.setattr(pl.dify_ingest, "upload_all_docs", make("dify"))


def _req(scan_on=True, parse_on=True, chunk_on=True, dify_on=True, dry=False):
    return pl.PipelineRequest(
        scan=pl.PipelineStep(enabled=scan_on, dry_run=dry),
        parse=pl.PipelineStep(enabled=parse_on, dry_run=dry),
        chunk=pl.PipelineStep(enabled=chunk_on, dry_run=dry),
        dify=pl.PipelineStep(enabled=dify_on, dry_run=dry),
    )


def test_all_enabled_all_ok(monkeypatch):
    _patch_stages(monkeypatch, {"scan": 0, "parse": 0, "chunk": 0, "dify": 0})
    report = pl.run_pipeline(_req())
    assert report.status == "ok"
    assert report.error is None


def test_scan_disabled_total_failure_is_failed(monkeypatch):
    """核心回归：默认关 scan，剩下 3 步全失败 → 必须是 failed 而非 partial。"""
    boom = RuntimeError("x")
    _patch_stages(monkeypatch, {"scan": 0, "parse": boom, "chunk": boom, "dify": boom})
    report = pl.run_pipeline(_req(scan_on=False))
    assert report.status == "failed"
    assert report.error and "parse" in report.error and "dify" in report.error


def test_partial_when_only_some_enabled_fail(monkeypatch):
    boom = RuntimeError("chunk blew up")
    _patch_stages(monkeypatch, {"scan": 0, "parse": 0, "chunk": boom, "dify": 0})
    report = pl.run_pipeline(_req(scan_on=False))
    assert report.status == "partial"


def test_all_steps_disabled_is_skipped(monkeypatch):
    # 不会被调用，但为保险仍 patch 成抛错，确保确实没跑任何步骤
    _patch_stages(monkeypatch, {k: RuntimeError("should not run") for k in
                               ("scan", "parse", "chunk", "dify")})
    req = _req(scan_on=False, parse_on=False, chunk_on=False, dify_on=False)
    report = pl.run_pipeline(req)
    assert report.status == "skipped"
    # 空跑不能被判成 dry_run=True
    assert report.dry_run is False


def test_dry_run_true_only_when_enabled_steps_are_dry(monkeypatch):
    _patch_stages(monkeypatch, {"scan": 0, "parse": 0, "chunk": 0, "dify": 0})
    report = pl.run_pipeline(_req(scan_on=False, dry=True))
    assert report.dry_run is True


def test_mixed_dry_run_is_not_dry(monkeypatch):
    """有一个启用步骤不是 dry_run 时，整体不应算 dry_run。"""
    _patch_stages(monkeypatch, {"scan": 0, "parse": 0, "chunk": 0, "dify": 0})
    req = pl.PipelineRequest(
        scan=pl.PipelineStep(enabled=False),
        parse=pl.PipelineStep(enabled=True, dry_run=True),
        chunk=pl.PipelineStep(enabled=True, dry_run=False),
        dify=pl.PipelineStep(enabled=True, dry_run=True),
    )
    report = pl.run_pipeline(req)
    assert report.dry_run is False
    assert report.status == "ok"


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
