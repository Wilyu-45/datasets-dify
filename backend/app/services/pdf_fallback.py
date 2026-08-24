"""PDF 解析 fallback 链（2026-07 新增）。

背景：MinerU 服务端在处理旧版 PDF（Acrobat PDFWriter 5.0 + GBK-EUC-H CMap）
时不能解码 Type0 中文字体，导致只识别到 ASCII 范围的数字（如 "2010 12"）。
但 PyMuPDF (fitz) 能正确处理 GBK-EUC-H CMap，提取完整中文文本。

Fallback 链（按效果排序）：
    Tier 1: PyMuPDF 渲染 PDF 为图片 → 打包为新 PDF → 上传给 MinerU vlm-engine
            （★ 推荐：VLM 走视觉路径，能识别版式、删除页码/页脚、给出准确结构）
    Tier 2: PyMuPDF 提取文本层 → 包装为新 PDF → 上传给 MinerU hybrid-engine
            （次选：MinerU 走文本路径，比 Tier 1 快，仍能给出版式化结构）
    Tier 3: PyMuPDF 直接文本提取（不调 API，最快；输出结构最简）

触发条件：MinerU 解析后 v2 块数过少（< _PARSE_QUALITY_MIN_BLOCKS）
且 PyMuPDF 能从 PDF 提取到 ≥ _PYMU_FALLBACK_MIN_CHARS 字符。
"""
from __future__ import annotations

import json
import logging
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, TYPE_CHECKING

from app.config import settings

if TYPE_CHECKING:
    from app.services.mineru_client import MinerUClient

log = logging.getLogger("ragsystem.pdf_fallback")


# 触发 fallback 的阈值（与 chunker 的 _is_parse_content_trivial 对齐）
_PYMU_FALLBACK_MIN_CHARS = 100  # 至少能提取到 100 字符才认为有内容
_PYMU_FALLBACK_MIN_PAGES = 1

# 渲染参数
_IMAGE_RENDER_DPI = 200  # 200 DPI 兼顾清晰度与文件大小
_IMAGE_RENDER_FORMAT = "png"  # PNG 无损


@dataclass
class FallbackResult:
    """fallback 解析的产物。"""

    parse_dir: Path
    md_path: Path
    v2_path: Path
    middle_path: Path
    model_path: Path
    char_count: int
    page_count: int
    used_fallback: bool = True
    backend: str = "pymupdf-fallback"


def is_pymupdf_available() -> bool:
    """检查 PyMuPDF 是否可用。"""
    try:
        import fitz  # noqa: F401
        return True
    except ImportError:
        return False


# ============ Tier 1: 图片 → VLM（★ 推荐） ============


