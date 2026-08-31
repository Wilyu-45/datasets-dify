"""知识库外延：网站抓取 API（2026-08 新增）。

两步式流程（先抓取待确认，确认后才入库）：

    1. POST /api/webscrape/run                — 选配置 + URL 列表 → 抓取生成「待确认任务」
       配置方案必填：其「抓取网站 URL」决定本批可抓取的网站（同域名白名单）；
       网页正文转 Markdown、附件文件下载，都先落在 data/webscrape/{task_id}/ 临时区，
       不写 manifest、不触发流水线。
    2. GET /api/webscrape/tasks               — 任务历史列表
       GET /api/webscrape/task/{id}           — 任务详情（逐项状态）
       GET /api/webscrape/task/{id}/preview/{idx} — 预览：网页正文全文 / 附件元信息
    3. POST /api/webscrape/task/{id}/confirm  — 人为勾选需要的项 + 再次选择配置
       → 选中项落地（正文 → parsed/，附件 → pending/）并登记 manifest →
       走 parse(MinerU) → chunk → dify 流水线 → 返回 PipelineReport

错误隔离：单个 URL 抓取失败只影响该项；confirm 只处理勾选项，未勾选项留在任务里。
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.config import settings
from app.services import config_run_log, config_store, webscraper

router = APIRouter(tags=["webscrape"])
log = logging.getLogger("ragsystem.api.webscrape")

# 单次请求最多抓取页数
MAX_URLS_PER_REQUEST = 50


# ============ 请求/响应模型 ============


class WebScrapeRunRequest(BaseModel):
    """抓取请求（生成待确认任务）。"""

    profile_id: str = Field(..., description="配置方案 ID（必填：决定抓取网站 URL 白名单）")
    urls: List[str] = Field(..., description="待抓取的 URL 列表（每行一个）")


class WebScrapeItem(BaseModel):
    """单个 URL 的抓取结果（任务项）。"""

    url: str
    ok: bool
    kind: str = "content"              # content=网页正文 / attachment=附件文件
    title: str = ""                    # content：页面标题；attachment：文件名 stem
    filename: Optional[str] = None     # attachment：原始文件名
    rel_path: Optional[str] = None     # 相对 data/webscrape/{task_id}/ 的路径
    char_count: Optional[int] = None   # content：正文字符数
    size: Optional[int] = None         # attachment：文件大小（字节）
    truncated: bool = False            # 正文是否超长截断
    confirmed: bool = False            # 是否已确认（confirm 后回填）
    ingest_status: Optional[str] = None  # confirm 后：ok / error
    ingest_error: Optional[str] = None
    error: Optional[str] = None        # 抓取失败原因


class WebScrapeTask(BaseModel):
    """抓取任务（含 items 明细）。"""

    id: str
    created_at: str
    updated_at: Optional[str] = None
    profile_id: Optional[str] = None
    profile_name: Optional[str] = None
    site_url: Optional[str] = None     # 抓取网站的配置快照
    status: str                        # pending / confirmed / done / cancelled
    confirm_time: Optional[str] = None
    confirm_profile: Optional[str] = None  # 确认时选用的配置名
    items: List[WebScrapeItem] = []
    # 以下为列表接口用（task 详情也附带，便于前端一眼看到汇总）
    total: int = 0
    ok_count: int = 0
    confirmed_count: int = 0


class WebScrapeRunResponse(BaseModel):
    """抓取任务创建响应。"""

    task: WebScrapeTask
    error: Optional[str] = None


class WebScrapeConfirmRequest(BaseModel):
    """确认请求：勾选需要的内容 + 选择确认入库用的配置。"""

    urls: List[str] = Field(..., description="确认要入库的 URL 列表（未勾选的不处理）")
    profile_id: str = Field(..., description="确认时选择的配置方案 ID（决定入库参数）")


class WebScrapeConfirmResponse(BaseModel):
    """确认响应：落地结果 + 整批 PipelineReport。"""

    task: WebScrapeTask
    landed: List[Dict[str, Any]] = []   # 每项落地结果（stem/filename/ok/error）
    pipeline: Optional[Dict[str, Any]] = None  # parse → chunk → dify 整批报告
    error: Optional[str] = None


class WebScrapePreview(BaseModel):
    """单项预览内容。"""

    url: str
    kind: str                       # content / attachment
    title: str = ""
    filename: Optional[str] = None
    content: Optional[str] = None   # content：markdown 全文
    size: Optional[int] = None


class WebScrapeTaskList(BaseModel):
    """任务列表（不含 items 明细）。"""

    total: int
    tasks: List[Dict[str, Any]]


# ============ 工具 ============


def _task_to_model(task: Dict[str, Any]) -> WebScrapeTask:
    items = []
    for it in task.get("items") or []:
        base = {
            "url": it.get("url", ""),
            "ok": bool(it.get("ok")),
            "kind": it.get("kind") or "content",
            "title": it.get("title") or "",
            "filename": it.get("filename"),
            "rel_path": it.get("rel_path"),
            "char_count": it.get("char_count"),
            "size": it.get("size"),
            "truncated": bool(it.get("truncated")),
            "confirmed": bool(it.get("confirmed")),
            "ingest_status": it.get("ingest_status"),
            "ingest_error": it.get("ingest_error"),
            "error": it.get("error"),
        }
        items.append(WebScrapeItem(**base))
    return WebScrapeTask(
        id=task["id"],
        created_at=task.get("created_at") or "",
        updated_at=task.get("updated_at"),
        profile_id=task.get("profile_id"),
        profile_name=task.get("profile_name"),
        site_url=task.get("site_url"),
        status=task.get("status") or webscraper.TASK_STATUS_PENDING,
        confirm_time=task.get("confirm_time"),
        confirm_profile=task.get("confirm_profile"),
        items=items,
        total=len(items),
        ok_count=sum(1 for it in items if it.ok),
        confirmed_count=sum(1 for it in items if it.confirmed),
    )


def _load_task_or_404(task_id: str) -> Dict[str, Any]:
    task = webscraper.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail=f"抓取任务不存在: {task_id}")
    return task


# ============ 接口 ============


@router.post("/webscrape/run", response_model=WebScrapeRunResponse)
def run_webscrape(req: WebScrapeRunRequest) -> WebScrapeRunResponse:
    """第一步：抓取 URL 列表生成「待确认任务」（不注册 manifest、不入库）。"""
    urls = [u for u in (req.urls or []) if u and u.strip()]
    if not urls:
        raise HTTPException(status_code=400, detail="URL 列表为空")
    if len(urls) > MAX_URLS_PER_REQUEST:
        raise HTTPException(status_code=400, detail=f"单次最多抓取 {MAX_URLS_PER_REQUEST} 个 URL")

    # 配置方案必填：决定「抓取网站 URL」白名单
    profile = config_store.get_profile(req.profile_id)
    if not profile:
        raise HTTPException(status_code=404, detail=f"配置方案不存在: {req.profile_id}")
    site_url = str((profile.get("config") or {}).get("webscrape_site_url") or "").strip()
    if not site_url:
        raise HTTPException(
            status_code=400,
            detail=f"配置方案「{profile.get('name')}」未设置「抓取网站 URL」，请先在配置中心完善配置",
        )

    log.info(
        "webscrape run start: urls=%d profile=%s site=%s",
        len(urls), profile.get("name"), site_url,
        extra={"step": "webscrape", "status": "start"},
    )
    task = webscraper.create_task(profile, urls)
    webscraper.save_task(task)
    resp = WebScrapeRunResponse(task=_task_to_model(task))
    log.info(
        "webscrape run done: task=%s total=%d ok=%d",
        task["id"], len(urls), resp.task.ok_count,
        extra={"step": "webscrape", "status": "done", "task_id": task["id"]},
    )
    return resp


@router.get("/webscrape/tasks", response_model=WebScrapeTaskList)
def list_webscrape_tasks(limit: int = 20) -> WebScrapeTaskList:
    """任务历史列表（按创建时间倒序）。"""
    tasks = webscraper.list_tasks(limit)
    return WebScrapeTaskList(total=len(tasks), tasks=tasks)


@router.get("/webscrape/task/{task_id}", response_model=WebScrapeTask)
def get_webscrape_task(task_id: str) -> WebScrapeTask:
    """任务详情（含逐项状态，供预览页渲染）。"""
    return _task_to_model(_load_task_or_404(task_id))


@router.get("/webscrape/task/{task_id}/preview/{index}", response_model=WebScrapePreview)
def preview_webscrape_item(task_id: str, index: int) -> WebScrapePreview:
    """预览任务中某一项：网页正文返回 Markdown 全文；附件返回文件信息。"""
    task = _load_task_or_404(task_id)
    items = task.get("items") or []
    if index < 0 or index >= len(items):
        raise HTTPException(status_code=404, detail=f"任务项不存在: index={index}")
    it = items[index]
    if not it.get("ok"):
        raise HTTPException(status_code=400, detail=f"该任务项抓取失败: {it.get('error')}")

    preview = WebScrapePreview(
        url=it.get("url", ""),
        kind=it.get("kind") or "content",
        title=it.get("title") or "",
        filename=it.get("filename"),
        size=it.get("size"),
    )
    if it.get("kind") == "content" and it.get("rel_path"):
        task_dir = webscraper.task_temp_dir(task_id)
        md_path = task_dir / it["rel_path"]
        if md_path.is_file():
            preview.content = md_path.read_text(encoding="utf-8")
        else:
            raise HTTPException(status_code=404, detail=f"正文文件不存在: {it['rel_path']}")
    return preview


@router.post("/webscrape/task/{task_id}/confirm", response_model=WebScrapeConfirmResponse)
def confirm_webscrape_task(task_id: str, req: WebScrapeConfirmRequest) -> WebScrapeConfirmResponse:
    """第二步：确认勾选内容 + 选择配置 → 落地并走 parse → chunk → dify 流水线。"""
    task = _load_task_or_404(task_id)
    if task.get("status") != webscraper.TASK_STATUS_PENDING:
        raise HTTPException(status_code=400, detail=f"任务已确认（status={task.get('status')}），请新建抓取任务")

    confirmed_urls = [u for u in (req.urls or []) if u and u.strip()]
    if not confirmed_urls:
        raise HTTPException(status_code=400, detail="未勾选任何内容")

    # 确认时再次选择配置（决定入库参数）
    profile = config_store.get_profile(req.profile_id)
    if not profile:
        raise HTTPException(status_code=404, detail=f"配置方案不存在: {req.profile_id}")

    # 1) 落地勾选项（正文 → parsed/，附件 → pending/）+ 登记 manifest
    t0 = time.perf_counter()
    landed = webscraper.land_confirmed_items(task, confirmed_urls)
    ok_landed = [r for r in landed if r.get("ok")]
    stems = [r["stem"] for r in ok_landed]

    # 2) 更新任务逐项确认状态（成功的标记 confirmed + ingest_status）
    url_confirm = {r["url"]: r for r in landed}
    items = task.get("items") or []
    for it in items:
        r = url_confirm.get(it.get("url"))
        if r is None:
            continue
        it["confirmed"] = True
        if r.get("ok"):
            it["ingest_status"] = "ok"
        else:
            it["ingest_status"] = "error"
            it["ingest_error"] = r.get("error")

    if not ok_landed:
        webscraper.save_task(task)
        errs = "; ".join(f"{r['url']}: {r['error']}" for r in landed)
        raise HTTPException(status_code=400, detail=f"勾选的项全部落地失败: {errs}")

    # 3) 复用上传批量流水线：parse → chunk → dify（附件由 MinerU 解析）
    from app.api.upload import _run_batch_pipeline

    pipeline: Optional[Dict[str, Any]] = None
    pipeline_error: Optional[str] = None
    try:
        pipeline = _run_batch_pipeline(
            target_stems=stems,
            profile=profile,
            source=config_run_log.SOURCE_WEBSCRAPE,
        )
        if pipeline and pipeline.get("status") == "failed":
            pipeline_error = pipeline.get("error")
    except Exception as e:  # noqa: BLE001
        pipeline_error = str(e)
        log.exception("webscrape confirm 流水线失败: task=%s", task_id)

    # 4) 更新任务状态
    task["status"] = webscraper.TASK_STATUS_CONFIRMED
    task["confirm_time"] = webscraper._now_str()
    task["confirm_profile"] = profile.get("name")
    for it in items:
        if it.get("confirmed") and it.get("ingest_status") == "ok" and pipeline_error:
            it["ingest_status"] = "error"
            it["ingest_error"] = f"流水线失败: {pipeline_error[:200]}"
    webscraper.save_task(task)

    resp = WebScrapeConfirmResponse(
        task=_task_to_model(task),
        landed=landed,
        pipeline=pipeline,
        error=pipeline_error,
    )
    log.info(
        "webscrape confirm done: task=%s landed=%d/%d status=%s duration=%dms",
        task_id, len(ok_landed), len(landed),
        pipeline.get("status") if pipeline else "none",
        int((time.perf_counter() - t0) * 1000),
        extra={"step": "webscrape", "status": "confirmed", "task_id": task_id},
    )
    return resp