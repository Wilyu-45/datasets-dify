"""知识库外延：网站抓取（2026-08 新增）。

流程（任务式，两步确认）：
    1. 抓取（POST /api/webscrape/run）：先选配置方案（决定「抓取网站 URL」
       白名单），逐 URL 分类处理：
           - 网页内容（HTML）→ 正文转为 Markdown，落到 data/webscrape/{task_id}/
           - 附件文件（PDF/DOCX 等链接）→ 下载原文件到 data/webscrape/{task_id}/
       此阶段不写 manifest、不入库，生成「待确认任务」。
    2. 确认（POST /api/webscrape/task/{id}/confirm）：人在预览页确认内容后，
       把选中的项落到正式区（Markdown → parsed/{stem}/；附件 → pending/）并
       登记 manifest，再走 chunk → dify 流水线（附件的 parse 阶段由 MinerU 解析）。

设计要点：
    - 不引入新依赖：抓取用 httpx（requirements 已有），HTML→Markdown 用标准库 html.parser
    - 每个 URL 独立 try/except：1 个失败不影响其他
    - 附件识别：URL 扩展名优先，命中不了再看 Content-Type（文档类 MIME）
    - 下载上限 50MB；正文截断 200_000 字符，防超大内容拖垮预览与入库
"""

from __future__ import annotations

import hashlib
import logging
import re
import uuid
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import unquote, urljoin, urlparse

import httpx

from app.config import settings

log = logging.getLogger("ragsystem.webscraper")

# ---- 抓取行为常量 ----
WEBSCRAPE_DIRNAME = "webscrape"              # data/webscrape/{task_id}/ 临时区
WEBSCRAPE_TIMEOUT_SECONDS = 30               # 单页抓取超时
WEBSCRAPE_MAX_CHARS = 200_000                # 单页正文截断上限
WEBSCRAPE_STEM_MAX_CHARS = 60                # 由标题生成的 stem 最大长度
WEBSCRAPE_MAX_DOWNLOAD_BYTES = 50 * 1024 * 1024  # 附件下载上限 50MB
TASK_STATUS_PENDING = "pending"              # 已抓取、待确认
TASK_STATUS_CONFIRMED = "confirmed"          # 已确认并触发流水线
TASK_STATUS_DONE = "done"                    # 流水线完成（含部分失败）
TASK_STATUS_CANCELLED = "cancelled"

# 视为附件文件的 URL 扩展名（大小写不敏感）
ATTACHMENT_EXTS = {
    ".pdf", ".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx", ".csv",
    ".txt", ".md", ".rtf", ".wps", ".et", ".dps", ".eml", ".msg",
    ".zip", ".rar", ".7z", ".tar", ".gz",
}
# 文档类 Content-Type 前缀（URL 无扩展名时据此判定附件）
ATTACHMENT_MIME_PREFIXES = (
    "application/pdf",
    "application/msword",
    "application/vnd.ms-word",
    "application/vnd.ms-excel",
    "application/vnd.ms-powerpoint",
    "application/vnd.openxmlformats-officedocument",
    "application/vnd.visio",
    "application/octet-stream",
    "application/x-zip-compressed",
    "application/zip",
    "application/x-rar-compressed",
    "text/csv",
    "text/plain",
)

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

_HEADERS = {
    "User-Agent": _USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}


def _collapse_ws(text: str) -> str:
    """合并连续空白为单个空格。"""
    return re.sub(r"[ \t\r\n\f\v]+", " ", text) if text else text


def _abs_url(url: str, base: str) -> str:
    """把相对链接补全为绝对链接（base 为页面 URL）。"""
    if not base or url.startswith(("http://", "https://", "//", "mailto:", "tel:")):
        return url if not url.startswith("//") else "https:" + url
    try:
        return urljoin(base, url)
    except ValueError:
        return url


# ============ HTML → Markdown ============