def render_pdf_to_image_pdf(
    pdf_path: Path,
    output_pdf_path: Path,
    *,
    dpi: int = _IMAGE_RENDER_DPI,
) -> Path:
    """用 PyMuPDF 把原 PDF 每页渲染为高分辨率图片，组合成新的「图片型 PDF」。

    用途：把"文本型 PDF（含 GBK-EUC-H CMap 等 MinerU 不识别的字体）"
    转换为"图片型 PDF（每页是全屏图片）"，让 MinerU vlm-engine 走视觉路径。

    与 PyMuPDF 文本提取的关键区别：
        - 文本提取：依赖 CMap 解码，对 GBK-EUC-H 有效但丢失版式（页眉/页脚/位置）
        - 图片渲染：不依赖 CMap，保留完整视觉信息，让 VLM 自己识别版式

    Args:
        pdf_path: 原 PDF 路径
        output_pdf_path: 输出图片型 PDF 路径
        dpi: 渲染分辨率（默认 200）

    Returns:
        output_pdf_path
    """
    try:
        import fitz
    except ImportError as e:
        raise RuntimeError(
            "PyMuPDF (fitz) not installed. Run: pip install pymupdf"
        ) from e

    pdf_path = Path(pdf_path).resolve()
    output_pdf_path = Path(output_pdf_path).resolve()
    if not pdf_path.is_file():
        raise FileNotFoundError(f"PDF 不存在: {pdf_path}")

    src_doc = fitz.open(str(pdf_path))
    try:
        page_count = src_doc.page_count
        # 创建新的空 PDF，复制原 PDF 的页面尺寸
        dst_doc = fitz.open()
        try:
            # 计算缩放矩阵（DPI 72 = 1 倍）
            zoom = dpi / 72.0
            matrix = fitz.Matrix(zoom, zoom)

            for page_num, page in enumerate(src_doc):
                # 1) 把页面渲染为 PNG bytes
                pix = page.get_pixmap(matrix=matrix, alpha=False)
                img_bytes = pix.tobytes(_IMAGE_RENDER_FORMAT)

                # 2) 在新 PDF 中创建同尺寸的页面
                #    用 1.0x 缩放（图片已包含 dpi），用 PDF 单位（point）作为页面大小
                page_rect = fitz.Rect(0, 0, page.rect.width, page.rect.height)
                new_page = dst_doc.new_page(
                    width=page_rect.width, height=page_rect.height
                )

                # 3) 把图片插入为整页内容
                new_page.insert_image(page_rect, stream=img_bytes)

            dst_doc.save(str(output_pdf_path))
        finally:
            dst_doc.close()
    finally:
        src_doc.close()

    log.info(
        "PyMuPDF 已把 %s 渲染为图片型 PDF (dpi=%d, %d 页, %s)",
        pdf_path.name, dpi, page_count, output_pdf_path.name,
    )
    return output_pdf_path


