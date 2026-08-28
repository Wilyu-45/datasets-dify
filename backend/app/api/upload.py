"""plan.md §3 单文件上传 + 全流程入库 API（新增于 2026-08-04）。
★ 2026-08 扩展为批量上传（§3 改造）：去掉预登记流程依赖，
        用户在 web 一次性选多个文件上传，每个文件独立跑 parse → chunk → dify 全流程，
        1 个失败不影响其他。

业务背景：
  用户需要测试一个文件能否顺利入库，无需先把文件信息写进 manifest。
  之前流程：input/ 放文件 → manifest 加行 → scan → parse → chunk → dify。
  痛点：测试一个文件要手动改 manifest 记录，繁琐。

新流程（单文件上传 + 一键全流程）：
  1. 前端选文件 → POST /api/upload/single (multipart/form-data)
  2. 后端把文件保存到 data/single_uploads/{stem}/source.{ext}
  3. 在 manifest 表插入一行（filename 字段 = 原文件名，import_status="已上传"）
  4. 把文件从 single_uploads/ 移到 input/ 等待 scan 自动处理
  5. 同步触发一次 scan + parse + chunk + dify 流水线（仅限该文件）
  6. 返回 PipelineReport，前端展示各阶段结果

批量上传（2026-08 新增）：
  1. 前端选多个文件 → POST /api/upload/batch (multipart/form-data)
  2. 后端逐个保存到 single_uploads/{stem}/source.{ext}，每个文件 try/except 包裹
  3. 全部加 manifest 行（同名 / 同 md5 走去重逻辑）
  4. 全部移到 pending/
  5. 一次性触发 parse + chunk + dify 流水线，target_stems=[s1, s2, ...]，
     避免对每个文件跑一次完整 pipeline（性能 & 写盘原子性更好）
  6. 返回 BatchUploadResponse：每个文件的 per-file summary + 整批 PipelineReport

为什么用 single_uploads/ 而不是直接放 input/：
  - single_uploads/ 是临时中转区，便于清理（测试完后用户可手动删）
  - input/ 是用户管理自己文件的目录，混入会污染
  - manifest 自动添加行后用户也能在清单里看到本次测试记录
"""

from __future__ import annotations

import hashlib
import logging
import shutil
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from pydantic import BaseModel

from app.config import settings
from app.models.schemas import ManifestRow
from app.services import (
    chunker,
    config_run_log,
    config_store,
    dify_ingest,
    manifest_store,
    parser,
    scanner,
)
from app.services import hasher

router = APIRouter(tags=["upload"])
log = logging.getLogger("ragsystem.api.upload")


# 单文件上传中转目录
SINGLE_UPLOADS_DIRNAME = "single_uploads"


class SingleUploadResponse(BaseModel):
    """单文件上传 + 一键入库的响应。"""

    filename: str                          # 用户上传的原始文件名
    stem: str                              # 文件 stem（去扩展名）
    md5: str                               # 文件 MD5
    size: int                              # 文件大小（字节）
    saved_path: str                        # 文件最终落地路径
    manifest_row_added: bool               # 是否成功添加到 manifest
    pipeline: Optional[Dict[str, Any]] = None  # 全流程结果（4 阶段 Report）
    error: Optional[str] = None            # 整体错误信息


class BatchUploadResponse(BaseModel):
    """批量文件上传 + 一键入库的响应（2026-08 新增）。

    设计要点：
    - items 复用 SingleUploadResponse 结构，前端按列表渲染每个文件的结果
    - pipeline 字段是整批文件的聚合 PipelineReport（一次 run_pipeline 调用），
      各 items[i].pipeline 字段是从中按 filename 过滤出来的 per-file summary
    - 1 个文件保存/移动失败不影响其他文件：失败项填 error，不计入 succeeded
    """

    total: int                             # 本批接收的文件总数
    succeeded: int                         # 成功保存到 pending/ 的文件数
    failed: int                            # 失败的文件数（保存或移动出错）
    duration_ms: int                       # 整批处理总耗时（毫秒）
    items: List[SingleUploadResponse]      # 每个文件的结果
    pipeline: Optional[Dict[str, Any]] = None  # 整批流水线 Report（auto_ingest=True 时才有）