class HTMLToMarkdown(HTMLParser):
    """把网页 HTML 转为近似 Markdown（面向知识库正文，非完全保真）。

    支持的标签子集：
        h1-h6 / p / br / a / strong / b / em / i / ul / ol / li
        code / pre / blockquote / table / tr / td / th / img / hr / div / section
    忽略的区域：script / style / nav / footer / aside / form / iframe / noscript
    （这些标签内部不输出；span 等行内标签的文本保留）

    行为约定：
        - 块级标签开始/结束处保证换行，行内标签只追加文本
        - 连续空白压缩为单个空格（pre 内保留原样）
        - 链接 [text](href)，图片 ![alt](src)，表格渲染为 Markdown 表格
        - 列表嵌套按 2 空格缩进
    """

    # 完全忽略内部内容的标签
    SKIP_TAGS = {
        "script", "style", "nav", "footer", "aside", "form",
        "iframe", "noscript", "head", "svg", "canvas",
    }

    def __init__(self, base_url: str = "") -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.out: List[str] = []                 # 输出行列表
        self.skip_depth = 0                      # >0 时不输出（SKIP_TAGS 内部）
        self.list_stack: List[str] = []          # 列表类型栈（ul/ol）
        self.in_pre = False                      # pre 内文本原样保留
        self.pre_buf: List[str] = []
        self.table_rows: List[List[str]] = []    # 表格数据（行 → 单元格文本列表）
        self._in_table = False
        self._cur_row: Optional[List[str]] = None     # 当前行单元格列表
        self._cur_cell: Optional[List[str]] = None    # 当前单元格文本片段
        self._href_stack: List[str] = []
        self.title_text: Optional[str] = None    # <title> 文本（页面标题候选）
        self._in_title = False

    # ---- 输出控制 ----

    def _ensure_newline(self) -> None:
        """当前输出不以空行结尾时补一个空行（用于块级元素分隔）。"""
        if not self.out:
            return
        if self.out[-1] != "":
            self.out.append("")

    def _append_to_out(self, text: str) -> None:
        if not self.out:
            self.out.append("")
        self.out[-1] += text

    # ---- HTMLParser 回调 ----

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:  # noqa: C901
        attr_map = {k.lower(): (v or "") for k, v in attrs}

        if tag in self.SKIP_TAGS:
            self.skip_depth += 1
            return

        if tag == "title":
            self._in_title = True
            return

        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self._ensure_newline()
            self.out.append("#" * int(tag[1]) + " ")
        elif tag == "br":
            self._ensure_newline()
        elif tag == "hr":
            self._ensure_newline()
            self.out.append("---")
            self._ensure_newline()
        elif tag in ("ul", "ol"):
            self._ensure_newline()
            self.list_stack.append(tag)
        elif tag == "li":
            self._ensure_newline()
            indent = "  " * max(0, len(self.list_stack) - 1)
            prefix = "- " if not self.list_stack or self.list_stack[-1] == "ul" else "1. "
            self._append_to_out(f"{indent}{prefix}")
        elif tag == "blockquote":
            self._ensure_newline()
            self._append_to_out("> ")
        elif tag == "pre":
            self._ensure_newline()
            self.out.append("```")
            self.in_pre = True
            self.pre_buf = []
        elif tag == "code" and not self.in_pre:
            self._append_to_out("`")
        elif tag == "table":
            self._ensure_newline()
            self._in_table = True
            self.table_rows = []
            self._cur_row = None
            self._cur_cell = None
        elif tag == "tr":
            self._cur_row = []
            self._cur_cell = None
        elif tag in ("td", "th"):
            self._cur_cell = []
        elif tag == "a":
            self._append_to_out("[")
            self._href_stack.append(attr_map.get("href", ""))
        elif tag == "img":
            src = attr_map.get("src", "")
            alt = attr_map.get("alt", "")
            if src and not src.startswith(("data:", "javascript:")):
                self._append_to_out(f"![{alt}]({_abs_url(src, self.base_url)})")
        elif tag in ("strong", "b"):
            self._append_to_out("**")
        elif tag in ("em", "i"):
            self._append_to_out("*")
        elif tag == "p":
            self._ensure_newline()

    def handle_startendtag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        """自闭合标签（<br/>、<img/>、<hr/>）走与开始标签相同的处理。"""
        if tag in ("br", "hr", "img"):
            self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:  # noqa: C901
        if tag in self.SKIP_TAGS:
            self.skip_depth = max(0, self.skip_depth - 1)
            return

        if tag == "title":
            self._in_title = False
            return

        if tag in ("h1", "h2", "h3", "h4", "h5", "h6", "li", "blockquote", "p"):
            self._ensure_newline()
        elif tag in ("ul", "ol"):
            if self.list_stack:
                self.list_stack.pop()
            self._ensure_newline()
        elif tag == "pre":
            self.in_pre = False
            code = "\n".join(self.pre_buf).strip("\n")
            self.out.append(code)
            self.out.append("```")
            self._ensure_newline()
        elif tag == "code" and not self.in_pre:
            self._append_to_out("`")
        elif tag == "table":
            self._render_table()
            self._in_table = False
            self._cur_row = None
            self._cur_cell = None
        elif tag == "tr":
            if self._cur_row is not None and self._cur_row:
                self.table_rows.append(self._cur_row)
            self._cur_row = None
            self._cur_cell = None
        elif tag in ("td", "th"):
            if self._cur_cell is not None:
                if self._cur_row is not None:
                    self._cur_row.append("".join(self._cur_cell).strip())
                self._cur_cell = None
        elif tag == "a":
            self._append_to_out("]")
            href = self._href_stack.pop() if self._href_stack else ""
            if href:
                self._append_to_out(f"({href})")
        elif tag in ("strong", "b"):
            self._append_to_out("**")
        elif tag in ("em", "i"):
            self._append_to_out("*")

    def handle_data(self, data: str) -> None:
        if self._in_title:
            # <title> 在 <head> 内（skip 区），必须在 skip_depth 检查之前采集
            self.title_text = (self.title_text or "") + data
            return
        if self.skip_depth > 0:
            return
        if self.in_pre:
            self.pre_buf.append(data)
            return
        text = _collapse_ws(data)
        if not text:
            return
        if self._cur_cell is not None:
            self._cur_cell.append(text)
        else:
            self._append_to_out(text)

    # ---- 表格渲染 ----

    def _render_table(self) -> None:
        """把收集到的 table_rows 渲染为 Markdown 表格。"""
        rows = [r for r in self.table_rows if r]
        if not rows:
            return
        self._ensure_newline()
        for i, row in enumerate(rows):
            cells = [c.replace("|", "\\|") for c in row]
            self.out.append("| " + " | ".join(cells) + " |")
            if i == 0:
                self.out.append("|" + "|".join(" --- " for _ in cells) + "|")
        self._ensure_newline()
        self.table_rows = []


