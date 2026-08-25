"""一键流水线服务（plan.md §3 全流程自动化）。

把 §3.1 扫描 + §3.2 解析 + §3.3 切分 + §3.4 Dify 入库串成一个无状态调用，
方便前端/CLI 一键触发。每个子步骤复用已有 service 函数，保持各阶段独立可观测。

核心设计：
- 每个子步骤可独立启用/禁用（默认全开）
- 各阶段失败可配置 stop_on_error 决定是否继续
- 返回每阶段完整 Report（与单步 API 一样），方便前端聚合展示
- 总耗时 / 总状态聚合在顶层
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional

from app.models.schemas import (
    ChunkReport,
    DifyUploadReport,
    ParseReport,
    ScanReport,
)
from app.services import chunker, dify_ingest, parser, scanner

log = logging.getLogger("ragsystem.pipeline")


@dataclass
class PipelineStep:
    """单个流水线步骤的配置。"""

    enabled: bool = True
    dry_run: bool = False
    # 各阶段专属参数
    force: bool = False  # chunk/dify 阶段：是否强制重做
    strategy: Optional[str] = None  # chunk 阶段：切分策略（空 → 用 settings.chunk_strategy 默认）

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {
            "enabled": self.enabled,
            "dry_run": self.dry_run,
            "force": self.force,
        }
        if self.strategy:
            d["strategy"] = self.strategy
        return d


@dataclass
class PipelineRequest:
    """一键流水线的入参。

    ★ 2026-08 新增 target_stems 白名单（单文件上传 + 一键入库）：
        - target_stems=None（默认）：流水线处理所有待处理文档
        - target_stems=[stem1, stem2, ...]：流水线只处理这些 stem 对应的文档
          用于「单文件上传 + 一键入库」场景——用户上传单文件后，
          流水线只处理这一个文件，不应该处理 manifest / chunks 目录里其他
          走完整 Excel 流程的文档。
    """

    scan: PipelineStep = None  # type: ignore
    parse: PipelineStep = None  # type: ignore
    chunk: PipelineStep = None  # type: ignore
    dify: PipelineStep = None  # type: ignore
    stop_on_error: bool = False
    target_stems: Optional[list] = None  # type: ignore

    def __post_init__(self) -> None:
        # 默认全开 + dry_run=False
        self.scan = self.scan or PipelineStep(enabled=True, dry_run=False)
        self.parse = self.parse or PipelineStep(enabled=True, dry_run=False)
        self.chunk = self.chunk or PipelineStep(enabled=True, dry_run=False)
        self.dify = self.dify or PipelineStep(enabled=True, dry_run=False)


@dataclass
class PipelineReport:
    """一键流水线的总报告。"""

    status: str  # "ok" | "partial" | "failed" | "skipped"
    dry_run: bool
    duration_ms: int
    scan: Optional[ScanReport] = None
    parse: Optional[ParseReport] = None
    chunk: Optional[ChunkReport] = None
    dify: Optional[DifyUploadReport] = None
    error: Optional[str] = None
    step_timings_ms: Dict[str, int] = None  # type: ignore
    target_stems: Optional[list] = None  # type: ignore

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {
            "status": self.status,
            "dry_run": self.dry_run,
            "duration_ms": self.duration_ms,
            "step_timings_ms": self.step_timings_ms or {},
        }
        if self.target_stems is not None:
            d["target_stems"] = list(self.target_stems)
        if self.scan is not None:
            d["scan"] = self.scan.model_dump() if hasattr(self.scan, "model_dump") else self.scan
        if self.parse is not None:
            d["parse"] = self.parse.model_dump() if hasattr(self.parse, "model_dump") else self.parse
        if self.chunk is not None:
            d["chunk"] = self.chunk.model_dump() if hasattr(self.chunk, "model_dump") else self.chunk
        if self.dify is not None:
            d["dify"] = self.dify.model_dump() if hasattr(self.dify, "model_dump") else self.dify
        if self.error:
            d["error"] = self.error
        return d


def run_pipeline(req: PipelineRequest) -> PipelineReport:
    """执行一键流水线：scan → parse → chunk → dify。

    设计原则：
    - 每阶段独立 try/except，单阶段失败不会中断后续阶段（除非 stop_on_error=True）
    - 全部 dry_run 时不调任何外部 API / Dify，纯本地检查
    - 总状态聚合：
        ok     = 全部成功（或全部 dry_run）
        partial= 部分成功（>0 步成功 + >0 步失败）
        failed = 全部失败或第一阶段就失败
        skipped= 全部 enabled=False
    """
    t0 = time.perf_counter()
    log.info(
        "pipeline start: scan=%s parse=%s chunk=%s dify=%s stop_on_error=%s",
        req.scan.enabled, req.parse.enabled, req.chunk.enabled, req.dify.enabled, req.stop_on_error,
        extra={"step": "pipeline", "status": "start"},
    )

    timings: Dict[str, int] = {}
    report = PipelineReport(
        status="pending",
        dry_run=all(s.dry_run for s in (req.scan, req.parse, req.chunk, req.dify) if s.enabled),
        duration_ms=0,
        step_timings_ms=timings,
        target_stems=req.target_stems,
    )
    errors: list = []

    def _run_step(name: str, fn, *args, **kwargs):
        ts = time.perf_counter()
        try:
            result = fn(*args, **kwargs)
            timings[name] = int((time.perf_counter() - ts) * 1000)
            log.info(
                "pipeline %s done in %dms",
                name, timings[name],
                extra={"step": "pipeline", "status": "step_done", "step_name": name},
            )
            return result, None
        except Exception as e:  # noqa: BLE001
            timings[name] = int((time.perf_counter() - ts) * 1000)
            log.exception("pipeline %s failed", name, extra={"step": "pipeline", "status": "step_failed", "step_name": name})
            return None, str(e)

    # 1) 扫描
    if req.scan.enabled:
        # ★ 2026-08 修复（流水线一致性）：force 标志对所有阶段都生效，
        #   包括 scan 和 parse（之前只有 chunk/dify 支持）
        report.scan, err = _run_step(
            "scan", scanner.scan_and_stage,
            dry_run=req.scan.dry_run, force=req.scan.force,
        )
        if err:
            errors.append(("scan", err))
            if req.stop_on_error:
                report.status = "failed"
                report.error = f"scan 阶段失败: {err}"
                report.duration_ms = int((time.perf_counter() - t0) * 1000)
                return report
    else:
        timings["scan"] = 0
        log.info("pipeline scan skipped", extra={"step": "pipeline", "status": "skipped"})

    # 2) 解析
    if req.parse.enabled:
        # ★ 2026-08 修复：parse 也支持 force（清空旧 parsed/ + 重新调 MinerU）
        # ★ 2026-08 修复：parse 支持 target_stems（单文件上传只处理这个文件）
        report.parse, err = _run_step(
            "parse", parser.parse_pending,
            dry_run=req.parse.dry_run, force=req.parse.force,
            target_stems=req.target_stems,
        )
        if err:
            errors.append(("parse", err))
            if req.stop_on_error:
                report.status = "partial" if report.scan else "failed"
                report.error = f"parse 阶段失败: {err}"
                report.duration_ms = int((time.perf_counter() - t0) * 1000)
                return report
    else:
        timings["parse"] = 0

    # 3) 切分
    if req.chunk.enabled:
        # ★ 2026-08 修复：chunk 支持 target_stems（单文件上传只切分这个文件）
        report.chunk, err = _run_step(
            "chunk", chunker.chunk_parsed,
            dry_run=req.chunk.dry_run, force=req.chunk.force,
            target_stems=req.target_stems,
            strategy=req.chunk.strategy or "",
        )
        if err:
            errors.append(("chunk", err))
            if req.stop_on_error:
                report.status = "partial" if (report.scan or report.parse) else "failed"
                report.error = f"chunk 阶段失败: {err}"
                report.duration_ms = int((time.perf_counter() - t0) * 1000)
                return report
    else:
        timings["chunk"] = 0

    # 4) Dify 入库
    if req.dify.enabled:
        # ★ 2026-08 修复：dify 支持 target_stems（单文件上传只入库这个文件）
        report.dify, err = _run_step(
            "dify", dify_ingest.upload_all_docs,
            dry_run=req.dify.dry_run, force=req.dify.force,
            target_stems=req.target_stems,
        )
        if err:
            errors.append(("dify", err))
            if req.stop_on_error:
                report.status = "partial" if (report.scan or report.parse or report.chunk) else "failed"
                report.error = f"dify 阶段失败: {err}"
                report.duration_ms = int((time.perf_counter() - t0) * 1000)
                return report
    else:
        timings["dify"] = 0

    # 5) 汇总状态
    if not errors:
        report.status = "ok"
    elif len(errors) == 4:
        report.status = "failed"
    else:
        report.status = "partial"
    report.error = "; ".join(f"{step}: {err}" for step, err in errors) if errors else None
    report.duration_ms = int((time.perf_counter() - t0) * 1000)

    log.info(
        "pipeline done: status=%s duration=%dms",
        report.status, report.duration_ms,
        extra={"step": "pipeline", "status": "done"},
    )
    return report
