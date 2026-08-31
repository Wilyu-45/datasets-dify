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
    """
    global _browser, _playwright
    with _lock:
        if _browser is None:
            from playwright.sync_api import sync_playwright

            executable = _find_chromium_executable()
            launch_kwargs = dict(headless=True, args=_LAUNCH_ARGS)
            if executable:
                launch_kwargs["executable_path"] = executable
            _playwright = sync_playwright().start()
            _browser = _playwright.chromium.launch(**launch_kwargs)
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
    """
    start = time.monotonic()
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
        try:
            ctx.close()
        except Exception:  # noqa: BLE001
            pass


def browser_download_file(url: str, dest: Path, timeout: int = 60) -> Optional[str]:
    """用浏览器下载附件文件到 dest（绕过 WAF 对静态下载的拦截）。

    Returns:
        成功返回下载提示字符串；失败返回 None（由调用方兜底）。
    """
    browser = _get_browser()
    ctx = _new_context(browser)
    page = ctx.new_page()
    try:
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
        try:
            ctx.close()
        except Exception:  # noqa: BLE001
            pass