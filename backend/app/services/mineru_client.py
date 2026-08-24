"""MinerU API 客户端（plan.md §3.2）。

调用的接口（mineru-api 3.x 自带 FastAPI 服务，multipart 上传）：
    POST {mineru_api_url}/file_parse
        multipart/form-data:
            files=<二进制文件>           # 必需
            backend=hybrid-engine       # 可选（pipeline/vlm-engine/hybrid-engine/vlm-http-client/hybrid-http-client）
            lang_list=["ch"]            # 可选
            formula_enable=true         # 可选
            table_enable=true           # 可选
            response_format_zip=true    # ★ 关键：返回 ZIP 包含所有产物
            return_md=true              # 仅当 response_format_zip=false 时才用
            return_middle_json=false    # 同上
            return_images=false         # 同上
            ... (更多参数见 OpenAPI /docs)

    响应：
        A) response_format_zip=true   → 200 application/zip  → 包含 .md/.json/图片/layout 等所有产物
        B) response_format_zip=false  → 200 application/json → { results: { stem: { md_content, ... } } }

约定：
    - 单文件同步解析；
    - 失败按指数退避重试 N 次（来自 settings.mineru_max_retries）；
    - 5xx/网络错误 → 重试；4xx → 立即失败（参数问题，重试无意义）；
    - 超时由 settings.mineru_api_timeout 控制。

落盘约定：
    - 把所有产物解压到 parsed_dir（默认 data/parsed/{stem}/）；
    - 一文档一文件夹，文件夹名 = 源文件 stem。
"""

from __future__ import annotations

import io
import json
import logging
import os
import shutil
import sys
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx


def _long_path(p: Path) -> str:
    """Windows 长路径支持（>260 字符），其他平台直接返回字符串。

    根因：Windows MAX_PATH=260，MinerU 长文件名 + parsed/ 嵌套后
    轻松超过此限制，导致 open() 报 FileNotFoundError。
    加 '\\\\?\\' 前缀可绕过 MAX_PATH 限制（最高 32767 字符）。
    """
    s = str(p)
    if sys.platform == "win32" and len(s) >= 240 and not s.startswith("\\\\?\\"):
        return "\\\\?\\" + s
    return s

from app.config import settings
from app.services.parse_progress import get_tracker

log = logging.getLogger("ragsystem.mineru_client")


class MinerUError(RuntimeError):
    """MinerU 调用失败。"""

    def __init__(
        self,
        message: str,
        *,
        attempts: int = 1,
        status_code: Optional[int] = None,
        body: Optional[str] = None,
    ) -> None:
        super().__init__(message)
        self.attempts = attempts
        self.status_code = status_code
        self.body = (body or "")[:500]


# OLE 复合文档 magic bytes：旧 .doc 格式
# 真正的 .doc 文件 = D0 CF 11 E0 A1 B1 1A E1
_OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"


# ★ 高质量后端白名单：对扫描件 / 复杂版面效果排序
#     vlm-engine   > hybrid-engine（VLM + 文本） > pipeline
# 见 mdapi.md：vlm-engine 切到 MinerU2.5-Pro-2604/2605，hybrid-engine 兼顾速度与精度。
# 任何不在白名单里的后端（特别是 pipeline）都会被强制升级或打印告警。
_HIGH_QUALITY_BACKENDS: frozenset[str] = frozenset(
    {
        "vlm-engine",
        "hybrid-engine",
        "vlm-http-client",
        "hybrid-http-client",
    }
)
# pipeline / pipeline-http-client 是"低质量"后端，仅纯 CPU 场景或快速冒烟使用
_LOW_QUALITY_BACKENDS: frozenset[str] = frozenset(
    {"pipeline", "pipeline-http-client"}
)


class _UnsupportedLegacyDocError(Exception):
    """用户文件是 .doc 旧 OLE 格式，MinerU 不支持。

    这是客户端预检测，不应浪费一次 API 调用。
    """

    def __init__(self, file_path: Path) -> None:
        self.file_path = file_path
        super().__init__(
            f"MinerU 不支持 .doc 旧 OLE 格式: {file_path.name}。"
            f"请用 Word/WPS 打开后「另存为 .docx」再上传。"
        )


# PDF 文件 magic bytes：所有合规 PDF 都以 "%PDF-" 开头（前 5 字节）
# 用于在 fitz.open / PdfReader 之前先校验，避免 PyMuPDF/pypdf 对非 PDF 文件
# 过度宽容（如把纯文本或部分字节识别为"1 页 PDF"）。
_PDF_MAGIC = b"%PDF-"


