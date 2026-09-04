"""plan.md §3.2 — 调用 MinerU API 解析。

核心流程（以 manifest 表 + pending/ 为主线）：
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
import re
import shutil
import time
from html.parser import HTMLParser
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


def _resolve_pending_path(name_in_manifest: str) -> Optional[Path]:
    """在 pending/ 找清单中的「文件名称」。

    1) 精确匹配
    2) 按 allowed_extensions 顺序追加扩展名（与 §3.1 行为一致）
    3) ★ stem 模糊匹配：把 manifest 中的 stem 与 pending/ 中所有文件做 stem 比较。
       场景：用户把 .doc 转成 .docx 后放回 pending/，但 manifest filename 还是 .doc。

    返回值：用 Path（真实找到的文件）。调用方负责在 stem 匹配时同步 manifest.filename。
    """
    if not settings.pending_dir.exists():
        return None
    exact = settings.pending_dir / name_in_manifest
    if exact.is_file():
        return exact
    for ext in settings.allowed_extensions:
        candidate = settings.pending_dir / f"{name_in_manifest}{ext}"
        if candidate.is_file():
            return candidate

    # ★ stem 模糊匹配：去掉 .doc 之类后缀，在 pending/ 中找同 stem 的任意文件
    # 防止类似 "name.doc" vs "name.docx" 在用户手动转换扩展名后找不到
    name_stem = Path(name_in_manifest).stem
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
    manifest_store.upsert(new_row)
    log.info(
        "manifest filename 已同步",
        extra={
            "step": "parse",
            "status": "filename_synced",
            "old_filename": old_filename,
            "new_filename": new_filename,
        },
    )


# ────────────────────────────────────────────────────────────────────────────
# 本地解析（MinerU 不支持的文档类型：.html / .htm）
# .xlsx 由 MinerU 处理（见 MinerUError 分支中的本地兜底逻辑）。
# 产物只有 markdown（无 content_list_v2.json），chunker 走 md-only 兜底切分。
# ────────────────────────────────────────────────────────────────────────────
_LOCAL_PARSE_EXTS = {".html", ".htm"}


def _extract_xlsx_text(src: Path) -> str:
    """用 openpyxl 读 .xlsx 全部 sheet → markdown 表格文本。"""
    try:
        import openpyxl
    except ImportError as e:
        raise RuntimeError("解析 .xlsx 需要 openpyxl（pip install openpyxl）") from e
    wb = openpyxl.load_workbook(src, read_only=True, data_only=True)
    parts: List[str] = []
    try:
        for ws in wb.worksheets:
            parts.append(f"\n## {ws.title}\n")
            rows = []
            for r in ws.iter_rows(values_only=True):
                cells = [
                    "" if c is None else str(c).replace("|", "\\|").replace("\r", " ").replace("\n", " ")
                    for c in r
                ]
                if any(c.strip() for c in cells):
                    rows.append(cells)
            if not rows:
                continue
            ncols = max(len(r) for r in rows)
            rows = [r + [""] * (ncols - len(r)) for r in rows]
            parts.append("| " + " | ".join(rows[0]) + " |")
            parts.append("|" + "---|" * ncols)
            for r in rows[1:]:
                parts.append("| " + " | ".join(r) + " |")
    finally:
        wb.close()
    text = "\n".join(parts).strip()
    if not text:
        raise RuntimeError(f"{src.name} 没有可提取的单元格内容")
    return text


class _HTMLToMarkdownParser(HTMLParser):
    """标准库实现：HTML → 简单 markdown（标题/段落/列表/表格/链接/图片 alt）。"""

    # 需要「配对闭合」的跳过容器（有文本内容，必须精确进出）
    _SKIP_TAGS = {"script", "style", "noscript", "head", "title"}
    # 无文本内容、通常不自闭合的自闭合标签：只忽略自身、不计入跳过深度。
    # ★ 2026-09：meta/link 若也按配对标签 +1 深度，XHTML 风格页面（如 SIFIC）
    #   里它们往往没有 </meta>、</link>，会让 _skip_depth 越叠越高减不回来，
    #   结果整个 <body> 的正文都被当作「跳过区域」→ 本地解析报没有可提取文本。
    _VOID_SKIP_TAGS = {"meta", "link"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._out: List[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list) -> None:  # noqa: D102
        t = tag.lower()
        if t in self._VOID_SKIP_TAGS:
            return
        if t in self._SKIP_TAGS:
            self._skip_depth += 1
        if self._skip_depth:
            return
        if t in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self._out.append("\n\n" + "#" * int(t[1]) + " ")
        elif t == "p":
            self._out.append("\n\n")
        elif t == "br":
            self._out.append("\n")
        elif t == "li":
            self._out.append("\n- ")
        elif t == "tr":
            self._out.append("\n")
        elif t in ("td", "th"):
            self._out.append(" | ")
        elif t in ("strong", "b"):
            self._out.append("**")
        elif t in ("em", "i"):
            self._out.append("*")
        elif t == "a":
            self._out.append("[")
        elif t == "img":
            # ★ 2026-09 HTML 入库不丢图：输出 markdown 图片语法并保留 src，
            #   解析后由 _download_html_images 下载到 images/ 并改写为本地引用。
            #   空 alt 兜底 "image"（markdown 语法与 Dify 附件上传都要求非空 alt）。
            a = {k.lower(): (v or "") for k, v in attrs}
            src = (a.get("src") or "").strip()
            alt = (a.get("alt") or "").strip()
            if src.startswith(("http://", "https://")):
                self._out.append(f"\n\n![{alt or 'image'}]({src})")
            elif alt:
                # 无 src 或相对地址（相对地址需 base_url，无法离线解析）：保留 alt 语义
                self._out.append(f"[图片: {alt}]")

    def handle_endtag(self, tag: str) -> None:  # noqa: D102
        t = tag.lower()
        if t in self._VOID_SKIP_TAGS:
            return
        if t in self._SKIP_TAGS and self._skip_depth:
            self._skip_depth -= 1
        if self._skip_depth:
            return
        if t in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self._out.append("\n")
        elif t in ("strong", "b"):
            self._out.append("**")
        elif t in ("em", "i"):
            self._out.append("*")
        elif t == "a":
            self._out.append("]")

    def handle_data(self, data: str) -> None:  # noqa: D102
        if not self._skip_depth:
            self._out.append(data)


def _extract_html_text(src: Path) -> str:
    """标准库 html.parser 提取 HTML 可见文本 → 简单 markdown。"""
    parser = _HTMLToMarkdownParser()
    try:
        parser.feed(src.read_text(encoding="utf-8", errors="replace"))
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"HTML 解析失败: {e}") from e
    parser.close()
    text = re.sub(r"\n{3,}", "\n\n", "".join(parser._out)).strip()
    if not text:
        raise RuntimeError(f"{src.name} 没有可提取的文本内容")
    return text


def _download_html_images(md_text: str, images_dir: Path) -> str:
    """把 HTML 本地解析出的 md 中 `![alt](https://…)` 图片下载到 images_dir。

    ★ 2026-09 HTML 入库不丢图：
        此前 HTML 走本地解析只有文本、图片全部丢失（image_count=0）。
        本函数把远程图片下载为 parsed/{stem}/images/img_NNN_xxx.jpg，
        并把 md 引用改写为 images/xxx —— 与 MinerU 产物一致，chunker 会把
        images/ 复制进 chunk、dify 上传，整条既有图片链路即可复用。

    失败兜底：下载失败/非图片 → 退化为 `[图片: alt]` 占位文本（保留 alt 语义，
    绝不因网络问题让整页解析失败）。返回改写后的 markdown。
    """
    from urllib.parse import urlsplit

    # 只处理 HTML 解析阶段保留的远程图引用；无图页面直接原样返回
    if "![" not in md_text:
        return md_text

    seen_url: Dict[str, str] = {}  # url → "images/xxx"
    failed_url: set = set()
    downloaded = 0

    def _sub(m: "re.Match[str]") -> str:
        nonlocal downloaded
        alt = m.group(1).strip()
        url = m.group(2).strip()
        if url in seen_url:
            return f"![{alt or 'image'}]({seen_url[url]})"
        if url in failed_url:
            return f"[图片: {alt}]" if alt else "[图片]"
        data = _fetch_html_image(url)
        if data is None:
            failed_url.add(url)
            log.info(
                "HTML 图片下载失败，保留占位: %s",
                url,
                extra={"step": "parse", "status": "img_failed"},
            )
            return f"[图片: {alt}]" if alt else "[图片]"
        ext = _image_ext(data, url)
        # slug 取 URL 文件名的 stem（去掉扩展与 !xxx 变体后缀），避免出现 xxx.jpg.jpg
        base = urlsplit(url).path.rsplit("/", 1)[-1].split("!", 1)[0]
        slug = re.sub(r"[^\w.\-]+", "_", Path(base).stem)[:60].strip("._") or "img"
        images_dir.mkdir(parents=True, exist_ok=True)
        name = f"img_{downloaded + 1:03d}_{slug}{ext}"
        (images_dir / name).write_bytes(data)
        seen_url[url] = f"images/{name}"
        downloaded += 1
        return f"![{alt or 'image'}]({seen_url[url]})"

    text = _HTML_MD_IMG_RE.sub(_sub, md_text)
    if downloaded:
        log.info(
            "HTML 本地解析下载图片 %d 张到 %s",
            downloaded, images_dir,
            extra={"step": "parse", "status": "img_ok"},
        )
    return text


_HTML_MD_IMG_RE = re.compile(r"!\[([^\]]*)\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
_HTML_IMG_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
_HTML_IMG_TIMEOUT = (8, 25)  # (connect, read) 秒
_HTML_IMG_MAX_BYTES = 15 * 1024 * 1024


def _fetch_html_image(url: str) -> Optional[bytes]:
    """下载单张网页图片；失败/超时/超大/非图片一律返回 None（静默，不影响解析）。"""
    import httpx

    try:
        with httpx.Client(
            headers={"User-Agent": _HTML_IMG_UA},
            timeout=_HTML_IMG_TIMEOUT,
            follow_redirects=True,
        ) as client:
            with client.stream("GET", url) as resp:
                resp.raise_for_status()
                ct = (resp.headers.get("content-type") or "").lower().split(";")[0].strip()
                if ct and not ct.startswith("image/"):
                    return None  # 源站返回的是错误页 / JSON / 压缩包等
                data = bytearray()
                for chunk in resp.iter_bytes(64 * 1024):
                    data += chunk
                    if len(data) > _HTML_IMG_MAX_BYTES:
                        return None
                return bytes(data)
    except Exception:  # noqa: BLE001  下载失败静默，整页文本解析不受影响
        return None


def _image_ext(data: bytes, url: str) -> str:
    """按内容 magic（优于 Content-Type / URL 扩展名）判断图片扩展名。"""
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return ".gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    if data.startswith(b"BM"):
        return ".bmp"
    if data[4:8] == b"ftyp" and b"avif" in data[8:16]:
        return ".avif"
    # 兜底：URL 自带扩展名（去掉 !xxx 变体后缀，如 xxx.jpg!1920）
    base = (url.rsplit("?", 1)[0].rsplit("/", 1)[-1] or "").split("!", 1)[0]
    ext = Path(base).suffix.lower()
    return ext if ext in {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".avif"} else ".jpg"


def _parse_local_document(src: Path, parsed_dir: Path) -> Path:
    """本地解析 MinerU 不支持的文档类型（.html / .htm）。

    也支持 .xlsx（openpyxl 提取表格），用作 MinerU 调用失败时的本地兜底。
    提取文本 → 生成 parsed/{stem}/{stem}.md（HTML 另把页面图片下载到
    parsed/{stem}/images/，md 用 `![alt](images/xxx)` 引用），返回 md 路径。
    """
    parsed_dir.mkdir(parents=True, exist_ok=True)
    if src.suffix.lower() == ".xlsx":
        text = _extract_xlsx_text(src)
    else:  # .html / .htm
        text = _extract_html_text(src)
        # 图片入库：远程图 → parsed/{stem}/images/，md 引用改写为本地相对路径
        text = _download_html_images(text, parsed_dir / "images")
    md_path = parsed_dir / f"{_safe_stem(src.name)}.md"
    md_path.write_text(text, encoding="utf-8")
    return md_path


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

    manifest: Dict[str, ManifestRow] = manifest_store.load()

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

        # 4) 本地解析（.xlsx / .html / .htm：MinerU 不支持，本地提取文本）
        if src.suffix.lower() in _LOCAL_PARSE_EXTS:
            _set_progress(src.name, 0, "本地解析中...", "parsing")
            try:
                parsed_dir = settings.parsed_dir / _safe_stem(src.name)
                md_path = _parse_local_document(src, parsed_dir)
                _set_progress(src.name, 100, "解析完成(本地)", "done")
                parsed_count += 1
                _write_manifest_row(
                    row,
                    parse_text=f"{str(parsed_dir.resolve())} [本地解析]",
                    sys_status="parsing_done",
                    err=None,
                )
                log.info(
                    "parse ok (本地解析)",
                    extra={
                        "step": "parse",
                        "status": "parsed",
                        "file_name": src.name,
                        "parse_dir": str(parsed_dir),
                        "fallback_used": False,
                        "duration_ms": int((time.perf_counter() - t0) * 1000),
                    },
                )
                actions.append(
                    ParseActionRecord(
                        filename=src.name,
                        action=ParseAction.PARSED,
                        parse_dir=str(parsed_dir.resolve()),
                        md=str(md_path.resolve()),
                        duration_ms=int((time.perf_counter() - t0) * 1000),
                        attempts=1,
                    )
                )
            except Exception as e:  # noqa: BLE001
                failed_count += 1
                err_text = f"本地解析失败: {e}"
                log.error(
                    "parse 失败（本地解析）",
                    extra={
                        "step": "parse",
                        "status": "failed",
                        "file_name": src.name,
                        "error_msg": str(e),
                        "duration_ms": int((time.perf_counter() - t0) * 1000),
                    },
                )
                _move_to_error(src, err_text)
                _write_manifest_row(row, parse_text=err_text, sys_status="error", err=err_text)
                actions.append(
                    ParseActionRecord(
                        filename=src.name,
                        action=ParseAction.PARSE_FAILED,
                        error=err_text,
                        duration_ms=int((time.perf_counter() - t0) * 1000),
                    )
                )
            continue

        # 5) 实际调 API（MinerU）
        _set_progress(src.name, 0, "正在调用 MinerU API...", "parsing")
        try:
            result = client.parse_file(src, settings.parsed_dir / _safe_stem(src.name))
            _set_progress(src.name, 100, "解析完成", "done")
            parsed_count += 1

            # ★ 质量检查（仅 .pdf）：MinerU 解析成功但产物过少 → 启动 fallback 链
            #   Tier 1: PyMuPDF 渲染 PDF 为图片 → MinerU vlm-engine 读图
            #   Tier 2: PyMuPDF 纯文本提取
            #   非 PDF（含 .xlsx）不做此检查：PyMuPDF fallback 仅适用于 PDF，
            #   小表格等「内容少但正常」的文档不应被误判为 trivial。
            fallback_used = False
            fallback_backend = None
            is_trivial = False
            if src.suffix.lower() == ".pdf":
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

            expected_parse_dir = settings.parsed_dir / _safe_stem(src.name)

            # ★ 2026-08：.xlsx 本地兜底（MinerU 调用失败时，openpyxl 提取表格为 markdown）
            if src.suffix.lower() == ".xlsx":
                try:
                    md_path = _parse_local_document(src, expected_parse_dir)
                    parsed_count += 1
                    parse_text = f"{str(expected_parse_dir.resolve())} [本地解析]"
                    _write_manifest_row(
                        row,
                        parse_text=parse_text,
                        sys_status="parsing_done",
                        err=f"mineru 调用失败，已用本地解析兜底: {err_text[:200]}",
                    )
                    log.warning(
                        "MinerU 调用失败但 xlsx 本地解析兜底成功 (file=%s)",
                        src.name,
                        extra={
                            "step": "parse",
                            "status": "fallback_ok",
                            "file_name": src.name,
                            "error_msg": err_text[:200],
                        },
                    )
                    try:
                        actions.append(
                            ParseActionRecord(
                                filename=src.name,
                                action=ParseAction.PARSED,
                                parse_dir=str(expected_parse_dir.resolve()),
                                md=str(md_path.resolve()),
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
                except Exception as fb_exc:  # noqa: BLE001
                    # 本地兜底也失败 → 继续走 error/ 分支
                    err_text = f"{err_text}（本地解析兜底也失败: {fb_exc}）"

            # ★ PyMuPDF fallback 自动救援：MinerU 4xx/5xx/网络错误时，
            # 对 .pdf 走 Tier 1/2/3 链，能恢复则不入 error/。
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
    manifest_store.upsert(new_row)