def vlm_image_fallback(
    pdf_path: Path,
    parsed_dir: Path,
    client: "MinerUClient",
) -> Optional[FallbackResult]:
    """Tier 1 fallback：PyMuPDF 渲染为图片 → MinerU vlm-engine 读图。

    优势：
        - 不依赖 PDF 文本层 / CMap，对任何 PDF 都有效
        - VLM 能识别版式，删除页码/页脚/页眉
        - 输出结构（章节、标题、段落）与 MinerU 直接解析一致

    Args:
        pdf_path: 原 PDF 路径
        parsed_dir: 期望的解析产物目录
        client: MinerUClient 实例（应是 vlm-engine 或 hybrid-engine）

    Returns:
        FallbackResult 如果成功；None 如果任何步骤失败
    """
    if not is_pymupdf_available():
        log.warning("PyMuPDF 未安装，跳过 Tier 1 fallback。pip install pymupdf")
        return None

    log.warning(
        "Tier 1 fallback 启动：PyMuPDF 渲染图片 → MinerU vlm-engine 读图 (PDF: %s)",
        pdf_path.name,
    )
    pdf_path = Path(pdf_path).resolve()
    parsed_dir = Path(parsed_dir).resolve()

    # 1) 渲染为图片型 PDF（临时文件）
    tmp_dir = settings.logs_dir / "_vlm_fallback_tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    image_pdf_path = tmp_dir / f"{pdf_path.stem}_img.pdf"

    try:
        render_pdf_to_image_pdf(pdf_path, image_pdf_path, dpi=_IMAGE_RENDER_DPI)
    except Exception as e:  # noqa: BLE001
        log.error("PyMuPDF 渲染失败: %s", e)
        return None

    # 2) 用 MinerU vlm-engine 解析图片 PDF
    try:
        result = client.parse_file(image_pdf_path, parsed_dir)
    except Exception as e:  # noqa: BLE001
        log.error("MinerU vlm-engine 解析图片 PDF 失败: %s", e)
        return None
    finally:
        # 清理临时图片 PDF
        try:
            image_pdf_path.unlink(missing_ok=True)
        except OSError:
            pass

    # 3) 验证产物：检查 v2 字符数是否足够
    v2_files = list(parsed_dir.rglob("*_content_list_v2.json"))
    if not v2_files:
        log.warning("VLM fallback 未生成 v2 文件")
        return None

    total_chars = 0
    try:
        v2 = json.loads(v2_files[0].read_text(encoding="utf-8"))
        for page_blocks in v2:
            for block in page_blocks or []:
                content = block.get("content", {})
                for tc in content.get("title_content", []):
                    total_chars += len(tc.get("content", ""))
                for pc in content.get("paragraph_content", []):
                    total_chars += len(pc.get("content", ""))
    except Exception:  # noqa: BLE001
        pass

    md_files = list(parsed_dir.rglob("*.md"))
    if md_files:
        try:
            total_chars += len(md_files[0].read_text(encoding="utf-8").strip())
        except OSError:
            pass

    if total_chars < _PYMU_FALLBACK_MIN_CHARS:
        log.warning(
            "VLM fallback 提取字符数过少 (%d < %d)，放弃",
            total_chars, _PYMU_FALLBACK_MIN_CHARS,
        )
        return None

    # 4) 标记产物 backend 为 vlm-image-fallback（区分纯 MinerU）
    model_files = list(parsed_dir.rglob("*_model.json"))
    if model_files:
        try:
            model_data = json.loads(model_files[0].read_text(encoding="utf-8"))
            if isinstance(model_data, list) and model_data:
                if isinstance(model_data[0], dict):
                    model_data[0]["backend"] = "vlm-image-fallback"
                    model_data[0]["note"] = (
                        "PyMuPDF 渲染 PDF 为图片 + MinerU vlm-engine 读图"
                    )
                    model_files[0].write_text(
                        json.dumps(model_data, ensure_ascii=False, indent=2),
                        encoding="utf-8",
                    )
        except Exception:  # noqa: BLE001
            pass

    log.info(
        "VLM fallback 成功: %d 字符 (backend=vlm-image-fallback, %s)",
        total_chars, result.parse_dir.name,
    )
    return FallbackResult(
        parse_dir=result.parse_dir,
        md_path=result.md_path or Path(""),
        v2_path=v2_files[0],
        middle_path=result.json_path or Path(""),
        model_path=model_files[0] if model_files else Path(""),
        char_count=total_chars,
        page_count=0,  # VLM 路径不单独统计页数
        used_fallback=True,
        backend="vlm-image-fallback",
    )


# ============ Tier 2: 文本层 → 包装 PDF → MinerU hybrid-engine ============


# A4 页面尺寸（PDF 单位 = 1/72 inch）
_T2_PAGE_WIDTH = 595.0
_T2_PAGE_HEIGHT = 842.0
_T2_MARGIN = 50.0
_T2_FONT_SIZE = 11.0
# PyMuPDF 嵌入字体的简化名（PyMuPDF 拒绝带空格的字体名，如 "Microsoft YaHei Regular"）
_T2_FONT_NAME = "msyh"

# Windows 系统字体路径（候选，按优先级匹配；找不到时回退到 PyMuPDF 内置 china-s）
_T2_FONT_CANDIDATES_WINDOWS = (
    r"C:\Windows\Fonts\msyh.ttc",     # 微软雅黑
    r"C:\Windows\Fonts\simhei.ttf",   # 黑体
    r"C:\Windows\Fonts\simsun.ttc",   # 宋体
    r"C:\Windows\Fonts\Deng.ttf",     # 等线
)


def _resolve_chinese_font_path() -> Optional[Path]:
    """找到系统中可用的中文字体文件路径。

    优先级：Windows 系统字体（msyh / simhei / simsun / Deng） → None（回退到 china-s）
    """
    # 优先 Windows 系统路径
    if sys.platform == "win32":
        for cand in _T2_FONT_CANDIDATES_WINDOWS:
            p = Path(cand)
            if p.is_file():
                return p
    # Linux/macOS 可扩展：找 Noto Sans CJK / Source Han Sans 等
    # 暂不实现，保持简单
    return None


