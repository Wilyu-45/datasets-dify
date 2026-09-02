"""落地文件在线预览（2026-09 新增，网页抓取「下载后文件预览」使用）。

设计目标：不引入新依赖，能看的格式直接看、不能看的给文件信息。
    - pdf            → 浏览器原生 PDF 查看器（iframe /file）
    - html/htm       → 后端清理脚本等危险内容后 iframe（/file）
    - png/jpg/...    → <img>（/file）
    - md/txt         → 后端解码为 UTF-8 文本（/file 返回 text/plain，前端渲染）
    - docx/pptx      → 基于 OOXML(zip+xml) 的轻量文本/表格/逐页提取（/office-preview 返回 HTML）
    - xlsx/csv       → 基于 openpyxl / csv 渲染为表格 HTML（/office-preview）
    - .doc/.xls/.ppt 等旧版 Office 及压缩包/邮件等 → 文件信息页（可下载自查）
"""

from __future__ import annotations

import csv
import html as html_mod
import io
import re
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

# ---- 类型判定 ----

_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg"}
_HTML_EXTS = {".html", ".htm"}
_TEXT_EXTS = {".txt", ".md", ".markdown"}
_ARCHIVE_EXTS = {".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz"}
# 可以生成 HTML 预览的 Office/表格（office-preview 接口）
_OFFICE_WEB_EXTS = {".docx", ".xlsx", ".xlsm", ".pptx", ".csv"}
# 旧版二进制 Office（无本地解析库 → 仅信息页 + 下载自查）
_OFFICE_LEGACY_EXTS = {".doc", ".xls", ".ppt"}


def preview_kind(filename: str) -> str:
    """按扩展名给出预览类型。

    取值：pdf / image / html / markdown / text / docx / xlsx / pptx / csv /
          doc / xls / ppt / archive / other
    """
    ext = Path(filename or "").suffix.lower()
    if ext == ".pdf":
        return "pdf"
    if ext in _IMAGE_EXTS:
        return "image"
    if ext in _HTML_EXTS:
        return "html"
    if ext == ".md" or ext == ".markdown":
        return "markdown"
    if ext in _TEXT_EXTS:
        return "text"
    if ext in (".docx",):
        return "docx"
    if ext in (".xlsx", ".xlsm"):
        return "xlsx"
    if ext == ".pptx":
        return "pptx"
    if ext == ".csv":
        return "csv"
    if ext in (".doc",):
        return "doc"
    if ext == ".xls":
        return "xls"
    if ext == ".ppt":
        return "ppt"
    if ext in _ARCHIVE_EXTS:
        return "archive"
    return "other"


def decode_bytes(data: bytes) -> str:
    """字节解码为文本：优先 UTF-8，失败回退 GB18030（中文政府站点常见）。"""
    for enc in ("utf-8", "gb18030"):
        try:
            return data.decode(enc)
        except (UnicodeDecodeError, ValueError):
            continue
    return data.decode("utf-8", errors="replace")


# ---- HTML 清洗（第三方 HTML 原样预览时使用，防脚本/跳转） ----


def sanitize_html(text: str) -> str:
    """移除可执行/干扰元素与属性：script/style/iframe/object/embed/form 等、
    on* 事件、javascript: 伪协议、meta refresh、base。"""
    # 成对标签整体删除（含内容）
    text = re.sub(
        r"<\s*(script|style|iframe|frame|object|embed|applet|form|frameset|base)[^>]*>.*?<\s*/\s*\1\s*>",
        "",
        text,
        flags=re.I | re.S,
    )
    # 残留的开始/自闭合危险标签
    text = re.sub(
        r"<\s*(script|style|iframe|frame|object|embed|applet|form|frameset|base|meta)[^>]*/?>",
        "",
        text,
        flags=re.I,
    )
    # 事件属性 onxxx="..."
    text = re.sub(
        r'\s+on\w+\s*=\s*(?:"[^"]*"|\'[^\']*\'|[^\s>]+)',
        "",
        text,
        flags=re.I,
    )
    # javascript: / vbscript: 伪协议
    text = re.sub(
        r'\s+(?:href|src|action|data|formaction)\s*=\s*["\']?\s*(?:javascript|vbscript):[^"\'>\s]*',
        "",
        text,
        flags=re.I,
    )
    return text