def _single_uploads_dir() -> Path:
    """单文件上传中转目录（data/single_uploads/）。"""
    p = settings.data_root / SINGLE_UPLOADS_DIRNAME
    p.mkdir(parents=True, exist_ok=True)
    return p


def _safe_stem(name: str) -> str:
    """从上传文件名提取 stem，清理路径分隔符与不安全字符。

    保留中文（业务文档名常含中文）。仅剔除 Windows 禁止的字符。
    """
    stem = Path(name).stem
    # 清理 Windows 禁止字符 < > : " / \ | ? *
    for ch in '<>:"/\\|?*':
        stem = stem.replace(ch, "_")
    # 清理前后空白
    stem = stem.strip().strip(".")
    if not stem:
        stem = f"uploaded_{uuid.uuid4().hex[:8]}"
    return stem


def _now_iso() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _build_pipeline_request(target_stem: str) -> Any:
    """构造一个针对单文件的 PipelineRequest（仅跑 parse + chunk + dify）。

    ★ 2026-08 单文件上传 + 一键入库：
        - scan 阶段：enabled=False（不执行扫描流程）
        - parse/chunk/dify：enabled=True，但通过 target_stems 白名单只处理这一个文件
          —— 这样 manifest 里其他待处理的文档不会被处理（那些要走完整清单流程）
    """
    from app.services.pipeline import PipelineRequest, PipelineStep

    return PipelineRequest(
        scan=PipelineStep(enabled=False, dry_run=False, force=False),
        parse=PipelineStep(enabled=True, dry_run=False, force=False),
        chunk=PipelineStep(enabled=True, dry_run=False, force=False),
        dify=PipelineStep(enabled=True, dry_run=False, force=False),
        stop_on_error=False,
        target_stems=[target_stem],  # ★ 只处理这个 stem 对应的文件
    )


def _resolve_run_config(profile_id: Optional[str]) -> Dict[str, Any]:
    """解析上传处理要使用的配置方案。

    显式 profile_id > 当前激活方案。
    显式指定但不存在 → 404；两者都没有 → 400（提示先去配置中心配置）。
    """
    if profile_id:
        profile = config_store.get_profile(profile_id)
        if not profile:
            raise HTTPException(status_code=404, detail=f"配置方案不存在：{profile_id}")
        return profile
    profile = config_store.get_active_profile()
    if not profile:
        raise HTTPException(
            status_code=400,
            detail="尚未配置任何配置方案：请先到「配置中心」配置知识库 ID 与切分策略，并选择一个方案激活",
        )
    return profile


def _run_single_file_pipeline(
    target_stem: str,
    profile: Optional[Dict[str, Any]] = None,
    source: str = config_run_log.SOURCE_UPLOAD_SINGLE,
) -> Dict[str, Any]:
    """对单文件运行 parse → chunk → dify 流水线（只处理这一个文件）。

    Args:
        target_stem: 文件 stem（不含扩展名），例如 "report_2024"
        profile: 配置方案（含 config 字段）。传了则在 pipeline 执行期间临时应用其配置。
        source: 处理配置记录的来源标识（写入 process_config_log 表）。

    运行结束后把当时生效的配置快照 + 结果状态写入 process_config_log 表。
    """
    from app.services.pipeline import run_pipeline

    req = _build_pipeline_request(target_stem)
    t0 = time.perf_counter()
    with config_store.apply_config(profile["config"] if profile else None):
        try:
            report = run_pipeline(req)
            d = report.to_dict()
            config_run_log.record_run(
                source=source,
                profile=profile,
                target_stems=[target_stem],
                status=d.get("status"),
                error=d.get("error"),
                duration_ms=d.get("duration_ms"),
            )
            return d
        except Exception as e:  # noqa: BLE001
            config_run_log.record_run(
                source=source,
                profile=profile,
                target_stems=[target_stem],
                status="error",
                error=str(e),
                duration_ms=int((time.perf_counter() - t0) * 1000),
            )
            raise


def _build_batch_pipeline_request(target_stems: List[str]) -> Any:
    """构造一个针对多个 stem 的 PipelineRequest（只跑 parse + chunk + dify）。"""
    from app.services.pipeline import PipelineRequest, PipelineStep

    return PipelineRequest(
        scan=PipelineStep(enabled=False, dry_run=False, force=False),
        parse=PipelineStep(enabled=True, dry_run=False, force=False),
        chunk=PipelineStep(enabled=True, dry_run=False, force=False),
        dify=PipelineStep(enabled=True, dry_run=False, force=False),
        stop_on_error=False,
        target_stems=target_stems,  # ★ 一次只处理这批 stem
    )


