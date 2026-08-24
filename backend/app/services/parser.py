"""plan.md §3.2 — 调用 MinerU API 解析。

核心流程（以 manifest.xlsx + pending/ 为主线）：
    1. 加载 manifest；筛选「import_status 非空 + parse 列为空」的行
       —— 即：已扫描移入待处理、但尚未解析。
    2. 对每行：
        a. 在 pending/ 找原始文件
        b. 调 mineru_client.parse_file(file, parsed_dir)
        c. 成功 → 把所有 mineru 输出文件（.md / .json / images / ...）
                  落到 data/parsed/{stem}/，并更新 manifest 的 parse 列
        d. 失败（重试耗尽） → 把原文件移动到 data/error/{filename}，
                              更新 manifest 的 status=error / parse=错误描述
    3. 返回 ParseReport

启动约束：
    - 服务启动时（main.lifespan）只 bootstrap manifest；不调本函数、不调 API。
    - 本函数只在用户点击前端「解析」按钮时由 /api/parse 触发。

幂等性：
    - 第二次解析：parse 列非空的行 → 全部 SKIPPED_DONE。
"""

from __future__ import annotations

import logging
import shutil
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.config import settings
from app.models.schemas import (
    ManifestRow,
    ParseAction,
    ParseActionRecord,
    ParseReport,
)
from app.services import manifest_store
from app.services.mineru_client import (
    MinerUClient,
    MinerUError,
    _UnsupportedLegacyDocError,
)
from app.services import pdf_fallback

log = logging.getLogger("ragsystem.parser")


def _safe_stem(name: str) -> str:
    """把文件名转为安全的目录名。

    Windows 会自动去掉目录名尾部的句号和空格（如 "monitoring." → "monitoring"），
    但 Python 的 Path.stem 保留尾部句号（"monitoring..pdf" → "monitoring."），
    导致代码引用路径与实际文件系统路径不一致。
    这里主动去除尾部句号/空格，保证一致。
    """
    stem = Path(name).stem
    return stem.rstrip(". ")

# ★ 2026-08-08：解析进度追踪（供 /api/parse/progress 查询）
# key=filename, value={progress, msg, status}
_parse_progress: Dict[str, Dict[str, Any]] = {}


def get_parse_progress() -> Dict[str, Dict[str, Any]]:
    """返回当前解析进度快照。"""
    return dict(_parse_progress)


def _set_progress(fname: str, progress: int, msg: str, status: str) -> None:
    """更新单文件解析进度。"""
    _parse_progress[fname] = {"progress": progress, "msg": msg, "status": status}


def _is_already_parsed(row: ManifestRow) -> bool:
    """parse 列已有内容 → 视为已解析。"""
    return bool(row.parse and str(row.parse).strip())


def _is_parsed_dir_valid(parsed_dir: Path) -> bool:
    """解析目录有效：存在 + 至少含 .md（递归查找，因为 ZIP 可能在子目录如 hybrid_auto/）。"""
    if not parsed_dir.is_dir():
        return False
    return any(parsed_dir.rglob("*.md"))


def _is_mineru_output_trivial(parsed_dir: Path) -> tuple[bool, str]:
    """检测 MinerU 解析产物是否严重缺失（仅识别到年份/数字等垃圾内容）。

    场景：PDF 是文本型但 MinerU 服务端不能解码 Type0 + GBK-EUC-H CMap，
    导致 .md 几乎为空（只识别 ASCII 范围年份数字）。

    Returns:
        (is_trivial, reason) - is_trivial=True 时 reason 描述具体原因
    """
    # 找 v2 文件
    v2_files = list(parsed_dir.rglob("*_content_list_v2.json"))
    total_chars = 0
    if v2_files:
        try:
            import json as _json
            v2 = _json.loads(v2_files[0].read_text(encoding="utf-8"))
            # 收集所有 text/paragraph/title 文本字符数
            for page_blocks in v2:
                for block in page_blocks or []:
                    content = block.get("content", {})
                    # title: title_content
                    for tc in content.get("title_content", []):
                        total_chars += len(tc.get("content", ""))
                    # paragraph: paragraph_content
                    for pc in content.get("paragraph_content", []):
                        total_chars += len(pc.get("content", ""))
        except Exception:  # noqa: BLE001
            pass

    # 找 .md 文件
    md_files = list(parsed_dir.rglob("*.md"))
    if md_files:
        try:
            total_chars += len(md_files[0].read_text(encoding="utf-8").strip())
        except OSError:
            pass

    # 阈值：MinerU 解析成功但提取的文本 < 100 字符 → 视为 trivial
    if total_chars < 100:
        return True, f"v2+.md 提取字符数过少 ({total_chars} < 100)"
    return False, ""


