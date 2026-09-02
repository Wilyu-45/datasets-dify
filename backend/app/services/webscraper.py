"""知识库外延：网站抓取（2026-08 新增）。

流程（任务式，两步确认）：
    1. 抓取（POST /api/webscrape/run）：先选「网站抓取配置」（决定抓取来源：
       其 webscrape_urls 列表），逐 URL 分类处理：
           - 网页内容（HTML）→ 正文转为 Markdown，落到 data/webscrape/{task_id}/
           - 附件文件（PDF/DOCX 等链接）→ 下载原文件到 data/webscrape/{task_id}/
       此阶段不写 manifest、不入库，生成「待确认任务」。
    2. 确认（POST /api/webscrape/task/{id}/confirm）：人在预览页确认内容后，
       把选中的项落到 pending/（网页 → 浏览器渲染 PDF，失败降级原始 HTML；
       附件 → 原文件）并登记 manifest（parse 列留空），统一走
       parse(MinerU) → chunk → dify 流水线。
       ★ 2026-08-31 变更：此前网页 Markdown 直接落 parsed/ 等价「已解析」，
       错乱正文跳过解析直接进切分；现与附件一致，全部过解析阶段。

★ 2026-08-31 两套配置：抓取的 URL 不再由页面输入，而是直接来自配置方案中
   webscrape_urls 列表；任务创建时把该列表快照进 site_url（JSON 文本）。

设计要点：
    - 不引入新依赖：抓取用 httpx（requirements 已有），HTML→Markdown 用标准库 html.parser
    - 每个 URL 独立 try/except：1 个失败不影响其他
    - 附件识别：URL 扩展名优先，命中不了再看 Content-Type（文档类 MIME）
    - 下载上限 50MB；正文截断 200_000 字符，防超大内容拖垮预览与入库
    - ★ 2026-08-31 站内递归：配置的 URL 通常只是网站首页，只抓首页拿不到子页面内容；
      开启 webscrape_crawl_enabled 后按 BFS 沿页面内链接逐层抓取（深度 / 页数上限
      在配置中心调整），范围限定在种子 URL 的同域名（带栏目路径时限同栏目），
      递归发现的附件链接同样下载
"""

from __future__ import annotations

import hashlib
import logging
import re
import uuid
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import unquote, urljoin, urlparse, urlunparse

import httpx

from app.config import settings

log = logging.getLogger("ragsystem.webscraper")

# ---- 抓取行为常量 ----
WEBSCRAPE_DIRNAME = "webscrape"              # data/webscrape/{task_id}/ 临时区
WEBSCRAPE_TIMEOUT_SECONDS = 30               # 单页抓取超时
WEBSCRAPE_MAX_CHARS = 200_000                # 单页正文截断上限
WEBSCRAPE_STEM_MAX_CHARS = 60                # 由标题生成的 stem 最大长度
WEBSCRAPE_CRAWL_DELAY_SECONDS = 0.5          # 递归抓取页间礼貌间隔（降低对目标站压力）
WEBSCRAPE_MAX_DOWNLOAD_BYTES = 50 * 1024 * 1024  # 附件下载上限 50MB
TASK_STATUS_PENDING = "pending"              # 已抓取、待确认下载
TASK_STATUS_CONFIRMED = "confirmed"          # 已确认下载（2026-09 起：下载后逐项预览确认，由 ingest 入库）
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

# ★ 2026-08-31 完整浏览器指纹：部分政府网站（如卫健委）WAF 对非浏览器请求返回 412
_HEADERS = {
    "User-Agent": _USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8,en;q=0.7",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Cache-Control": "max-age=0",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "same-origin",
    "Sec-Fetch-User": "?1",
}


def _http_headers_for(url: str) -> Dict[str, str]:
    """按目标 URL 生成请求头：浏览器指纹 + 同源 Referer（防 WAF 反爬 412）。"""
    h = dict(_HEADERS)
    try:
        parts = urlparse(url)
        if parts.scheme in ("http", "https") and parts.netloc:
            h["Referer"] = f"{parts.scheme}://{parts.netloc}/"
    except ValueError:
        pass
    return h


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