def _run_batch_pipeline(
    target_stems: List[str],
    profile: Optional[Dict[str, Any]] = None,
    source: str = config_run_log.SOURCE_UPLOAD_BATCH,
) -> Dict[str, Any]:
    """对一批文件运行 parse → chunk → dify 流水线（只处理这批文件）。

    ★ 2026-08 批量上传优化：与单文件版共用 run_pipeline，
    但用 target_stems=[s1, s2, ...] 一次性传所有 stem，避免对每个文件跑一次完整 pipeline。
    若传了 profile 配置方案，则在 pipeline 执行期间临时应用其配置。
    运行结束后把当时生效的配置快照 + 结果状态写入 process_config_log 表。
    """
    from app.services.pipeline import run_pipeline

    req = _build_batch_pipeline_request(target_stems)
    t0 = time.perf_counter()
    with config_store.apply_config(profile["config"] if profile else None):
        try:
            report = run_pipeline(req)
            d = report.to_dict()
            config_run_log.record_run(
                source=source,
                profile=profile,
                target_stems=list(target_stems),
                status=d.get("status"),
                error=d.get("error"),
                duration_ms=d.get("duration_ms"),
            )
            return d
        except Exception as e:  # noqa: BLE001
            config_run_log.record_run(
                source=source,
                profile=profile,
                target_stems=list(target_stems),
                status="error",
                error=str(e),
                duration_ms=int((time.perf_counter() - t0) * 1000),
            )
            raise


def _split_pipeline_report_by_stem(
    report: Dict[str, Any], target_stems: List[str]
) -> Dict[str, Dict[str, Any]]:
    """把整批 PipelineReport 按 stem 拆分出 per-file summary。

    返回 {stem: {parse, chunk, dify, status, error}}，方便前端按文件展示结果。
    任何 stem 找不到对应记录的，per-file summary 仍返回但各阶段为空。
    """
    stem_set = set(target_stems)
    # 初始化空 summary
    per_file: Dict[str, Dict[str, Any]] = {
        s: {
            "parse": None,
            "chunk": None,
            "dify": None,
            "status": "ok",
            "error": None,
        }
        for s in target_stems
    }

    def _update_with_actions(stage: str, actions: list, key: str) -> None:
        if not actions:
            return
        for a in actions:
            # parse/chunk 用 filename；dify 用 stem
            name = a.get(key) if isinstance(a, dict) else getattr(a, key, None)
            if not name:
                continue
            # filename 可能是 "report.pdf"，需要 stem 比对
            from pathlib import Path as _P
            stem = _P(str(name)).stem
            if stem not in stem_set:
                continue
            cur = per_file[stem]
            # 取该 stem 的第一条记录（多次解析会以最后一次为准，但单文件单次跑不会）
            if cur.get(stage) is None:
                cur[stage] = a if isinstance(a, dict) else (
                    a.model_dump() if hasattr(a, "model_dump") else a
                )
            # 失败标记
            action_value = a.get("action") if isinstance(a, dict) else getattr(a, "action", None)
            if action_value and "fail" in str(action_value).lower():
                err = a.get("error") if isinstance(a, dict) else getattr(a, "error", None)
                if err:
                    cur["error"] = f"{stage} 失败: {err}"
                    cur["status"] = "partial"

    parse_report = report.get("parse") or {}
    chunk_report = report.get("chunk") or {}
    dify_report = report.get("dify") or {}
    _update_with_actions("parse", parse_report.get("actions") or [], "filename")
    _update_with_actions("chunk", chunk_report.get("actions") or [], "filename")
    _update_with_actions("dify", dify_report.get("actions") or [], "stem")

    return per_file