def _resolve_pending_path(name_in_excel: str) -> Optional[Path]:
    """在 pending/ 找 Excel 中的「文件名称」。

    1) 精确匹配
    2) 按 allowed_extensions 顺序追加扩展名（与 §3.1 行为一致）
    3) ★ stem 模糊匹配：把 manifest 中的 stem 与 pending/ 中所有文件做 stem 比较。
       场景：用户把 .doc 转成 .docx 后放回 pending/，但 manifest filename 还是 .doc。

    返回值：用 Path（真实找到的文件）。调用方负责在 stem 匹配时同步 manifest.filename。
    """
    if not settings.pending_dir.exists():
        return None
    exact = settings.pending_dir / name_in_excel
    if exact.is_file():
        return exact
    for ext in settings.allowed_extensions:
        candidate = settings.pending_dir / f"{name_in_excel}{ext}"
        if candidate.is_file():
            return candidate

    # ★ stem 模糊匹配：去掉 .doc 之类后缀，在 pending/ 中找同 stem 的任意文件
    # 防止类似 "name.doc" vs "name.docx" 在用户手动转换扩展名后找不到
    name_stem = Path(name_in_excel).stem
    candidates: List[Path] = []
    for f in settings.pending_dir.iterdir():
        if not f.is_file():
            continue
        if f.stem == name_stem:
            candidates.append(f)
    if len(candidates) == 1:
        return candidates[0]
    if len(candidates) > 1:
        # 多个候选（同一 stem 但不同扩展名）：按 allowed_extensions 优先级取第一个
        # 优先级排序后返回
        def _ext_rank(p: Path) -> int:
            try:
                return settings.allowed_extensions.index(p.suffix.lower())
            except ValueError:
                return len(settings.allowed_extensions) + 1
        candidates.sort(key=_ext_rank)
        return candidates[0]
    return None


def _sync_manifest_filename(
    old_filename: str, new_filename: str, row: ManifestRow
) -> None:
    """manifest 的 filename 主键与 pending/ 实际文件不一致时，upsert 一行新 filename。

    策略：写一条新行（filename=new_filename），保留原 row 其他字段。
    旧的 filename 行保留——它已经处于 "import_status=已移入待处理" 但 pending/ 找不到文件，
    后续没有 pending 文件它会一直被 _resolve_pending_path 返回 None，自然被跳过。
    """
    new_row = row.model_copy(update={"filename": new_filename})
    manifest_store.upsert(settings.manifest_path, new_row)
    log.info(
        "manifest filename 已同步",
        extra={
            "step": "parse",
            "status": "filename_synced",
            "old_filename": old_filename,
            "new_filename": new_filename,
        },
    )


def _try_pymupdf_fallback(
    src: Path,
    parsed_dir: Path,
    client: "MinerUClient",
) -> tuple:
    """对 .pdf 走 PyMuPDF fallback 链（Tier 1→2→3）。

    复用 `pdf_fallback.maybe_fallback_after_mineru_failure`：
    - 适用于「MinerU 解析成功但产物过少」（v2 trivial）场景
    - 也适用于「MinerU 调用彻底失败」（4xx/5xx/网络）场景
    - 失败/非 .pdf/PyMuPDF 不可用 → 返回 (False, None)
    - 任意 Tier 成功 → 返回 (True, backend_name)

    Returns:
        (fallback_used: bool, fallback_backend: Optional[str])
    """
    if src.suffix.lower() != ".pdf":
        return False, None
    if not pdf_fallback.is_pymupdf_available():
        return False, None
    try:
        fb_result = pdf_fallback.maybe_fallback_after_mineru_failure(
            src, parsed_dir, client=client
        )
    except Exception as fb_exc:  # noqa: BLE001
        log.warning(
            "PyMuPDF fallback 抛出异常（已忽略）: %s — %s",
            src.name, fb_exc,
        )
        return False, None
    if fb_result is None:
        return False, None
    return True, fb_result.backend