def cleaned_html_bytes(data: bytes) -> bytes:
    """HTML 文件预览：解码 → 清洗 → UTF-8 字节。"""
    return sanitize_html(decode_bytes(data)).encode("utf-8", errors="replace")


# ---- Office/表格 → HTML 轻量预览（后端生成，前端 iframe） ----


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _attr_val(el: ET.Element, local_name: str) -> str:
    """取属性（兼容带命名空间）：如 gridSpan 的 w:val。"""
    for k, v in el.attrib.items():
        if _local(k) == local_name:
            return v or ""
    return ""


def _info_page_html(title: str, reason: str) -> str:
    body = (
        '<div class="pv-info">'
        f"<div class='pv-info-icon'>&#128196;</div>"
        f"<div class='pv-info-title'>{html_mod.escape(title)}</div>"
        f"<p>{html_mod.escape(reason)}</p>"
        "<p>该文件为本次确认下载的原文件，已保存到待处理区；"
        "可直接使用下方「下载原文件」在本地打开查看后再回来点「确定」入库。</p>"
        "</div>"
    )
    return _wrap_html(title, body)


# ---------- DOCX（zip + 主文档 XML，段落/表格/文本，图片以占位符标记） ----------


def _docx_para_html(p: ET.Element) -> str:
    parts: list[str] = []
    has_img = False
    for node in p.iter():
        t = _local(node.tag)
        if t == "t":
            parts.append(html_mod.escape(node.text or ""))
        elif t in ("br", "cr"):
            parts.append("<br>")
        elif t == "tab":
            parts.append("&nbsp;&nbsp;")
        elif t in ("drawing", "pict", "object", "embeddedObject"):
            has_img = True
    text = "".join(parts)
    if not text.strip() and not has_img:
        return ""
    if has_img:
        text = f"{text} <span class='ph'>[图片]</span>".strip()
    return f"<p>{text}</p>"


def _docx_table_html(tbl: ET.Element) -> str:
    rows: list[str] = []
    for tr in tbl:
        if _local(tr.tag) != "tr":
            continue
        cells: list[str] = []
        for tc in tr:
            if _local(tc.tag) != "tc":
                continue
            colspan = 1
            for sub in tc:
                if _local(sub.tag) != "tcPr":
                    continue
                for prop in sub:
                    if _local(prop.tag) == "gridSpan":
                        try:
                            colspan = max(1, int(_attr_val(prop, "val") or "1"))
                        except ValueError:
                            colspan = 1
            inner: list[str] = []
            for cc in tc:
                t = _local(cc.tag)
                if t == "p":
                    inner.append(_docx_para_html(cc))
                elif t == "tbl":
                    inner.append(_docx_table_html(cc))
            cells.append(f"<td colspan='{colspan}'>{''.join(inner)}</td>")
        rows.append(f"<tr>{''.join(cells)}</tr>")
    if not rows:
        return ""
    return f"<table class='pv-table'><tbody>{''.join(rows)}</tbody></table>"


def _docx_block_parts(el: ET.Element, out: list[str]) -> None:
    for child in el:
        t = _local(child.tag)
        if t == "p":
            out.append(_docx_para_html(child))
        elif t == "tbl":
            out.append(_docx_table_html(child))
        elif t == "sdt":  # 结构文档标签，内容在 sdtContent
            for inner in child:
                if _local(inner.tag) == "sdtContent":
                    _docx_block_parts(inner, out)


def docx_to_html(data: bytes) -> str:
    """提取 docx 主文档文本为 HTML（段落 + 表格；图片给占位符）。"""
    try:
        with io.BytesIO(data) as buf, zipfile.ZipFile(buf) as zf:
            xml = zf.read("word/document.xml")
    except Exception:  # noqa: BLE001 非标准 docx
        return _info_page_html("Word 文档", "该 .docx 结构无法解析，暂不能在线预览。")
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return _info_page_html("Word 文档", "该 .docx 结构无法解析，暂不能在线预览。")
    parts: list[str] = []
    for child in root:
        if _local(child.tag) == "body":
            _docx_block_parts(child, parts)
    body = "".join(x for x in parts if x)
    if not body:
        body = "<p>（未在文档中提取到文字内容，可能为纯图片/扫描件）</p>"
    return _wrap_html("Word 文档预览", body)


# ---------- PPTX（zip + 每页 slide XML 的文本段落） ----------