async def _save_and_stage_upload(file: UploadFile) -> Dict[str, Any]:
    """把单个上传文件保存到 single_uploads/、加 manifest、移到 pending/。

    抽出来供单文件/批量端点复用。返回 dict 包含：
        - ok: bool
        - error: Optional[str]
        - filename / stem / md5 / size / saved_path / manifest_row_added
    调用方根据 ok 字段决定后续处理（成功才入 pipeline 列表）。
    """
    if not file.filename:
        return {"ok": False, "error": "未提供文件名", "filename": ""}

    raw_name = file.filename
    original_path = Path(raw_name)
    ext = original_path.suffix.lower()
    if ext not in settings.allowed_extensions:
        return {
            "ok": False,
            "error": f"不支持的扩展名 {ext!r}，允许：{', '.join(settings.allowed_extensions)}",
            "filename": raw_name,
        }
    stem = _safe_stem(raw_name)

    # 1) 保存到 single_uploads/{stem}/{stem}{ext}
    upload_dir = _single_uploads_dir() / stem
    upload_dir.mkdir(parents=True, exist_ok=True)
    saved_filename = f"{stem}{ext}"
    saved_path = upload_dir / saved_filename

    total = 0
    md5 = hashlib.md5()
    try:
        with saved_path.open("wb") as fp:
            while chunk := await file.read(1024 * 1024):  # 1 MB
                fp.write(chunk)
                md5.update(chunk)
                total += len(chunk)
    except Exception as e:  # noqa: BLE001
        log.exception(
            "save_and_stage: 写盘失败 filename=%s",
            saved_filename,
            extra={"step": "upload", "status": "write_error", "file_name": saved_filename},
        )
        shutil.rmtree(upload_dir, ignore_errors=True)
        return {"ok": False, "error": f"写盘失败: {e}", "filename": saved_filename}
    md5_hex = md5.hexdigest()
    log.info(
        "save_and_stage: saved %s (%d bytes, md5=%s)",
        saved_path, total, md5_hex,
        extra={"step": "upload", "status": "saved", "file_name": saved_filename, "size": total},
    )

    # 2) ★ 2026-08 修复（manifest 状态被重置为 new）：
    #   之前在 upsert 之前没做 dedup 检查，导致同名/同 md5 的"重传"会把已有
    #   status='done' 行覆盖成 status='new'，把已入库的文档状态搞坏。
    #   这里先检查 pending/ 下是否已有同 md5 文件，有就跳过整次 upsert。
    settings.pending_dir.mkdir(parents=True, exist_ok=True)
    pending_dst = settings.pending_dir / saved_filename
    if pending_dst.exists() and hasher.md5_of_file(pending_dst, settings.scan_chunk_size) == md5_hex:
        # 同 md5 已存在 pending/ → 视为完全重复，不动 manifest，删 single_uploads/ 即可
        log.info(
            "save_and_stage: pending/ 已有同 md5 文件，整体跳过（保留原 manifest 状态）: %s",
            pending_dst,
            extra={"step": "upload", "status": "skipped_duplicate_fully"},
        )
        saved_path.unlink(missing_ok=True)
        shutil.rmtree(upload_dir, ignore_errors=True)
        return {
            "ok": True,
            "filename": saved_filename,
            "stem": stem,
            "md5": md5_hex,
            "size": total,
            "saved_path": str(pending_dst),
            "manifest_row_added": False,  # 没动 manifest
            "duplicate": True,  # ★ 标记：本次是重传，没新建 manifest 行
            "error": None,
        }

    # 3) 加 manifest 行（新建一行；status 保留 new 由后续 pipeline 推进）
    manifest_store.bootstrap(settings.data_root)
    now = _now_iso()
    new_row = ManifestRow(
        filename=saved_filename,
        status="new",
        md5=md5_hex,
        create_time=now,
        update_time=now,
        import_status="已上传",
        process_status="待扫描",
        process_note=f"上传，大小 {total} 字节",
    )
    try:
        manifest_store.upsert(new_row)
        log.info(
            "save_and_stage: manifest row added for %s",
            saved_filename,
            extra={"step": "upload", "status": "manifest_added", "file_name": saved_filename},
        )
    except Exception as e:  # noqa: BLE001
        log.exception(
            "save_and_stage: manifest 写盘失败",
            extra={"step": "upload", "status": "manifest_error", "file_name": saved_filename, "error_msg": str(e)},
        )
        shutil.rmtree(upload_dir, ignore_errors=True)
        return {"ok": False, "error": f"manifest 写盘失败: {e}", "filename": saved_filename}

    # 4) 移动到 pending/（md5 不一致时改名）
    try:
        if pending_dst.exists():
            # md5 不一致（前面已过滤同 md5）：加 _<6hex> 后缀
            import hashlib as _h
            h6 = _h.md5(str(time.time_ns()).encode()).hexdigest()[:6]
            pending_dst = pending_dst.with_name(
                f"{pending_dst.stem}_{h6}{pending_dst.suffix}"
            )
            shutil.move(str(saved_path), str(pending_dst))
            shutil.rmtree(upload_dir, ignore_errors=True)
            log.info(
                "save_and_stage: 重命名后移入 pending/: %s",
                pending_dst,
                extra={"step": "upload", "status": "renamed", "file_name": pending_dst.name},
            )
        else:
            shutil.move(str(saved_path), str(pending_dst))
            shutil.rmtree(upload_dir, ignore_errors=True)
    except Exception as e:  # noqa: BLE001
        log.exception(
            "save_and_stage: 移入 pending/ 失败",
            extra={"step": "upload", "status": "move_error", "error_msg": str(e)},
        )
        shutil.rmtree(upload_dir, ignore_errors=True)
        return {"ok": False, "error": f"移入 pending/ 失败: {e}", "filename": saved_filename}

    # 4) 更新 manifest 的 import_status 为"已移入待处理"
    updated_row = manifest_store.load().get(saved_filename)
    if updated_row:
        updated_row = updated_row.model_copy(update={
            "import_status": "已移入待处理",
            "process_status": "已移入待处理",
            "update_time": _now_iso(),
        })
        manifest_store.upsert(updated_row)

    return {
        "ok": True,
        "filename": saved_filename,
        "stem": stem,
        "md5": md5_hex,
        "size": total,
        "saved_path": str(pending_dst),
        "manifest_row_added": True,
        "error": None,
    }