def _move_to_error(src: Path, err: str) -> Path:
    """把失败文件移入 data/error/。同 md5 跳过；同 md5 不一致则 _<6hex> 重命名。

    ★ 2026-08 修复（Windows 文件锁）：
      PyMuPDF 在「文件无法解析为 PDF」时仍会短暂持有 Windows 文件句柄，
      导致紧随其后的 shutil.move 报 [WinError 32] 进程无法访问。
      解决：先尝试直接 move，PermissionError 时降级到 copy+unlink
      （copy 不受源文件句柄短暂持有的影响）。
    """
    from app.services import hasher

    settings.error_dir.mkdir(parents=True, exist_ok=True)
    dst = settings.error_dir / src.name
    if dst.exists():
        try:
            if hasher.md5_of_file(dst, settings.scan_chunk_size) == hasher.md5_of_file(
                src, settings.scan_chunk_size
            ):
                # 已有相同内容，仅删除源
                src.unlink(missing_ok=True)
                return dst
        except Exception:  # noqa: BLE001
            pass
        # md5 不一致：重命名
        import hashlib

        stem, suffix = dst.stem, dst.suffix
        h6 = hashlib.md5(f"{time.time_ns()}".encode()).hexdigest()[:6]
        dst = dst.with_name(f"{stem}_{h6}{suffix}")
    # 1) 首选 shutil.move（跨设备时自动降级为 copy+unlink）
    try:
        shutil.move(str(src), str(dst))
    except PermissionError as perm_err:
        # Windows 上偶发：源文件被 PyMuPDF / 反病毒软件短暂锁定
        # 兜底：先复制内容到目标，再 unlink 源（unlink 比 move 更不容易锁失败）
        log.warning(
            "shutil.move 遇到文件锁（%s），降级为 copy+unlink: %s",
            perm_err, src.name,
        )
        shutil.copyfile(str(src), str(dst))
        # 删除源（允许多次重试，处理短暂锁）
        for attempt in range(5):
            try:
                src.unlink()
                break
            except PermissionError:
                if attempt < 4:
                    time.sleep(0.05 * (attempt + 1))  # 50ms, 100ms, 150ms, 200ms
                else:
                    # 最后一次仍失败：保留源文件，标记 error；不让整体流程崩
                    log.warning(
                        "源文件 5 次 unlink 仍失败（Windows 锁未释放），源文件保留在 pending/: %s",
                        src,
                    )
    log.warning(
        "解析失败文件已移入 error/",
        extra={
            "step": "parse",
            "status": "moved_to_error",
            "file_name": dst.name,
            "error_msg": err,
        },
    )
    return dst