# ============ 抓取与网页转换 ============


def _detect_encoding(raw: bytes, content_type: Optional[str]) -> str:
    """推断网页编码：HTTP header charset > HTML meta charset > utf-8。"""
    if content_type:
        m = re.search(r"charset=([\w-]+)", content_type, re.I)
        if m:
            return m.group(1)
    head = raw[:4096].decode("ascii", errors="ignore")
    m = re.search(r'<meta[^>]+charset=["\']?([\w-]+)', head, re.I)
    if m:
        return m.group(1)
    return "utf-8"


def fetch_page_markdown(url: str, timeout: int = WEBSCRAPE_TIMEOUT_SECONDS) -> Tuple[str, str]:
    """抓取 HTML 网页并转为 Markdown。

    Returns:
        (title, markdown) — title 来自 <title> 标签（可能为空）

    Raises:
        httpx.HTTPError / ValueError: 网络错误或非 HTML 响应
    """
    with httpx.Client(timeout=timeout, follow_redirects=True, headers=_HEADERS) as client:
        resp = client.get(url)
        resp.raise_for_status()
        content_type = (resp.headers.get("content-type") or "").lower()
        if content_type and "html" not in content_type and "xml" not in content_type and "text" not in content_type:
            raise ValueError(f"响应不是网页（Content-Type: {content_type}）")
        raw = resp.content
        encoding = _detect_encoding(raw, content_type or None)
        html_text = raw.decode(encoding, errors="replace")

    parser = HTMLToMarkdown(base_url=str(resp.url))
    parser.feed(html_text)
    parser.close()

    lines: List[str] = []
    for line in parser.out:
        line = line.rstrip()
        if line:
            lines.append(line)
        elif lines and lines[-1] != "":
            lines.append("")
    markdown = "\n".join(lines).strip()

    title = (parser.title_text or "").strip()
    if markdown and not title:
        title = _title_from_url(url)
    return title, markdown