def fetch_page_html(url: str, timeout: int = WEBSCRAPE_TIMEOUT_SECONDS) -> Tuple[str, str]:
    """抓取页面原始 HTML（含 WAF 412/403/502 时的浏览器内核降级）。

    递归抓取与正文转换共用本函数：一次请求同时服务「正文转 Markdown」
    和「提取页面内链接继续递归」。

    Returns:
        (html_text, final_url) — final_url 为重定向后的最终地址（相对链接基准）

    Raises:
        httpx.HTTPError / ValueError: 网络错误或非 HTML 响应
    """
    import time as _t

    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
        resp = None
        # 412（WAF 反爬）时重试一次（更换 Referer / 间隔后通常放行）
        for attempt in range(2):
            resp = client.get(url, headers=_http_headers_for(url))
            if resp.status_code == 412 and attempt == 0:
                _t.sleep(1.0)
                resp.close()
                continue
            break
        if resp.status_code in (412, 502, 403):
            # WAF 反爬（412/403）或源站故障（502）→ 浏览器内核降级（自动执行 JS 挑战）
            log.info("webscrape httpx blocked(%d), fallback to browser: %s", resp.status_code, url)
            resp.close()
            from .browser_fetch import browser_fetch_html

            b = browser_fetch_html(url, timeout=timeout)
            if not b["ok"]:
                raise ValueError(b["error"])
            return b["html"], b["final_url"] or url
        resp.raise_for_status()
        content_type = (resp.headers.get("content-type") or "").lower()
        if content_type and "html" not in content_type and "xml" not in content_type and "text" not in content_type:
            raise ValueError(f"响应不是网页（Content-Type: {content_type}）")
        raw = resp.content
        encoding = _detect_encoding(raw, content_type or None)
        return raw.decode(encoding, errors="replace"), str(resp.url)


def _parse_html(html_text: str, base_url: str) -> Tuple[str, str]:
    """HTML → Markdown（正文转换共用入口）。Returns: (title, markdown)。"""
    parser = HTMLToMarkdown(base_url=base_url)
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
    return (parser.title_text or "").strip(), markdown


def fetch_page_markdown(url: str, timeout: int = WEBSCRAPE_TIMEOUT_SECONDS) -> Tuple[str, str]:
    """抓取 HTML 网页并转为 Markdown。

    Returns:
        (title, markdown) — title 来自 <title> 标签（可能为空）
    """
    html_text, final_url = fetch_page_html(url, timeout=timeout)
    title, markdown = _parse_html(html_text, final_url)
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


# ============ 站内递归：链接提取与范围控制 ============


def _normalize_url(url: str) -> str:
    """链接规范化（递归去重用）：去 fragment、补根路径、scheme/host 小写。

    非 http(s) 链接返回空串（mailto: / javascript: 等一律不跟随）。
    """
    try:
        p = urlparse(url)
    except ValueError:
        return ""
    if p.scheme.lower() not in ("http", "https") or not p.netloc:
        return ""
    return urlunparse((p.scheme.lower(), p.netloc.lower(), p.path or "/", "", p.query, ""))


class _LinkExtractor(HTMLParser):
    """提取页面内全部 <a href>（绝对化），供递归抓取发现子页面。

    与 HTMLToMarkdown 不同：不看 SKIP_TAGS —— 导航栏/页脚里的栏目链接
    恰恰是首页通往子页面的主要入口，必须收集。
    """

    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.links: List[str] = []

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        if tag != "a":
            return
        href = next((v for k, v in attrs if k.lower() == "href" and v), None)
        if not href:
            return
        u = _abs_url(href.strip(), self.base_url)
        if u.startswith(("http://", "https://")):
            self.links.append(u)


# ★ 附件 URL 兜底提取正则：匹配引号字符串里以 / 开头的站内路径或完整
# http(s) URL、且扩展名是附件类型（供 JS 变量里挂附件的政府 CMS 站使用）。
# 例：var wordDocPath = '/group2/M00/54/C3/wKg...doc';
_ATTACH_URL_RE = re.compile(
    r"[\"']((?:https?://[^\"'\s]+)?"
    r"/[A-Za-z0-9_\-./%~]+\.(?:pdf|docx?|xlsx?|pptx?|csv|txt|md|rtf|wps|et|dps"
    r"|zip|rar|7z|tar|gz|eml|msg))[\"']",
    re.I,
)


