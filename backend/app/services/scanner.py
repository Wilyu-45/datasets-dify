"""plan.md §3.1 — PostgreSQL 驱动的文件读取与状态管理。

核心流程（以 manifest 表为主线，input/ 是文件来源）：
    1. 读取 manifest 表，定位每行的「文件名称」「导入情况」
    2. 对每一行：
       - 如果「导入情况」非空（已标记） → 跳过（幂等）
       - 如果「导入情况」为空（未导入）：
         a. 去 input/ 找同名文件
         b. 找不到 → 记 MISSING，不动 manifest
         c. 找到 → 算 MD5，处理 pending/ 冲突，shutil.move 到 pending/
         d. 更新 manifest（import_status / process_status / status / md5 / 时间戳）
    3. 返回 ScanReport

启动约束：
    - 服务启动时（main.lifespan）只调用 manifest_store.bootstrap() 确保 PostgreSQL manifest 表就绪，
      **不会**调用本函数，也不会移动任何文件。
    - 本函数只在用户点击前端「扫描」按钮时由 /api/scan 触发。

幂等性：
    - 第二次扫描：所有行的「导入情况」都已非空 → 全部跳过，staged=0。
"""

from __future__ import annotations

import logging
import shutil
import time
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from app.config import settings
from app.models.schemas import (
    FileAction,
    FileActionRecord,
    ManifestRow,
    ScanReport,
)
from app.services import hasher, manifest_store

log = logging.getLogger("ragsystem.scanner")


def _is_already_imported(row: ManifestRow) -> bool:
    """「导入情况」列有非空文本即视为已处理。"""
    return bool(row.import_status and str(row.import_status).strip())


def _list_pending_files() -> Set[str]:
    if not settings.pending_dir.exists():
        return set()
    return {p.name for p in settings.pending_dir.iterdir() if p.is_file()}


def _resolve_input_path(name_in_excel: str) -> Optional[Path]:
    """在 input/ 解析 Excel 中的「文件名称」到真实文件路径。

    规则：
    1) 精确匹配（包含用户写出的扩展名如果有）
    2) 否则按 allowed_extensions 顺序追加扩展名尝试
    3) 多个候选 → 按配置优先级取第一个命中

    Returns:
        命中的 Path，未找到返回 None
    """
    if not settings.input_dir.exists():
        return None
    # 1) 精确匹配
    exact = settings.input_dir / name_in_excel
    if exact.is_file():
        return exact
    # 2) 按优先级尝试追加扩展名
    for ext in settings.allowed_extensions:
        candidate = settings.input_dir / f"{name_in_excel}{ext}"
        if candidate.is_file():
            return candidate
    return None


def _collision_rename(target: Path) -> Path:
    """pending/ 同名冲突 → 在扩展名前插入 _<6hex>。"""
    stem = target.stem
    suffix = target.suffix
    import hashlib
    suffix_hex = hashlib.md5(str(time.time_ns()).encode()).hexdigest()[:6]
    new_name = f"{stem}_{suffix_hex}{suffix}"
    return target.with_name(new_name)


