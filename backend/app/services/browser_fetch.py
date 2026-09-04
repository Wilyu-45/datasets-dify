# -*- coding: utf-8 -*-
"""浏览器内核抓取引擎（Playwright Chromium）：处理 WAF JS 挑战类站点。

背景（2026-08-31）：卫健委等政府网站使用云防护 WAF：
- 静态 httpx 请求 → 412（JS 动态令牌挑战）或 502（回源失败）；
- 真实浏览器可自动执行挑战脚本通过 412，但 502 属于源站/云防护回源故障（任何客户端都一样）。

本模块在 httpx 静态抓取失败（412/502）时降级为真实浏览器渲染：
1. browser_fetch_html(): 打开网页 → 自动等待 JS 挑战通过 → 返回渲染后 HTML（复用 HTMLToMarkdown 转换）
2. browser_cookies_for(): 通过挑战后导出 cookie 集，供附件下载走 curl_cffi（Chrome 指纹）时携带

设计注意：
- 模块级单例浏览器 + 全局锁：Playwright sync API 只能在普通线程使用（FastAPI 同步路由在线程池执行，符合要求）；
  同一时刻只执行一个浏览器操作，避免并发冲突。
- 挑战等待：监听 document 响应状态序列，出现 <400 即为通过；超时仍无成功则按最终 title/内容判定错误类型。
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger("ragsystem.browser_fetch")

# 完整 Chromium 内核探测：瑞数类 WAF 会识别默认 headless shell（UA 含 HeadlessChrome 特征），
# 卡死在 412 挑战；必须用完整内核（Playwright 在指定 executable_path 时自动使用新无头模式）。
# 注意：不能再显式传 --headless=new，会与 Playwright 自动参数冲突反而被识别。
_MS_PLAYWRIGHT = Path.home() / "AppData" / "Local" / "ms-playwright"


def _find_chromium_executable() -> Optional[str]:
    """优先使用完整 Chromium 内核（playwright install chromium 下载），找不到返回 None。"""
    try:
        for exe in sorted(_MS_PLAYWRIGHT.glob("chromium-*/chrome-win*/chrome.exe"), reverse=True):
            if exe.is_file():
                return str(exe)
    except OSError:
        pass
    return None


# 与 webscraper._USER_AGENT 保持一致（避免循环导入，单独声明）
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# 模块级单例：浏览器（懒启动）
_browser = None
_playwright = None
_lock = threading.Lock()

# 挑战页/错误页特征词（title 或 body 文本命中即认为尚未拿到真实页面）
_CHALLENGE_MARKERS = (
    "验证", "安全验证", "Just a moment", "Attention Required",
    "Checking your browser", "Verify", "verify", "校验",
)
_SOURCE_ERROR_MARKERS = (
    "云防护节点", "源站服务器", "Bad Gateway", "bad gateway",
    "服务器错误", "Service Unavailable",
)

# 反检测启动参数：隐藏自动化标记，降低被 WAF 识别为无头爬虫的概率
_LAUNCH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--disable-infobars",
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--lang=zh-CN",
]


def _get_browser():
    """懒启动单例 Chromium（线程安全）。

    注意：默认 headless 模式使用精简 headless shell，其 UA 带 HeadlessChrome 特征，
    会被瑞数类 WAF 识别并卡在 412 挑战；这里显式指定完整内核 + --headless=new。

    ★ 2026-08-31 Windows 事件循环策略修复：
    uvicorn 在 Windows 上把 asyncio 策略设为 WindowsSelectorEventLoopPolicy
    （uvicorn/loops/asyncio.py），Playwright sync API 的 asyncio.new_event_loop()
    随之拿到 SelectorEventLoop —— 它不支持子进程，启动 Node 驱动时抛
    NotImplementedError。这里在 start() 前临时切回 Proactor 策略（Python 默认、
    支持子进程），启动完成后恢复原策略，不影响 uvicorn 主循环。
    """
    global _browser, _playwright
    with _lock:
        if _browser is None:
            import asyncio
            import sys

            from playwright.sync_api import sync_playwright

            executable = _find_chromium_executable()
            launch_kwargs = dict(headless=True, args=_LAUNCH_ARGS)
            if executable:
                launch_kwargs["executable_path"] = executable
            old_policy = None
            if sys.platform == "win32" \
                    and not isinstance(asyncio.get_event_loop_policy(), asyncio.WindowsProactorEventLoopPolicy):
                old_policy = asyncio.get_event_loop_policy()
                asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
            try:
                _playwright = sync_playwright().start()
                _browser = _playwright.chromium.launch(**launch_kwargs)
            finally:
                if old_policy is not None:
                    asyncio.set_event_loop_policy(old_policy)
            log.info(
                "browser engine started (playwright chromium%s)",
                f" full-core @ {executable}" if executable else " (headless-shell)",
            )
    return _browser


def _new_context(browser):
    """新建隔离 context（独立 cookie / 存储，UA 与静态抓取保持一致）。"""
    ctx = browser.new_context(
        user_agent=_USER_AGENT,
        locale="zh-CN",
        viewport={"width": 1366, "height": 900},
    )
    # 隐藏自动化痕迹（navigator.webdriver 等），降低被 WAF 识别为无头爬虫的概率
    ctx.add_init_script(
        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
    )
    return ctx


def _challenge_passed(doc_statuses):
    """挑战是否已通过：出现非 412 的 document 响应即为通过。

    瑞数类 WAF：初次 document 响应 412（JS 挑战页），JS 执行后自动重发请求；
    之后拿到的响应（无论 200 还是源站 502）都是真实响应，说明挑战已通过。
    """
    return any(s != 412 for s in doc_statuses)


def _text_of(page) -> str:
    """取页面可见文本（失败返回空串）。"""
    try:
        return page.evaluate("() => (document.body ? document.body.innerText : '')")
    except Exception:  # noqa: BLE001
        return ""


def _looks_error_page(title: str, text: str) -> Optional[str]:
    """判断是否为源站/WAF 故障页，返回错误描述（不是则 None）。"""
    joined = f"{title}\n{text}"
    if any(m in joined for m in _SOURCE_ERROR_MARKERS):
        return "网站源站当前不可用（502），请稍后再试或确认网站在浏览器中可正常打开"
    return None


def browser_fetch_html(
    url: str,
    timeout: int = 45,
    challenge_wait: int = 20,
) -> Dict[str, Any]:
    """用真实浏览器打开 url 并等待 JS 挑战通过。

    Returns:
        {"ok": True, "html", "final_url", "title", "cookies"}
        {"ok": False, "error"} — error 为面向用户的友好描述

    说明：浏览器可自动通过 412 类 JS 挑战；但若源站/云防护回源故障（502），
    浏览器同样拿不到真实页面，此时返回明确错误而非静默失败。
    ★ 浏览器获取/上下文创建也在 try 内：任何失败都返回 ok=False，
    由调用方走降级路径（绝不向上抛异常）。
    """
    start = time.monotonic()
    ctx = None
    try:
        browser = _get_browser()
        ctx = _new_context(browser)
        page = ctx.new_page()
        doc_statuses: List[int] = []
        page.on(
            "response",
            lambda r: doc_statuses.append(r.status)
            if r.request.resource_type == "document"
            else None,
        )
        try:
            page.goto(url, timeout=timeout * 1000, wait_until="domcontentloaded")
        except Exception as e:  # noqa: BLE001
            # 412/跳转可能导致 goto 本身异常/超时——不视为失败，交由挑战等待逻辑收尾
            log.debug("browser goto warn: %s", e)

        deadline = start + timeout + challenge_wait
        passed = _challenge_passed(doc_statuses)
        while not passed and time.monotonic() < deadline:
            time.sleep(0.5)
            passed = _challenge_passed(doc_statuses)

        if not passed:
            # 超时仍全为 412：区分「反爬无法通过」与「源站/WAF 故障」
            try:
                title = page.title()
            except Exception:  # noqa: BLE001
                title = ""
            text = _text_of(page)
            err = _looks_error_page(title, text)
            if err:
                return {"ok": False, "error": err}
            return {
                "ok": False,
                "error": (
                    "网站反爬验证（412）在浏览器中未能自动通过，"
                    "请在浏览器中打开该地址确认可正常访问"
                ),
            }

        final_status = doc_statuses[-1] if doc_statuses else None
        if final_status and final_status >= 400:
            # 挑战已通过但源站/云防护返回错误（如 502 回源故障）
            try:
                title = page.title()
            except Exception:  # noqa: BLE001
                title = ""
            text = _text_of(page)
            err = _looks_error_page(title, text)
            return {"ok": False, "error": err or f"网站返回错误（HTTP {final_status}）"}

        # 已通过挑战：等网络空闲后取渲染结果
        try:
            page.wait_for_load_state("networkidle", timeout=5000)
        except Exception:  # noqa: BLE001
            pass
        html = page.content()
        final_url = page.url
        try:
            title = page.title()
        except Exception:  # noqa: BLE001
            title = ""
        cookies = [{"name": c["name"], "value": c["value"], "domain": c.get("domain") or ""} for c in ctx.cookies()]
        log.info(
            "browser fetch ok: url=%s final=%s html=%d cookies=%d dt=%.1fs",
            url, final_url, len(html), len(cookies), time.monotonic() - start,
        )
        return {"ok": True, "html": html, "final_url": final_url, "title": title, "cookies": cookies}
    except Exception as e:  # noqa: BLE001
        log.exception("browser fetch error: url=%s", url)
        return {"ok": False, "error": f"浏览器抓取异常: {e}"}
    finally:
        if ctx is not None:
            try:
                ctx.close()
            except Exception:  # noqa: BLE001
                pass


def browser_cookies_for(url: str, timeout: int = 45) -> List[Dict[str, str]]:
    """浏览器访问 url 通过挑战后导出 cookie（供附件下载携带）。

    Returns:
        cookie 列表（name/value/domain）；失败返回空列表。
    """
    start = time.monotonic()
    ctx = None
    try:
        browser = _get_browser()
        ctx = _new_context(browser)
        page = ctx.new_page()
        doc_statuses: List[int] = []
        page.on(
            "response",
            lambda r: doc_statuses.append(r.status)
            if r.request.resource_type == "document"
            else None,
        )
        try:
            page.goto(url, timeout=timeout * 1000, wait_until="domcontentloaded")
        except Exception:  # noqa: BLE001
            pass
        deadline = start + timeout
        while not _challenge_passed(doc_statuses) and time.monotonic() < deadline:
            time.sleep(0.5)
        try:
            page.wait_for_load_state("networkidle", timeout=5000)
        except Exception:  # noqa: BLE001
            pass
        cookies = [
            {"name": c["name"], "value": c["value"], "domain": c.get("domain") or ""}
            for c in ctx.cookies()
        ]
        log.info("browser cookies: url=%s cookies=%d dt=%.1fs", url, len(cookies), time.monotonic() - start)
        return cookies
    except Exception:  # noqa: BLE001
        log.exception("browser cookies error: url=%s", url)
        return []
    finally:
        if ctx is not None:
            try:
                ctx.close()
            except Exception:  # noqa: BLE001
                pass


def browser_download_file(url: str, dest: Path, timeout: int = 60) -> Optional[str]:
    """用浏览器下载附件文件到 dest（绕过 WAF 对静态下载的拦截）。

    Returns:
        成功返回下载提示字符串；失败返回 None（由调用方兜底）。
    """
    ctx = None
    try:
        browser = _get_browser()
        ctx = _new_context(browser)
        page = ctx.new_page()
        with ctx.expect_download(timeout=timeout * 1000) as dl_info:
            try:
                page.goto(url, timeout=timeout * 1000, wait_until="domcontentloaded")
            except Exception:  # noqa: BLE001
                pass
        download = dl_info.value
        dest.parent.mkdir(parents=True, exist_ok=True)
        download.save_as(str(dest))
        return "browser"
    except Exception:  # noqa: BLE001
        log.warning("browser download failed: url=%s", url)
        return None
    finally:
        if ctx is not None:
            try:
                ctx.close()
            except Exception:  # noqa: BLE001
                pass


# 打印 PDF 前注入的隐藏样式：与 webscraper.HTMLToMarkdown.SKIP_TAGS 对齐，
# 把导航栏/页脚/侧栏等版面噪音从 PDF 里去掉（MinerU 只解析可见内容）。
_PDF_HIDE_NOISE_CSS = (
    "nav,footer,aside,form,iframe,noscript,svg,canvas{display:none!important}"
)

# ★ 2026-09 打印前页面预处理（evaluate 执行的 JS）：解决「弹窗广告盖正文 / 图片不全」。
# 1) 浮层/广告/蒙层容器：与 webscraper.HTMLToMarkdown._NOISE_KEYWORDS 同一套特征词，
#    按 id/class 拆词（支持 aiModal / adv-modal / maskBox 等写法）命中即整块隐藏。
#    只处理容器标签，body/html 不参与，避免 modal-open 类状态名误杀整页。
# 2) 懒加载图片：data-src 系占位属性还原到 src、loading=lazy → eager，
#    再模拟滚动到底部让基于视口的懒加载真正拉图，最后滚回顶部等待解码。
_PDF_PREPARE_JS = r"""
async () => {
  const KW = new Set(['modal','popup','dialog','toast','mask','overlay','drawer','nav','ad','ads','adv','adsbygoogle','qrcode','layer']);
  const TAGOK = new Set(['DIV','SECTION','MAIN','UL','OL','TABLE','NAV','HEADER','FOOTER','DL']);
  const toks = (s) => (s || '').replace(/[-_]+/g, ' ').replace(/([a-z0-9])([A-Z])/g, '$1 $2').toLowerCase().split(/\s+/).filter(Boolean);
  for (const el of document.querySelectorAll('div,section,main,ul,ol,table,nav,header,footer,dl')) {
    if (el === document.body || el === document.documentElement) continue;
    const words = new Set(toks(el.id).concat(toks(typeof el.className === 'string' ? el.className : '')));
    let hit = false;
    for (const w of words) { if (KW.has(w)) { hit = true; break; } }
    if (!hit) {
      const st = (el.getAttribute('style') || '').replace(/\s+/g, '').toLowerCase();
      if (st.includes('display:none') || st.includes('visibility:hidden') || st.includes('position:fixed')) hit = true;
    }
    if (hit) { try { el.style.setProperty('display', 'none', 'important'); } catch (e) {} }
  }
  // ---- 图片：占位属性还原 + 关闭懒加载 ----
  const REAL_IMG = ['data-src', 'data-original', 'data-lazy-src', 'data-url', 'data-img', 'data-lazy', 'data-echo', 'data-actualsrc', 'data-normal'];
  for (const img of document.images) {
    img.loading = 'eager';
    for (const a of REAL_IMG) {
      const v = img.getAttribute(a);
      if (v) { img.src = v; break; }
    }
    if (img.srcset === '' || img.srcset === undefined) {
      for (const a of ['data-srcset', 'data-lazy-srcset', 'data-original-srcset']) {
        const v = img.getAttribute(a);
        if (v) { img.srcset = v; break; }
      }
    }
  }
  for (const v of document.querySelectorAll('video[data-poster]')) {
    if (!v.poster) v.poster = v.getAttribute('data-poster');
  }
  for (const s of document.querySelectorAll('source')) {
    if ((!s.srcset || s.srcset === '') && s.getAttribute('data-srcset')) s.srcset = s.getAttribute('data-srcset');
  }
  // ---- 滚动整页触发懒加载，再回顶 ----
  try {
    const step = Math.max(window.innerHeight || 800, 800);
    const total = Math.max(document.body.scrollHeight, document.documentElement.scrollHeight);
    for (let y = 0; y < total + step; y += step) {
      window.scrollTo(0, y);
      await new Promise((r) => setTimeout(r, 50));
    }
    window.scrollTo(0, 0);
  } catch (e) {}
  // ---- 等待图片真实解码完成（网络已空闲，滚动触发的请求基本就绪）----
  try {
    await Promise.allSettled(Array.from(document.images).map((im) => (im.complete ? Promise.resolve() : im.decode ? im.decode().catch(() => {}) : Promise.resolve())));
  } catch (e) {}
  await new Promise((r) => setTimeout(r, 800));
}
"""


def _prepare_pdf_page(page) -> None:
    """打印前页面预处理：隐藏浮层/广告/蒙层 + 触发懒加载图片（失败不阻塞打印）。"""
    try:
        page.add_style_tag(content=_PDF_HIDE_NOISE_CSS)
    except Exception:  # noqa: BLE001  样式注入失败不影响打印
        pass
    try:
        page.evaluate(_PDF_PREPARE_JS)
    except Exception as e:  # noqa: BLE001  预处理失败只告警，仍按原样打印
        log.warning("browser pdf prepare skipped: err=%s", e)
    try:
        page.evaluate("window.scrollTo(0,0)")
    except Exception:  # noqa: BLE001
        pass


# ★ 2026-09 打印纸宽自适应：网页大多按桌面屏幕宽度（约 1200~1366px）设计，
# 若硬套 A4（可用排版宽仅约 734px），固定宽度的轮播大图/版块会超出纸面被横向裁掉
# （表现为 PDF 里图片"被截断/显示不全"）。打印前测量页面设计宽度，据此决定纸宽。
_DESIGN_WIDTH_JS = r"""
() => {
  const root = document.documentElement;
  const body = document.body;
  let maxW = Math.max(
    root ? root.scrollWidth : 0, root ? root.clientWidth : 0,
    body ? body.scrollWidth : 0, body ? body.clientWidth : 0,
  );
  try {
    for (const el of body ? body.querySelectorAll('*') : []) {
      const w = el.getBoundingClientRect().width;
      if (w > maxW) maxW = w;
    }
  } catch (e) {}
  // 页面普遍为桌面流式布局（body 宽=视口宽），下限取屏幕设计宽；上限防御异常超大元素
  const w = Math.max(1200, Math.min(2000, Math.round(maxW)));
  return w;
}
"""


def _print_page_pdf(page, dest: Path) -> None:
    """等待渲染稳定 → 隐藏噪音/浮层 → 懒加载图片 → 按页面宽度打印 PDF（供两个入口共用）。

    纸宽 = 页面实际设计宽度（默认桌面屏 1366px），保证固定宽度图片/版块不被 A4 窄纸横向裁切；
    测量失败时退回 A4。
    """
    _prepare_pdf_page(page)
    dest.parent.mkdir(parents=True, exist_ok=True)
    common = dict(
        print_background=True,
        margin={"top": "10mm", "bottom": "10mm", "left": "8mm", "right": "8mm"},
    )
    try:
        width_css = int(page.evaluate(_DESIGN_WIDTH_JS) or 0)
    except Exception:  # noqa: BLE001
        width_css = 0
    if 794 < width_css <= 2000:
        page.pdf(path=str(dest), width=f"{width_css}px", **common)
    else:
        page.pdf(path=str(dest), format="A4", **common)


def browser_print_pdf(url: str, dest: Path, timeout: int = 60) -> bool:
    """★ 2026-08-31 用浏览器打开 url 并打印为 PDF（网页内容走 MinerU 解析的前置）。

    - 与 browser_fetch_html 相同的挑战等待逻辑（WAF 站点先过挑战再打印）
    - 打印前隐藏 nav/footer/aside 等噪音（与正文转换的 SKIP_TAGS 对齐）
    Returns:
        成功 True；失败 False（调用方降级为 HTML 本地解析）。
    ★ 浏览器获取/上下文创建也在 try 内：任何失败都返回 False，绝不抛异常
    （否则会跳过调用方的 HTML 降级路径）。
    """
    start = time.monotonic()
    ctx = None
    try:
        browser = _get_browser()
        ctx = _new_context(browser)
        page = ctx.new_page()
        doc_statuses: List[int] = []
        page.on(
            "response",
            lambda r: doc_statuses.append(r.status)
            if r.request.resource_type == "document"
            else None,
        )
        try:
            page.goto(url, timeout=timeout * 1000, wait_until="domcontentloaded")
        except Exception:  # noqa: BLE001
            log.debug("pdf goto warn: %s", url)
        deadline = start + timeout
        while not _challenge_passed(doc_statuses) and time.monotonic() < deadline:
            time.sleep(0.5)
        try:
            page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:  # noqa: BLE001
            pass
        _print_page_pdf(page, dest)
        log.info(
            "browser print pdf ok: url=%s -> %s dt=%.1fs",
            url, dest.name, time.monotonic() - start,
        )
        return True
    except Exception as e:  # noqa: BLE001
        log.warning("browser print pdf failed: url=%s err=%s", url, e)
        return False
    finally:
        if ctx is not None:
            try:
                ctx.close()
            except Exception:  # noqa: BLE001
                pass


def browser_print_local_html_pdf(html_path: Path, dest: Path) -> bool:
    """把本地保存的 HTML 打印为 PDF（file:// 打开，不再访问网络）。

    场景：确认入库时在线渲染失败（网站已改版/不可达）的降级路径 ——
    用抓取时保存的原始 HTML 保证「预览的内容 = 入库的内容」。
    注意：页面里相对路径的 CSS/图片在 file:// 下无法解析，版式可能退化，
    但 DOM 顺序的正文文本完整，MinerU 仍可提取。
    """
    start = time.monotonic()
    ctx = None
    try:
        browser = _get_browser()
        ctx = _new_context(browser)
        page = ctx.new_page()
        page.goto(html_path.resolve().as_uri(), timeout=30_000, wait_until="domcontentloaded")
        try:
            page.wait_for_load_state("networkidle", timeout=5000)
        except Exception:  # noqa: BLE001
            pass
        _print_page_pdf(page, dest)
        log.info(
            "browser print local html pdf ok: %s -> %s dt=%.1fs",
            html_path.name, dest.name, time.monotonic() - start,
        )
        return True
    except Exception as e:  # noqa: BLE001
        log.warning("browser print local html pdf failed: %s err=%s", html_path.name, e)
        return False
    finally:
        if ctx is not None:
            try:
                ctx.close()
            except Exception:  # noqa: BLE001
                pass