def _title_from_url(url: str) -> str:
    """URL → 标题回退：取路径最后一段（去扩展名），失败用域名。"""
    parsed = urlparse(url)
    path = parsed.path.rstrip("/")
    if path:
        seg = path.rsplit("/", 1)[-1]
        seg = re.sub(r"\.(html?|php|aspx?|jsp|shtml)$", "", seg, flags=re.I)
        if seg:
            return seg
    return parsed.netloc or url


# ============ 附件识别与下载 ============


def _url_path_ext(url: str) -> str:
    """取 URL 路径尾段的小写扩展名（无则返回空串）。"""
    path = unquote(urlparse(url).path).rstrip("/")
    seg = path.rsplit("/", 1)[-1] if path else ""
    return Path(seg).suffix.lower() if seg else ""


def is_attachment_url(url: str) -> bool:
    """URL 尾段带附件扩展名 → 视为附件链接。"""
    return _url_path_ext(url) in ATTACHMENT_EXTS


def _is_doc_mime(content_type: str) -> bool:
    ct = (content_type or "").lower().split(";")[0].strip()
    return any(ct.startswith(p) for p in ATTACHMENT_MIME_PREFIXES)


def _filename_from_disposition(disposition: Optional[str]) -> Optional[str]:
    """从 Content-Disposition 提取文件名（支持 filename= / filename*=UTF-8''）。"""
    if not disposition:
        return None
    m = re.search(r"filename\*=UTF-8''([^;]+)", disposition, re.I)
    if m:
        return unquote(m.group(1).strip().strip('"'))
    m = re.search(r'filename=["\']?([^";\']+)', disposition, re.I)
    if m:
        return m.group(1).strip()
    return None


def _safe_download_name(name: str, url: str) -> str:
    """附件保存名安全化：清理非法字符，空则从 URL 推断。"""
    name = (name or "").strip().strip('"')
    if not name:
        seg = unquote(urlparse(url).path).rstrip("/").rsplit("/", 1)[-1]
        name = seg or f"download_{uuid.uuid4().hex[:8]}"
    for ch in '<>:"/\\|?*':
        name = name.replace(ch, "_")
    return name.strip().strip(".") or f"download_{uuid.uuid4().hex[:8]}"