def write_text_to_pdf(
    pages: List[Dict[str, Any]],
    output_pdf_path: Path,
    *,
    page_width: float = _T2_PAGE_WIDTH,
    page_height: float = _T2_PAGE_HEIGHT,
    margin: float = _T2_MARGIN,
    font_size: float = _T2_FONT_SIZE,
    font_name: str = _T2_FONT_NAME,
) -> Path:
    """把 PyMuPDF 提取的文本包装为新 PDF（每页一段文本）。

    用途：把"文本型 PDF（含 GBK-EUC-H CMap 等 MinerU 不识别的字体）"
    转换为"可读文本 PDF（嵌入中文字体）"，让 MinerU hybrid-engine
    走文本路径 + VLM 辅助解析，给出准确结构。

    与 PyMuPDF 渲染为图片 PDF 的区别：
        - 图片 PDF：依赖图片识别，VLM 读图（Tier 1）
        - 文本 PDF：依赖文本提取，hybrid-engine 文本路径（Tier 2，更快）

    字体策略：
        - 优先嵌入系统 msyh.ttc（微软雅黑），用 `doc.subset_fonts()` 子集化
          后 PDF 大小约 100~300KB（而不是嵌入完整字体的 19MB）
        - 找不到系统字体时回退到 PyMuPDF 内置 `china-s`（CID 字体），
          无 CMap 嵌入但部分解析器可能识别失败
        - 字体注册失败时回退到默认字体（不阻塞流程）

    Args:
        pages: extract_with_pymupdf 返回的 pages 列表，每项含 'text' 字段
        output_pdf_path: 输出 PDF 路径
        page_width / page_height: 页面尺寸（PDF point）
        margin: 边距
        font_size: 字号
        font_name: PyMuPDF 字体名（默认 "msyh"，对应嵌入的微软雅黑）

    Returns:
        output_pdf_path
    """
    try:
        import fitz
    except ImportError as e:
        raise RuntimeError(
            "PyMuPDF (fitz) not installed. Run: pip install pymupdf"
        ) from e

    output_pdf_path = Path(output_pdf_path).resolve()
    output_pdf_path.parent.mkdir(parents=True, exist_ok=True)

    # 找到可用的中文字体文件
    font_path = _resolve_chinese_font_path()
    use_embedded_font = font_path is not None
    if not use_embedded_font:
        log.warning(
            "未找到系统中文字体（msyh.ttc / simhei.ttf / simsun.ttc / Deng.ttf），"
            "Tier 2 fallback 将回退到 PyMuPDF 内置 china-s CID 字体"
            "（部分 PDF 解析器可能识别失败）。"
        )

    doc = fitz.open()
    try:
        text_rect = fitz.Rect(margin, margin, page_width - margin, page_height - margin)
        overflow_count = 0
        for page_data in pages:
            page = doc.new_page(width=page_width, height=page_height)
            text = page_data.get("text", "")
            if not text:
                continue

            # 注册字体（嵌入到每一页；PyMuPDF 会在 subset_fonts 时自动去重）
            if use_embedded_font:
                try:
                    page.insert_font(
                        fontname=font_name, fontfile=str(font_path)
                    )
                except Exception as reg_err:  # noqa: BLE001
                    # 单页注册失败：标记为不用嵌入字体，后续用默认
                    log.warning(
                        "write_text_to_pdf: page.insert_font 失败: %s，回退到默认字体",
                        reg_err,
                    )
                    use_embedded_font = False

            # 写入文本
            actual_font = font_name if use_embedded_font else "china-s"
            try:
                rc = page.insert_textbox(
                    text_rect, text, fontsize=font_size, fontname=actual_font
                )
            except Exception as font_err:  # noqa: BLE001
                # 字体不支持时回退到默认字体（不阻塞主流程）
                log.warning(
                    "write_text_to_pdf: 字体 %s 不可用，回退到默认: %s",
                    actual_font, font_err,
                )
                rc = page.insert_textbox(text_rect, text, fontsize=font_size)
            if rc < 0:
                overflow_count += 1

        # ★ 关键：保存前做字体子集化（subset_fonts），把用到的字符子集
        # 从完整字体文件中抽出来嵌入。子集化后用 ez_save 做垃圾回收 + 压缩。
        # 不子集化时嵌入完整 msyh.ttc = 19MB，子集化后约 100~300KB。
        if use_embedded_font:
            try:
                doc.subset_fonts()
            except Exception as sub_err:  # noqa: BLE001
                # subset_fonts 失败不阻塞：至少能保存包含完整字体的 PDF（但会很大）
                log.warning("write_text_to_pdf: subset_fonts 失败: %s", sub_err)
        # ez_save: garbage=4 + deflate=True（彻底 GC + 流压缩）
        doc.save(
            str(output_pdf_path),
            garbage=4,
            deflate=True,
            clean=True,
        )
        if overflow_count:
            log.warning(
                "write_text_to_pdf: %d 页文本溢出（字号 %s 太小或内容太长）",
                overflow_count, font_size,
            )
    finally:
        doc.close()

    log.info(
        "write_text_to_pdf: 已把 %d 页文本写入 %s (字体=%s, 字号=%s, 嵌入=%s, 大小=%s bytes)",
        len(pages), output_pdf_path.name,
        font_name if use_embedded_font else "china-s",
        font_size, use_embedded_font, output_pdf_path.stat().st_size,
    )
    return output_pdf_path