@router.post("/upload/single", response_model=SingleUploadResponse)
async def post_upload_single(
    file: UploadFile = File(..., description="待入库的单个文件（PDF / DOCX / DOC / PPTX / XLSX / HTML）"),
    auto_ingest: bool = Form(
        True,
        description="上传后是否自动触发 parse + chunk + dify 全流程入库",
    ),
    profile_id: str = Form(
        "",
        description="配置方案 ID；为空则使用当前激活配置方案（必选，未配置则拒绝处理）",
    ),
) -> SingleUploadResponse:
    """单文件上传（multipart/form-data）。

    流程：
        1. 保存文件到 data/single_uploads/{stem}/{stem}{ext}
        2. 算 MD5
        3. 在 manifest 表插入一行（import_status="已上传"）
        4. 把文件移动到 pending/ 等待 parse 直接读取
        5. （可选）触发全流程：parse → chunk → dify（仅限该文件）
        6. 返回结果

    业务约束：
        - 文件名带扩展名（如 .pdf / .docx）
        - 文件大小无硬上限（multipart 限制由 uvicorn/tomorrow 控制，默认足够）
        - 文件名重复（已上传过）会覆盖 manifest 的 import_status，并允许再次入库

    ★ 2026-08 重构：保存/加 manifest/移 pending 的逻辑抽到 _save_and_stage_upload，
       与批量上传共用，单文件端点只负责"一个文件"语义 + 触发全流程。
    """
    result = await _save_and_stage_upload(file)
    if not result["ok"]:
        raise HTTPException(status_code=400, detail=result["error"])

    stem = result["stem"]
    # 触发全流程（可选）
    # ★ 2026-08：只处理这个文件（target_stems=[stem]），不处理 manifest 里其他文件
    pipeline_report: Optional[Dict[str, Any]] = None
    error_text: Optional[str] = None
    if auto_ingest:
        # ★ 2026-08 配置中心：处理前必须已配置好方案（显式 profile_id > 激活方案）
        profile = _resolve_run_config(profile_id or None)
        try:
            pipeline_report = _run_single_file_pipeline(stem, profile)
            log.info(
                "single upload: pipeline done, status=%s",
                pipeline_report.get("status"),
                extra={"step": "upload", "status": "pipeline_done", "file_name": result["filename"]},
            )
        except Exception as e:  # noqa: BLE001
            log.exception(
                "single upload: pipeline 异常",
                extra={"step": "upload", "status": "pipeline_error", "error_msg": str(e)},
            )
            error_text = f"pipeline 异常: {e}"

    return SingleUploadResponse(
        filename=result["filename"],
        stem=stem,
        md5=result["md5"],
        size=result["size"],
        saved_path=result["saved_path"],
        manifest_row_added=result["manifest_row_added"],
        pipeline=pipeline_report,
        error=error_text,
    )