def download_attachment(
    url: str,
    dest_dir: Path,
    timeout: int = WEBSCRAPE_TIMEOUT_SECONDS,
) -> Dict[str, Any]:
    """下载附件文件到 dest_dir（流式，上限 WEBSCRAPE_MAX_DOWNLOAD_BYTES）。

    Returns:
        {"ok", "filename", "rel_path", "size", "error"}；失败时 ok=False。

    Raises:
        不抛异常（抓取失败信息放在 error 字段）。
    """
    out: Dict[str, Any] = {"ok": False, "error": None}
    try:
        with httpx.Client(timeout=timeout, follow_redirects=True, headers=_HEADERS) as client:
            with client.stream("GET", url) as resp:
                resp.raise_for_status()
                ctype = resp.headers.get("content-type") or ""
                # 明明是网页却按附件下载 → 报错提示换 URL
                if "html" in ctype.lower():
                    raise ValueError(f"该链接返回 HTML 网页而非附件文件（Content-Type: {ctype}）")
                filename = (
                    _filename_from_disposition(resp.headers.get("content-disposition"))
                    or _safe_download_name("", url)
                )
                filename = _safe_download_name(filename, url)
                if not Path(filename).suffix:
                    raise ValueError("无法从链接识别附件文件类型")
                total = 0
                tmp = dest_dir / f".{filename}.part"
                with tmp.open("wb") as f:
                    for chunk in resp.iter_bytes(chunk_size=1 << 16):
                        total += len(chunk)
                        if total > WEBSCRAPE_MAX_DOWNLOAD_BYTES:
                            raise ValueError(
                                f"附件超过下载上限 {WEBSCRAPE_MAX_DOWNLOAD_BYTES // (1024 * 1024)}MB"
                            )
                        f.write(chunk)
                tmp.replace(dest_dir / filename)
        out.update(ok=True, filename=filename, rel_path=filename, size=total)
        log.info(
            "webscrape 附件下载成功: url=%s file=%s size=%d",
            url, filename, total,
            extra={"step": "webscrape", "status": "attachment_ok", "url": url},
        )
    except (httpx.HTTPError, ValueError, OSError) as e:
        out["error"] = str(e)
        log.warning("webscrape 附件下载失败: url=%s err=%s", url, e,
                    extra={"step": "webscrape", "status": "attachment_failed", "url": url})
    except Exception as e:  # noqa: BLE001
        out["error"] = f"未知错误: {e}"
        log.exception("webscrape 附件下载异常: url=%s", url)
    return out


# ============ 站点白名单校验 ============


def url_allowed_check(url: str, site_url: str) -> Optional[str]:
    """校验 URL 是否属于配置的「抓取网站 URL」。

    Returns:
        None = 允许；否则返回拒绝原因（供前端/日志展示）。
    """
    if not site_url or not site_url.strip():
        return "配置方案未设置「抓取网站 URL」，请先在配置中心完善配置"
    site_url = site_url.strip().rstrip("/")
    try:
        site = urlparse(site_url)
    except ValueError:
        return f"配置的抓取网站 URL 非法: {site_url}"
    if site.scheme not in ("http", "https") or not site.netloc:
        return f"配置的抓取网站 URL 非法（需 http(s)://域名）: {site_url}"
    try:
        target = urlparse(url)
    except ValueError:
        return f"URL 非法: {url}"
    if target.scheme not in ("http", "https") or not target.netloc:
        return "URL 必须以 http:// 或 https:// 开头"
    # 同域名校验；配置 URL 带路径前缀时要求目标以该前缀开头
    if target.netloc.lower() == site.netloc.lower():
        return None
    if site.path and (site_url + "/") and url.startswith(site_url + "/"):
        return None
    return f"URL 不属于配置的抓取网站（{site_url}），仅允许抓取该网站下的内容"


# ============ 任务式抓取 ============


def _safe_title(text: str) -> str:
    """清理标题中 Windows 非法字符与站点后缀噪音。"""
    if not text:
        return ""
    for ch in '<>:"/\\|?*\n\r\t':
        text = text.replace(ch, "")
    text = text.strip()
    # 去掉常见的 "标题 - 站点名" 式后缀（取第一个分隔符前）
    text = re.split(r"\s*[-–—|｜·]\s*", text, maxsplit=1)[0] if text else ""
    return text.strip().rstrip(". ") or ""


def _safe_stem(title: str, url: str) -> str:
    """由页面标题生成安全 stem（清理非法字符、截断、空标题回退 URL）。"""
    stem = _safe_title(title) or _safe_title(_title_from_url(url))
    stem = stem[:WEBSCRAPE_STEM_MAX_CHARS].rstrip(". ")
    return stem or "webpage"