def scan_and_stage(dry_run: bool = False, force: bool = False) -> ScanReport:
    """§3.1 主入口（PostgreSQL manifest 表驱动）。

    遍历 manifest 表，对每行「导入情况」为空的记录：
        - 在 input/ 找同名文件 → 移到 pending/，更新 manifest
        - 找不到 → 记 MISSING，不动 manifest

    ★ 2026-08 新增 force 参数（流水线一致性）：
        - force=True：忽略 import_status 检查，重新扫描所有行
          （如果文件已不在 input/，记 MISSING；如还在则重新移入 pending/）
        - force=False（默认）：import_status 非空 → 跳过（幂等）
    """
    started = time.perf_counter()
    log.info(
        "scan started",
        extra={"step": "scan", "status": "start", "dry_run": dry_run, "force": force},
    )

    settings.ensure_dirs()
    # bootstrap：确保 manifest 存在且列齐全（用户原 11 列 + 系统 5 列）
    manifest_store.bootstrap(settings.data_root)

    # 1) 加载 manifest
    manifest: Dict[str, ManifestRow] = manifest_store.load()
    pending_names = _list_pending_files()

    actions: List[FileActionRecord] = []
    new_count = staged_count = skipped_count = renamed_count = failed_count = 0
    missing_on_disk = 0
    rows_to_write: List[ManifestRow] = []

    # 2) 遍历每一行
    for fname, row in manifest.items():
        t0 = time.perf_counter()

        # 已经处理过（导入情况非空） → 跳过（除非 force）
        if _is_already_imported(row) and not force:
            skipped_count += 1
            actions.append(
                FileActionRecord(
                    filename=fname,
                    action=FileAction.SKIPPED_DONE,
                    note=f"导入情况={row.import_status}，已处理",
                    duration_ms=int((time.perf_counter() - t0) * 1000),
                )
            )
            continue
        # ★ 2026-08 修复（流水线一致性）：force=True 时即使已 staged 也重新走一遍
        #   扫描逻辑（pending/ 已有同 md5 会自动跳过；input/ 缺失则记 MISSING）
        if _is_already_imported(row) and force:
            log.info(
                "scan: force=True，强制重扫（已 staged 行）",
                extra={"step": "scan", "status": "force_rescan", "file_name": fname},
            )

        # 未导入：在 input/ 找文件（支持扩展名自动补全）
        src = _resolve_input_path(fname)
        if src is None:
            # 找不到 → 仅 WARNING + 记 MISSING，不动 manifest
            missing_on_disk += 1
            log.warning(
                "manifest 中导入情况为空但 input/ 找不到该文件",
                extra={
                    "file_name": fname,
                    "step": "scan",
                    "status": "missing",
                    "error_msg": f"已尝试扩展名: {', '.join(settings.allowed_extensions[:5])}…",
                },
            )
            actions.append(
                FileActionRecord(
                    filename=fname,
                    action=FileAction.MISSING,
                    error=f"Excel 中『{fname}』标记未导入，input/ 也找不到（已尝试常见扩展名）",
                )
            )
            continue

        # 实际文件名（可能含扩展名，可能与 fname 不同）
        actual_filename = src.name

        # 文件存在 → 算 MD5、移入 pending/、更新 manifest
        try:
            md5_val = hasher.md5_of_file(src, settings.scan_chunk_size)

            if dry_run:
                # dry_run：不移动、不写盘，但记录
                new_count += 1
                actions.append(
                    FileActionRecord(
                        filename=actual_filename,
                        action=FileAction.DRY_RUN,
                        md5=md5_val,
                        from_path=str(src),
                        duration_ms=int((time.perf_counter() - t0) * 1000),
                    )
                )
                # dry_run 仍更新 manifest 的 filename 字段（这样重复 dry_run 也能命中精确名）
                rows_to_write.append(
                    _row_after_stage(
                        row, actual_filename, md5_val, str(src), None,
                        action="dry_run",
                    )
                )
                continue

            # 实际移动流程
            dst = settings.pending_dir / actual_filename
            renamed_this = False
            action_type: FileAction = FileAction.STAGED

            if dst.exists():
                if hasher.md5_of_file(dst, settings.scan_chunk_size) == md5_val:
                    # md5 一致，视作已就绪
                    log.info(
                        "pending/ 中已有同 md5 文件，跳过移动",
                        extra={
                            "file_name": actual_filename,
                            "step": "scan",
                            "status": "skipped_duplicate",
                        },
                    )
                    skipped_count += 1
                    actions.append(
                        FileActionRecord(
                            filename=actual_filename,
                            action=FileAction.SKIPPED_DONE,
                            md5=md5_val,
                            from_path=str(src),
                            to_path=str(dst),
                            duration_ms=int((time.perf_counter() - t0) * 1000),
                        )
                    )
                    rows_to_write.append(
                        _row_after_stage(
                            row, actual_filename, md5_val, str(src), str(dst),
                            action="staged",
                        )
                    )
                    continue
                else:
                    # md5 不一致，重命名
                    dst = _collision_rename(dst)
                    renamed_this = True
                    renamed_count += 1
                    log.warning(
                        "pending/ 同名 md5 不一致，已重命名",
                        extra={
                            "file_name": actual_filename,
                            "step": "scan",
                            "status": "renamed",
                            "error_msg": dst.name,
                        },
                    )

            # 移动文件
            shutil.move(str(src), str(dst))
            staged_count += 1
            new_count += 1
            action_type = (
                FileAction.COLLISION_RENAMED if renamed_this else FileAction.STAGED
            )

            # 记 action
            actions.append(
                FileActionRecord(
                    filename=actual_filename,
                    action=action_type,
                    md5=md5_val,
                    from_path=str(src),
                    to_path=str(dst),
                    duration_ms=int((time.perf_counter() - t0) * 1000),
                )
            )

            # 写 manifest 行（用 actual_filename 更新 filename 字段）
            action_str = "renamed" if renamed_this else "staged"
            rows_to_write.append(
                _row_after_stage(
                    row, actual_filename, md5_val, str(src), str(dst),
                    action=action_str,
                )
            )

        except Exception as e:  # noqa: BLE001
            failed_count += 1
            log.exception(
                "扫描单文件失败",
                extra={
                    "file_name": actual_filename if 'actual_filename' in locals() else fname,
                    "step": "scan",
                    "status": "error",
                    "error_msg": str(e),
                },
            )
            actions.append(
                FileActionRecord(
                    filename=actual_filename if 'actual_filename' in locals() else fname,
                    action=FileAction.FAILED,
                    from_path=str(src),
                    error=str(e),
                    duration_ms=int((time.perf_counter() - t0) * 1000),
                )
            )
            # 失败也要写一次 manifest 以记录错误
            try:
                try:
                    md5_fail = hasher.md5_of_file(src, settings.scan_chunk_size)
                except Exception:  # noqa: BLE001
                    md5_fail = row.md5 or ""
                rows_to_write.append(
                    _row_after_stage(
                        row, actual_filename if 'actual_filename' in locals() else fname,
                        md5_fail, str(src), None,
                        action="failed", note=str(e),
                    )
                )
            except Exception:  # noqa: BLE001
                log.exception("记录失败行到 manifest 时再次失败", extra={"file_name": fname})

    # 3) 批量写 manifest
    if not dry_run and rows_to_write:
        try:
            manifest_store.bulk_upsert(rows_to_write)
        except Exception as e:  # noqa: BLE001
            log.exception(
                "manifest 写盘失败",
                extra={"step": "scan", "status": "error", "error_msg": str(e)},
            )
            failed_count += len(rows_to_write)
            raise

    duration_ms = int((time.perf_counter() - started) * 1000)
    report = ScanReport(
        dry_run=dry_run,
        scanned=staged_count + missing_on_disk + failed_count + sum(
            1 for a in actions if a.action in (FileAction.DRY_RUN,)
        ),
        staged=staged_count,
        new=new_count,
        skipped_done=skipped_count,
        renamed=renamed_count,
        missing_on_disk=missing_on_disk,
        failed=failed_count,
        actions=actions,
    )
    log.info(
        "scan finished",
        extra={
            "step": "scan",
            "status": "done",
            "duration_ms": duration_ms,
            "scanned": report.scanned,
            "staged": report.staged,
            "new": report.new,
            "skipped": report.skipped_done,
            "renamed": report.renamed,
            "missing": report.missing_on_disk,
            "failed": report.failed,
        },
    )
    return report