def text_to_mineru_fallback(
    pdf_path: Path,
    parsed_dir: Path,
    client: "MinerUClient",
) -> Optional[FallbackResult]:
    """Tier 2 fallback：PyMuPDF 提取文本 → 包装为新 PDF → 上传给 MinerU 解析。

    与 Tier 1 区别：
        - Tier 1：渲染为图片（200 DPI PNG）→ vlm-engine 视觉路径
                （最准，识别版式/页眉/页脚；最慢）
        - Tier 2：提取文本层 → 包装为可读文本 PDF → hybrid-engine 文本路径
                （次准，hybrid-engine 走文本 + VLM 辅助；较快）

    与 Tier 3 区别：
        - Tier 3：纯 PyMuPDF 文本提取（不调 API，最快但结构简单）
        - Tier 2：仍调 MinerU API，能获得更准确的章节/标题结构

    Args:
        pdf_path: 原 PDF 路径
        parsed_dir: 期望的解析产物目录
        client: MinerUClient 实例（推荐 hybrid-engine，但任何 backend 都行）

    Returns:
        FallbackResult 如果成功；None 如果任何步骤失败
    """
    if not is_pymupdf_available():
        log.warning("PyMuPDF 未安装，跳过 Tier 2 fallback。pip install pymupdf")
        return None

    log.warning(
        "Tier 2 fallback 启动：PyMuPDF 提取文本 → 包装 PDF → MinerU 解析 (PDF: %s, backend=%s)",
        pdf_path.name, client.backend,
    )
    pdf_path = Path(pdf_path).resolve()
    parsed_dir = Path(parsed_dir).resolve()

    # 1) PyMuPDF 提取文本（按页 + block）
    try:
        extracted = extract_with_pymupdf(pdf_path)
    except Exception as e:  # noqa: BLE001
        log.error("PyMuPDF 文本提取失败: %s", e)
        return None

    if extracted["total_chars"] < _PYMU_FALLBACK_MIN_CHARS:
        log.warning(
            "Tier 2 提取字符数过少 (%d < %d)，放弃",
            extracted["total_chars"], _PYMU_FALLBACK_MIN_CHARS,
        )
        return None

    # 2) 包装为新 PDF（可读文本，无 CMap 依赖）
    tmp_dir = settings.logs_dir / "_vlm_fallback_tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    text_pdf_path = tmp_dir / f"{pdf_path.stem}_text.pdf"

    try:
        write_text_to_pdf(extracted["pages"], text_pdf_path)
    except Exception as e:  # noqa: BLE001
        log.error("write_text_to_pdf 失败: %s", e)
        return None

    # 3) MinerU 解析文本 PDF
    try:
        result = client.parse_file(text_pdf_path, parsed_dir)
    except Exception as e:  # noqa: BLE001
        log.error("MinerU 解析文本 PDF 失败: %s", e)
        return None
    finally:
        # 清理临时文本 PDF
        try:
            text_pdf_path.unlink(missing_ok=True)
        except OSError:
            pass

    # 4) 验证产物：检查 v2 字符数是否足够
    v2_files = list(parsed_dir.rglob("*_content_list_v2.json"))
    if not v2_files:
        log.warning("Tier 2 fallback 未生成 v2 文件")
        return None

    total_chars = 0
    try:
        v2 = json.loads(v2_files[0].read_text(encoding="utf-8"))
        for page_blocks in v2:
            for block in page_blocks or []:
                content = block.get("content", {})
                for tc in content.get("title_content", []):
                    total_chars += len(tc.get("content", ""))
                for pc in content.get("paragraph_content", []):
                    total_chars += len(pc.get("content", ""))
    except Exception:  # noqa: BLE001
        pass

    md_files = list(parsed_dir.rglob("*.md"))
    if md_files:
        try:
            total_chars += len(md_files[0].read_text(encoding="utf-8").strip())
        except OSError:
            pass

    if total_chars < _PYMU_FALLBACK_MIN_CHARS:
        log.warning(
            "Tier 2 fallback MinerU 输出过少 (%d < %d)，放弃",
            total_chars, _PYMU_FALLBACK_MIN_CHARS,
        )
        return None

    # 5) 标记产物 backend 为 pymupdf-text-fallback
    model_files = list(parsed_dir.rglob("*_model.json"))
    if model_files:
        try:
            model_data = json.loads(model_files[0].read_text(encoding="utf-8"))
            if isinstance(model_data, list) and model_data:
                if isinstance(model_data[0], dict):
                    model_data[0]["backend"] = "pymupdf-text-fallback"
                    model_data[0]["note"] = (
                        "PyMuPDF 提取 PDF 文本 + 包装为可读 PDF + "
                        "MinerU hybrid-engine 解析（文本路径）"
                    )
                    model_files[0].write_text(
                        json.dumps(model_data, ensure_ascii=False, indent=2),
                        encoding="utf-8",
                    )
        except Exception:  # noqa: BLE001
            pass

    log.info(
        "Tier 2 fallback 成功: %d 字符 (backend=pymupdf-text-fallback, %s)",
        total_chars, result.parse_dir.name,
    )
    return FallbackResult(
        parse_dir=result.parse_dir,
        md_path=result.md_path or Path(""),
        v2_path=v2_files[0],
        middle_path=result.json_path or Path(""),
        model_path=model_files[0] if model_files else Path(""),
        char_count=total_chars,
        page_count=extracted["page_count"],
        used_fallback=True,
        backend="pymupdf-text-fallback",
    )