# 单文件端点最多接收 600 个文件（与前端 BatchFileUpload.maxCount 对齐）
_MAX_BATCH_UPLOAD = 600


@router.post("/upload/batch", response_model=BatchUploadResponse)
async def post_upload_batch(
    files: List[UploadFile] = File(
        ..., description="待入库的多个文件（PDF / DOCX / DOC / PPTX / XLSX / HTML）"
    ),
    auto_ingest: bool = Form(
        True,
        description="上传后是否自动触发 parse + chunk + dify 全流程入库（一次性跑所有文件）",
    ),
    profile_id: str = Form(
        "",
        description="配置方案 ID；为空则使用当前激活配置方案（必选，未配置则拒绝处理）",
    ),
) -> BatchUploadResponse:
    """批量文件上传（multipart/form-data，2026-08 新增）。

    流程：
        1. 逐个保存到 single_uploads/{stem}/{stem}{ext}，每个文件 try/except 包裹
           —— 1 个文件保存/移动失败不影响其他文件
        2. 全部加 manifest 行（同 md5 走去重 / 重命名逻辑）
        3. 全部移到 pending/
        4. 收集所有成功保存的 stem 列表，一次性触发 parse + chunk + dify 流水线
           —— 用 target_stems=[s1, s2, ...] 一次跑所有文件，避免对每个文件跑一次完整 pipeline
        5. 把整批 PipelineReport 按 stem 拆分成 per-file summary，填入 items[i].pipeline
        6. 返回 BatchUploadResponse：total / succeeded / failed / items / pipeline

    业务约束：
        - 单批最多 50 个文件（前端 maxCount 对齐）
        - 文件名带扩展名（如 .pdf / .docx）
        - 至少要 1 个文件，0 个文件返回 400
        - 1 个文件失败 → 该文件 items[i].error 填具体原因，其他文件继续处理
        - auto_ingest=False 时只保存到 pending/，不触发全流程
    """
    t0 = time.perf_counter()

    if not files:
        raise HTTPException(status_code=400, detail="未提供任何文件")

    if len(files) > _MAX_BATCH_UPLOAD:
        raise HTTPException(
            status_code=400,
            detail=f"单批最多 {_MAX_BATCH_UPLOAD} 个文件，本次提交 {len(files)} 个",
        )

    log.info(
        "api /upload/batch called: %d files, auto_ingest=%s",
        len(files), auto_ingest,
        extra={"step": "api", "status": "batch_start", "file_count": len(files)},
    )

    # 1) 逐个保存（每个文件 try/except 包裹，互不影响）
    items: List[SingleUploadResponse] = []
    successful_stems: List[str] = []
    succeeded = 0
    failed = 0

    for idx, f in enumerate(files):
        try:
            result = await _save_and_stage_upload(f)
            if not result["ok"]:
                log.warning(
                    "batch upload: file %d/%d failed: %s err=%s",
                    idx + 1, len(files), result.get("filename"), result["error"],
                    extra={"step": "upload", "status": "batch_item_failed"},
                )
                items.append(SingleUploadResponse(
                    filename=result.get("filename") or (f.filename or ""),
                    stem="",
                    md5="",
                    size=0,
                    saved_path="",
                    manifest_row_added=False,
                    pipeline=None,
                    error=result["error"],
                ))
                failed += 1
            else:
                items.append(SingleUploadResponse(
                    filename=result["filename"],
                    stem=result["stem"],
                    md5=result["md5"],
                    size=result["size"],
                    saved_path=result["saved_path"],
                    manifest_row_added=result["manifest_row_added"],
                    pipeline=None,
                    error=None,
                ))
                successful_stems.append(result["stem"])
                succeeded += 1
        except Exception as e:  # noqa: BLE001
            # 单文件未知异常：不让整批崩，记 error 继续
            log.exception(
                "batch upload: file %d/%d unexpected error",
                idx + 1, len(files),
                extra={"step": "upload", "status": "batch_item_unexpected_error"},
            )
            items.append(SingleUploadResponse(
                filename=(f.filename if f else f"file_{idx}"),
                stem="",
                md5="",
                size=0,
                saved_path="",
                manifest_row_added=False,
                pipeline=None,
                error=f"未预期错误: {e}",
            ))
            failed += 1

    # 2) 一次性触发全流程（仅对成功保存的 stem）
    pipeline_report: Optional[Dict[str, Any]] = None
    if auto_ingest and successful_stems:
        # ★ 2026-08 配置中心：处理前必须已配置好方案（显式 profile_id > 激活方案）
        profile = _resolve_run_config(profile_id or None)
        try:
            pipeline_report = _run_batch_pipeline(successful_stems, profile)
            log.info(
                "batch upload: pipeline done, status=%s stems=%d",
                pipeline_report.get("status"), len(successful_stems),
                extra={"step": "upload", "status": "batch_pipeline_done"},
            )
            # 3) 把整批报告按 stem 拆分到每个 item
            per_file = _split_pipeline_report_by_stem(pipeline_report, successful_stems)
            for item in items:
                if item.error or not item.stem:
                    continue
                summary = per_file.get(item.stem)
                if summary:
                    item.pipeline = {
                        "status": summary["status"],
                        "parse": summary["parse"],
                        "chunk": summary["chunk"],
                        "dify": summary["dify"],
                        "error": summary["error"],
                    }
                    # 任一阶段失败 → 整文件 partial
                    if summary["error"]:
                        item.error = summary["error"]
        except Exception as e:  # noqa: BLE001
            log.exception(
                "batch upload: pipeline 异常",
                extra={"step": "upload", "status": "batch_pipeline_error", "error_msg": str(e)},
            )
            # pipeline 整体崩了：整批记 error，但已保存的文件不丢
            pipeline_report = {
                "status": "failed",
                "dry_run": False,
                "duration_ms": 0,
                "step_timings_ms": {},
                "error": f"pipeline 异常: {e}",
            }
            for item in items:
                if not item.error:
                    item.error = f"pipeline 异常: {e}"

    duration_ms = int((time.perf_counter() - t0) * 1000)
    log.info(
        "batch upload: done total=%d succeeded=%d failed=%d duration=%dms",
        len(files), succeeded, failed, duration_ms,
        extra={"step": "upload", "status": "batch_done"},
    )

    return BatchUploadResponse(
        total=len(files),
        succeeded=succeeded,
        failed=failed,
        duration_ms=duration_ms,
        items=items,
        pipeline=pipeline_report,
    )