def _row_after_stage(
    existing: Optional[ManifestRow],
    filename: str,
    md5_val: str,
    from_path: str,
    to_path: Optional[str],
    *,
    action: str = "staged",
    note: str = "",
) -> ManifestRow:
    """根据已有行（或新行）构造更新后的 ManifestRow。

    同步更新用户原列：
        导入情况  — 是否已移入待处理（"已移入待处理" / "已移入待处理(重命名)" / "移入失败" / "试运行-已识别"）
        处理情况  — 当前处理阶段（"已扫描" / "已扫描(重命名)" / "扫描失败"）
        处理说明  — 自由备注（默认写入"md5: <hash>"或错误信息）
    以及系统 5 列：
        status    — 管线 FSM
        md5/create_time/update_time/error_msg
    """
    now = manifest_store.now_iso()

    # 动作 → 用户列文本
    if action == "staged":
        import_status = "已移入待处理"
        process_status = "已扫描"
    elif action == "renamed":
        import_status = "已移入待处理(重命名)"
        process_status = "已扫描(重命名)"
    elif action == "dry_run":
        import_status = "试运行-已识别"
        process_status = "试运行-已识别"
    elif action == "failed":
        import_status = "移入失败"
        process_status = "扫描失败"
    else:
        import_status = None
        process_status = None

    # 处理说明默认内容
    if note:
        process_note = note
    elif action == "failed":
        process_note = note or "扫描过程中发生异常"
    else:
        # 默认记录 md5 便于人工核对
        process_note = f"md5={md5_val[:12]}…"

    # 系统 status
    if action == "failed":
        sys_status = "error"
    elif action == "dry_run":
        sys_status = existing.status if existing else "new"
    else:
        sys_status = "pending"

    if existing is None:
        return ManifestRow(
            filename=filename,
            status=sys_status,
            md5=md5_val,
            create_time=now,
            update_time=now,
            import_status=import_status,
            process_status=process_status,
            process_note=process_note,
            error_msg=note if action == "failed" else None,
        )

    update_kwargs: Dict[str, object] = {
        "filename": filename,  # 始终使用最新文件名（可能是补全扩展名后的）
        "status": sys_status,
        "md5": md5_val,
        "update_time": now,
        "create_time": existing.create_time or now,
        "error_msg": note if action == "failed" else None,
    }
    # 仅当 import_status / process_status 有新值时才覆盖
    if import_status is not None:
        update_kwargs["import_status"] = import_status
    if process_status is not None:
        update_kwargs["process_status"] = process_status
    if process_note is not None:
        update_kwargs["process_note"] = process_note

    return existing.model_copy(update=update_kwargs)