def extract_page_links(html_text: str, base_url: str) -> List[str]:
    """提取页面内可跟随的绝对链接（规范化 + 去重，保持出现顺序）。

    两个来源：
        1. <a href> 标签（常规）
        2. ★ 引号字符串里的附件路径兜底：政府站 CMS 常把附件 URL 写在 JS
           变量里（如 var wordDocPath = '/group2/.../xx.doc'），<a> 是
           JS 动态生成的 —— httpx 静态 HTML 里没有 <a>，只有变量
           （ja.gov.cn 抓取丢附件的原因之三）
    """
    ex = _LinkExtractor(base_url)
    try:
        ex.feed(html_text)
        ex.close()
    except Exception:  # noqa: BLE001 页面 HTML 残缺时尽力提取已解析部分
        pass
    out: List[str] = []
    seen: set = set()
    for u in ex.links:
        nu = _normalize_url(u)
        if nu and nu not in seen:
            seen.add(nu)
            out.append(nu)
    # 附件路径兜底：以 / 开头的站内路径或完整 http(s) URL，扩展名为附件类型
    for m in _ATTACH_URL_RE.finditer(html_text):
        raw = m.group(1)
        absu = raw if raw.startswith(("http://", "https://")) else _abs_url(raw, base_url)
        nu = _normalize_url(absu)
        if nu and nu not in seen and is_attachment_url(nu):
            seen.add(nu)
            out.append(nu)
    return out


def _site_scope_ok(link: str, seed: str) -> bool:
    """递归范围：仅跟随与种子 URL 同站的链接。

    - www 前缀视为同站（www.example.com == example.com）
    - ★ 附件链接（.pdf/.docx 等扩展名）不受栏目前缀限制：政府站附件常放在
      /group2/ 之类静态资源目录，与页面路径无关，必须放行
    - 栏目前缀限制仅在种子是显式目录（路径以 / 结尾，如 http://a.gov.cn/zcwjk/）
      时生效；具体页面做种子（如 /public/xxx/123.html）时同站链接均可跟随。
      ★ 2026-08-31 修复：此前把种子整条路径一律当栏目前缀，文章页做种子时
      连页面里挂的附件都被拦掉（ja.gov.cn 抓取丢附件的原因）
    """
    try:
        l, s = urlparse(link), urlparse(seed)
    except ValueError:
        return False
    if not l.netloc or not s.netloc:
        return False
    if l.netloc.lower().removeprefix("www.") != s.netloc.lower().removeprefix("www."):
        return False
    if is_attachment_url(link):
        return True
    if (s.path or "/").endswith("/"):
        prefix = s.path.rstrip("/")
        if prefix and not (l.path or "/").startswith(prefix + "/"):
            return False
    return True


def profile_crawl_settings(profile: Dict[str, Any]) -> Dict[str, Any]:
    """读取配置方案中的站内递归参数（旧配置缺字段时按默认值兜底）。"""
    config = profile.get("config") or {}

    def _clamp_int(key: str, default: int, lo: int, hi: int) -> int:
        try:
            v = int(config.get(key, default))
        except (TypeError, ValueError):
            v = default
        return max(lo, min(hi, v))

    enabled = config.get("webscrape_crawl_enabled")
    return {
        "enabled": bool(enabled) if enabled is not None else True,
        "depth": _clamp_int("webscrape_crawl_depth", 2, 0, 5),
        "max_pages": _clamp_int("webscrape_crawl_max_pages", 20, 1, 200),
    }


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
    import time as _t

    try:
        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            ok = False
            total = 0
            filename = None
            for attempt in range(2):
                try:
                    with client.stream("GET", url, headers=_http_headers_for(url)) as resp:
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
                    ok = True
                    break
                except httpx.HTTPStatusError as e:
                    sc = e.response.status_code
                    # 412（WAF 反爬）时先重试一次
                    if sc == 412 and attempt == 0:
                        _t.sleep(1.0)
                        continue
                    if sc in (412, 502, 403):
                        # WAF 拦截 / 源站故障 → 浏览器内核降级下载
                        log.info("webscrape attachment httpx blocked(%d), browser fallback: %s", sc, url)
                        fname = filename or _safe_download_name("", url)
                        from .browser_fetch import browser_download_file

                        if browser_download_file(url, dest_dir / fname, timeout=timeout):
                            filename = fname
                            total = (dest_dir / fname).stat().st_size
                            ok = True
                            break
                        raise ValueError(
                            "网站反爬/源站故障，附件下载失败（浏览器内核亦无法获取）："
                            f"{url}；请确认网站在浏览器中可正常访问"
                        )
                    raise
        if not ok:
            raise httpx.HTTPError(f"下载失败: {url}")
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