def _pptx_collect_text(node: ET.Element, out: list[str]) -> None:
    t = _local(node.tag)
    if t == "t":
        out.append(node.text or "")
        return
    if t == "br":
        out.append("\n")
        return
    if t == "tab":
        out.append("\t")
        return
    if t == "p":
        out.append("\n")
    for c in node:
        _pptx_collect_text(c, out)


def pptx_to_html(data: bytes) -> str:
    """把 pptx 每页文字按幻灯片输出为 HTML（图片/版式不强求还原）。"""
    try:
        with io.BytesIO(data) as buf, zipfile.ZipFile(buf) as zf:
            slide_names = sorted(
                (
                    n
                    for n in zf.namelist()
                    if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)
                ),
                key=lambda n: int(re.search(r"(\d+)", n).group(1)),
            )
            slides: list[str] = []
            for name in slide_names:
                num = int(re.search(r"(\d+)", name).group(1))
                try:
                    root = ET.fromstring(zf.read(name))
                except ET.ParseError:
                    continue
                out: list[str] = []
                _pptx_collect_text(root, out)
                text = "".join(out)
                lines = [ln.strip() for ln in text.split("\n")]
                text = "\n".join(ln for ln in lines if ln).strip()
                if text:
                    body = html_mod.escape(text).replace("\n", "<br>")
                else:
                    body = "<p class='muted'>（本页无文字内容，可能为图片或图表）</p>"
                slides.append(
                    f"<div class='slide'><div class='slide-no'>第 {num} 张幻灯片</div>"
                    f"<div class='slide-body'>{body}</div></div>"
                )
    except Exception:  # noqa: BLE001 非标准 pptx
        return _info_page_html("PPT 演示文稿", "该 .pptx 结构无法解析，暂不能在线预览。")
    if not slides:
        return _info_page_html("PPT 演示文稿", "未在演示文稿中提取到可预览内容。")
    return _wrap_html("PPT 演示文稿预览", "".join(slides))


# ---------- XLSX / CSV（表格 HTML） ----------


def _cell_text(v) -> str:
    if v is None:
        return ""
    s = str(v)
    s = re.sub(r"\s+", " ", s).strip()
    if len(s) > 300:
        s = s[:300] + "…"
    return s


def xlsx_to_html(path: Path) -> str:
    """openpyxl 读取所有工作表 → 表格 HTML（只读，限制行列避免超大文件拖垮预览）。"""
    try:
        from openpyxl import load_workbook
    except Exception as e:  # noqa: BLE001
        return _info_page_html("Excel 工作簿", f"缺少 openpyxl，暂不能在线预览（{e}）。")
    sections: list[str] = []
    try:
        wb = load_workbook(path, read_only=True, data_only=True)
        for idx, ws in enumerate(wb.worksheets[:50]):
            rows_html: list[str] = []
            total_rows = 0
            for row in ws.iter_rows(values_only=True):
                total_rows += 1
                if total_rows > 2000:
                    break
                cells = "".join(
                    f"<td>{html_mod.escape(_cell_text(v))}</td>"
                    for v in row[:80]
                )
                rows_html.append(f"<tr>{cells}</tr>")
            head = f"<div class='sheet'><div class='sheet-name'>{html_mod.escape(ws.title)}</div>"
            if not rows_html:
                head += "<p class='muted'>（工作表为空）</p></div>"
            else:
                head += (
                    "<div class='table-scroll'><table class='pv-table'>"
                    f"<tbody>{''.join(rows_html)}</tbody></table></div></div>"
                )
            if total_rows > 2000:
                head = head.replace(
                    "</div></div>",
                    "<p class='muted'>（仅预览前 2000 行，完整内容见原文件）</p></div></div>",
                    1,
                )
            sections.append(head)
        wb.close()
    except Exception:  # noqa: BLE001
        return _info_page_html("Excel 工作簿", "该 Excel 文件无法解析，暂不能在线预览。")
    if not sections:
        return _info_page_html("Excel 工作簿", "工作簿中没有可预览的工作表。")
    return _wrap_html("Excel 工作簿预览", "".join(sections))


