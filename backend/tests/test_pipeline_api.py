"""一键流水线接口入参传递（★ 2026-10 潜在 bug 排查）。

回归背景：/api/pipeline/run 的记录写的是 `req.target_stems`，但 HTTP 入参模型
PipelineRunRequest 从未开放该字段，也没把它透传进 PipelineRequest ——
于是 process_config_log 的 target_stems 列永远是空表，事后追不到
「这批文档当时到底处理了哪些文件」。

策略：把 run_pipeline / record_run / apply_config 等副作用全部 mock 掉，
只断言「请求体 → PipelineRequest → 配置追溯记录」这条参数链路。
"""

from __future__ import annotations

import logging
from contextlib import nullcontext
from typing import Any, Dict

logging.disable(logging.CRITICAL)


def _patch_pipeline_side_effects(monkeypatch, captured: Dict[str, Any]):
    from app.api import pipeline as pipeline_mod
    from app.services import config_run_log, config_store
    from app.services.pipeline import PipelineReport

    def fake_run_pipeline(req):
        captured["req"] = req
        return PipelineReport(status="ok", dry_run=False, duration_ms=1)

    def fake_record_run(**kwargs):
        captured["record"] = kwargs

    monkeypatch.setattr(pipeline_mod, "run_pipeline", fake_run_pipeline)
    monkeypatch.setattr(pipeline_mod.config_run_log, "record_run", fake_record_run)
    monkeypatch.setattr(
        config_run_log, "snapshot_settings_config", lambda: {"chunk_strategy": "structure"}
    )
    # 不真正改写全局 settings（apply_config 是 contextmanager）
    monkeypatch.setattr(config_store, "apply_config", lambda cfg: nullcontext())
    monkeypatch.setattr(config_store, "get_active_profile", lambda *a: None)
    return pipeline_mod


def test_pipeline_run_passes_target_stems(monkeypatch):
    """★ 核心回归：body.target_stems 必须同时到达流水线和追溯记录。"""
    captured: Dict[str, Any] = {}
    pipeline_mod = _patch_pipeline_side_effects(monkeypatch, captured)

    body = pipeline_mod.PipelineRunRequest(target_stems=["doc_a", "doc_b"])
    out = pipeline_mod.post_pipeline_run(body)

    assert out["status"] == "ok"
    assert captured["req"].target_stems == ["doc_a", "doc_b"]
    assert captured["record"]["target_stems"] == ["doc_a", "doc_b"]


def test_pipeline_run_target_stems_defaults_to_none(monkeypatch):
    """不传 target_stems（旧调用方 / 空 body）→ None = 处理全部待处理文档。"""
    captured: Dict[str, Any] = {}
    pipeline_mod = _patch_pipeline_side_effects(monkeypatch, captured)

    pipeline_mod.post_pipeline_run(pipeline_mod.PipelineRunRequest())
    assert captured["req"].target_stems is None
    assert captured["record"]["target_stems"] is None


def test_pipeline_run_transmits_step_flags(monkeypatch):
    """各步骤开关与 stop_on_error 仍然按原语义透传。"""
    captured: Dict[str, Any] = {}
    pipeline_mod = _patch_pipeline_side_effects(monkeypatch, captured)

    body = pipeline_mod.PipelineRunRequest(
        scan=pipeline_mod.PipelineStepIn(enabled=True),
        parse=pipeline_mod.PipelineStepIn(enabled=False),
        chunk=pipeline_mod.PipelineStepIn(enabled=True, force=True, strategy="recursive"),
        dify=pipeline_mod.PipelineStepIn(enabled=True, dry_run=True),
        stop_on_error=True,
        target_stems=["doc_a"],
    )
    pipeline_mod.post_pipeline_run(body)

    req = captured["req"]
    assert req.scan.enabled is True
    assert req.parse.enabled is False
    assert req.chunk.force is True and req.chunk.strategy == "recursive"
    assert req.dify.dry_run is True
    assert req.stop_on_error is True


def test_pipeline_run_unknown_profile_returns_404(monkeypatch):
    """显式指定不存在的配置方案 → 404，不能悄悄用默认配置跑。"""
    import pytest
    from fastapi import HTTPException

    captured: Dict[str, Any] = {}
    pipeline_mod = _patch_pipeline_side_effects(monkeypatch, captured)
    from app.services import config_store

    monkeypatch.setattr(config_store, "get_profile", lambda pid: None)

    with pytest.raises(HTTPException) as exc:
        pipeline_mod.post_pipeline_run(pipeline_mod.PipelineRunRequest(profile_id="nope"))
    assert exc.value.status_code == 404
    assert "req" not in captured  # 校验失败即退出，不进流水线