@router.post("/upload/single/ingest")
def post_upload_single_ingest(
    filename: str,
    profile_id: str = "",
) -> Dict[str, Any]:
    """对已上传的单文件触发全流程入库（不重复上传文件）。

    使用场景：用户在第一步上传后关掉了 auto_ingest，或想重跑全流程。
    调用前确保文件已通过 /api/upload/single 上传并在 manifest 中。

    ★ 2026-08：只处理这个文件（target_stems=[stem]），
    不处理 manifest / chunks 目录里其他走完整清单流程的文档。
    """
    log.info(
        "api /upload/single/ingest called: filename=%s",
        filename,
        extra={"step": "api", "status": "single_ingest", "file_name": filename},
    )
    # 从 filename 提取 stem（去除扩展名），作为 target_stems
    target_stem = Path(filename).stem
    # ★ 2026-08 配置中心：处理前必须已配置好方案
    profile = _resolve_run_config(profile_id or None)
    if not profile:
        raise HTTPException(
            status_code=400,
            detail="尚未配置任何配置方案：请先到「配置中心」配置知识库 ID 与切分策略，并选择一个方案激活",
        )
    try:
        return _run_single_file_pipeline(
            target_stem, profile, source=config_run_log.SOURCE_UPLOAD_REINGEST
        )
    except Exception as e:  # noqa: BLE001
        log.exception(
            "single ingest 接口异常",
            extra={"step": "api", "status": "single_ingest_error", "error_msg": str(e)},
        )
        raise HTTPException(status_code=500, detail=str(e)) from e