def _count_pdf_pages(pdf_path: Path) -> Optional[int]:
    """用 PyMuPDF 准确检测 PDF 页数。返回 None 表示检测失败。

    ★ 2026-08-06：用于「长文档路由」——超长页数 PDF 自动切到 vlm-engine。
    检测失败时返回 None，调用方按"使用默认 backend"处理（不误切）。

    优先用 PyMuPDF（fitz）：速度快、不依赖外部工具、准确。
    fallback：尝试 pypdf（纯 Python，无 native 依赖）。
    """
    if not pdf_path.is_file():
        return None
    # 0) magic bytes 预检测：避免 PyMuPDF/pypdf 对非 PDF 过度宽容
    try:
        with open(pdf_path, "rb") as f:
            head = f.read(5)
        if head[:5] != _PDF_MAGIC:
            log.debug("_count_pdf_pages: %s 不是 PDF (magic=%r)", pdf_path.name, head)
            return None
    except OSError as e:
        log.debug("_count_pdf_pages: 读 magic 失败 %s: %s", pdf_path.name, e)
        return None
    # 1) PyMuPDF（首选，快准）
    try:
        import fitz  # type: ignore

        doc = fitz.open(str(pdf_path))
        try:
            return int(doc.page_count)
        finally:
            doc.close()
    except ImportError:
        pass
    except Exception as e:  # noqa: BLE001
        log.debug("PyMuPDF page count failed: %s", e)
    # 2) pypdf（纯 Python fallback）
    try:
        from pypdf import PdfReader  # type: ignore

        reader = PdfReader(str(pdf_path))
        return len(reader.pages)
    except ImportError:
        pass
    except Exception as e:  # noqa: BLE001
        log.debug("pypdf page count failed: %s", e)
    return None


@dataclass
class ParseResult:
    """一次成功解析的落盘结果。"""

    parse_dir: Path
    md_path: Optional[Path] = None
    json_path: Optional[Path] = None
    images: List[Path] = field(default_factory=list)
    other_files: List[Path] = field(default_factory=list)  # 其它产物（layout, spans, content_list...）
    attempts: int = 1
    response_kind: str = "zip"  # "zip" | "json"

    @property
    def file_count(self) -> int:
        return (
            (1 if self.md_path else 0)
            + (1 if self.json_path else 0)
            + len(self.images)
            + len(self.other_files)
        )