# ============ Tier 3: 纯文本提取（最快，结构最简） ============


def extract_with_pymupdf(pdf_path: Path) -> Dict[str, Any]:
    """用 PyMuPDF 提取 PDF 文本层（按页 + 按 block）。

    Returns:
        dict with keys: pages, total_chars, char_count_by_page
    """
    try:
        import fitz
    except ImportError as e:
        raise RuntimeError(
            "PyMuPDF (fitz) not installed. Run: pip install pymupdf"
        ) from e

    pdf_path = Path(pdf_path).resolve()
    if not pdf_path.is_file():
        raise FileNotFoundError(f"PDF 不存在: {pdf_path}")

    doc = fitz.open(str(pdf_path))
    pages: List[Dict[str, Any]] = []
    total_chars = 0
    try:
        for page_idx, page in enumerate(doc):
            # 提取所有文本块（按视觉位置排序）
            blocks_raw = page.get_text("dict")
            page_blocks: List[Dict[str, Any]] = []
            page_text_parts: List[str] = []

            for block in blocks_raw.get("blocks", []):
                if block.get("type") != 0:  # 0 = text, 1 = image
                    continue
                # 拼装 block 文本
                block_text_parts: List[str] = []
                for line in block.get("lines", []):
                    line_parts: List[str] = []
                    for span in line.get("spans", []):
                        text = span.get("text", "")
                        if text:
                            line_parts.append(text)
                    if line_parts:
                        block_text_parts.append("".join(line_parts))
                block_text = "\n".join(block_text_parts).strip()
                if not block_text:
                    continue
                bbox = block.get("bbox", [0, 0, 0, 0])
                # 简单判断是否标题：按 bbox 高度 + 是否含 "第X条"
                page_blocks.append({
                    "bbox": [bbox[0], bbox[1], bbox[2], bbox[3]],
                    "text": block_text,
                })
                page_text_parts.append(block_text)

            page_text = "\n".join(page_text_parts)
            total_chars += len(page_text)
            pages.append({
                "page_num": page_idx + 1,
                "blocks": page_blocks,
                "text": page_text,
            })
    finally:
        doc.close()

    return {
        "pages": pages,
        "total_chars": total_chars,
        "page_count": len(pages),
    }