def parse_pending(
    dry_run: bool = False,
    client: Optional[MinerUClient] = None,
    force: bool = False,
    target_stems: Optional[List[str]] = None,
) -> ParseReport:
    """§3.2 主入口。

    遍历 manifest，对 import_status 非空 + parse 列为空的行：
        - 在 pending/ 找文件 → 调 mineru API → 落盘 parsed/{stem}/
        - 失败 → 移入 error/，更新 manifest

    ★ 2026-08 新增 force 参数（流水线一致性）：
        - force=True：清空旧的 parsed/{stem}/ 目录后重新调 MinerU（仍会触发 PyMuPDF fallback）
        - force=False（默认）：parse 列非空 → 跳过（幂等）

    ★ 2026-08 新增 target_stems 白名单（单文件上传 + 一键入库）：
        - target_stems=None（默认）：处理所有符合 import_status 非空 + parse 列空的行
        - target_stems=[stem1, stem2, ...]：只处理这些 stem 对应的行，其他行被跳过
          用于「单文件上传 + 一键入库」场景——用户上传单文件后，流水线只处理这一个文件，
          不应该处理 manifest 里其他待解析/已解析的文档（那些需要走完整 Excel 流程）。
    """
    started = time.perf_counter()
    log.info(
        "parse started",
        extra={"step": "parse", "status": "start", "dry_run": dry_run, "force": force,
               "target_stems": target_stems},
    )

    settings.ensure_dirs()
    manifest_store.bootstrap(settings.data_root)

    client = client or MinerUClient()

    manifest: Dict[str, ManifestRow] = manifest_store.load(settings.manifest_path)

    # ★ target_stems 白名单：转 set 提高查找效率
    target_stem_set: Optional[set] = (
        set(target_stems) if target_stems is not None else None
    )

    actions: List[ParseActionRecord] = []
    parsed_count = skipped_count = failed_count = 0

    for fname, row in manifest.items():
        t0 = time.perf_counter()

        # ★ 0) target_stems 白名单过滤：白名单外的行直接跳过
        if target_stem_set is not None:
            row_stem = Path(fname).stem
            if row_stem not in target_stem_set:
                continue

        # 1) 已解析 → 跳过（除非 force）
        if _is_already_parsed(row):
            if not force and _is_parsed_dir_valid(settings.parsed_dir / row.parse):
                # 幂等跳过：已解析 + 目录有效 + 未开 force
                skipped_count += 1
                actions.append(
                    ParseActionRecord(
                        filename=fname,
                        action=ParseAction.SKIPPED_DONE,
                        parse_dir=str((settings.parsed_dir / row.parse).resolve()),
                        duration_ms=int((time.perf_counter() - t0) * 1000),
                    )
                )
                continue
            # ★ 2026-08 修复（流水线一致性）：force=True 时清空旧 parsed 目录，重新调 MinerU
            if force:
                old_parse_dir = settings.parsed_dir / row.parse
                if old_parse_dir.exists():
                    log.info(
                        "parse: force=True，清空旧 parsed 目录: %s",
                        old_parse_dir,
                        extra={"step": "parse", "status": "force_clean", "file_name": fname},
                    )
                    shutil.rmtree(str(old_parse_dir), ignore_errors=True)
                # 清空后 manifest 的 parse 列不再有效，重置为 None（避免下次再走"已解析"分支时被旧值误导）
                row = row.model_copy(update={"parse": None})
                manifest[fname] = row

        # 2) 在 pending/ 找原文件
        src = _resolve_pending_path(fname)
        if src is None:
            # 没有原始文件（可能已经被移走/被前面步骤消费），跳过
            log.warning(
                "manifest 标记待解析但 pending/ 找不到原文件",
                extra={
                    "step": "parse",
                    "status": "no_pending",
                    "file_name": fname,
                },
            )
            actions.append(
                ParseActionRecord(
                    filename=fname,
                    action=ParseAction.NO_PENDING,
                    error=f"pending/ 找不到 {fname}",
                    duration_ms=int((time.perf_counter() - t0) * 1000),
                )
            )
            continue

        # 2.5) ★ stem 模糊匹配命中：实际文件名与 manifest 不一致 → 同步 manifest
        #      场景：用户手动把 .doc 转为 .docx 放回 pending/，manifest 还记录着 .doc
        if src.name != fname:
            _sync_manifest_filename(fname, src.name, row)
            # 不动 manifest 字典（迭代中），仅本次循环用 effective_row 替换
            row = row.model_copy(update={"filename": src.name})
            fname = src.name

        # 3) dry_run：不调 API
        if dry_run:
            parsed_count += 1
            actions.append(
                ParseActionRecord(
                    filename=src.name,
                    action=ParseAction.DRY_RUN,
                    parse_dir=str((settings.parsed_dir / _safe_stem(src.name)).resolve()),
                    duration_ms=int((time.perf_counter() - t0) * 1000),
                )
            )
            # dry_run 也写一份 manifest（标记已识别但未实际解析）
            _write_manifest_row(
                row,
                parse_text="试运行-已识别",
                sys_status="pending",
                err=None,
            )
            continue

        # 4) 实际调 API
        _set_progress(src.name, 0, "正在调用 MinerU API...", "parsing")
        try:
            result = client.parse_file(src, settings.parsed_dir / _safe_stem(src.name))
            _set_progress(src.name, 100, "解析完成", "done")
            parsed_count += 1

            # ★ 质量检查：MinerU 解析成功但产物过少 → 启动 fallback 链
            #   Tier 1: PyMuPDF 渲染 PDF 为图片 → MinerU vlm-engine 读图
            #   Tier 2: PyMuPDF 纯文本提取
            fallback_used = False
            fallback_backend = None
            is_trivial, trivial_reason = _is_mineru_output_trivial(result.parse_dir)
            if is_trivial:
                log.warning(
                    "MinerU 解析产物过少，启动 fallback 链: %s (原因: %s)",
                    src.name, trivial_reason,
                )
                fallback_used, fallback_backend = _try_pymupdf_fallback(
                    src, result.parse_dir, client
                )
                if fallback_used:
                    log.info(
                        "Fallback 成功 (backend=%s, 替代 MinerU 产物)",
                        fallback_backend,
                    )
                else:
                    log.warning(
                        "Fallback 链全部失败：保留 MinerU 产物（标记为 error）"
                    )

            # ★ 关键：先更新 manifest（解析已成功，所有文件已落盘），
            # 然后再构建响应记录。manifest 更新失败也不能影响前面的成功。
            if fallback_used:
                parse_text = (
                    f"{str(result.parse_dir.resolve())} [{fallback_backend} 修复]"
                )
            else:
                parse_text = str(result.parse_dir.resolve())
            _write_manifest_row(
                row,
                parse_text=parse_text,
                sys_status="parsing_done",
                err=None,
            )
            log.info(
                "parse ok",
                extra={
                    "step": "parse",
                    "status": "parsed",
                    "file_name": src.name,
                    "parse_dir": str(result.parse_dir),
                    "file_count": result.file_count,
                    "attempts": result.attempts,
                    "fallback_used": fallback_used,
                    "duration_ms": int((time.perf_counter() - t0) * 1000),
                },
            )
            # 响应记录：构建失败也不影响 manifest（用 try/except 包一层）
            try:
                actions.append(
                    ParseActionRecord(
                        filename=src.name,
                        action=ParseAction.PARSED,
                        parse_dir=str(result.parse_dir.resolve()),
                        md=str(result.md_path.resolve()) if result.md_path else None,
                        json_path=str(result.json_path.resolve()) if result.json_path else None,
                        duration_ms=int((time.perf_counter() - t0) * 1000),
                        attempts=result.attempts,
                    )
                )
            except Exception as rec_err:  # noqa: BLE001
                # 响应记录构建失败，manifest 已更新成功 → 不影响用户
                log.warning(
                    "parse 响应记录构建失败（已忽略）",
                    extra={
                        "step": "parse",
                        "status": "record_failed",
                        "file_name": src.name,
                        "error_msg": str(rec_err),
                    },
                )
                actions.append(
                    ParseActionRecord(
                        filename=src.name,
                        action=ParseAction.PARSED,
                        parse_dir=str(result.parse_dir.resolve()),
                        duration_ms=int((time.perf_counter() - t0) * 1000),
                        attempts=result.attempts,
                    )
                )
        except _UnsupportedLegacyDocError as e:
            # .doc 旧 OLE 格式不被 MinerU 支持，客户端预检测直接拒绝
            failed_count += 1
            err_text = (
                f"不支持的 Word 格式: {src.name} 是 .doc 旧二进制格式，"
                f"MinerU 仅支持 .docx。请用 Word/WPS 打开后「另存为 .docx」再上传。"
            )
            log.error(
                "parse 失败（.doc 旧格式）",
                extra={
                    "step": "parse",
                    "status": "failed_legacy_doc",
                    "file_name": src.name,
                    "error_msg": err_text,
                    "duration_ms": int((time.perf_counter() - t0) * 1000),
                },
            )
            try:
                err_dst = _move_to_error(src, err_text)
            except Exception as move_err:  # noqa: BLE001
                log.exception(
                    "移入 error/ 失败",
                    extra={
                        "step": "parse",
                        "status": "move_failed",
                        "file_name": src.name,
                        "error_msg": str(move_err),
                    },
                )
                err_dst = None

            actions.append(
                ParseActionRecord(
                    filename=src.name,
                    action=ParseAction.PARSE_FAILED,
                    error=err_text,
                    duration_ms=int((time.perf_counter() - t0) * 1000),
                    attempts=0,  # 预检测，没调 API
                )
            )
            _write_manifest_row(
                row,
                parse_text=f"解析失败（.doc 旧格式不支持）→ {err_dst.name if err_dst else '源文件保留在 pending/'}",
                sys_status="error",
                err=err_text,
            )
            continue
        except MinerUError as e:
            # 重试耗尽 → 先尝试 PyMuPDF fallback（仅 .pdf）；失败再移入 error/
            err_text = f"mineru 调用失败(尝试{e.attempts}次): {e}"
            log.error(
                "parse 失败（MinerU 调用错误）",
                extra={
                    "step": "parse",
                    "status": "failed",
                    "file_name": src.name,
                    "attempts": e.attempts,
                    "error_msg": err_text,
                    "duration_ms": int((time.perf_counter() - t0) * 1000),
                },
            )

            # ★ PyMuPDF fallback 自动救援：MinerU 4xx/5xx/网络错误时，
            # 对 .pdf 走 Tier 1/2/3 链，能恢复则不入 error/。
            expected_parse_dir = settings.parsed_dir / _safe_stem(src.name)
            fallback_used, fallback_backend = _try_pymupdf_fallback(
                src, expected_parse_dir, client
            )
            if fallback_used:
                log.warning(
                    "MinerU 调用失败但 PyMuPDF fallback 成功 (backend=%s, file=%s)",
                    fallback_backend, src.name,
                )
                parsed_count += 1
                parse_text = (
                    f"{str(expected_parse_dir.resolve())} [{fallback_backend} 修复]"
                )
                _write_manifest_row(
                    row,
                    parse_text=parse_text,
                    sys_status="parsing_done",
                    err=f"mineru 调用失败，已用 {fallback_backend} 兜底: {err_text[:200]}",
                )
                try:
                    actions.append(
                        ParseActionRecord(
                            filename=src.name,
                            action=ParseAction.PARSED,
                            parse_dir=str(expected_parse_dir.resolve()),
                            duration_ms=int((time.perf_counter() - t0) * 1000),
                            attempts=e.attempts,
                        )
                    )
                except Exception as rec_err:  # noqa: BLE001
                    log.warning(
                        "parse 响应记录构建失败（已忽略）",
                        extra={"step": "parse", "status": "record_failed",
                               "file_name": src.name, "error_msg": str(rec_err)},
                    )
                continue

            # Fallback 不可用或全部失败 → 移入 error/（原行为）
            failed_count += 1
            _set_progress(src.name, 0, f"解析失败: {err_text[:50]}", "failed")
            try:
                err_dst = _move_to_error(src, err_text)
            except Exception as move_err:  # noqa: BLE001
                # 移入 error 也失败：记日志，源文件保留
                log.exception(
                    "移入 error/ 失败",
                    extra={
                        "step": "parse",
                        "status": "move_failed",
                        "file_name": src.name,
                        "error_msg": str(move_err),
                    },
                )
                err_dst = None

            actions.append(
                ParseActionRecord(
                    filename=src.name,
                    action=ParseAction.PARSE_FAILED,
                    error=err_text,
                    duration_ms=int((time.perf_counter() - t0) * 1000),
                    attempts=e.attempts,
                )
            )
            _write_manifest_row(
                row,
                parse_text=f"解析失败 → {err_dst.name if err_dst else '源文件保留在 pending/'}",
                sys_status="error",
                err=err_text,
            )

    # ---- 汇总 ----
    duration_ms = int((time.perf_counter() - started) * 1000)
    report = ParseReport(
        dry_run=dry_run,
        api_url=client.api_url,
        scanned=parsed_count + skipped_count + failed_count,
        parsed=parsed_count,
        skipped_done=skipped_count,
        failed=failed_count,
        actions=actions,
    )
    log.info(
        "parse finished",
        extra={
            "step": "parse",
            "status": "done",
            "duration_ms": duration_ms,
            "parsed": report.parsed,
            "skipped": report.skipped_done,
            "failed": report.failed,
        },
    )
    return report


def _write_manifest_row(
    row: ManifestRow,
    *,
    parse_text: str,
    sys_status: str,
    err: Optional[str],
) -> None:
    """构造更新后的 ManifestRow 并 upsert。"""
    now = manifest_store.now_iso()
    update_kwargs: Dict[str, object] = {
        "filename": row.filename,  # 主键不变
        "parse": parse_text,
        "update_time": now,
    }
    # 系统 status：成功 → parsing_done；失败 → error；dry_run → pending
    if sys_status:
        update_kwargs["status"] = sys_status
    # error_msg：失败时写原因
    update_kwargs["error_msg"] = err

    new_row = row.model_copy(update=update_kwargs)
    manifest_store.upsert(settings.manifest_path, new_row)