def profile_webscrape_urls(profile: Dict[str, Any]) -> List[str]:
    """取配置方案中的抓取 URL 列表（兼容旧字段 webscrape_site_url 单值）。"""
    config = profile.get("config") or {}
    urls = config.get("webscrape_urls") or []
    if isinstance(urls, str):
        # 旧版可能存了单值字符串/JSON 文本，兼容解析
        import json

        try:
            urls = json.loads(urls)
        except json.JSONDecodeError:
            urls = [urls]
    if not isinstance(urls, list):
        urls = [urls]
    out = [str(u).strip() for u in urls if str(u).strip()]
    # 旧字段兜底（新列表为空时用旧「抓取网站 URL」单值）
    if not out:
        legacy = str(config.get("webscrape_site_url") or "").strip()
        if legacy:
            out = [legacy]
    return out


def url_allowed_check(url: str, allowed_urls: List[str]) -> Optional[str]:
    """校验 URL 是否属于配置的「抓取网站 URL 列表」。

    Returns:
        None = 允许；否则返回拒绝原因（供前端/日志展示）。
    """
    allowed_urls = [u for u in (allowed_urls or []) if u and u.strip()]
    if not allowed_urls:
        return "配置方案未设置「抓取网站 URL 列表」，请先在配置中心完善配置"
    try:
        target = urlparse(url)
    except ValueError:
        return f"URL 非法: {url}"
    if target.scheme not in ("http", "https") or not target.netloc:
        return "URL 必须以 http:// 或 https:// 开头"
    hosts = set()
    for au in allowed_urls:
        try:
            p = urlparse(au.strip().rstrip("/"))
        except ValueError:
            continue
        if p.netloc:
            hosts.add(p.netloc.lower())
            # 配置 URL 带路径前缀时，目标必须以前缀开头（同一站点的子目录）
            if au.strip().rstrip("/") and url.startswith(au.strip().rstrip("/") + "/"):
                return None
    if target.netloc.lower() in hosts:
        return None
    return f"URL 不属于配置的抓取网站列表（{', '.join(allowed_urls)}），仅允许抓取列表中的网站"


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
    timeout: int = WEBSCRAPE_TIMEOUT_SECONDS,
) -> Dict[str, Any]:
    """抓取配置方案中的 URL 列表，生成「待确认任务」。

    ★ 2026-08-31 站内递归：开启 webscrape_crawl_enabled（默认开）时，从每个
    种子 URL（通常是首页）出发，BFS 沿页面内同站链接逐层抓取子页面与附件：
        - 深度上限 webscrape_crawl_depth（0=旧版只抓 URL 列表本身）
        - 总页数上限 webscrape_crawl_max_pages（URL 列表本身始终全部处理）
        - 仅跟随白名单内且与种子同域名（带栏目路径时限同栏目）的链接
      种子 URL 失败仍逐条生成 error 项（前端展示）；递归发现的页面失败只记
      日志 —— 坏链在网站上很常见，逐条进任务列表只会刷屏。

    Args:
        profile: 网站抓取配置方案 dict（含 id/name/config；抓取来源 = config.webscrape_urls）

    Returns:
        任务 dict：
        {
            "id", "created_at", "profile_id", "profile_name", "site_url"(JSON 快照),
            "urls", "status": "pending", "items": [...],
        }
        每个 item：{url, ok, kind(content/attachment), depth(0=种子,1..N=递归层级),
                    title, filename, rel_path, char_count, size, truncated, confirmed, error, ...}
    """
    import json
    import time as _t
    from collections import deque

    from app.services import manifest_store  # 仅用于 stem 冲突检查

    urls = profile_webscrape_urls(profile)
    crawl = profile_crawl_settings(profile)
    task_id = uuid.uuid4().hex
    task_dir = task_temp_dir(task_id)

    items: List[Dict[str, Any]] = []
    manifest = manifest_store.load()  # 1 次快照，供整批 stem 去重

    # ---- 队列初始化：种子 URL 全部入队（保持配置顺序）----
    # 元素：(url, depth, seed_url, is_seed) — seed_url 用于递归范围判定
    queue: "deque[Tuple[str, int, str, bool]]" = deque()
    enqueued: set = set()  # 规范化 URL 去重（防重复抓取与环）
    for raw in urls:
        url = (raw or "").strip()
        item: Dict[str, Any] = {"url": url, "ok": False, "depth": 0}
        if not url:
            item["error"] = "URL 为空"
            items.append(item)
            continue
        deny = url_allowed_check(url, urls)
        if deny:
            item["error"] = deny
            items.append(item)
            continue
        nu = _normalize_url(url) or url
        if nu in enqueued:
            item["error"] = "URL 列表中重复（本次只抓取一次）"
            items.append(item)
            continue
        enqueued.add(nu)
        queue.append((url, 0, url, True))

    # 页数上限：URL 列表本身始终全部处理，递归总量受上限约束（上限不低于列表长度）
    max_items = max(len(urls), crawl["max_pages"]) if crawl["enabled"] else len(urls)
    seq = 0          # 临时文件名序号（与处理顺序一致，失败项跳过号段无妨）
    fetched_any = False

    while queue:
        url, depth, seed, is_seed = queue.popleft()
        if not is_seed and len(items) >= max_items:
            log.info("webscrape 递归达到页数上限 %d，停止扩展", max_items,
                     extra={"step": "webscrape", "status": "crawl_capped"})
            break
        if fetched_any and crawl["enabled"]:
            _t.sleep(WEBSCRAPE_CRAWL_DELAY_SECONDS)  # 礼貌间隔，降低对目标站压力
        fetched_any = True

        item: Dict[str, Any] = {"url": url, "ok": False, "depth": depth}
        try:
            if is_attachment_url(url):
                dl = download_attachment(url, task_dir, timeout=timeout)
                if not dl.get("ok"):
                    raise ValueError(dl.get("error") or "附件下载失败")
                item.update(
                    ok=True,
                    kind="attachment",
                    title=_safe_stem(Path(dl["filename"]).stem, url),
                    filename=dl["filename"],
                    rel_path=dl["rel_path"],
                    size=dl.get("size"),
                )
            else:
                html_text, final_url = fetch_page_html(url, timeout=timeout)
                title, markdown = _parse_html(html_text, final_url)
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
                rel = f"{seq:03d}_{stem}.md"
                (task_dir / rel).write_text(markdown, encoding="utf-8")
                # ★ 原始 HTML 一并保存：确认入库时渲染 PDF 失败的降级源
                #   （file:// 打印或直接入 pending 走本地解析）
                html_rel = f"{seq:03d}_{stem}.html"
                (task_dir / html_rel).write_text(html_text, encoding="utf-8", errors="replace")
                item.update(
                    ok=True,
                    kind="content",
                    title=title or stem,
                    stem_base=stem,
                    rel_path=rel,
                    html_rel_path=html_rel,
                    char_count=len(markdown),
                    truncated=truncated,
                )
                # ★ 递归扩展：从本页提取同站链接，未超深度即入队
                #   （网页/附件在出队时按 URL 分类处理，附件下载后不再扩展）
                #   ★ 附件插队首：页面挂的关联文件（正文附件）比栏目导航更接近
                #     用户要的内容 —— 政府站附件链接常排在页尾导航之后，
                #     按普通顺序排队会因页数上限被挤掉（ja.gov.cn 丢附件的原因之二）
                if crawl["enabled"] and depth < crawl["depth"]:
                    sub_pages: List[Tuple[str, int, str, bool]] = []
                    sub_atts: List[Tuple[str, int, str, bool]] = []
                    for link in extract_page_links(html_text, final_url):
                        if link in enqueued:
                            continue
                        if url_allowed_check(link, urls) is not None:
                            continue
                        if not _site_scope_ok(link, seed):
                            continue
                        enqueued.add(link)
                        entry = (link, depth + 1, seed, False)
                        (sub_atts if is_attachment_url(link) else sub_pages).append(entry)
                    queue.extendleft(reversed(sub_atts))
                    queue.extend(sub_pages)
        except (httpx.HTTPError, ValueError, OSError) as e:
            item["error"] = str(e)
            log.warning("webscrape 抓取失败: url=%s err=%s", url, e,
                        extra={"step": "webscrape", "status": "item_failed", "url": url})
        except Exception as e:  # noqa: BLE001
            item["error"] = f"未知错误: {e}"
            log.exception("webscrape 抓取异常: url=%s", url)

        # 种子失败保留 error 项（前端逐条展示）；递归项失败只记日志不进列表
        if is_seed or item.get("ok"):
            items.append(item)
        seq += 1

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
        it.setdefault("ingest_status", None)   # confirm 下载后：downloaded/ok/error（待预览确认→入库）
        it.setdefault("ingest_error", None)

    task = {
        "id": task_id,
        "created_at": _now_str(),
        "profile_id": profile.get("id"),
        "profile_name": profile.get("name"),
        "site_url": json.dumps(urls, ensure_ascii=False),  # URL 列表快照（JSON 文本）
        "urls": urls,
        "status": TASK_STATUS_PENDING,
        "items": items,
    }
    log.info(
        "webscrape 任务已创建: id=%s urls=%d items=%d(递归发现 %d) ok=%d crawl=%s",
        task_id, len(urls), len(items),
        sum(1 for it in items if it.get("depth")),
        sum(1 for it in items if it.get("ok")),
        crawl,
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

    ★ 2026-08-31 网页内容与附件一样走解析流水线（用户反馈正文直接切分太错乱）：
        - content（网页正文）：
            ① 浏览器在线渲染 PDF → pending/{stem}.pdf → MinerU 解析
            ② 渲染失败 → 抓取时保存的原始 HTML 打印 PDF → 同上
            ③ 仍失败 → 原始 HTML 直接入 pending/ → 解析阶段本地解析
          manifest 的 parse 列留空 → 流水线 parse 阶段统一处理；
          临时区的 Markdown 仅作预览，不入库。
        - attachment（附件文件）：移到 pending/{filename}，同样 parse 列为空
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
            if it.get("kind") == "content":
                stem = _padded_unique_stem(it.get("stem_base") or _safe_stem(it.get("title", ""), url), url)
                filename = _land_content_as_document(it, stem, url, task_dir)
            else:  # attachment
                src = task_dir / (it.get("rel_path") or "")
                if not src.is_file():
                    raise FileNotFoundError(f"临时文件不存在: {it.get('rel_path')}")
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
                "webscrape 确认落地: url=%s kind=%s file=%s",
                url, out["kind"], filename,
                extra={"step": "webscrape", "status": "landed", "url": url},
            )
        except Exception as e:  # noqa: BLE001
            out["error"] = f"落地失败: {e}"
            log.exception("webscrape 确认落地失败: url=%s", url)
        results.append(out)
    return results