def write_fallback_outputs(
    pdf_path: Path,
    parsed_dir: Path,
    extracted: Dict[str, Any],
) -> FallbackResult:
    """把 PyMuPDF 提取结果写成与 MinerU 一致的目录结构。"""
    pdf_path = Path(pdf_path).resolve()
    parsed_dir = Path(parsed_dir).resolve()
    if parsed_dir.exists():
        shutil.rmtree(parsed_dir, ignore_errors=True)
    parsed_dir.mkdir(parents=True, exist_ok=True)

    stem = parsed_dir.name
    inner = parsed_dir / "hybrid_auto"
    inner.mkdir(parents=True, exist_ok=True)

    # 1) .md
    md_path = inner / f"{stem}.md"
    md_lines: List[str] = []
    for page in extracted["pages"]:
        if page["text"]:
            md_lines.append(page["text"])
            md_lines.append("")  # 段落分隔
    md_path.write_text("\n".join(md_lines), encoding="utf-8")

    # 2) content_list_v2.json (结构与 MinerU v2 一致)
    v2_path = inner / f"{stem}_content_list_v2.json"
    v2_data: List[List[Dict[str, Any]]] = []
    for page in extracted["pages"]:
        page_blocks: List[Dict[str, Any]] = []
        for blk in page["blocks"]:
            text = blk["text"]
            # 简单判断标题/段落（与 chunker._maybe_promote_to_title 兼容）
            is_title = (
                text.startswith("第") and ("章" in text or "条" in text)
            ) or text.endswith("通知") or text.startswith("济政发")
            if is_title:
                page_blocks.append({
                    "type": "title",
                    "content": {
                        "level": 1,
                        "title_content": [{"type": "text", "content": text}],
                    },
                    "bbox": blk["bbox"],
                })
            else:
                page_blocks.append({
                    "type": "paragraph",
                    "content": {
                        "paragraph_content": [{"type": "text", "content": text}],
                    },
                    "bbox": blk["bbox"],
                })
        v2_data.append(page_blocks)
    v2_path.write_text(
        json.dumps(v2_data, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # 3) middle.json (MinerU 风格)
    middle_path = parsed_dir / f"{stem}_middle.json"
    middle_data: List[Dict[str, Any]] = []
    for page in extracted["pages"]:
        middle_data.append({
            "page_num": page["page_num"],
            "blocks": page["blocks"],
        })
    middle_path.write_text(
        json.dumps(middle_data, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # 4) model.json (空 stub，标识 fallback)
    model_path = parsed_dir / f"{stem}_model.json"
    model_path.write_text(
        json.dumps(
            [{"backend": "pymupdf-fallback", "note": "MinerU 不支持 GBK-EUC-H CMap，由 PyMuPDF fallback 提取"}],
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    return FallbackResult(
        parse_dir=parsed_dir,
        md_path=md_path,
        v2_path=v2_path,
        middle_path=middle_path,
        model_path=model_path,
        char_count=extracted["total_chars"],
        page_count=extracted["page_count"],
    )


def maybe_fallback_after_mineru_failure(
    pdf_path: Path,
    parsed_dir: Path,
    client: Optional["MinerUClient"] = None,
) -> Optional[FallbackResult]:
    """MinerU 解析失败（产物过少）后的 fallback 链。

    流程（按效果从高到低）：
        Tier 1: PyMuPDF 渲染 PDF 为图片 → MinerU vlm-engine 读图
                （★ 推荐：VLM 视觉路径，识别版式、删除页码/页脚；最准、最慢）
        Tier 2: PyMuPDF 提取文本 → 包装为可读 PDF → MinerU hybrid-engine 解析
                （次选：MinerU 文本路径，比 Tier 1 快，仍给出版式化结构）
        Tier 3: PyMuPDF 纯文本提取（不调 API，最快但结构最简）

    Args:
        pdf_path: 原始 PDF
        parsed_dir: 期望的解析产物目录
        client: MinerUClient 实例（用于 Tier 1 / Tier 2 调 MinerU API）

    Returns:
        FallbackResult 如果任何 Tier 成功；None 如果都失败
    """
    if not is_pymupdf_available():
        log.warning("PyMuPDF 未安装，跳过 fallback。pip install pymupdf")
        return None

    log.warning(
        "MinerU 解析产物过少，启动 fallback 链 (PDF: %s)",
        pdf_path.name,
    )

    # ============ Tier 1: PyMuPDF 渲染 + VLM 读图 ============
    if client is not None:
        tier1 = vlm_image_fallback(pdf_path, parsed_dir, client)
        if tier1 is not None:
            return tier1
        log.warning("Tier 1 (VLM 读图) 失败，回退到 Tier 2 (PyMuPDF 文本 → MinerU 解析)")
    else:
        log.info("未提供 client，跳过 Tier 1，直接走 Tier 2")

    # ============ Tier 2: PyMuPDF 文本 → 包装 PDF → MinerU 解析 ============
    if client is not None:
        tier2 = text_to_mineru_fallback(pdf_path, parsed_dir, client)
        if tier2 is not None:
            return tier2
        log.warning("Tier 2 (文本 → MinerU) 失败，回退到 Tier 3 (纯 PyMuPDF 文本)")
    else:
        log.info("未提供 client，跳过 Tier 2，直接走 Tier 3")

    # ============ Tier 3: PyMuPDF 纯文本提取（不调 API，兜底） ============
    try:
        extracted = extract_with_pymupdf(pdf_path)
    except Exception as e:  # noqa: BLE001
        log.error("PyMuPDF 纯文本 fallback 失败: %s", e)
        return None

    if extracted["total_chars"] < _PYMU_FALLBACK_MIN_CHARS:
        log.warning(
            "PyMuPDF fallback 提取字符数过少 (%d < %d)，放弃",
            extracted["total_chars"],
            _PYMU_FALLBACK_MIN_CHARS,
        )
        return None

    result = write_fallback_outputs(pdf_path, parsed_dir, extracted)
    log.info(
        "Tier 3 (PyMuPDF 纯文本) fallback 成功: %d 页, %d 字符",
        result.page_count,
        result.char_count,
    )
    return result