def task_temp_dir(task_id: str) -> Path:
    """任务临时目录（data/webscrape/{task_id}/），自动创建。"""
    d = settings.data_root / WEBSCRAPE_DIRNAME / task_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def create_task(
    profile: Dict[str, Any],
    urls: List[str],
    timeout: int = WEBSCRAPE_TIMEOUT_SECONDS,
) -> Dict[str, Any]:
    """抓取一批 URL 生成「待确认任务」。

    Args:
        profile: 配置方案 dict（含 id/name/config；决定抓取网站白名单）
        urls: 待抓取的 URL 列表

    Returns:
        任务 dict：
        {
            "id", "created_at", "profile_id", "profile_name", "site_url",
            "status": "pending", "items": [...],
        }
        每个 item：{url, ok, kind(content/attachment), title, filename, rel_path,
                    char_count, size, truncated, confirmed, error, ...}
        失败/被拒的 URL 也占一项（ok=False + error），便于前端逐条展示。
    """
    from app.services import manifest_store  # 仅用于 stem 冲突检查

    task_id = uuid.uuid4().hex
    task_dir = task_temp_dir(task_id)
    config = profile.get("config") or {}
    site_url = str(config.get("webscrape_site_url") or "").strip()

    items: List[Dict[str, Any]] = []
    manifest = manifest_store.load()  # 1 次快照，供整批 stem 去重

    for i, raw in enumerate(urls):
        url = (raw or "").strip()
        item: Dict[str, Any] = {"url": url, "ok": False}
        if not url:
            item["error"] = "URL 为空"
            items.append(item)
            continue
        deny = url_allowed_check(url, site_url)
        if deny:
            item["error"] = deny
            items.append(item)
            continue
        try:
            if is_attachment_url(url):
                dl = download_attachment(url, task_dir, timeout=timeout)
                if not dl.get("ok"):
                    item["error"] = dl.get("error") or "附件下载失败"
                else:
                    item.update(
                        ok=True,
                        kind="attachment",
                        title=_safe_stem(Path(dl["filename"]).stem, url),
                        filename=dl["filename"],
                        rel_path=dl["rel_path"],
                        size=dl.get("size"),
                    )
            else:
                title, markdown = fetch_page_markdown(url, timeout=timeout)
                if not markdown:
                    raise ValueError("页面未提取到正文内容")
                truncated = len(markdown) > WEBSCRAPE_MAX_CHARS
                if truncated:
                    markdown = markdown[:WEBSCRAPE_MAX_CHARS]
                stem = _safe_stem(title, url)
                # 同批去重（与 manifest 的冲突留在确认阶段再解决）
                used = {it.get("stem_base") for it in items if it.get("ok")}
                if stem in used:
                    stem = f"{stem[:40]}__{hashlib.sha1(url.encode('utf-8')).hexdigest()[:6]}"
                rel = f"{i:03d}_{stem}.md"
                (task_dir / rel).write_text(markdown, encoding="utf-8")
                item.update(
                    ok=True,
                    kind="content",
                    title=title or stem,
                    stem_base=stem,
                    rel_path=rel,
                    char_count=len(markdown),
                    truncated=truncated,
                )
        except (httpx.HTTPError, ValueError, OSError) as e:
            item["error"] = str(e)
            log.warning("webscrape 抓取失败: url=%s err=%s", url, e,
                        extra={"step": "webscrape", "status": "item_failed", "url": url})
        except Exception as e:  # noqa: BLE001
            item["error"] = f"未知错误: {e}"
            log.exception("webscrape 抓取异常: url=%s", url)
        items.append(item)

    # 失败项统一补全字段，保证前端渲染结构一致
    for it in items:
        it.setdefault("kind", "content")
        it.setdefault("title", "")
        it.setdefault("filename", None)
        it.setdefault("rel_path", None)
        it.setdefault("char_count", None)
        it.setdefault("size", None)
        it.setdefault("truncated", False)
        it.setdefault("confirmed", False)
        it.setdefault("ingest_status", None)   # confirm 后：ok / error
        it.setdefault("ingest_error", None)

    task = {
        "id": task_id,
        "created_at": _now_str(),
        "profile_id": profile.get("id"),
        "profile_name": profile.get("name"),
        "site_url": site_url,
        "status": TASK_STATUS_PENDING,
        "items": items,
    }
    log.info(
        "webscrape 任务已创建: id=%s urls=%d ok=%d site=%s",
        task_id, len(urls), sum(1 for it in items if it.get("ok")), site_url,
        extra={"step": "webscrape", "status": "task_created", "task_id": task_id},
    )
    return task