def _land_content_as_document(it: Dict[str, Any], stem: str, url: str, task_dir: Path) -> str:
    """把网页正文落成 pending/ 中的待解析文档（PDF 优先，HTML 兜底）。

    三级降级：在线渲染 PDF → 本地 HTML 打印 PDF → 原始 HTML 文件。
    每级都防御性捕获异常（渲染层任何意外都不能跳过后面的降级路径）。
    返回落在 pending/ 的文件名（manifest parse 列留空，由解析阶段处理）。
    """
    from .browser_fetch import browser_print_local_html_pdf, browser_print_pdf

    html_src = task_dir / (it.get("html_rel_path") or "")
    pdf_name = _unique_pending_name(f"{stem}.pdf", url)

    def _try_render(render) -> bool:
        try:
            return bool(render())
        except Exception as e:  # noqa: BLE001
            log.warning("webscrape PDF 渲染异常: url=%s err=%s", url, e,
                        extra={"step": "webscrape", "status": "render_error", "url": url})
            return False

    # ① 在线渲染：内容/版式与网站当前一致，MinerU 结构识别效果最好
    if _try_render(lambda: browser_print_pdf(url, settings.pending_dir / pdf_name)):
        return pdf_name

    # ② 网站已不可达/改版 → 用抓取时保存的原始 HTML 打印（保证=预览内容）
    if html_src.is_file():
        if _try_render(lambda: browser_print_local_html_pdf(html_src, settings.pending_dir / pdf_name)):
            return pdf_name
        # ③ 浏览器打印彻底失败 → 原始 HTML 直接入 pending/（解析阶段本地解析）
        html_name = _unique_pending_name(f"{stem}.html", url)
        html_src.replace(settings.pending_dir / html_name)
        return html_name

    raise FileNotFoundError(
        f"网页 PDF 渲染失败，且抓取时保存的原始 HTML 不存在: {it.get('html_rel_path')}"
    )


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
    """按 id 读取任务（items 从 JSONB 解析，urls 从 site_url JSON 解析）。"""
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
    _attach_task_urls(task)
    return task


def _attach_task_urls(task: Dict[str, Any]) -> None:
    """从 site_url 列（JSON 文本快照）解析出 urls 列表，挂到 task['urls']。"""
    import json

    raw = task.get("site_url")
    urls: List[str] = []
    if raw:
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            parsed = None
        if isinstance(parsed, list):
            urls = [str(u) for u in parsed]
        elif isinstance(parsed, str) and parsed.strip():
            urls = [parsed]
        elif isinstance(raw, str) and raw.strip():
            urls = [raw]
    task["urls"] = urls


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
        _attach_task_urls(t)
        out.append(t)
    return out