class MinerUClient:
    """薄包装 httpx，对接 mineru-api 的 /file_parse。"""

    def __init__(
        self,
        *,
        api_url: Optional[str] = None,
        timeout: Optional[int] = None,
        max_retries: Optional[int] = None,
        # ★ 2026-08-06：重试参数重命名 + 新增
        #   - retry_initial_wait: 首次重试等待秒数（默认 30，给 API 重启留时间）
        #   - retry_backoff_factor: 指数退避倍数（默认 2.0）
        #   - retry_max_wait: 单次重试最大等待（默认 300s = 5 min）
        #   - backoff: 旧字段（兼容保留）会作为 retry_backoff_factor 默认值
        backoff: Optional[float] = None,
        retry_initial_wait: Optional[float] = None,
        retry_backoff_factor: Optional[float] = None,
        retry_max_wait: Optional[float] = None,
        token: Optional[str] = None,
        response_format_zip: Optional[bool] = None,
        backend: Optional[str] = None,
        effort: Optional[str] = None,
        enforce_high_quality: Optional[bool] = None,
        lang_list: Optional[List[str]] = None,
        formula_enable: Optional[bool] = None,
        table_enable: Optional[bool] = None,
        return_md: Optional[bool] = None,
        return_middle_json: Optional[bool] = None,
        return_model_output: Optional[bool] = None,
        return_content_list: Optional[bool] = None,
        return_images: Optional[bool] = None,
        reject_legacy_doc: Optional[bool] = None,
        # ★ 2026-08-06：长文档路由配置（可选，不传则走 settings）
        long_doc_pages_threshold: Optional[int] = None,
        long_doc_backend: Optional[str] = None,
        long_doc_effort: Optional[str] = None,
    ) -> None:
        self.api_url = (api_url or settings.mineru_api_url).rstrip("/")
        self.timeout = timeout or settings.mineru_api_timeout
        self.max_retries = max_retries or settings.mineru_max_retries
        # ★ 重试参数优先级：入参 > settings
        #   兼容逻辑：旧 backoff 字段以 settings.mineru_retry_backoff_factor 兜底
        self.retry_initial_wait = (
            retry_initial_wait
            if retry_initial_wait is not None
            else settings.mineru_retry_initial_wait
        )
        self.retry_backoff_factor = (
            retry_backoff_factor
            if retry_backoff_factor is not None
            else settings.mineru_retry_backoff_factor
        )
        self.retry_max_wait = (
            retry_max_wait
            if retry_max_wait is not None
            else settings.mineru_retry_max_wait
        )
        # 旧 backoff 字段现在仅作为有效配置。保留为实例属性以便兼容
        self.backoff = backoff if backoff is not None else settings.mineru_retry_backoff
        self.token = token if token is not None else settings.mineru_api_token
        # multipart 参数
        self.response_format_zip = (
            response_format_zip
            if response_format_zip is not None
            else settings.mineru_response_format_zip
        )
        # ★ 后端选择 + 高质量校验
        #  1) 解析传入 / settings 里的 backend；
        #  2) 校验：若 enforce_high_quality 且 backend 是低质量（pipeline），
        #     打印 WARNING 并自动升级到 hybrid-engine（保证效果）；
        #  3) 校验：若 backend 是未知值（非白名单也非低质量），也打印 WARNING 但不修改。
        chosen_backend = (backend or settings.mineru_backend or "").strip()
        self.enforce_high_quality = (
            enforce_high_quality
            if enforce_high_quality is not None
            else settings.mineru_enforce_high_quality
        )
        self.backend = self._resolve_backend(chosen_backend)
        # hybrid-engine / vlm-engine 的 effort 档位（仅 hybrid-engine 生效；其他后端 MinerU 会忽略）
        chosen_effort = (effort or settings.mineru_backend_effort or "high").strip().lower()
        if chosen_effort not in {"low", "medium", "high"}:
            log.warning(
                "mineru effort 值非法: %r，回退到 high", chosen_effort
            )
            chosen_effort = "high"
        self.effort = chosen_effort
        self.lang_list = lang_list or settings.mineru_lang_list
        self.formula_enable = (
            formula_enable
            if formula_enable is not None
            else settings.mineru_formula_enable
        )
        self.table_enable = (
            table_enable
            if table_enable is not None
            else settings.mineru_table_enable
        )
        # ★ 5 个 return_* 开关：默认全开，让 MinerU 把所有产物都放进 ZIP
        self.return_md = (
            return_md if return_md is not None else settings.mineru_return_md
        )
        self.return_middle_json = (
            return_middle_json
            if return_middle_json is not None
            else settings.mineru_return_middle_json
        )
        self.return_model_output = (
            return_model_output
            if return_model_output is not None
            else settings.mineru_return_model_output
        )
        self.return_content_list = (
            return_content_list
            if return_content_list is not None
            else settings.mineru_return_content_list
        )
        self.return_images = (
            return_images
            if return_images is not None
            else settings.mineru_return_images
        )
        # .doc 旧 OLE 预检测开关
        self.reject_legacy_doc = (
            reject_legacy_doc
            if reject_legacy_doc is not None
            else settings.mineru_reject_legacy_doc
        )
        # ★ 2026-08-06：长文档路由配置
        self.long_doc_pages_threshold = (
            long_doc_pages_threshold
            if long_doc_pages_threshold is not None
            else settings.mineru_long_doc_pages_threshold
        )
        self.long_doc_backend = (
            long_doc_backend
            if long_doc_backend is not None
            else settings.mineru_long_doc_backend
        )
        self.long_doc_effort = (
            long_doc_effort
            if long_doc_effort is not None
            else settings.mineru_long_doc_effort
        )
        # endpoint
        self._endpoint = f"{self.api_url}/file_parse"

    # -------------------- 路由决策 --------------------

    def _resolve_long_doc_routing(
        self, file_path: Path
    ) -> tuple[Optional[str], Optional[str], Optional[int]]:
        """根据文件页数决定是否切换为长文档 backend。

        Returns:
            (backend, effort, page_count) — 如果不需要切换，(None, None, None)
            如果需要切换，返回切换后的 backend / effort 和实际页数。
            page_count 为 None 表示无法检测页数（如文件不是 PDF），不切换。

        ★ 2026-08-06 新增：超长页数文档（≥ threshold）走 vlm-engine（精准解析 API）。
        本地部署场景下 vlm-engine 走视觉路径，避免 hybrid-engine 文本路径
        遇复杂 CMap/编码时崩服务。
        """
        if self.long_doc_pages_threshold <= 0:
            # 路由被禁用
            return None, None, None
        if file_path.suffix.lower() != ".pdf":
            # 仅对 PDF 走路由（DOCX/PPTX/XLSX 页数估算不准，宁可不切换）
            return None, None, None
        page_count = _count_pdf_pages(file_path)
        if page_count is None:
            log.warning(
                "mineru 长文档路由：未能检测 PDF 页数（%s），使用默认 backend=%s",
                file_path.name, self.backend,
            )
            return None, None, None
        if page_count < self.long_doc_pages_threshold:
            log.debug(
                "mineru 长文档路由：%s 只有 %d 页（< 阈值 %d），使用默认 backend=%s",
                file_path.name, page_count, self.long_doc_pages_threshold, self.backend,
            )
            return None, None, page_count
        # 切换为长文档 backend
        # 走 _resolve_backend 校验高质量后端
        chosen = self._resolve_backend(self.long_doc_backend)
        chosen_effort = (self.long_doc_effort or "high").strip().lower()
        if chosen_effort not in {"low", "medium", "high"}:
            chosen_effort = "high"
        log.info(
            "mineru 长文档路由：%s 有 %d 页（≥ 阈值 %d），切换 backend=%s → %s（effort=%s）",
            file_path.name, page_count, self.long_doc_pages_threshold,
            self.backend, chosen, chosen_effort,
        )
        return chosen, chosen_effort, page_count

    def _compute_retry_wait(self, attempt: int) -> float:
        """计算第 attempt 次失败后的重试等待秒数。

        公式：wait = min(initial * factor^(attempt-1), max_wait)
        - attempt=1：initial（默认 30s）
        - attempt=2：initial * factor（默认 60s）
        - attempt=3：initial * factor^2（默认 120s）
        - 上限：max_wait（默认 300s）

        ★ 2026-08-06：默认配置足以覆盖 mineru-router / supervisor 重启服务
        的典型耗时（30~90s）。如果 max_wait 不足以覆盖，重试也会被该值封顶。
        """
        if attempt < 1:
            return 0.0
        raw = self.retry_initial_wait * (self.retry_backoff_factor ** (attempt - 1))
        return min(raw, self.retry_max_wait)

    # -------------------- backend 校验 --------------------

    def _resolve_backend(self, backend: str) -> str:
        """校验并按需升级 backend。

        - 若 enforce_high_quality=False：原样返回（只做未知值 WARNING）。
        - 若 enforce_high_quality=True 且 backend 在白名单：原样返回。
        - 若 enforce_high_quality=True 且 backend 是 pipeline 等"低质量"：WARNING 并升级到 hybrid-engine。
        - 若 backend 是未知值（非白名单也非低质量）：WARNING，但不动值（让 MinerU 自己报错，避免静默切换）。

        这样可以保证「.env 误填 pipeline / 调用方传错」时实际效果不会崩塌。
        """
        b = (backend or "").strip()
        if not b:
            log.warning("mineru backend 为空，回退到 hybrid-engine")
            return "hybrid-engine"

        if b in _HIGH_QUALITY_BACKENDS:
            return b

        if b in _LOW_QUALITY_BACKENDS:
            if self.enforce_high_quality:
                log.warning(
                    "mineru backend=%s 是低质量（纯 OCR），强制升级到 hybrid-engine（高质量 VLM）",
                    b,
                )
                return "hybrid-engine"
            log.warning(
                "mineru backend=%s 是低质量后端（pipeline），"
                "对扫描件 / 复杂版面效果差。建议改为 hybrid-engine 或 vlm-engine。",
                b,
            )
            return b

        # 未知后端：保留原值让 MinerU 校验，避免静默切换隐藏配置错误
        log.warning(
            "mineru backend=%r 不在已知列表（%s / %s），"
            "将原样发给 MinerU，若报错请检查配置。",
            b,
            sorted(_HIGH_QUALITY_BACKENDS),
            sorted(_LOW_QUALITY_BACKENDS),
        )
        return b

    # -------------------- public --------------------

    def health_check(self, timeout: float = 10.0) -> dict:
        """检查 MinerU API 健康状态。

        调 MinerU 3.x 自带的 GET /health 端点，返回结构化信息。
        用于 pipeline 启动前预检 + /api/health 端点展示。

        Args:
            timeout: 健康检查专用超时（秒），默认 10s

        Returns:
            dict: {
                "healthy": bool,        # 是否可达
                "version": str | None,  # MinerU 版本号
                "status": str,          # "healthy" / "unreachable" / "error"
                "detail": str,          # 人可读描述
            }
        """
        headers: Dict[str, str] = {}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        health_url = f"{self.api_url}/health"
        try:
            with httpx.Client(timeout=timeout) as client:
                resp = client.get(health_url, headers=headers)
                if 200 <= resp.status_code < 300:
                    try:
                        data = resp.json()
                    except Exception:  # noqa: BLE001
                        data = {}
                    version = data.get("version") or "unknown"
                    api_status = data.get("status") or "ok"
                    queued = data.get("queued_tasks", 0)
                    processing = data.get("processing_tasks", 0)
                    failed = data.get("failed_tasks", 0)
                    log.info(
                        "mineru api 健康: version=%s status=%s queued=%s processing=%s failed=%s",
                        version, api_status, queued, processing, failed,
                        extra={"step": "health", "status": "ok"},
                    )
                    return {
                        "healthy": True,
                        "version": str(version),
                        "status": "healthy",
                        "detail": f"v{version}, queued={queued}, processing={processing}, failed={failed}",
                    }
                else:
                    log.warning(
                        "mineru api 健康检查异常: HTTP %s",
                        resp.status_code,
                        extra={
                            "step": "health",
                            "status": "unhealthy",
                            "error_msg": f"HTTP {resp.status_code}",
                        },
                    )
                    return {
                        "healthy": False,
                        "version": None,
                        "status": "error",
                        "detail": f"HTTP {resp.status_code}",
                    }
        except httpx.TimeoutException:
            log.warning(
                "mineru api 健康检查超时 (%ss)",
                timeout,
                extra={
                    "step": "health",
                    "status": "unhealthy",
                    "error_msg": f"timeout after {timeout}s",
                },
            )
            return {
                "healthy": False,
                "version": None,
                "status": "unreachable",
                "detail": f"timeout after {timeout}s",
            }
        except httpx.HTTPError as e:
            log.warning(
                "mineru api 健康检查异常: %s",
                e,
                extra={
                    "step": "health",
                    "status": "unhealthy",
                    "error_msg": str(e),
                },
            )
            return {
                "healthy": False,
                "version": None,
                "status": "unreachable",
                "detail": str(e),
            }

    def parse_file(self, file_path: Path, parsed_dir: Path) -> ParseResult:
        """上传 file_path → 解析 → 把所有产物落盘到 parsed_dir。

        抛 MinerUError 表示重试耗尽；调用方应将文件移入 error/。
        抛 _UnsupportedLegacyDocError 表示文件是 .doc 旧 OLE 格式（不会被 MinerU 接受）。
        """
        file_path = Path(file_path).resolve()
        if not file_path.is_file():
            raise MinerUError(f"待解析文件不存在: {file_path}")

        # ★ 2026-08-07：初始化进度跟踪
        tracker = get_tracker()
        tracker.update(file_path.name, 0, "准备解析...", "parsing")

        # ★ 预检测：.doc 旧 OLE 格式 MinerU 不支持，提前拒绝避免浪费 API
        if self.reject_legacy_doc and file_path.suffix.lower() == ".doc":
            try:
                with open(file_path, "rb") as f:
                    magic = f.read(8)
                if magic == _OLE_MAGIC:
                    parsed_dir = Path(parsed_dir).resolve()
                    self._cleanup_on_failure(parsed_dir)
                    tracker.update(file_path.name, 0, "不支持的 .doc 格式", "failed")
                    raise _UnsupportedLegacyDocError(file_path)
            except OSError:
                # 读不到 magic 就当 docx 处理（让 MinerU 自己判断）
                pass

        parsed_dir = Path(parsed_dir).resolve()
        # 干净目录：避免上次产物残留
        # ★ 2026-08-12：用 _long_path 绕过 Windows MAX_PATH
        lp = _long_path(parsed_dir)
        if Path(lp).exists():
            shutil.rmtree(lp, ignore_errors=True)
        Path(lp).mkdir(parents=True, exist_ok=True)

        # ★ 2026-08-06：长文档路由决策
        # 提前在重试循环外决定本次调用是否走 vlm-engine，
        # 避免每次重试都重新检测页数。
        # 返回值都使用临时变量，不动 self.backend/effort（保持原始状态给下次调用）
        long_backend, long_effort, _pages = self._resolve_long_doc_routing(file_path)
        if long_backend:
            chosen_backend = long_backend
            chosen_effort = long_effort
        else:
            chosen_backend = self.backend
            chosen_effort = self.effort

        tracker.update(file_path.name, 10, f"使用 {chosen_backend} 引擎", "parsing")

        attempts = 0
        last_err: Optional[MinerUError] = None

        for attempt in range(1, self.max_retries + 1):
            attempts = attempt
            try:
                tracker.update(
                    file_path.name,
                    20 + (attempt - 1) * 15,
                    f"调用 MinerU API（第 {attempt} 次）...",
                    "parsing",
                )
                t0 = time.perf_counter()
                response = self._post(file_path, backend=chosen_backend, effort=chosen_effort)
                dt = int((time.perf_counter() - t0) * 1000)
                log.info(
                    "mineru api ok",
                    extra={
                        "step": "mineru",
                        "status": "ok",
                        "file_name": file_path.name,
                        "duration_ms": dt,
                        "attempts": attempt,
                        "backend": chosen_backend,
                        "effort": chosen_effort,
                    },
                )
                tracker.update(file_path.name, 70, "解压产物...", "parsing")
                result = self._write_outputs(response, parsed_dir, attempts=attempts)
                tracker.update(file_path.name, 100, "解析完成", "done")
                return result
            except _RetryableMinerUError as e:
                last_err = MinerUError(str(e), attempts=attempt)
                # ★ 2026-08-06：重试等待走新公式（initial * factor^(attempt-1)），
                # 不是以前的 backoff ** (attempt-1)。默认配置 30/60/120s
                # 足以覆盖 mineru-router / supervisor 重启服务的耗时。
                wait = self._compute_retry_wait(attempt)
                log.warning(
                    "mineru api 失败，准备重试",
                    extra={
                        "step": "mineru",
                        "status": "retry",
                        "file_name": file_path.name,
                        "attempts": attempt,
                        "max_retries": self.max_retries,
                        "wait_s": wait,
                        "backend": chosen_backend,
                        "error_msg": str(e),
                    },
                )
                if attempt < self.max_retries:
                    tracker.update(
                        file_path.name,
                        20 + attempt * 15,
                        f"第 {attempt} 次失败，{wait}s 后重试...",
                        "parsing",
                    )
                    time.sleep(wait)
            except _FatalMinerUError as e:
                # 4xx：清掉空目录，避免污染 parsed/
                self._cleanup_on_failure(parsed_dir)
                tracker.update(file_path.name, 0, f"解析失败：{e}", "failed")
                raise MinerUError(
                    str(e), attempts=attempt, status_code=e.status_code, body=e.body
                ) from e

        # 重试耗尽：清掉空目录
        self._cleanup_on_failure(parsed_dir)
        assert last_err is not None
        tracker.update(
            file_path.name,
            0,
            f"解析失败（重试 {attempts} 次）：{last_err}",
            "failed",
        )
        raise last_err

    @staticmethod
    def _cleanup_on_failure(parsed_dir: Path) -> None:
        """解析失败时，清理空目录（避免在 parsed/ 留下一堆空文件夹）。"""
        try:
            if parsed_dir.exists() and not any(parsed_dir.rglob("*")):
                parsed_dir.rmdir()
        except OSError:
            # 不影响主流程
            pass

    # -------------------- internal --------------------

    def _post(
        self,
        file_path: Path,
        *,
        backend: Optional[str] = None,
        effort: Optional[str] = None,
    ) -> httpx.Response:
        """multipart/form-data POST /file_parse。

        ★ 2026-08-06：接受可选的 backend / effort 参数。默认走 self.backend / self.effort。
        parse_file 在长文档路由时会传不同的 backend（vlm-engine）进来，
        避免修改 self.backend 影响其他请求。
        """
        # 请求头：仅在有 token 时加 auth
        # 注意：Content-Type 由 httpx 自动加 boundary，不能手设
        headers: Dict[str, str] = {}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"

        # form fields
        # 注意：lang_list 是 list，必须以 dict-of-list 形式发送，httpx 会展开为
        # multipart 重复字段（lang_list=ch&lang_list=en），FastAPI 才能正确解析。
        # 注意：return_* 默认是 false，**必须显式设为 true** 才会输出对应产物。
        # 不传 return_images 时 MinerU 就只返 md，不返 json/images（这是常见的"只拿到 md"原因）。
        use_backend = backend if backend is not None else self.backend
        use_effort = effort if effort is not None else self.effort
        data: Dict[str, Any] = {
            "backend": use_backend,
            # ★ 高质量 hybrid-engine 才接受 effort（vlm-engine / pipeline 忽略此字段）
            #   - high  ：极致精度 + image analysis（推荐，效果最好）
            #   - medium：默认，速度更快，但不支持 image analysis
            "effort": use_effort,
            "lang_list": list(self.lang_list),  # ★ 关键：传 list，不要 json.dumps
            "formula_enable": str(self.formula_enable).lower(),
            "table_enable": str(self.table_enable).lower(),
            "response_format_zip": str(self.response_format_zip).lower(),
            # 5 个产物开关：默认全开
            "return_md": str(self.return_md).lower(),
            "return_middle_json": str(self.return_middle_json).lower(),
            "return_model_output": str(self.return_model_output).lower(),
            "return_content_list": str(self.return_content_list).lower(),
            "return_images": str(self.return_images).lower(),
        }

        try:
            with open(file_path, "rb") as f:
                files = {"files": (file_path.name, f, self._guess_mime(file_path))}
                with httpx.Client(timeout=self.timeout) as client:
                    return client.post(
                        self._endpoint, files=files, data=data, headers=headers
                    )
        except httpx.TimeoutException as e:
            raise _RetryableMinerUError(f"timeout after {self.timeout}s: {e}") from e
        except httpx.HTTPError as e:
            raise _RetryableMinerUError(f"http error: {e}") from e

    @staticmethod
    def _guess_mime(p: Path) -> str:
        ext = p.suffix.lower()
        return {
            ".pdf": "application/pdf",
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".tiff": "image/tiff",
            ".tif": "image/tiff",
            ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            ".doc": "application/msword",
            ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
            ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        }.get(ext, "application/octet-stream")

    def _write_outputs(
        self, response: httpx.Response, parsed_dir: Path, *, attempts: int
    ) -> ParseResult:
        status = response.status_code
        ctype = (response.headers.get("content-type") or "").lower()

        if 200 <= status < 300 and "zip" in ctype:
            return self._write_from_zip(response, parsed_dir, attempts=attempts)
        if 200 <= status < 300 and "json" in ctype:
            return self._write_from_json(response, parsed_dir, attempts=attempts)
        if 200 <= status < 300:
            # 兜底：先按 zip，再按 json 试
            try:
                return self._write_from_zip(response, parsed_dir, attempts=attempts)
            except Exception:  # noqa: BLE001
                return self._write_from_json(response, parsed_dir, attempts=attempts)

        body_snippet = (response.text or "")[:500]
        if 400 <= status < 500:
            raise _FatalMinerUError(
                f"mineru 4xx: {status} {response.reason_phrase}",
                status_code=status,
                body=body_snippet,
            )
        raise _RetryableMinerUError(f"mineru 5xx: {status} {response.reason_phrase}")

    # ---------- 形态 A：ZIP 响应（推荐，response_format_zip=true）----------

    def _write_from_zip(
        self, response: httpx.Response, parsed_dir: Path, *, attempts: int
    ) -> ParseResult:
        try:
            buf = io.BytesIO(response.content)
            with zipfile.ZipFile(buf) as zf:
                # ★ 2026-08-07：手动解压，避免 extractall 在 Windows 上因路径非法字符失败
                # 背景：MinerU 返回的 ZIP 内部路径可能包含中文括号、过长文件名等，
                # 直接 extractall 会报 [Errno 2] No such file or directory。
                # 解决：逐个条目解压，用安全路径替换。
                for info in zf.infolist():
                    # 跳过目录条目
                    if info.filename.endswith('/'):
                        continue
                    
                    # 构造目标路径
                    # 去掉 ZIP 内可能存在的顶层目录（如 "{stem}_text/"），直接放到 parsed_dir
                    parts = Path(info.filename).parts
                    # 如果路径有 2 层以上，去掉第 1 层（顶层目录）
                    if len(parts) > 1:
                        safe_name = Path(*parts[1:])
                    else:
                        safe_name = Path(*parts)
                    
                    target_path = parsed_dir / safe_name
                    # ★ 2026-08-12：mkdir 也用 _long_path 绕过 Windows MAX_PATH
                    Path(_long_path(target_path.parent)).mkdir(parents=True, exist_ok=True)
                    
                    # 解压文件（★ 2026-08-12 修复：用 Path.open() 代替内置 open()
                    # 内置 open() 在 Windows 上不能正确处理 \\?\ 长路径前缀，
                    # 而 pathlib.Path.open() 内部使用 CreateFileW 可正确处理。）
                    with zf.open(info) as src, Path(_long_path(target_path)).open('wb') as dst:
                        dst.write(src.read())
        except zipfile.BadZipFile as e:
            raise _RetryableMinerUError(f"响应不是合法 ZIP: {e}") from e
        except Exception as e:  # noqa: BLE001
            raise _RetryableMinerUError(f"无法解压 mineru zip 响应: {e}") from e

        return self._collect_outputs(parsed_dir, attempts=attempts, kind="zip")

    # ---------- 形态 B：JSON 响应（response_format_zip=false）----------

    def _write_from_json(
        self, response: httpx.Response, parsed_dir: Path, *, attempts: int
    ) -> ParseResult:
        try:
            data = response.json()
        except Exception as e:  # noqa: BLE001
            raise _RetryableMinerUError(f"响应不是合法 JSON: {e}") from e

        if not isinstance(data, dict):
            raise _RetryableMinerUError("mineru 响应不是 JSON object")

        results = data.get("results") or {}
        if not isinstance(results, dict) or not results:
            raise _RetryableMinerUError("mineru JSON 响应缺少 results 字段")

        # 找到与上传文件匹配的 stem
        # results 的 key 是上传文件去后缀名（stem）
        # 我们只有一个文件，所以直接用第一个值
        stem, payload = next(iter(results.items()))
        if not isinstance(payload, dict):
            raise _RetryableMinerUError(f"results[{stem}] 不是 dict")

        # 写 .md
        md_text = payload.get("md_content") or ""
        md_path = parsed_dir / f"{stem}.md"
        md_path.write_text(str(md_text), encoding="utf-8")

        # 写 .json（如果 middle_json 存在就写，否则用 content_list 包一层）
        json_payload: Dict[str, Any] = {}
        if "middle_json" in payload and payload["middle_json"]:
            json_payload["middle_json"] = payload["middle_json"]
        if "model_output" in payload and payload["model_output"]:
            json_payload["model_output"] = payload["model_output"]
        if "content_list" in payload and payload["content_list"]:
            json_payload["content_list"] = payload["content_list"]
        if not json_payload:
            json_payload = {"raw": payload}
        json_path = parsed_dir / f"{stem}.json"
        json_path.write_text(
            json.dumps(json_payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        # 写 images
        image_paths: List[Path] = []
        images = payload.get("images") or {}
        if isinstance(images, dict) and images:
            images_dir = parsed_dir / "images"
            images_dir.mkdir(exist_ok=True)
            for name, b64 in images.items():
                p = self._decode_b64(b64, images_dir / name)
                if p is not None:
                    image_paths.append(p)

        return ParseResult(
            parse_dir=parsed_dir,
            md_path=md_path,
            json_path=json_path,
            images=image_paths,
            other_files=[],
            attempts=attempts,
            response_kind="json",
        )

    @staticmethod
    def _decode_b64(b64: str, target: Path) -> Optional[Path]:
        import base64

        try:
            raw = base64.b64decode(b64, validate=False)
        except Exception:  # noqa: BLE001
            return None
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
        return target

    # ---------- 收尾：清点产物 ----------

    def _collect_outputs(
        self, parsed_dir: Path, *, attempts: int, kind: str
    ) -> ParseResult:
        """在已落盘的目录里找 .md / .json / images / 其它文件。"""
        md_files = sorted(parsed_dir.rglob("*.md"))
        json_files = sorted(parsed_dir.rglob("*.json"))
        image_files = (
            sorted(parsed_dir.rglob("*.png"))
            + sorted(parsed_dir.rglob("*.jpg"))
            + sorted(parsed_dir.rglob("*.jpeg"))
            + sorted(parsed_dir.rglob("*.webp"))
        )
        # 其它文件
        all_files = {p for p in parsed_dir.rglob("*") if p.is_file()}
        others = sorted(all_files - set(md_files) - set(json_files) - set(image_files))

        return ParseResult(
            parse_dir=parsed_dir,
            md_path=md_files[0] if md_files else None,
            json_path=json_files[0] if json_files else None,
            images=image_files,
            other_files=others,
            attempts=attempts,
            response_kind=kind,
        )


# 内部异常：4xx 不重试，5xx/网络重试
class _RetryableMinerUError(Exception):
    pass


class _FatalMinerUError(Exception):
    def __init__(self, message: str, *, status_code: int, body: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.body = body