def _now_str() -> str:
    import time
    return time.strftime("%Y-%m-%d %H:%M:%S")


# ============ 确认落地（确认接口调用） ============


def _padded_unique_stem(base: str, url: str) -> str:
    """生成不与 manifest/parsed 冲突的 stem（重复时追加 URL hash 短串）。"""
    from app.services import manifest_store

    manifest = manifest_store.load()
    stem = base
    while True:
        if not any(Path(fname).stem == stem for fname in manifest) \
                and not (settings.parsed_dir / stem).exists():
            return stem
        stem = f"{base[:40]}__{hashlib.sha1(url.encode('utf-8')).hexdigest()[:6]}"


def _unique_pending_name(desired: str, url: str) -> str:
    """pending/ 重名时生成不冲突的文件名（保持 manifest 与 pending 一致）。"""
    p = settings.pending_dir / desired
    if not p.exists():
        return desired
    stem, dot_ext = Path(desired).stem, Path(desired).suffix
    suffix = hashlib.sha1(url.encode("utf-8")).hexdigest()[:6]
    return f"{stem[:40]}_{suffix}{dot_ext}"


def land_confirmed_items(task: Dict[str, Any], confirmed_urls: List[str]) -> List[Dict[str, Any]]:
    """把任务中选中的项落地为正式产物并登记 manifest，返回每项落地结果。

    - content（网页正文）：临时 md 移到 parsed/{stem}/{stem}.md，
      manifest 的 parse 列填产物目录绝对路径 → 等价「已解析」，切分/入库直接可用
    - attachment（附件文件）：移到 pending/{filename}，manifest 的 parse 列为空
      → 由流水线 parse（MinerU）阶段解析
    两项都以 import_status="已抓取"、process_note=源 URL 登记。

    Returns:
        [{url, kind, stem, filename, ok, error}]（失败项 ok=False）
    """
    from app.models.schemas import ManifestRow
    from app.services import manifest_store

    task_dir = task_temp_dir(task["id"])
    confirmed_set = set(confirmed_urls or [])
    results: List[Dict[str, Any]] = []

    for it in task.get("items") or []:
        if not it.get("ok") or it.get("url") not in confirmed_set:
            continue
        url = it["url"]
        out: Dict[str, Any] = {"url": url, "kind": it.get("kind"), "ok": False, "error": None}
        try:
            src = task_dir / it["rel_path"]
            if not src.is_file():
                raise FileNotFoundError(f"临时文件不存在: {it.get('rel_path')}")
            if it.get("kind") == "content":
                stem = _padded_unique_stem(it.get("stem_base") or _safe_stem(it.get("title", ""), url), url)
                parsed_dir = settings.parsed_dir / stem
                parsed_dir.mkdir(parents=True, exist_ok=True)
                md_path = parsed_dir / f"{stem}.md"
                src.replace(md_path)
                filename = f"{stem}.md"
                manifest_row = ManifestRow(
                    filename=filename,
                    import_status="已抓取",
                    process_note=url,
                    md5=hashlib.md5(md_path.read_bytes()).hexdigest(),
                    parse=str(parsed_dir.resolve()),
                    status="已抓取",
                )
            else:  # attachment
                desired = it.get("filename") or ""
                target_name = _unique_pending_name(desired, url)
                src.replace(settings.pending_dir / target_name)
                filename = target_name
                stem = Path(target_name).stem
                manifest_row = ManifestRow(
                    filename=filename,
                    import_status="已抓取",
                    process_note=url,
                    status="已抓取",
                )
            manifest_store.upsert(manifest_row)
            out.update(ok=True, stem=stem, filename=filename)
            log.info(
                "webscrape 确认落地: url=%s kind=%s stem=%s",
                url, out["kind"], stem,
                extra={"step": "webscrape", "status": "landed", "url": url},
            )
        except Exception as e:  # noqa: BLE001
            out["error"] = f"落地失败: {e}"
            log.exception("webscrape 确认落地失败: url=%s", url)
        results.append(out)
    return results


