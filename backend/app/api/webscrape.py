"""知识库外延：网站抓取 API（2026-08 新增）。

三步式流程（抓取 → 确认下载 → 文件预览确认入库，2026-09 拆分）：

    1. POST /api/webscrape/run                — 选「网站抓取配置」→ 抓取其配置的 URL 列表，生成「待确认任务」
       配置方案必选网站抓取类型（type=webscrape），其「抓取网站 URL 列表」
       （webscrape_urls）即本批抓取来源；
       网页正文转 Markdown、附件文件下载，都先落在 data/webscrape/{task_id}/ 临时区，
       不写 manifest、不触发流水线。
    2. GET /api/webscrape/tasks               — 任务历史列表
       GET /api/webscrape/task/{id}           — 任务详情（逐项状态）
       GET /api/webscrape/task/{id}/preview/{idx} — 抓取内容预览：网页正文全文 / 附件元信息
    3. POST /api/webscrape/task/{id}/confirm  — ①确认下载：选中项落地 pending/（网页 → 浏览器渲染
       PDF，附件 → 原文件）并登记 manifest（parse 列留空）。只下载，不触发流水线；
       下载完成后逐项进入「文件预览」（下载后的真实文件在线预览）。
    4. POST /api/webscrape/task/{id}/ingest   — ②在文件预览处点「确定」后调用：仅对该预览项
       （单 URL）走 parse(MinerU) → chunk → dify 流水线。
    5. GET  /api/webscrape/task/{id}/file/{idx}         — 落地原文件流（PDF/HTML/图片/文本等）
       GET  /api/webscrape/task/{id}/office-preview/{idx} — Office（Word/Excel/PPT/CSV）转 HTML 预览

★ 2026-08-31 两套配置：抓取 URL 来自配置本身，页面不再输入。
★ 2026-09-02 「先确认再入库」细化为：确认下载 → 文件预览（点确定）→ 逐项入库；
   入库前可确认下载到的文件是否正是要入库的内容（参考网页版 Office 的在线预览）。

错误隔离：单个 URL 抓取失败只影响该项；confirm/ingest 只处理勾选项，未勾选项留在任务里。
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field
from urllib.parse import quote

from app.config import settings
from app.services import config_run_log, config_store, file_preview, webscraper, webscrape_store

router = APIRouter(tags=["webscrape"])
log = logging.getLogger("ragsystem.api.webscrape")

# 单次请求最多抓取页数
MAX_URLS_PER_REQUEST = 50


# ============ 请求/响应模型 ============


class WebScrapeRunRequest(BaseModel):
    """抓取请求（生成待确认任务）：URL 列表来自配置本身，无需页面传入。"""

    profile_id: str = Field(
        ...,
        description="网站抓取配置方案 ID（必填：其「抓取网站 URL 列表」即抓取来源）",
    )


class WebScrapeItem(BaseModel):
    """单个 URL 的抓取结果（任务项）。"""

    url: str
    ok: bool
    kind: str = "content"              # content=网页正文 / attachment=附件文件
    depth: Optional[int] = None        # 递归层级：0=URL 列表本身，1..N=递归发现的页面
    title: str = ""                    # content：页面标题；attachment：文件名 stem
    filename: Optional[str] = None     # attachment：原始文件名；confirm 下载后=落地文件名（content 为 pdf/html）
    rel_path: Optional[str] = None     # 相对 data/webscrape/{task_id}/ 的路径
    char_count: Optional[int] = None   # content：正文字符数
    size: Optional[int] = None         # attachment：文件大小（字节）
    truncated: bool = False            # 正文是否超长截断
    confirmed: bool = False            # 是否已确认（confirm 下载后回填 True）
    ingest_status: Optional[str] = None  # 入库状态：downloaded=已下载待预览确认 / ok=已入库 / error=失败
    ingest_error: Optional[str] = None
    dataset_id: Optional[str] = None   # confirm 后：入库的目标知识库 ID（每次确认时选择）
    dataset_name: Optional[str] = None # confirm 后：目标知识库名称（溯源展示用）
    confirm_profile_id: Optional[str] = None  # confirm 后：确认下载时选择的配置方案 ID（ingest 阶段据此执行）
    stem: Optional[str] = None         # confirm 下载后：落地文件的 stem（流水线/记录回填用）
    error: Optional[str] = None        # 抓取失败原因


class WebScrapeTask(BaseModel):
    """抓取任务（含 items 明细）。"""

    id: str
    created_at: str
    updated_at: Optional[str] = None
    profile_id: Optional[str] = None
    profile_name: Optional[str] = None
    site_url: Optional[str] = None     # 抓取网站的配置快照（URL 列表 JSON 文本）
    urls: List[str] = []               # 抓取来源 URL 列表（由 site_url 快照解析）
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
    """确认请求：勾选需要的内容 + 选择确认入库用的配置 + 目标知识库。

    ★ 2026-08-31：网站抓取配置不含知识库 ID（可入不同知识库），
    每次确认入库时单独指定目标知识库（dataset_id），覆盖配置中可能残留的库。
    """

    urls: List[str] = Field(..., description="确认要入库的 URL 列表（未勾选的不处理）")
    profile_id: str = Field(..., description="确认时选择的配置方案 ID（决定切分等入库参数）")
    dataset_id: str = Field(..., description="目标知识库 ID（Dify 数据集），本次确认的内容入库到这个知识库")


class WebScrapeConfirmResponse(BaseModel):
    """确认下载响应：落地结果（★ 不跑流水线，流水线在文件预览确定后由 ingest 触发）。"""

    task: WebScrapeTask
    landed: List[Dict[str, Any]] = []   # 每项落地结果（stem/filename/ok/error）
    error: Optional[str] = None


class WebScrapeIngestRequest(BaseModel):
    """文件预览处点「确定」后的入库请求：仅该预览项走解析-切分-入库。"""

    urls: List[str] = Field(..., description="要入库的 URL 列表（本次在预览面板点确定的内容）")


class WebScrapeIngestItemResult(BaseModel):
    """单个 URL 的入库结果。"""

    url: str
    stem: str = ""
    ok: bool
    error: Optional[str] = None
    status: str = ""            # ok / error
    parse: Optional[str] = None
    dify_doc_id: Optional[str] = None


class WebScrapeIngestResponse(BaseModel):
    """入库响应：任务（逐项状态已刷新）+ 每个 URL 的流水线产物与结果。"""

    task: WebScrapeTask
    results: List[WebScrapeIngestItemResult] = []
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
            "depth": it.get("depth"),
            "title": it.get("title") or "",
            "filename": it.get("filename"),
            "rel_path": it.get("rel_path"),
            "char_count": it.get("char_count"),
            "size": it.get("size"),
            "truncated": bool(it.get("truncated")),
            "confirmed": bool(it.get("confirmed")),
            "ingest_status": it.get("ingest_status"),
            "ingest_error": it.get("ingest_error"),
            "dataset_id": it.get("dataset_id"),
            "dataset_name": it.get("dataset_name"),
            "confirm_profile_id": it.get("confirm_profile_id"),
            "stem": it.get("stem"),
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
        urls=list(task.get("urls") or []),
        status=task.get("status") or webscraper.TASK_STATUS_PENDING,
        confirm_time=task.get("confirm_time"),
        confirm_profile=task.get("confirm_profile"),
        items=items,
        total=len(items),
        ok_count=sum(1 for it in items if it.ok),
        confirmed_count=sum(1 for it in items if it.confirmed),
    )


def _lookup_dataset_name(dataset_id: str) -> Optional[str]:
    """按 ID 查 Dify 知识库名称（溯源展示用）；查询失败仅返回 None，不影响确认流程。"""
    from app.services.dify_uploader import DifyClient, DifyError

    if not dataset_id:
        return None
    try:
        client = DifyClient()
        for page in range(1, 6):
            payload = client.list_datasets(page=page, limit=100)
            for d in payload.get("data") or []:
                if str(d.get("id")) == str(dataset_id):
                    return str(d.get("name") or "") or None
            if not payload.get("has_more"):
                break
    except (DifyError, Exception):  # noqa: BLE001
        return None
    return None


def _load_task_or_404(task_id: str) -> Dict[str, Any]:
    task = webscraper.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail=f"抓取任务不存在: {task_id}")
    return task


def _index_item_or_404(task: Dict[str, Any], index: int) -> Dict[str, Any]:
    items = task.get("items") or []
    if index < 0 or index >= len(items):
        raise HTTPException(status_code=404, detail=f"任务项不存在: index={index}")
    it = items[index]
    if not it.get("ok"):
        raise HTTPException(status_code=400, detail=f"该任务项抓取失败: {it.get('error')}")
    if not it.get("confirmed"):
        raise HTTPException(status_code=400, detail="该任务项尚未确认下载，请先点击「确认下载」")
    return it


def _landed_file_or_404(it: Dict[str, Any]) -> Path:
    """定位该项 confirm 下载落地到 pending/ 的文件。"""
    name = it.get("filename")
    if not name:
        raise HTTPException(status_code=400, detail="该项缺少落地文件信息（可能尚未完成确认下载）")
    pending = settings.pending_dir.resolve()
    path = (pending / name).resolve()
    if path.parent != pending or not path.is_file():
        raise HTTPException(status_code=404, detail=f"落地文件不存在或已被清理: {name}")
    return path


def _cd_header(filename: str, inline: bool) -> str:
    """Content-Disposition 头（UTF-8 文件名）。"""
    kind = "inline" if inline else "attachment"
    return f"{kind}; filename*=UTF-8''{quote(filename or 'file')}"


def _backfill_records_from_manifest(stems: List[str]) -> Dict[str, Dict[str, Any]]:
    """把本次流水线产物从 manifest 回填到 webscrape_records 台账。

    返回 {stem: {status, parse, dify_doc_id, error_msg}}（无匹配的行不存在）。
    """
    from app.services import manifest_store

    manifest = manifest_store.load()
    by_stem = {Path(fname).stem: row for fname, row in manifest.items()}
    results: Dict[str, Dict[str, Any]] = {}
    for stem in stems:
        row = by_stem.get(stem)
        if row is None:
            continue
        err = row.error_msg or ""
        if err:
            status, msg = webscrape_store.STATUS_ERROR, err
        elif row.dify_doc_id:
            status, msg = webscrape_store.STATUS_INGESTED, ""
        elif row.parse:
            status, msg = webscrape_store.STATUS_PARSED, ""
        else:
            status, msg = webscrape_store.STATUS_LANDED, ""
        results[stem] = {
            "status": status,
            "parse": row.parse or "",
            "chunks": row.chunks or "",
            "dify_doc_id": row.dify_doc_id or "",
            "error_msg": msg,
        }
    if results:
        webscrape_store.update_pipeline_result(stems, results)
    return results


# ============ 接口 ============


@router.post("/webscrape/run", response_model=WebScrapeRunResponse)
def run_webscrape(req: WebScrapeRunRequest) -> WebScrapeRunResponse:
    """第一步：按配置的 URL 列表抓取生成「待确认任务」（不注册 manifest、不入库）。"""
    # 配置方案必填且必须是「网站抓取配置」：决定抓取来源（webscrape_urls 列表）
    profile = config_store.get_profile(req.profile_id)
    if not profile:
        raise HTTPException(status_code=404, detail=f"配置方案不存在: {req.profile_id}")
    if config_store.profile_type_of(profile) != config_store.PROFILE_TYPE_WEBSCRAPE:
        raise HTTPException(
            status_code=400,
            detail=(
                f"配置方案「{profile.get('name')}」是文档处理配置，不能用于网站抓取；"
                "请先在「配置中心」创建并选择网站抓取配置（带「抓取网站 URL 列表」）"
            ),
        )
    urls = webscraper.profile_webscrape_urls(profile)
    if not urls:
        raise HTTPException(
            status_code=400,
            detail=f"配置方案「{profile.get('name')}」未设置「抓取网站 URL 列表」，请先在配置中心完善配置",
        )
    if len(urls) > MAX_URLS_PER_REQUEST:
        raise HTTPException(status_code=400, detail=f"配置的 URL 列表超过单次上限 {MAX_URLS_PER_REQUEST} 个")

    log.info(
        "webscrape run start: urls=%d profile=%s",
        len(urls), profile.get("name"),
        extra={"step": "webscrape", "status": "start"},
    )
    task = webscraper.create_task(profile)
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


@router.get("/webscrape/records")
def list_webscrape_records(limit: int = 100) -> Dict[str, Any]:
    """★ 2026-08-31 网站抓取入库台账（webscrape_records 表）。

    每条确认入库的抓取内容一行（独立于文档上传的 manifest 表）：
    源 URL / 递归层级 / 落地文件 / 目标知识库 / 所用配置 / 流水线产物与状态。
    """
    records = webscrape_store.list_records(limit)
    return {"total": len(records), "records": records}


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
    """第二步(1/2)：确认并下载 —— 勾选内容落地到 pending/ 后即停。

    下载完成后前端自动打开该项的「文件预览」，由用户点「确定」调 ingest 接口，
    才走解析-切分-入库（下载→预览→入库，2026-09 拆分）。
    """
    task = _load_task_or_404(task_id)
    st = task.get("status")
    if st not in (webscraper.TASK_STATUS_PENDING, webscraper.TASK_STATUS_CONFIRMED):
        raise HTTPException(status_code=400, detail=f"任务状态不允许确认下载（status={st}）")

    confirmed_urls = [u for u in (req.urls or []) if u and u.strip()]
    if not confirmed_urls:
        raise HTTPException(status_code=400, detail="未勾选任何内容")
    dataset_id = (req.dataset_id or "").strip()
    if not dataset_id:
        raise HTTPException(status_code=400, detail="未选择目标知识库（本次确认的内容入到该知识库）")

    # 确认时再次选择配置（决定切分等入库参数，记录到 item 供 ingest 阶段使用）；
    # 知识库 ID 不入配置，每次确认单独指定 dataset_id（可入不同知识库）
    profile = config_store.get_profile(req.profile_id)
    if not profile:
        raise HTTPException(status_code=404, detail=f"配置方案不存在: {req.profile_id}")
    dataset_name = _lookup_dataset_name(dataset_id)

    # 只处理「尚未下载」或「下载失败待重试」的项，避免同一项重复落地
    item_by_url = {it.get("url"): it for it in task.get("items") or []}
    to_land: List[str] = []
    for u in confirmed_urls:
        it = item_by_url.get(u)
        if not it or not it.get("ok"):
            continue
        if it.get("confirmed") and it.get("filename"):
            continue  # 已确认下载过，跳过（入库走 ingest，不走 confirm）
        to_land.append(u)
    if not to_land:
        raise HTTPException(
            status_code=400,
            detail="所选内容均已确认下载（可在「文件预览」中打开文件并点确定完成入库），无需重复下载",
        )

    # 1) 落地勾选项（正文 → 渲染 PDF/HTML 入 pending/，附件 → 原文件移入 pending/）+ 登记 manifest
    t0 = time.perf_counter()
    landed = webscraper.land_confirmed_items(task, to_land)
    ok_landed = [r for r in landed if r.get("ok")]

    # 2) 更新任务逐项状态：下载成功 = confirmed + downloaded（待预览确认后 ingest）
    url_confirm = {r["url"]: r for r in landed}
    items = task.get("items") or []
    for it in items:
        r = url_confirm.get(it.get("url"))
        if r is None:
            continue
        it["confirmed"] = True
        if r.get("ok"):
            it["ingest_status"] = "downloaded"
            it["ingest_error"] = None
            it["dataset_id"] = dataset_id            # 溯源：本次确认入到哪个知识库
            it["dataset_name"] = dataset_name
            it["confirm_profile"] = profile.get("name")
            it["confirm_profile_id"] = profile.get("id")
            it["filename"] = r.get("filename")       # 落地文件名（content：pdf/html）
            it["stem"] = r.get("stem")
        else:
            it["ingest_status"] = "error"
            it["ingest_error"] = r.get("error")

    if not ok_landed:
        webscraper.save_task(task)
        errs = "; ".join(f"{r['url']}: {r['error']}" for r in landed)
        raise HTTPException(status_code=400, detail=f"勾选的项全部落地失败: {errs}")

    # ★ 2026-08-31 入库台账：每落地成功一项，登记一行到 webscrape_records
    #   （独立于文档上传的 manifest；ingest 流水线完成后回填产物与状态）
    for r in ok_landed:
        src_item = item_by_url.get(r.get("url")) or {}
        try:
            webscrape_store.upsert_record(
                task_id,
                r["url"],
                title=str(src_item.get("title") or ""),
                kind=str(r.get("kind") or "content"),
                depth=int(src_item.get("depth") or 0),
                filename=str(r.get("filename") or ""),
                stem=str(r.get("stem") or ""),
                dataset_id=dataset_id,
                dataset_name=dataset_name or "",
                profile_id=str(profile.get("id") or ""),
                profile_name=str(profile.get("name") or ""),
            )
        except Exception:  # noqa: BLE001 台账失败不阻断确认下载主流程
            log.exception("webscrape 入库台账登记失败: task=%s url=%s", task_id, r.get("url"))

    # 3) 任务状态：确认下载完成（不触发流水线）
    if st != webscraper.TASK_STATUS_CONFIRMED:
        task["confirm_time"] = webscraper._now_str()
        task["confirm_profile"] = profile.get("name")
    task["status"] = webscraper.TASK_STATUS_CONFIRMED
    webscraper.save_task(task)

    resp = WebScrapeConfirmResponse(task=_task_to_model(task), landed=landed)
    log.info(
        "webscrape confirm(download) done: task=%s landed=%d/%d duration=%dms",
        task_id, len(ok_landed), len(landed),
        int((time.perf_counter() - t0) * 1000),
        extra={"step": "webscrape", "status": "downloaded", "task_id": task_id},
    )
    return resp


@router.post("/webscrape/task/{task_id}/ingest", response_model=WebScrapeIngestResponse)
def ingest_webscrape_items(task_id: str, req: WebScrapeIngestRequest) -> WebScrapeIngestResponse:
    """第二步(2/2)：文件预览处点「确定」→ 仅对该预览项走 parse → chunk → dify。"""
    task = _load_task_or_404(task_id)
    requested = [u for u in (req.urls or []) if u and u.strip()]
    if not requested:
        raise HTTPException(status_code=400, detail="未指定要入库的 URL")
    item_by_url = {it.get("url"): it for it in task.get("items") or []}

    # 1) 校验并分组：同一 (profile, dataset) 合并一次流水线；不同组依次跑
    groups: Dict[tuple, List[Dict[str, Any]]] = {}   # (profile_id, dataset_id) -> [item,...]
    errors: Dict[str, str] = {}
    for u in requested:
        it = item_by_url.get(u)
        if not it or not it.get("ok"):
            errors[u] = "该内容不存在或抓取失败"
            continue
        if not it.get("confirmed") or not it.get("filename") or not it.get("stem"):
            errors[u] = "该项尚未确认下载，请先在列表点击「确认下载」"
            continue
        if it.get("ingest_status") == "ok":
            errors[u] = "该项已完成入库（无需重复解析）"
            continue
        pid = it.get("confirm_profile_id") or task.get("profile_id")
        did = it.get("dataset_id")
        if not pid or not did:
            errors[u] = "该项缺少入库配置（目标知识库/切分配置），请重新「确认下载」"
            continue
        groups.setdefault((pid, did), []).append(it)

    results: List[WebScrapeIngestItemResult] = []
    pipeline_error: Optional[str] = None

    for (pid, did), its in groups.items():
        profile = config_store.get_profile(pid)
        if not profile:
            err = f"配置方案不存在: {pid}"
            pipeline_error = err
            for it in its:
                it["ingest_status"] = "error"
                it["ingest_error"] = err
                results.append(WebScrapeIngestItemResult(
                    url=it["url"], stem=it.get("stem") or "", ok=False, status="error", error=err,
                ))
            continue
        profile_config = dict(profile.get("config") or {})
        profile_config["dify_dataset_id"] = did
        profile_effective = dict(profile, config=profile_config)
        stems = [it.get("stem") or "" for it in its if it.get("stem")]
        if not stems:
            continue

        # 2) 复用上传批量流水线：parse → chunk → dify（附件由 MinerU 解析）
        from app.api.upload import _run_batch_pipeline

        try:
            pipeline = _run_batch_pipeline(
                target_stems=stems,
                profile=profile_effective,
                source=config_run_log.SOURCE_WEBSCRAPE,
            )
            if pipeline and pipeline.get("status") == "failed":
                pipeline_error = pipeline.get("error")
        except Exception as e:  # noqa: BLE001
            pipeline_error = str(e)
            log.exception("webscrape ingest 流水线失败: task=%s", task_id)

        # 3) 从 manifest 回填入库台账 → 逐项标记入库状态
        try:
            manifest_results = _backfill_records_from_manifest(stems)
        except Exception:  # noqa: BLE001 回填失败不影响结果返回
            log.exception("webscrape ingest 台账回填失败: task=%s", task_id)
            manifest_results = {}

        for it in its:
            stem = it.get("stem") or ""
            row = manifest_results.get(stem)
            if row is not None and row["status"] == webscrape_store.STATUS_ERROR:
                it["ingest_status"] = "error"
                it["ingest_error"] = (row.get("error_msg") or "").strip() or pipeline_error
            elif row is not None and row["status"] in (
                webscrape_store.STATUS_INGESTED, webscrape_store.STATUS_PARSED,
            ):
                it["ingest_status"] = "ok"
                it["ingest_error"] = None
            elif pipeline_error:
                it["ingest_status"] = "error"
                it["ingest_error"] = f"流水线失败: {pipeline_error[:200]}"
            else:
                # 行存在但未解析完成（异常状态）：视为失败可重试
                it["ingest_status"] = "error"
                it["ingest_error"] = row.get("error_msg") if row else "流水线未产出结果，可重试"
            results.append(WebScrapeIngestItemResult(
                url=it["url"],
                stem=stem,
                ok=it["ingest_status"] == "ok",
                status="ok" if it["ingest_status"] == "ok" else "error",
                error=it.get("ingest_error"),
                parse=(row or {}).get("parse") if row else None,
                dify_doc_id=(row or {}).get("dify_doc_id") if row else None,
            ))

    # 补上因缺配置等原因无法入库的项（结果中也带出来，方便前端提示）
    for u, err in errors.items():
        results.append(WebScrapeIngestItemResult(
            url=u, stem="", ok=False, status="error", error=err,
        ))

    task["status"] = webscraper.TASK_STATUS_CONFIRMED
    webscraper.save_task(task)
    log.info(
        "webscrape ingest done: task=%s urls=%d ok=%d error=%s",
        task_id, len(requested),
        sum(1 for r in results if r.ok), pipeline_error or "-",
        extra={"step": "webscrape", "status": "ingested", "task_id": task_id},
    )
    return WebScrapeIngestResponse(
        task=_task_to_model(task),
        results=results,
        error=pipeline_error,
    )


@router.get("/webscrape/task/{task_id}/file/{index}")
def webscrape_landed_file(task_id: str, index: int):
    """下载落地原文件流：PDF/图片直接内联，HTML 清洗后内联，文本解码返回，其余下载。

    供「文件预览」抽屉 iframe/<img> 展示实际下载到的文件。
    """
    task = _load_task_or_404(task_id)
    it = _index_item_or_404(task, index)
    path = _landed_file_or_404(it)
    kind = file_preview.preview_kind(path.name)
    name = path.name

    if kind == "pdf":
        return FileResponse(path, media_type="application/pdf", headers={"Content-Disposition": _cd_header(name, True)})
    if kind == "image":
        mime = {
            ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
            ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp",
            ".svg": "image/svg+xml",
        }.get(name.lower()[name.lower().rfind("."):], "application/octet-stream")
        return FileResponse(path, media_type=mime, headers={"Content-Disposition": _cd_header(name, True)})
    if kind == "html":
        cleaned = file_preview.cleaned_html_bytes(path.read_bytes())
        return Response(content=cleaned, media_type="text/html; charset=utf-8",
                        headers={"Content-Disposition": _cd_header(name, True)})
    if kind in ("markdown", "text"):
        text = file_preview.decode_bytes(path.read_bytes())
        return Response(content=text.encode("utf-8"), media_type="text/plain; charset=utf-8",
                        headers={"Content-Disposition": _cd_header(name, True)})
    # office 用 /office-preview；其余（压缩包/旧版 Office/二进制）走下载自查
    return FileResponse(path, media_type="application/octet-stream",
                        headers={"Content-Disposition": _cd_header(name, False)})


@router.get("/webscrape/task/{task_id}/office-preview/{index}")
def webscrape_office_preview(task_id: str, index: int):
    """Word/Excel/PPT/CSV 在线预览：后端轻量转换（docx/pptx 提取文本与表格、
    xlsx/csv 渲染表格，旧版 Office 给信息页），返回可 iframe 的 HTML。"""
    task = _load_task_or_404(task_id)
    it = _index_item_or_404(task, index)
    path = _landed_file_or_404(it)
    html = file_preview.render_office_preview(path.name, path)
    return Response(content=html.encode("utf-8"), media_type="text/html; charset=utf-8",
                    headers={"Content-Disposition": _cd_header(path.name + ".preview.html", True)})