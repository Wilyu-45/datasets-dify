"""Dify 入库主逻辑（plan.md §3.4）。

遍历 data/chunks/ 下所有「文档命名」的子目录，对每个目录：
    1. 创建一个 Dify 文档（name = 目录名 / 文档 stem，方便溯源）
    2. 等待文档 indexing 完成
    3. 读取 chunk_metadata.json，遍历每个 chunk_NNN_*.md：
       a) 上传 chunk 内 `![](images/xxx.jpg)` 引用的所有图片到 Dify
       b) 把 chunk 内容 + attachment_ids 一起 add_segments 提交
    4. 把 Dify 文档 ID / 状态写回 manifest 的 dify_doc_id、dify_status 列
    5. ★ 入库成功后将文件夹从 data/chunks/{stem}/ 移动到 data/output/{stem}/
       并更新 manifest.chunks = "output/{stem}"，status = "done"

幂等性：
- 通过 dify_status 列控制：
  - 空 = 待处理
  - "uploading" = 正在处理（中途崩溃时人工排查）
  - "done" = 全部完成（包含所有 chunk + 图片）
- 重跑时 skip 已 done 的行
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from app.config import settings
from app.models.schemas import (
    DifyActionRecord,
    DifyUploadReport,
    ManifestRow,
)
from app.services import image_host, manifest_store
from app.services.dify_uploader import DifyClient, DifyError
from app.services import doc_metadata

log = logging.getLogger("ragsystem.dify_ingest")


# 匹配 markdown 图片语法 `![alt](path)`，分别捕获 alt 和 path
_IMG_RE_FULL = re.compile(r"!\[([^\]]*)\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
# 仅捕获 path 的简化版（与原 _IMG_RE 行为一致）
_IMG_RE = re.compile(r"!\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")


@dataclass
class ChunkUploadInfo:
    """单个 chunk 的入库信息。"""

    chunk_id: str
    file_name: str
    title_path: str
    content: str
    image_refs: List[str]  # 相对 chunks 目录的路径 "images/xxx.jpg"
    attachment_ids: List[str] = field(default_factory=list)  # 上传后获得的 file_ids
    # ★ 2026-08-13 表格独立成段元数据
    chunk_type: str = ""
    table_name: str = ""


# ============ 工具函数 ============


def _list_chunk_dirs(target_stems: Optional[List[str]] = None) -> List[Path]:
    """列出 data/chunks/ 与 data/output/ 下所有子目录（每目录对应 1 个 Dify 文档）。

    来源：
    - data/chunks/{stem}/  ← §3.3 切分产物（待入库）
    - data/output/{stem}/  ← §3.4 入库成功后已归档的目录（force 重传时需要）

    同一 stem 不会同时存在于两个位置（成功后会移动）；若重复则优先 chunks/。

    ★ 2026-08 新增 target_stems 白名单（单文件上传 + 一键入库）：
        - target_stems=None（默认）：返回所有目录
        - target_stems=[stem1, stem2, ...]：只返回 stem 在白名单内的目录
    """
    out: Dict[str, Path] = {}
    for root in (settings.chunks_dir, settings.output_dir):
        if not root.exists():
            continue
        for p in root.iterdir():
            if not p.is_dir():
                continue
            # chunks/ 优先于 output/（理论上不会同时存在，但兜底防重）
            if p.name not in out:
                out[p.name] = p

    # ★ target_stems 白名单过滤
    if target_stems is not None:
        target_stem_set = set(target_stems)
        out = {stem: path for stem, path in out.items() if stem in target_stem_set}
    return [out[k] for k in sorted(out.keys())]


def _load_metadata(chunks_dir: Path) -> Optional[Dict[str, Any]]:
    """读取 chunk_metadata.json；缺失或损坏返回 None。"""
    p = chunks_dir / "chunk_metadata.json"
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:  # noqa: BLE001
        log.warning("chunk_metadata.json 解析失败: %s — %s", p, e)
        return None


def _read_chunk_file(chunks_dir: Path, file_name: str) -> str:
    """读取单个 chunk_NNN_xxx.md 文件的全文。"""
    p = chunks_dir / file_name
    if not p.is_file():
        return ""
    return p.read_text(encoding="utf-8")


def _extract_image_refs(content: str) -> List[str]:
    """从 chunk markdown 内容中提取所有 ![](images/xxx) 引用。

    Returns:
        去重后的相对路径列表（保留顺序）。
    """
    seen = set()
    out: List[str] = []
    for m in _IMG_RE.finditer(content):
        ref = m.group(1).strip()
        if not ref:
            continue
        # 跳过外链（http/https/data:）
        if ref.startswith(("http://", "https://", "data:")):
            continue
        # 统一用正斜杠
        ref = ref.replace("\\", "/")
        # ★ 2026-08-12 修复：跳过无文件名的引用（如 "images/"、"images/"）
        # 这类引用来自 markdown 中不完整的图片语法 ![](images/)
        if not Path(ref).name:
            continue
        if ref in seen:
            continue
        seen.add(ref)
        out.append(ref)
    return out


def _resolve_image_path(chunks_dir: Path, ref: str) -> Optional[Path]:
    """把相对路径（images/xxx.jpg）解析为 chunks_dir 下的绝对路径。

    兼容以下情形：
    - images/xxx.jpg           → chunks_dir/images/xxx.jpg
    - xxx.jpg                  → chunks_dir/images/xxx.jpg
    - ../images/xxx.jpg        → chunks_dir/images/xxx.jpg（罕见但兜底）
    """
    if not ref:
        return None
    ref_clean = ref.replace("\\", "/").lstrip("/")
    # 去掉 ../ 前缀（只在 chunks 目录内查找）
    while ref_clean.startswith("../"):
        ref_clean = ref_clean[3:]
    candidates = [
        chunks_dir / ref_clean,
        chunks_dir / "images" / Path(ref_clean).name,
    ]
    for c in candidates:
        if c.is_file():
            return c.resolve()
    # 兜底：rglob
    matches = list((chunks_dir / "images").rglob(Path(ref_clean).name)) if (chunks_dir / "images").exists() else []
    if matches:
        return matches[0].resolve()
    return None


def _get_dify_base_url() -> str:
    """从 settings.dify_api_url 拿到 Dify 服务的根域名（去掉 /v1）。

    用途：Dify 的文件预览 URL 在根路径（`/files/{id}/file-preview`），
    不在 `/v1` 下；而 API URL 形如 `https://dify.17vision.com/v1`，
    不能直接拼路径。

    Examples:
        api_url = "https://dify.17vision.com/v1"  →  "https://dify.17vision.com"
        api_url = "https://api.dify.ai/v1"        →  "https://api.dify.ai"
        api_url = "https://example.com"           →  "https://example.com"
        api_url = ""                              →  ""
    """
    base = (settings.dify_api_url or "").rstrip("/")
    # 去掉结尾的 /v1（API 前缀不在文件 URL 里）
    if base.endswith("/v1"):
        base = base[:-3]
    return base


def _pick_dify_source_url(uploaded: Any) -> str:
    """从 Dify /files/upload 返回的 DifyUploadedFile 中挑选 content 用的 URL。

    优先用 Dify 返回的 source_url（含 timestamp/nonce/sign 签名），
    若为空则兜底为无签名的 /files/{id}/file-preview（已知有 400 风险，但保留行为）。
    """
    if uploaded.source_url:
        return uploaded.source_url
    log.warning(
        "dify: source_url 为空，回退到无签名 URL（可能 400）: file=%s",
        uploaded.name,
    )
    return f"{_get_dify_base_url()}/files/{uploaded.file_id}/file-preview"


def _build_public_url(stem: str, ref: str) -> str:
    """根据 settings.image_host_backend 派发到对应后端生成公网 URL。

    薄 shim：实际实现见 app/services/image_host.py。
    历史行为（cloudflared tunnel 暴露 /static/output/...）由 tunnel 后端提供，
    默认 backend=tunnel 时与改动前完全一致。
    """
    return image_host.build_image_url(settings.image_host_backend, stem, ref)


def _rewrite_image_refs_in_content(content: str, ref_to_url: Dict[str, str]) -> str:
    """把 markdown 里的 `![alt](ref)` 替换为 `![alt](public_url)`。

    仅替换在 ref_to_url 字典里的 ref；外链（http/https/data:）和不存在的 ref 保持原样。

    ★ 2026-07 修复：原 alt 文本为空时（`![](images/xxx.jpg)`），Dify 编辑器
    无法识别为图片（缺少 alt 标识），不显示预览。统一兜底为 `image`，
    替换后变成 `![image](https://dify.17vision.com/.../file-preview)`。
    """
    if not ref_to_url:
        return content

    def _repl(m: re.Match) -> str:  # type: ignore[type-arg]
        alt = m.group(1)
        path = m.group(2).strip()
        # 跳过外链
        if path.startswith(("http://", "https://", "data:")):
            return m.group(0)
        path_norm = path.replace("\\", "/").lstrip("/")
        new_url = ref_to_url.get(path_norm)
        if new_url:
            # ★ 兜底：alt 文本为空时填 "image"，让 Dify 编辑器识别为图片
            alt_final = alt.strip() if alt and alt.strip() else "image"
            return f"![{alt_final}]({new_url})"
        return m.group(0)

    return _IMG_RE_FULL.sub(_repl, content)


def _stage_for_upload(chunks_dir: Path) -> Tuple[Path, bool]:
    """为 Dify 上传做"预复制"：把 chunks_dir 的内容复制到 output/{stem}/。

    目的：Dify 处理 segment 时如果遇到内联 URL，会去拉取该 URL 的图片。
    chucks_dir 在上传成功后会被移动到 output/，但 Dify 可能在 add_segments
    返回后**异步**拉取 URL。为避免 URL 在拉取前就失效（chunks 已被移走），
    我们在上传开始前把 chunks 复制到 output/，让所有 URL 都指向 output/。
    Dify 拉取完成后，chunks 的命运由 _cleanup_after_upload 决定。

    Args:
        chunks_dir: 当前位于 data/chunks/ 下的文档目录（或 force 重传时已位于 output/ 的目录）

    Returns:
        (canonical_dir, was_copied)
        - canonical_dir: 实际作为 Dify 上传源的目录（绝大多数情况仍是 chunks_dir）
        - was_copied:    是否执行了复制。True 时说明 chunks 和 output 都有同一份数据。
    """
    stem = chunks_dir.name
    # 已经在 output/ 下了（如 force 重传），无需复制
    try:
        chunks_dir.resolve().relative_to(settings.chunks_dir.resolve())
        in_chunks = True
    except ValueError:
        in_chunks = False

    if not in_chunks:
        return chunks_dir, False

    output_subdir = settings.output_dir / stem
    if output_subdir.exists():
        # 已存在（force 重传场景），不覆盖
        log.info(
            "dify: output/{}/ 已存在，跳过预复制（force 重传场景）",
            stem,
            extra={"step": "dify", "status": "stage_skip", "stem": stem},
        )
        return chunks_dir, False

    settings.output_dir.mkdir(parents=True, exist_ok=True)
    shutil.copytree(str(chunks_dir), str(output_subdir))
    log.info(
        "dify: chunks 已预复制到 output",
        extra={
            "step": "dify",
            "status": "staged",
            "stem": stem,
            "from": str(chunks_dir),
            "to": str(output_subdir),
        },
    )
    return chunks_dir, True


def _cleanup_after_upload(chunks_dir: Path, was_copied: bool, success: bool) -> str:
    """上传结束后清理"预复制"产物，把数据放到正确位置。

    - 成功 + was_copied: 删除 chunks_dir（output/ 副本成为新家），相当于旧 _archive_chunks_dir
    - 成功 + !was_copied: 已有 output/ 副本（force 重传），不动
    - 失败 + was_copied: 删除 output/ 副本，保留 chunks/ 原始以便重试
    - 失败 + !was_copied: 什么都不做（数据本来就在原位）

    Returns:
        描述清理动作的字符串（用于 UI 提示）
    """
    if not was_copied:
        return "无需归档清理"
    stem = chunks_dir.name
    output_subdir = settings.output_dir / stem
    if success:
        if chunks_dir.is_dir():
            shutil.rmtree(str(chunks_dir))
        return f"已归档 chunks/{stem} → output/{stem}/（已删除原 chunks/）"
    else:
        if output_subdir.is_dir():
            shutil.rmtree(str(output_subdir))
        return f"已删除预复制的 output/{stem}/（保留 chunks/{stem}/ 原始以便重试）"


def _load_manifest_index() -> Dict[str, Any]:
    """读取 manifest（dict 形式），用于按 stem 关联 dify 状态。"""
    return manifest_store.load()


def upload_one_doc(
    chunks_dir: Path,
    client: DifyClient,
    *,
    force: bool = False,
    field_map: Optional[Dict[str, Dict[str, Any]]] = None,
    doc_meta_cache: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Tuple[str, List[ChunkUploadInfo], Optional[str]]:
    """把单个 chunks 目录入库到 Dify。

    Returns:
        (dify_document_id, chunk_infos, error_or_None)
        - dify_document_id: Dify 文档 ID，失败时为空字符串
        - chunk_infos: 已上传的 chunk 信息列表（含 attachment_ids）
        - error_or_None: 失败时的错误信息，成功时为 None
    """
    stem = chunks_dir.name
    metadata = _load_metadata(chunks_dir)
    if metadata is None:
        return "", [], f"chunk_metadata.json 缺失或损坏: {chunks_dir}"

    raw_chunks = metadata.get("chunks") or []
    if not raw_chunks:
        return "", [], f"chunk_metadata.json 中没有任何 chunk: {chunks_dir}"

    # 1) 读取所有 chunk 内容
    chunk_infos: List[ChunkUploadInfo] = []
    for raw in raw_chunks:
        file_name = raw.get("file_name") or ""
        if not file_name:
            continue
        content = _read_chunk_file(chunks_dir, file_name)
        # 优先用 metadata 的 image_refs（已校验），再从 content 里正则提取（兜底）
        # ★ 2026-08-12 修复：同时过滤 metadata 中的无效引用（如 "images/"）
        meta_refs = [r for r in (raw.get("image_refs") or []) if r and Path(r.replace("\\", "/")).name]
        content_refs = _extract_image_refs(content)
        # 合并去重，保留顺序
        seen = set()
        image_refs: List[str] = []
        for r in meta_refs + content_refs:
            r = r.replace("\\", "/")
            if r and r not in seen:
                seen.add(r)
                image_refs.append(r)
        chunk_infos.append(
            ChunkUploadInfo(
                chunk_id=raw.get("chunk_id", ""),
                file_name=file_name,
                title_path=raw.get("title_path", ""),
                content=content,
                image_refs=image_refs,
                chunk_type=raw.get("chunk_type", ""),
                table_name=raw.get("table_name", ""),
            )
        )

    # 2) 收集所有唯一的图片引用（避免重复上传）
    all_refs: List[str] = []
    seen_refs: set = set()
    for ci in chunk_infos:
        for r in ci.image_refs:
            if r and r not in seen_refs:
                seen_refs.add(r)
                all_refs.append(r)

    # 3) 上传所有图片（带 401 自动降级）
    #
    # 策略选择（按优先级）：
    # A) dify_app_api_key 非空 且 settings.dify_skip_file_upload=False
    #    → /files/upload 拿 file_id + Dify source_url
    #      - file_id 写到 attachment_ids（让 Dify 编辑器从 attachments 渲染预览）
    #      - content URL 选择（★ 2026-08 修复）：
    #          1) 优先 public_url（永久）—— 当 image_host 活跃时
    #          2) 降级 Dify source_url（5min 签名）—— 仅作为兜底
    # A') dify_app_api_key 非空 但 settings.dify_skip_file_upload=True（★ 2026-08-04 新增）
    #    → 跳过 /files/upload 整个端点（content 仍写 OSS 永久 URL，不带 attachment_ids）
    #      适用：Dify 端 RAG_APP_API_KEY 配了但 /files/upload 拿到的 5min 签名 URL
    #      在 content 里反复过期。让 Dify 自己从公网 URL 拉图存为 attachment。
    # B) 否则 settings.public_base_url 非空 → 公网 URL 模式（cloudflared/OSS）
    #    - 段里直接是外链 URL，Dify 拉外链图
    # C) 否则跳过图片，段里仍是 `![](images/xxx.jpg)` 相对路径
    ref_to_file_id: Dict[str, str] = {}
    ref_to_public_url: Dict[str, str] = {}  # 写到 segment content 里的 URL
    upload_blocked = False  # 一旦遇到 401 全程不再尝试 /files/upload
    # 决策：
    # - app_api_key 非空 且 dify_skip_file_upload=False → /files/upload（首选）
    # - app_api_key 非空 但 dify_skip_file_upload=True → 跳过 /files/upload，走公网 URL 模式
    # - app_api_key 空 → 走公网 URL 模式
    use_file_upload_strategy = bool(settings.dify_app_api_key) and not bool(settings.dify_skip_file_upload)
    # ★ 2026-08 修复：image_host 活跃时（public_base_url 配齐），即使走 /files/upload 也要
    #   在 content 里写永久公网 URL（避免聊天召回时 5min 签名过期 → 图片不显示）
    use_public_url_in_content = image_host.is_active(
        settings.image_host_backend, settings
    )
    use_public_url_strategy = (not use_file_upload_strategy) and use_public_url_in_content

    # ★ 2026-08-04 新增：OSS 后端 → 提前一次性上传 chunks_dir/images/ 下所有图片
    #   - 上传成功：ref → 永久公网 URL（Dify 召回时永远可访问，不依赖 tunnel 存活）
    #   - 上传失败：该 ref 不在 oss_ref_to_url 中，content 里保留原相对路径
    #   - 非 OSS 后端：oss_ref_to_url 始终为空，走原 tunnel/_build_public_url 逻辑
    oss_ref_to_url: Dict[str, str] = {}
    if settings.image_host_backend == "oss" and use_public_url_in_content:
        oss_ref_to_url = image_host.prepare_chunks_images(
            "oss", stem, chunks_dir,
        )
        if oss_ref_to_url:
            log.info(
                "OSS 图片预上传完成: stem=%s uploaded=%d",
                stem, len(oss_ref_to_url),
            )

    for ref in all_refs:
        path = _resolve_image_path(chunks_dir, ref)
        if path is None:
            log.warning("图片找不到，跳过: stem=%s ref=%s", stem, ref)
            continue

        # 策略 A：/files/upload 模式（首选）
        if use_file_upload_strategy and not upload_blocked:
            try:
                uploaded = client.upload_file(path)
                ref_to_file_id[ref] = uploaded.file_id
                # ★ 2026-08-04 修复（Dify 聊天召回图片不显示）：
                #   现象：之前 content 用 Dify source_url（带 5min 签名的 URL）。
                #         Dify 编辑器预览会重新签名（sign_content 字段），但聊天召回时
                #         Dify 不会再签名原 source_url，5min 后 URL 失效 → 图片不显示。
                #   解决：当 public_base_url（tunnel/OSS）已配置时，content 写永久公网 URL；
                #         attachment_ids 仍用 Dify file_id（保证编辑器预览）。
                #   兜底：public_base_url 未配置时，content 仍用 Dify source_url
                #         （行为与改动前一致，签名过期时聊天召回图片会失效）。
                if use_public_url_in_content:
                    # ★ 2026-08-04：OSS 后端时优先用预上传得到的永久外链
                    if ref in oss_ref_to_url:
                        ref_to_public_url[ref] = oss_ref_to_url[ref]
                    else:
                        public_url = _build_public_url(stem, ref)
                        if public_url:
                            ref_to_public_url[ref] = public_url
                        else:
                            # 公网 URL 生成失败时降级到 Dify source_url
                            log.warning(
                                "dify: 公网 URL 生成失败，降级到 Dify source_url（5min 签名）: ref=%s",
                                ref,
                            )
                            ref_to_public_url[ref] = _pick_dify_source_url(uploaded)
                else:
                    ref_to_public_url[ref] = _pick_dify_source_url(uploaded)
                log.info(
                    "dify 图片上传成功",
                    extra={"step": "dify", "status": "uploaded", "stem": stem,
                           "file": uploaded.name, "file_id": uploaded.file_id,
                           "preview_url": (ref_to_public_url[ref] or "")[:120]},
                )
            except DifyError as e:
                log.error(
                    "dify 图片上传失败: stem=%s ref=%s err=%s",
                    stem, ref, e,
                )
                if e.status_code == 401:
                    # 整个 /files/upload 端点对此 App Key 关闭，降级到公网 URL 模式
                    upload_blocked = True
                    log.warning(
                        "dify /files/upload 401 — 后续图片不再尝试上传，降级到公网 URL 策略",
                        extra={"step": "dify", "status": "upload_blocked", "stem": stem},
                    )
                # 401 之外的其他失败：本图跳过，ref 不进入 ref_to_public_url，段里保持相对路径
            except ValueError as e:
                # app_api_key 未配置时 client.upload_file 抛 ValueError
                log.warning("dify app_api_key 未配置，降级到公网 URL: %s", e)
                upload_blocked = True
            else:
                # ★ 走到这里说明 /files/upload 成功，log 已在分支内部打印
                pass

            # ★ 兜底：当 /files/upload 失败（被 401 阻断 / ValueError / 其他异常）
            #   但 image_host 公网 URL 已配置时，仍在 content 里写永久公网 URL，
            #   保证聊天召回时图片可显示（attachment_ids 这张图就空缺，编辑器预览不出来，
            #   但聊天召回仍能显示图片 → 优于完全不显示）。
            if ref not in ref_to_public_url and use_public_url_in_content:
                public_url = _build_public_url(stem, ref)
                if public_url:
                    log.info(
                        "dify: /files/upload 失败但有公网 URL，content 写公网 URL: ref=%s",
                        ref,
                    )
                    ref_to_public_url[ref] = public_url
            continue

        # 策略 B：公网 URL 模式
        if use_public_url_strategy:
            # ★ 2026-08-04：OSS 后端时优先用预上传的永久外链
            if ref in oss_ref_to_url:
                ref_to_public_url[ref] = oss_ref_to_url[ref]
            else:
                ref_to_public_url[ref] = _build_public_url(stem, ref)
            continue

        # 策略 C：没有任何图片托管配置，段里保持 `![](images/xxx.jpg)` 相对路径
        log.debug("图片托管未配置，段里保持相对路径: stem=%s ref=%s", stem, ref)

    # 4) 把 file_id 绑到 attachment_ids（仅 /files/upload 策略 A 拿到 file_id 时有意义）
    # ★ 2026-07-31：去掉 10 张附件上限截断。
    #   用户的实测：跨段复制可预览 URL 到无图段，**也不显示预览**。
    #   原因：Dify 段里图片预览的渲染依赖 attachment_ids（Dify 编辑器从
    #   attachment_ids 拿 file_id → 拿签名 URL → 渲染）。
    #   段里 content 的 markdown 只是辅助信息，单独写 URL 不绑 attachment
    #   不会触发预览。
    #   因此：所有上传成功的图都必须进 attachment_ids，**不截断**。
    #   如果 Dify 端真的有硬限制（10 张），add_segments 会 400 报错，
    #   到时再针对性处理。
    for ci in chunk_infos:
        for ref in ci.image_refs:
            fid = ref_to_file_id.get(ref)
            if not fid or fid in ci.attachment_ids:
                continue
            ci.attachment_ids.append(fid)

    # 4.5) ★ 把 segment content 里的 `![image](images/xxx.jpg)` 替换为
    #       完整的公网 URL（Dify 自家或我们提供的），Dify 索引时拉取并内嵌。
    #       这是图片能在 Dify 段中显示的关键。
    if ref_to_public_url:
        for ci in chunk_infos:
            ci.content = _rewrite_image_refs_in_content(ci.content, ref_to_public_url)

    # 5) 创建 Dify 文档（占位 text，文档形态由 Dify 索引时识别）
    #    ★ 2026-08-12 修复：用极简占位文本，避免 Dify 自动索引生成的分段
    #      与 add_segments 创建的真实分段内容重复。
    #      之前用第一段 chunk 前 200 字符做占位，Dify 会自动切出 1 个分段，
    #      该分段内容与 chunk_001 高度重叠（用户看到的"分段1是分段2截断"问题）。
    placeholder_text = stem
    try:
        doc = client.create_document_by_text(name=stem, text=placeholder_text)
    except DifyError as e:
        return "", chunk_infos, f"创建 Dify 文档失败: {e}"
    if not doc.document_id:
        return "", chunk_infos, "Dify 文档创建成功但未返回 document_id"

    # 6) 等待 indexing 完成（否则 add_segments 会 404）
    try:
        client.wait_document_ready(doc.document_id)
    except DifyError as e:
        return doc.document_id, chunk_infos, f"等待 Dify 文档 indexing 超时: {e}"

    # 7) 批量 add_segments
    #    ★ 2026-07-31：Dify 端对单段 attachment_ids 有 10 张硬限制
    #      （SINGLE_CHUNK_ATTACHMENT_LIMIT 环境变量，默认 10，
    #      官方文档：「Exceeded maximum attachment limit of 10」）。
    #      用户实测 WST 809 文档 11+ 张图同段会 400。
    #      策略：
    #      1) 先按 chunk 真实 attachment_ids 提交（不截断，能完整预览就完整）
    #      2) 4xx 错误且 body 含 "attachment limit" → 自动截断到 10 张重试
    #         超出部分仍写在 content URL 里（不丢信息，只是预览不全）
    DIFY_MAX_ATTACHMENTS_PER_SEGMENT = 10
    seg_payloads: List[Dict[str, Any]] = []
    for ci in chunk_infos:
        item: Dict[str, Any] = {"content": ci.content}
        if ci.attachment_ids:
            item["attachment_ids"] = list(ci.attachment_ids)
        # ★ 2026-08-13：表格独立成段——添加 keywords 便于过滤和溯源
        if ci.chunk_type == "table":
            kw = ["table"]
            if ci.table_name:
                kw.append(ci.table_name)
            item["keywords"] = kw
        seg_payloads.append(item)
    try:
        created_segs = client.add_segments(doc.document_id, seg_payloads)
    except DifyError as e:
        if e.status_code == 400 and "attachment limit" in (e.body or "").lower():
            # Dify 端硬截断：把每段 attachment_ids 截到 10 张重试
            truncated_any = False
            for item, ci in zip(seg_payloads, chunk_infos):
                aids = item.get("attachment_ids") or []
                if len(aids) > DIFY_MAX_ATTACHMENTS_PER_SEGMENT:
                    dropped = len(aids) - DIFY_MAX_ATTACHMENTS_PER_SEGMENT
                    item["attachment_ids"] = aids[:DIFY_MAX_ATTACHMENTS_PER_SEGMENT]
                    truncated_any = True
                    # 同步回写 ci.attachment_ids（保持一致）
                    ci.attachment_ids = list(item["attachment_ids"])
                    log.warning(
                        "dify: 段 attachment_ids 超出 Dify 端 %d 上限，已截断到 %d（丢弃 %d 个，"
                        "这些图的 URL 仍写在 content 里，仅 Dify 编辑器预览不显示）",
                        DIFY_MAX_ATTACHMENTS_PER_SEGMENT,
                        DIFY_MAX_ATTACHMENTS_PER_SEGMENT,
                        dropped,
                        extra={
                            "step": "dify",
                            "status": "attachments_truncated_at_dify",
                            "stem": stem,
                            "chunk": ci.chunk_id,
                            "limit": DIFY_MAX_ATTACHMENTS_PER_SEGMENT,
                            "dropped": dropped,
                        },
                    )
            if not truncated_any:
                # 错误说 limit 但实际没超 —— 可能是其它参数问题
                return doc.document_id, chunk_infos, f"add_segments 失败: {e}"
            try:
                created_segs = client.add_segments(doc.document_id, seg_payloads)
            except DifyError as e2:
                return doc.document_id, chunk_infos, f"add_segments 失败（截断后仍报错）: {e2}"
        else:
            return doc.document_id, chunk_infos, f"add_segments 失败: {e}"

    # ★ 2026-08-12：诊断日志——检查 add_segments 实际创建了多少分段
    log.info(
        "dify add_segments 结果: stem=%s requested=%d created=%d",
        stem, len(seg_payloads), len(created_segs),
    )
    if len(created_segs) < len(seg_payloads):
        log.warning(
            "dify add_segments 丢失分段! stem=%s requested=%d created=%d missing=%d",
            stem, len(seg_payloads), len(created_segs),
            len(seg_payloads) - len(created_segs),
        )

    # 8) ★★★ 关键（2026-07-31 修复）：
    #   Dify Knowledge API 在 POST /segments 端点上**静默丢弃 attachment_ids**，
    #   必须再调一次 POST /segments/{id}（update_segment）才能让 attachments
    #   真正被持久化到 Dify 数据库，编辑器才能基于 attachments 渲染图片预览。
    #   按 index 顺序对齐：created_segs[i] 对应 chunk_infos[i]
    updated_attachment_count = 0
    for ci, seg in zip(chunk_infos, created_segs):
        if not ci.attachment_ids:
            continue
        seg_id = seg.segment_id
        if not seg_id:
            log.warning(
                "dify: 段 id 缺失，跳过 attachment_ids 持久化: stem=%s chunk=%s",
                stem, ci.chunk_id,
            )
            continue
        try:
            client.update_segment(
                doc.document_id,
                seg_id,
                attachment_ids=list(ci.attachment_ids),
            )
            updated_attachment_count += 1
        except DifyError as e:
            # 单段更新失败不应影响整体成功（其它段已经上传）
            log.error(
                "dify: update_segment 失败: stem=%s chunk=%s seg_id=%s err=%s",
                stem, ci.chunk_id, seg_id, e,
            )
    if updated_attachment_count > 0:
        log.info(
            "dify: attachment_ids 已通过 update_segment 持久化",
            extra={
                "step": "dify",
                "status": "attachments_persisted",
                "stem": stem,
                "count": updated_attachment_count,
            },
        )

    # 9) ★★★ 关键（2026-08-04 修复：Dify 索引重签覆盖 OSS URL）：
    #   现象：Dify 索引时（POST /segments 之后）会从 content 里的图片 URL 拉图、
    #         存到 Dify 自家存储、然后把 content 里的 URL 替换为 5min 签名的 Dify URL
    #         （控制参数：Dify 服务端 FILES_ACCESS_TIMEOUT，默认 300s）。
    #   影响：召回时拿到的 content 是 Dify 签名 URL，5 分钟后过期 → 图片 404 不显示。
    #   修复：再调一次 update_segment(content=ci.content) 把原始 OSS URL 写回去。
    #         - Dify 0.x/1.x：update_segment 不会重跑 clean task，写入即生效（content 恢复为 OSS URL）
    #         - 某些 Dify 版本：update_segment 也会触发重签 → 此修复无效
    #           兜底方案：把 content 里的 markdown 图片语法换成 <img src="..."> HTML 标签，
    #                    Dify 的 get_sign_content() 正则只匹配 ![...](...)，不匹配 <img>，
    #                    即可彻底绕过 Dify 重签。配置开关：RAG_DIFY_USE_HTML_IMG_TAG=1
    restored_content_count = 0
    _img_re_for_skip = re.compile(r"!\[[^\]]*\]\(")
    for ci, seg in zip(chunk_infos, created_segs):
        seg_id = seg.segment_id
        if not seg_id or not ci.content:
            continue
        # ★ 内容里必须含图片才值得二次重写（纯文本段重写无意义，省一次 HTTP）
        if not _img_re_for_skip.search(ci.content):
            continue
        try:
            client.update_segment(
                doc.document_id,
                seg_id,
                content=ci.content,
            )
            restored_content_count += 1
        except DifyError as e:
            log.error(
                "dify: 二次 update_segment(content) 失败（OSS URL 可能被 Dify 重签覆盖）: "
                "stem=%s chunk=%s seg_id=%s err=%s",
                stem, ci.chunk_id, seg_id, e,
            )
    if restored_content_count > 0:
        log.info(
            "dify: content 已通过 update_segment 二次写入（覆盖 Dify 重签的 5min 签名 URL）",
            extra={
                "step": "dify",
                "status": "content_restored",
                "stem": stem,
                "count": restored_content_count,
            },
        )

    # 10) ★★★ 文档元数据写入（2026-08 新增）：
    #   从 doc_metadata 表读取的文档元数据（doc_type_primary / topic_primary 等）
    #   通过 Dify Metadata API 设置到文档上。
    #   field_map 和 doc_meta_cache 由 upload_all_docs 传入（避免每文档重复读元数据表）。
    if field_map and doc_meta_cache:
        op = doc_metadata.build_metadata_operation(
            doc.document_id, stem, field_map, doc_meta_cache,
        )
        if op:
            try:
                client.batch_update_document_metadata([op])
                log.info(
                    "dify: 文档元数据设置成功",
                    extra={
                        "step": "dify",
                        "status": "metadata_set",
                        "stem": stem,
                        "fields": len(op["metadata_list"]),
                    },
                )
            except DifyError as e:
                log.warning(
                    "dify: 文档元数据设置失败（不影响入库成功）: stem=%s err=%s",
                    stem, e,
                )

    return doc.document_id, chunk_infos, None


# ============ 主入口 ============


def upload_all_docs(
    *,
    dry_run: bool = False,
    force: bool = False,
    client: Optional[DifyClient] = None,
    target_stems: Optional[List[str]] = None,
) -> DifyUploadReport:
    """扫描 data/chunks/ 下所有目录，逐个入库到 Dify。

    ★ 2026-08 新增 target_stems 白名单（单文件上传 + 一键入库）：
        - target_stems=None（默认）：处理 data/chunks/ + data/output/ 下所有目录
        - target_stems=[stem1, stem2, ...]：只处理这些 stem 对应的目录
          用于「单文件上传 + 一键入库」场景——只入库这一个文件。
    """
    started = time.perf_counter()
    log.info(
        "dify upload all start",
        extra={"step": "dify", "status": "start", "dry_run": dry_run,
               "target_stems": target_stems},
    )

    chunk_dirs = _list_chunk_dirs(target_stems=target_stems)
    actions: List[DifyActionRecord] = []
    uploaded = skipped = failed = 0

    # 加载 manifest（写 dify_doc_id / dify_status 用）
    manifest = _load_manifest_index()
    rows_to_write: List[Any] = []

    # ★ 加载文档元数据 + 确保 Dify 元数据字段存在
    field_map: Optional[Dict[str, Dict[str, Any]]] = None
    doc_meta_cache: Optional[Dict[str, Dict[str, Any]]] = None
    if not dry_run and chunk_dirs:
        try:
            doc_meta_cache = doc_metadata.load_doc_metadata()
            if doc_meta_cache:
                c0 = client or DifyClient()
                field_map = doc_metadata.ensure_metadata_fields(c0)
                log.info(
                    "dify: 文档元数据已加载，字段已同步",
                    extra={"step": "dify", "status": "metadata_loaded",
                           "docs_with_meta": len(doc_meta_cache),
                           "fields": len(field_map)},
                )
        except Exception as meta_exc:  # noqa: BLE001
            log.warning(
                "dify: 文档元数据加载失败（不影响入库流程）: %s",
                meta_exc,
            )
            field_map = None
            doc_meta_cache = None

    for chunks_dir in chunk_dirs:
        stem = chunks_dir.name
        t0 = time.perf_counter()

        # 1) 幂等检查
        existing_row = _find_manifest_row_by_stem(manifest, stem)
        if existing_row is not None and not force:
            existing_status = (existing_row.dify_status or "").strip()
            if existing_status == "done":
                skipped += 1
                actions.append(
                    DifyActionRecord(
                        stem=stem,
                        action="skipped_done",
                        dify_doc_id=existing_row.dify_doc_id,
                        chunks_dir=str(chunks_dir.resolve()),
                        note="manifest dify_status=done，已处理",
                        duration_ms=int((time.perf_counter() - t0) * 1000),
                    )
                )
                continue

        if dry_run:
            actions.append(
                DifyActionRecord(
                    stem=stem,
                    action="dry_run",
                    chunks_dir=str(chunks_dir.resolve()),
                    note="dry_run 模式，不实际调用 Dify",
                )
            )
            continue

        # 2) 实际入库
        # 2a) 预复制 chunks/ → output/（让 Dify 拉图的 URL 始终指向 output/）
        try:
            _, was_copied = _stage_for_upload(chunks_dir)
        except Exception as stage_exc:  # noqa: BLE001
            log.error(
                "dify: 预复制 chunks → output 失败: stem=%s err=%s",
                stem, stage_exc,
            )
            failed += 1
            actions.append(
                DifyActionRecord(
                    stem=stem,
                    action="failed",
                    chunks_dir=str(chunks_dir.resolve()),
                    error=f"预复制失败: {stage_exc}",
                    duration_ms=int((time.perf_counter() - t0) * 1000),
                )
            )
            continue

        # 如果没有传 client，则现场创建
        own_client = client is None
        c = client or DifyClient()
        try:
            dify_doc_id, _chunk_infos, err = upload_one_doc(
                chunks_dir, c, force=force,
                field_map=field_map, doc_meta_cache=doc_meta_cache,
            )
        except Exception as e:  # noqa: BLE001
            log.exception("dify 上传单文档失败: stem=%s", stem)
            err = f"未捕获异常: {e}"
            dify_doc_id = ""
        finally:
            if own_client:
                # httpx.Client 在内部已 close（with 语句），这里无需显式释放
                pass

        duration_ms = int((time.perf_counter() - t0) * 1000)

        # 2b) 清理预复制（成功→删 chunks/ 留 output/，失败→删 output/ 留 chunks/）
        cleanup_note = ""
        try:
            cleanup_note = _cleanup_after_upload(chunks_dir, was_copied, err is None)
        except Exception as cleanup_exc:  # noqa: BLE001
            log.error(
                "dify: 清理预复制失败: stem=%s err=%s",
                stem, cleanup_exc,
            )
            cleanup_note = f"清理异常（数据可能重复）: {cleanup_exc}"

        if err is None:
            uploaded += 1
            status_str = "done"
            actions.append(
                DifyActionRecord(
                    stem=stem,
                    action="uploaded",
                    dify_doc_id=dify_doc_id,
                    chunks_dir=str(chunks_dir.resolve()),
                    note=cleanup_note or None,
                    duration_ms=duration_ms,
                )
            )
        else:
            failed += 1
            status_str = "error"
            actions.append(
                DifyActionRecord(
                    stem=stem,
                    action="failed",
                    dify_doc_id=dify_doc_id or None,
                    chunks_dir=str(chunks_dir.resolve()),
                    error=err,
                    duration_ms=duration_ms,
                )
            )

        # 写 manifest
        # ★ 2026-08-20 修复：manifest 中不存在该文档行时（如 manifest 被外部脚本
        # 重建/清空），入库成功后也应新建一行写回 dify_doc_id / dify_status，
        # 否则前端 ManifestTable 永远看不到已入库文档。
        now_iso = manifest_store.now_iso()
        if existing_row is not None:
            update_kwargs: Dict[str, Any] = {
                "dify_doc_id": dify_doc_id or existing_row.dify_doc_id or None,
                "dify_status": status_str,
                "error_msg": err or existing_row.error_msg,
                "update_time": now_iso,
            }
            # ★ 成功后更新 chunks 列为新路径 "output/{stem}" + status="done"
            if err is None:
                update_kwargs["chunks"] = f"output/{stem}"
                update_kwargs["status"] = "done"
            updated = existing_row.model_copy(update=update_kwargs)
            rows_to_write.append(updated)
            manifest[existing_row.filename] = updated
        else:
            new_row = ManifestRow(
                filename=stem,
                status="done" if err is None else "error",
                chunks=f"output/{stem}" if err is None else None,
                dify_doc_id=dify_doc_id or None,
                dify_status=status_str,
                error_msg=err or None,
                create_time=now_iso,
                update_time=now_iso,
                process_status="已入库" if err is None else "入库失败",
                process_note="(manifest 无原记录，入库成功后自动补建)" if err is None else None,
            )
            rows_to_write.append(new_row)
            manifest[new_row.filename] = new_row

    # 3) 批量写 manifest
    if rows_to_write:
        try:
            manifest_store.bulk_upsert(rows_to_write)
        except Exception as e:  # noqa: BLE001
            log.exception("dify manifest 写盘失败")
            # 不抛，避免已经入库的文档被回滚；只记录

    duration_ms = int((time.perf_counter() - started) * 1000)
    report = DifyUploadReport(
        dry_run=dry_run,
        scanned=len(chunk_dirs),
        uploaded=uploaded,
        skipped_done=skipped,
        failed=failed,
        actions=actions,
        api_url=settings.dify_api_url,
        dataset_id=settings.dify_dataset_id,
    )
    log.info(
        "dify upload all done",
        extra={
            "step": "dify",
            "status": "done",
            "duration_ms": duration_ms,
            "scanned": report.scanned,
            "uploaded": report.uploaded,
            "skipped": report.skipped_done,
            "failed": report.failed,
        },
    )
    return report


def _find_manifest_row_by_stem(manifest: Dict[str, Any], stem: str) -> Optional[Any]:
    """根据 chunks 目录的 stem 在 manifest 里找对应行。

    匹配规则：
    1) 先按 chunks 列完全匹配
    2) 再按 chunks 列的 basename（即 directory 名）
    3) 再按 filename stem 包含 stem
    """
    # 1) chunks == stem
    for fname, row in manifest.items():
        if (row.chunks or "").strip() == stem:
            return row
    # 2) chunks 末尾段 == stem
    for fname, row in manifest.items():
        c = (row.chunks or "").replace("\\", "/").rstrip("/")
        if c and Path(c).name == stem:
            return row
    # 3) filename stem 包含 stem（兜底）
    for fname, row in manifest.items():
        if Path(fname).stem == stem:
            return row
    return None