def csv_to_html(data: bytes) -> str:
    text = decode_bytes(data)
    try:
        reader = list(csv.reader(io.StringIO(text)))
    except Exception:  # noqa: BLE001
        reader = []
    if not reader:
        return _wrap_html("CSV 文件预览", "<p>（CSV 无可预览内容）</p>")
    total = len(reader)
    rows_html: list[str] = []
    for row in reader[:1000]:
        cells = "".join(
            f"<td>{html_mod.escape(_cell_text(v))}</td>" for v in row[:80]
        )
        rows_html.append(f"<tr>{cells}</tr>")
    note = f"<p class='muted'>共 {total} 行，展示前 {min(1000, total)} 行。</p>" if total > 1000 else ""
    return _wrap_html(
        "CSV 文件预览",
        note + f"<div class='table-scroll'><table class='pv-table'><tbody>{''.join(rows_html)}</tbody></table></div>",
    )


# ---------- 汇总入口 + 通用 HTML 外壳 ----------


_PV_CSS = """
*{box-sizing:border-box}
body{margin:0;background:#fff;color:#24292f;
  font:14px/1.7 -apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif}
.pv{padding:20px 24px;word-break:break-word}
.pv h1{font-size:18px;margin:0 0 4px}
.pv .meta{color:#6b7280;font-size:12px;margin-bottom:12px}
.pv p{margin:6px 0}
.pv .muted{color:#6b7280}
.pv .ph{color:#b45309;font-size:12px;border:1px dashed #d9a460;border-radius:4px;padding:0 4px}
.pv .pv-info{text-align:center;color:#4b5563;padding:60px 30px}
.pv .pv-info-icon{font-size:48px;margin-bottom:10px}
.pv .pv-info-title{font-size:16px;font-weight:600;color:#111827;margin-bottom:6px}
.pv .pv-table{border-collapse:collapse;width:100%;margin:8px 0;font-size:13px}
.pv .pv-table td,.pv .pv-table th{border:1px solid #d0d7de;padding:4px 8px;vertical-align:top}
.table-scroll{overflow:auto;max-height:60vh}
.sheet{margin-bottom:14px}
.sheet-name{font-weight:600;color:#1f2328;margin:6px 0;border-left:3px solid #1677ff;padding-left:8px}
.slide{margin-bottom:16px;border:1px solid #e5e7eb;border-radius:8px;overflow:hidden}
.slide-no{background:#f3f4f6;padding:4px 12px;font-weight:600;font-size:13px;color:#374151}
.slide-body{padding:10px 14px}
.slide-body p{margin:4px 0}
"""


def _wrap_html(title: str, body: str) -> str:
    return (
        "<!DOCTYPE html><html lang='zh'><head><meta charset='utf-8'>"
        f"<title>{html_mod.escape(title)}</title><style>{_PV_CSS}</style></head>"
        f"<body class='pv'>{body}</body></html>"
    )


def render_office_preview(filename: str, path: Path) -> str:
    """office-preview 接口入口：按扩展名生成可 iframe 的 HTML 预览页。

    .docx/.pptx 走 XML 提取，.xlsx/.csv 走表格渲染；
    旧版 Office 与未知二进制格式返回信息页（前端提供「下载原文件」自查）。
    """
    kind = preview_kind(filename)
    ext = Path(filename or "").suffix.lower()
    if kind == "docx":
        try:
            return docx_to_html(path.read_bytes())
        except Exception:  # noqa: BLE001
            return _info_page_html("Word 文档", "读取文件失败，无法在线预览。")
    if kind == "pptx":
        try:
            return pptx_to_html(path.read_bytes())
        except Exception:  # noqa: BLE001
            return _info_page_html("PPT 演示文稿", "读取文件失败，无法在线预览。")
    if kind == "xlsx":
        return xlsx_to_html(path)
    if kind == "csv":
        try:
            return csv_to_html(path.read_bytes())
        except Exception:  # noqa: BLE001
            return _info_page_html("CSV 文件", "读取文件失败，无法在线预览。")
    # 旧版 Office / 压缩包 / 其他二进制
    friendly = {
        ".doc": "Word 97-2003 (.doc)",
        ".xls": "Excel 97-2003 (.xls)",
        ".ppt": "PowerPoint 97-2003 (.ppt)",
    }.get(ext, f".{ext.lstrip('.')} 格式")
    return _info_page_html(
        friendly,
        "旧版 Office / 二进制格式暂不支持在浏览器中直接预览，"
        "点击下方「下载原文件」在本地打开查看。",
    )