# ============ 任务持久化（webscrape_tasks 表） ============


def save_task(task: Dict[str, Any]) -> None:
    """插入或更新一条抓取任务（幂等，按 id 覆盖）。"""
    from psycopg.types.json import Jsonb

    from app import db

    row = {
        "id": task["id"],
        "created_at": task.get("created_at"),
        "updated_at": _now_str(),
        "profile_id": task.get("profile_id"),
        "profile_name": task.get("profile_name"),
        "site_url": task.get("site_url"),
        "status": task.get("status"),
        "confirm_time": task.get("confirm_time"),
        "confirm_profile": task.get("confirm_profile"),
        "items": Jsonb(task.get("items") or []),
    }
    with db.get_conn() as conn:
        conn.execute(
            """
            INSERT INTO webscrape_tasks
                (id, created_at, updated_at, profile_id, profile_name, site_url,
                 status, confirm_time, confirm_profile, items)
            VALUES
                (%(id)s, %(created_at)s, %(updated_at)s, %(profile_id)s, %(profile_name)s, %(site_url)s,
                 %(status)s, %(confirm_time)s, %(confirm_profile)s, %(items)s)
            ON CONFLICT (id) DO UPDATE SET
                updated_at = EXCLUDED.updated_at,
                profile_id = EXCLUDED.profile_id,
                profile_name = EXCLUDED.profile_name,
                site_url = EXCLUDED.site_url,
                status = EXCLUDED.status,
                confirm_time = EXCLUDED.confirm_time,
                confirm_profile = EXCLUDED.confirm_profile,
                items = EXCLUDED.items
            """,
            row,
        )
        conn.commit()


def get_task(task_id: str) -> Optional[Dict[str, Any]]:
    """按 id 读取任务（items 从 JSONB 解析）。"""
    import json

    from app import db

    with db.get_conn() as conn:
        cur = conn.execute(
            "SELECT id, created_at, updated_at, profile_id, profile_name, site_url, "
            "status, confirm_time, confirm_profile, items FROM webscrape_tasks WHERE id = %s",
            (task_id,),
        )
        rec = cur.fetchone()
    if not rec:
        return None
    task = dict(rec)
    raw = task.get("items")
    if isinstance(raw, str):
        try:
            task["items"] = json.loads(raw)
        except json.JSONDecodeError:
            task["items"] = []
    return task


def list_tasks(limit: int = 20) -> List[Dict[str, Any]]:
    """按创建时间倒序列出最近任务（不含 items 明细，仅列表信息）。"""
    import json

    from app import db

    limit = max(1, min(int(limit), 200))
    with db.get_conn() as conn:
        cur = conn.execute(
            "SELECT id, created_at, updated_at, profile_id, profile_name, site_url, "
            "status, confirm_time, confirm_profile, items FROM webscrape_tasks "
            "ORDER BY created_at DESC, id DESC LIMIT %s",
            (limit,),
        )
        rows = cur.fetchall()
    out: List[Dict[str, Any]] = []
    for r in rows:
        t = dict(r)
        raw = t.get("items")
        items: Any = []
        if isinstance(raw, str):
            try:
                items = json.loads(raw)
            except json.JSONDecodeError:
                items = []
        elif isinstance(raw, list):
            items = raw
        t["total"] = len(items)
        t["ok_count"] = sum(1 for it in items if it.get("ok"))
        t["confirmed_count"] = sum(1 for it in items if it.get("confirmed"))
        t.pop("items", None)  # 列表页不需要明细
        out.append(t)
    return out