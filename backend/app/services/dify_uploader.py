"""Dify Knowledge API 客户端（plan.md §3.4）。

Dify 是知识库 + LLM 应用平台。本服务通过其 Service API 把本地切分好的
chunks 入库到指定 Dataset：

    POST {dify_api_url}/files/upload                       上传图片
    POST {dify_api_url}/datasets/{ds}/document/create_by_text   创建空文档
    GET  {dify_api_url}/datasets/{ds}/documents/{doc}      查文档状态
    POST {dify_api_url}/datasets/{ds}/documents/{doc}/segments  批量加分段

约定：
- 单步失败重试 3 次（指数退避：1s → 2s → 4s）；
- 4xx 立即失败（参数问题，重试无意义），5xx/网络重试；
- 所有时间用秒；chunk 内容用纯 markdown（含 `![](images/xxx.jpg)` 引用）。
- Dify 文档创建后必须等 indexing_status=completed 才能 add_segments，
  这里提供 `wait_document_ready` 做轮询等待。

注意：
- Dify 知识库的 API Key（dataset-xxx 开头）权限最大，不要写到前端代码。
- 服务端 Dify 默认文档形态是 text_model（即按段落切），本服务
  自带切分逻辑（Dify 这一步可视为「容器」），所以传占位 text 即可。
"""

from __future__ import annotations

import logging
import mimetypes
import re
import time
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

from app.config import settings

log = logging.getLogger("ragsystem.dify_client")


class DifyError(RuntimeError):
    """Dify 调用失败。"""

    def __init__(
        self,
        message: str,
        *,
        attempts: int = 1,
        status_code: Optional[int] = None,
        body: Optional[str] = None,
    ) -> None:
        super().__init__(message)
        self.attempts = attempts
        self.status_code = status_code
        self.body = (body or "")[:500]


# 内部异常分类：4xx 不重试，5xx/网络重试
class _RetryableDifyError(Exception):
    pass


class _FatalDifyError(Exception):
    def __init__(self, message: str, *, status_code: int, body: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.body = body


class _TableParser(HTMLParser):
    """把 HTML <table> 解析为二维行列表（单元格为去标签后的文本）。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: List[List[str]] = []
        self._row: Optional[List[str]] = None
        self._cell: Optional[List[str]] = None
        self._in_table = False

    def handle_starttag(self, tag: str, attrs: list) -> None:  # noqa: D102
        if tag == "table":
            self._in_table = True
            self.rows = []
        elif tag == "tr" and self._in_table:
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = []

    def handle_data(self, data: str) -> None:  # noqa: D102
        if self._cell is not None:
            self._cell.append(data)

    def handle_endtag(self, tag: str) -> None:  # noqa: D102
        if tag in ("td", "th") and self._cell is not None:
            if self._row is not None:
                self._row.append("".join(self._cell).strip())
            self._cell = None
        elif tag == "tr" and self._row is not None:
            self.rows.append(self._row)
            self._row = None
        elif tag == "table":
            self._in_table = False


def _table_rows_to_markdown(rows: List[List[str]]) -> str:
    """二维表格 -> markdown 表格文本。"""
    if not rows:
        return ""
    max_cols = max(len(r) for r in rows)
    lines: List[str] = []
    for idx, row in enumerate(rows):
        cells = [c.replace("|", "\\|").replace("\n", " ").strip() for c in row]
        cells += [""] * (max_cols - len(cells))
        lines.append("| " + " | ".join(cells) + " |")
        if idx == 0:
            lines.append("|" + "|".join([" --- "] * max_cols) + "|")
    return "\n".join(lines)


def _html_tables_to_markdown(content: str) -> str:
    """把内容中的每个 <table>...</table> 块替换为 markdown 表格，并剥离残余 HTML 标签。

    背景（2026-08-20 实测）：Dify 会静默丢弃超长的 HTML 表格分段
    （如 11662 字符的 HTML 表），且同批中其它分段一起被回滚；
    转成 markdown 表格后可正常持久化。
    """
    out_parts: List[str] = []
    rest = content
    while "<table" in rest:
        start = rest.find("<table")
        before = rest[:start]
        end = rest.find("</table>", start)
        if end == -1:
            end = len(rest)
        else:
            end += len("</table>")
        html = rest[start:end]
        parser = _TableParser()
        parser.feed(html)
        out_parts.append(before)
        out_parts.append(_table_rows_to_markdown(parser.rows))
        rest = rest[end:]
    out_parts.append(rest)
    text = "\n\n".join(p.strip() for p in out_parts if p.strip())
    # 剥离可能残留的 HTML 标签
    return re.sub(r"<[^>]+>", "", text)


def _split_oversized_content(content: str, limit: int) -> List[str]:
    """把超长分段拆成多段。

    优先按空行（段落）边界拆，再按单行边界，最后硬切。
    返回的每段均 ≤ limit。
    """
    if len(content) <= limit:
        return [content]
    parts: List[str] = []
    # 1) 按段落（\n\n）边界切
    paragraphs = re.split(r"(\n\s*\n)", content)
    current = ""
    for para in paragraphs:
        if len(current) + len(para) <= limit:
            current += para
            continue
        if current:
            parts.append(current)
            current = ""  # ★ 必须重置，否则超长段落会被二次 flush 导致内容重复
        # 2) 段落本身超长：按行边界切
        if len(para) > limit:
            for line in re.split(r"(\n)", para):
                if len(current) + len(line) <= limit:
                    current += line
                    continue
                if current:
                    parts.append(current)
                # 3) 单行仍超长：硬切（每片 ≤ limit，不附加前缀以免超限）
                if len(line) > limit:
                    current = ""
                    for i in range(0, len(line), limit):
                        parts.append(line[i : i + limit])
                else:
                    current = line
        else:
            current = para
    if current:
        parts.append(current)
    return [p for p in parts if p.strip()]


@dataclass
class DifyDocument:
    """create_by_text 返回的文档对象（仅取必要字段）。"""

    document_id: str
    name: str
    batch: Optional[str] = None
    indexing_status: str = "waiting"
    enabled: bool = True

    @property
    def is_ready(self) -> bool:
        return self.indexing_status == "completed" and self.enabled


@dataclass
class DifySegment:
    """add_segments 返回的单个分段。"""

    segment_id: str
    document_id: str
    position: int
    content: str
    word_count: int = 0
    tokens: int = 0
    status: str = "completed"


@dataclass
class DifyUploadedFile:
    """upload_file 返回的单个文件。"""

    file_id: str
    name: str
    size: int
    extension: str
    mime_type: str
    url: Optional[str] = None
    source_url: Optional[str] = None


class DifyClient:
    """薄包装 httpx，对接 Dify Knowledge API。"""

    def __init__(
        self,
        *,
        api_url: Optional[str] = None,
        api_key: Optional[str] = None,
        dataset_id: Optional[str] = None,
        timeout: Optional[int] = None,
        max_retries: Optional[int] = None,
        backoff: Optional[float] = None,
        indexing_technique: Optional[str] = None,
        doc_form: Optional[str] = None,
        app_api_key: Optional[str] = None,
    ) -> None:
        self.api_url = (api_url or settings.dify_api_url).rstrip("/")
        self.api_key = api_key or settings.dify_api_key
        # App API Key：仅用于 /files/upload 端点。Knowledge API Key 没有这个权限。
        self.app_api_key = app_api_key or settings.dify_app_api_key
        self.dataset_id = dataset_id or settings.dify_dataset_id
        self.timeout = timeout or settings.dify_timeout
        self.max_retries = max_retries or settings.dify_max_retries
        self.backoff = backoff or settings.dify_retry_backoff
        self.indexing_technique = (
            indexing_technique or settings.dify_indexing_technique
        )
        self.doc_form = doc_form or settings.dify_doc_form
        if not self.api_key:
            raise ValueError("dify_api_key 未配置（settings.dify_api_key 或环境变量 RAG_DIFY_API_KEY）")
        if not self.dataset_id:
            raise ValueError("dify_dataset_id 未配置（settings.dify_dataset_id 或环境变量 RAG_DIFY_DATASET_ID）")

    # -------------------- 公共方法 --------------------

    def upload_file(self, file_path: Path, *, user: str = "ragsystem") -> DifyUploadedFile:
        """上传一个本地文件（图片等）到 Dify，返回文件 ID。

        端点 /files/upload 属于 Dify **App API**（不是 Knowledge API），
        必须用 app- 开头的 App API Key，dataset- 开头 Knowledge API Key 会被 401 拒绝。
        调用方负责传入合适的 app_api_key（或让本方法从 settings.dify_app_api_key 读）。

        后续在 add_segments 的 attachment_ids 中引用 file_id 即可，
        或把返回的 source_url 直接嵌入 markdown 内容（Dify 索引时会拉取并内嵌）。
        """
        file_path = Path(file_path).resolve()
        if not file_path.is_file():
            raise FileNotFoundError(f"文件不存在: {file_path}")

        if not self.app_api_key:
            raise ValueError(
                "dify_app_api_key 未配置（settings.dify_app_api_key 或环境变量 RAG_DIFY_APP_API_KEY）。"
                "/files/upload 端点属于 Dify App API，需要 app- 开头的 Key；"
                "Knowledge API 的 dataset- Key 在此处会被 401。"
            )

        mime, _ = mimetypes.guess_type(str(file_path))
        if not mime:
            mime = "application/octet-stream"

        log.info(
            "dify upload_file start",
            extra={"step": "dify", "status": "upload", "file": file_path.name, "size": file_path.stat().st_size},
        )

        def _do() -> Dict[str, Any]:
            with open(file_path, "rb") as f:
                files = {"file": (file_path.name, f, mime)}
                data = {"user": user}
                with httpx.Client(timeout=self.timeout) as client:
                    resp = client.post(
                        f"{self.api_url}/files/upload",
                        # ★ 关键：/files/upload 必须用 App API Key
                        headers=self._auth_headers(use_app_key=True),
                        files=files,
                        data=data,
                    )
            return self._parse(resp, expect_json=True)

        payload = self._with_retry(_do, label=f"upload_file({file_path.name})")
        return DifyUploadedFile(
            file_id=payload.get("id", ""),
            name=payload.get("name", file_path.name),
            size=int(payload.get("size") or 0),
            extension=payload.get("extension", file_path.suffix.lstrip(".")),
            mime_type=payload.get("mime_type", mime),
            url=payload.get("url"),
            source_url=payload.get("source_url"),  # ★ 完整 Dify 域 URL（含签名）
        )

    def create_document_by_text(
        self,
        name: str,
        text: str,
        *,
        user: str = "ragsystem",
        reuse_existing: bool = True,
    ) -> DifyDocument:
        """创建（或复用）文档（text 可为占位）。

        ★ 2026-08-20 修复：部分 Dify 部署在 create_by_text 时对同名文档做
        「复用 + 用新 text 重新索引」，会把已入库的分段全部清空（只留下新
        占位文本切出的 1 段），且会留下重复文档。因此：
        - 先按名称查找已有文档；
        - 已存在且 indexing_status=completed 时：清空其旧分段后**直接复用**
          （不再调 create_by_text，避免 re-index 清空分段）；
        - 不存在时才真正创建。

        Args:
            reuse_existing: True（默认）时复用同名已完成文档。
        """
        if reuse_existing:
            existing = self.find_document_by_name(name, user=user)
            if existing is not None:
                log.info(
                    "dify: 已存在同名文档，复用并清空旧分段",
                    extra={
                        "step": "dify",
                        "status": "reuse",
                        "doc_name": name,
                        "document_id": existing.document_id,
                        "indexing_status": existing.indexing_status,
                    },
                )
                if existing.indexing_status == "completed":
                    self.delete_all_segments(existing.document_id, user=user)
                return existing

        body = {
            "name": name,
            "text": text,
            "indexing_technique": self.indexing_technique,
            "doc_form": self.doc_form,
            "process_rule": {"mode": "automatic"},
        }
        log.info(
            "dify create_document_by_text start",
            extra={"step": "dify", "status": "create", "doc_name": name, "text_len": len(text)},
        )

        def _do() -> Dict[str, Any]:
            with httpx.Client(timeout=self.timeout) as client:
                resp = client.post(
                    f"{self.api_url}/datasets/{self.dataset_id}/document/create_by_text",
                    headers=self._auth_headers(extra={"Content-Type": "application/json"}),
                    params={"user": user},
                    json=body,
                )
            return self._parse(resp, expect_json=True)

        payload = self._with_retry(_do, label=f"create_document({name})")
        doc = payload.get("document") or {}
        return DifyDocument(
            document_id=doc.get("id", ""),
            name=doc.get("name", name),
            batch=doc.get("batch") or payload.get("batch"),
            indexing_status=doc.get("indexing_status", "waiting"),
            enabled=bool(doc.get("enabled", True)),
        )

    def find_document_by_name(
        self,
        name: str,
        *,
        user: str = "ragsystem",
    ) -> Optional[DifyDocument]:
        """在知识库中按名称精确查找文档（分页扫描）。

        Returns:
            匹配文档；不存在返回 None。
        """
        page = 1
        while True:
            payload = self.list_documents(page=page, limit=100)
            for item in payload.get("data") or []:
                if item.get("name") == name:
                    return DifyDocument(
                        document_id=item.get("id", ""),
                        name=item.get("name", name),
                        batch=item.get("batch"),
                        indexing_status=item.get("indexing_status", "waiting"),
                        enabled=bool(item.get("enabled", True)),
                    )
            if not payload.get("has_more"):
                break
            page += 1
        return None

    def delete_segment(self, document_id: str, segment_id: str, *, user: str = "ragsystem") -> Dict[str, Any]:
        """删除单个分段。"""
        def _do() -> Dict[str, Any]:
            with httpx.Client(timeout=self.timeout) as client:
                resp = client.delete(
                    f"{self.api_url}/datasets/{self.dataset_id}/documents/{document_id}/segments/{segment_id}",
                    headers=self._auth_headers(),
                    params={"user": user},
                )
            return self._parse(resp, expect_json=True)

        return self._with_retry(_do, label=f"delete_segment({document_id}/{segment_id})")

    def delete_all_segments(self, document_id: str, *, user: str = "ragsystem") -> int:
        """清空文档的所有分段（逐条 DELETE）。

        用于复用同名文档前清场，避免 add_segments 后出现重复内容。
        返回删除条数。
        """
        segs = self.list_segments(document_id, user=user)
        deleted = 0
        for seg in segs:
            seg_id = seg.get("id") or seg.get("segment_id")
            if not seg_id:
                continue
            try:
                self.delete_segment(document_id, seg_id, user=user)
                deleted += 1
            except DifyError as e:
                log.warning(
                    "dify: 删除旧分段失败",
                    extra={
                        "step": "dify",
                        "status": "delete_segment_failed",
                        "document_id": document_id,
                        "segment_id": seg_id,
                        "error_msg": str(e)[:200],
                    },
                )
        if deleted:
            log.info(
                "dify: 已清空文档旧分段",
                extra={"step": "dify", "status": "segments_cleared", "document_id": document_id, "deleted": deleted},
            )
        return deleted

    def get_document(self, document_id: str) -> DifyDocument:
        """查询单个文档的当前状态（用于等待 indexing 完成）。"""
        def _do() -> Dict[str, Any]:
            with httpx.Client(timeout=self.timeout) as client:
                resp = client.get(
                    f"{self.api_url}/datasets/{self.dataset_id}/documents/{document_id}",
                    headers=self._auth_headers(),
                    params={"user": "ragsystem"},
                )
            return self._parse(resp, expect_json=True)

        payload = self._with_retry(_do, label=f"get_document({document_id})")
        return DifyDocument(
            document_id=payload.get("id", document_id),
            name=payload.get("name", ""),
            batch=payload.get("batch"),
            indexing_status=payload.get("indexing_status", "waiting"),
            enabled=bool(payload.get("enabled", True)),
        )

    def test_connection(self) -> Dict[str, Any]:
        """轻量级连通性测试：GET /datasets/{id}。

        用于在「执行入库」之前快速验证：
        - API URL 可达（DNS / 网络 / 端口）
        - API Key 有效（不会被 401 拒绝）
        - Dataset ID 存在（不会被 404 拒绝）

        Returns:
            {
                "ok": bool,
                "api_url": str,
                "dataset_id": str,
                "dataset_name": str | None,
                "doc_count": int | None,
                "elapsed_ms": int,
                "error": str | None,         # 失败时填入
                "error_code": int | None,    # HTTP 状态码（4xx/5xx 时填入）
            }
        """
        t0 = time.perf_counter()
        result: Dict[str, Any] = {
            "ok": False,
            "api_url": self.api_url,
            "dataset_id": self.dataset_id,
            "dataset_name": None,
            "doc_count": None,
            "elapsed_ms": 0,
            "error": None,
            "error_code": None,
        }
        try:
            with httpx.Client(timeout=min(self.timeout, 15)) as client:
                resp = client.get(
                    f"{self.api_url}/datasets/{self.dataset_id}",
                    headers=self._auth_headers(),
                )
            if 200 <= resp.status_code < 300:
                payload = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
                result.update({
                    "ok": True,
                    "dataset_name": payload.get("name"),
                    "doc_count": payload.get("document_count") or payload.get("total_documents"),
                })
            else:
                # 4xx / 5xx：原样返出，让前端能区分错误类型
                body_text = (resp.text or "")[:500]
                hint = self._hint_for_status(resp.status_code, body_text)
                result.update({
                    "ok": False,
                    "error_code": resp.status_code,
                    "error": f"{resp.status_code} {resp.reason_phrase}: {body_text}{hint}",
                })
        except httpx.ConnectError as e:
            result["error"] = f"无法连接 Dify ({self.api_url}): {e}"
        except httpx.TimeoutException as e:
            result["error"] = f"连接 Dify 超时: {e}"
        except Exception as e:  # noqa: BLE001
            result["error"] = f"未知错误: {e}"
        result["elapsed_ms"] = int((time.perf_counter() - t0) * 1000)
        return result

    @staticmethod
    def _hint_for_status(status_code: int, body: str) -> str:
        """根据 HTTP 状态码给可操作的修复建议。"""
        if status_code == 401:
            return (
                "  →  提示：API Key 无效或已过期。"
                "请到 Dify 控制台 → 知识库 → API 访问 → 重新生成 dataset- 开头的新 Key，"
                "然后写到 backend/.env 的 RAG_DIFY_API_KEY 后重启服务。"
            )
        if status_code == 403:
            return "  →  提示：API Key 没有访问该知识库的权限，请确认 Key 与 dataset_id 来自同一工作区。"
        if status_code == 404:
            return (
                "  →  提示：知识库 ID 不存在。"
                "请到 Dify 控制台 → 知识库 → URL 末尾的 UUID 即为 dataset_id，"
                "并写到 backend/.env 的 RAG_DIFY_DATASET_ID 后重启服务。"
            )
        if status_code == 400 and "tenant" in body.lower():
            return "  →  提示：API Key 与 dataset 来自不同租户（自托管 vs 云服务）。检查 RAG_DIFY_API_URL 是否正确。"
        return ""

    def wait_document_ready(
        self,
        document_id: str,
        *,
        timeout: Optional[float] = None,
        poll_interval: Optional[float] = None,
    ) -> DifyDocument:
        """轮询等待文档 indexing_status=completed 且 enabled=True。"""
        deadline = time.monotonic() + (timeout or settings.dify_indexing_wait_timeout)
        interval = poll_interval or settings.dify_indexing_poll_interval
        last = None
        while time.monotonic() < deadline:
            doc = self.get_document(document_id)
            last = doc
            if doc.is_ready:
                log.info(
                    "dify document ready",
                    extra={"step": "dify", "status": "ready", "document_id": document_id},
                )
                return doc
            if doc.indexing_status == "error":
                raise DifyError(
                    f"Dify 文档 indexing 失败: name={doc.name}, id={document_id}"
                )
            log.debug(
                "dify document not ready, polling",
                extra={"step": "dify", "status": "wait", "document_id": document_id, "indexing_status": doc.indexing_status},
            )
            time.sleep(interval)
        raise DifyError(
            f"Dify 文档等待 indexing 完成超时（>{timeout or settings.dify_indexing_wait_timeout}s）: id={document_id}, last_status={(last.indexing_status if last else 'unknown')}"
        )

    def add_segments(
        self,
        document_id: str,
        segments: List[Dict[str, Any]],
        *,
        user: str = "ragsystem",
    ) -> List[DifySegment]:
        """批量添加分段。

        segments 每项支持字段：
            - content (str, 必填)
            - answer (str, 可选)
            - keywords (List[str], 可选)
            - attachment_ids (List[str], 可选)

        注意（2026-07-31 修复关键发现）：
        Dify Knowledge API 在 POST segments 端点上**静默丢弃 attachment_ids**：
        请求体里带 attachment_ids，服务端响应里 attachments=[]。
        必须再调一次 update_segment（POST segments/{id}）才能让 attachments
        真正被持久化，Dify 编辑器才能基于 attachments 渲染图片预览。

        ★ 2026-08-20 修复（WS 628-2 事件）：
        1) 超长分段（> settings.dify_max_segment_chars）会被 Dify **静默丢弃**，
           且同批中其它分段一起被回滚（HTTP 200 + 返回 created id，随后消失）。
           这里先对超长段做 HTML→markdown 转换 + 按段落拆分，保证单段不超限。
        2) 全部发送完成后重新拉取全量分段核对，发现「返回成功但未落库」的段
           立即记 ERROR 日志（含内容预览），避免再次静默丢失。

        Returns:
            创建成功的 DifySegment 列表（按返回顺序）。
        """
        if not segments:
            return []

        max_chars = settings.dify_max_segment_chars
        prepared: List[Dict[str, Any]] = []
        for seg in segments:
            content = (seg.get("content") or "")
            if len(content) > max_chars:
                converted = _html_tables_to_markdown(content)
                if len(converted) > max_chars:
                    pieces = _split_oversized_content(converted, max_chars)
                else:
                    pieces = [converted]
                log.warning(
                    "dify add_segments: 超长分段已处理 len=%d -> %d 段 (max=%d)",
                    len(content), len(pieces), max_chars,
                    extra={
                        "step": "dify",
                        "status": "segment_oversized_split",
                        "document_id": document_id,
                        "origin_len": len(content),
                        "pieces": len(pieces),
                        "preview": content[:80],
                    },
                )
                # 首段保留元数据（attachment_ids/keywords/answer），拆分出的后续段只带内容
                for idx, piece in enumerate(pieces):
                    item: Dict[str, Any] = {"content": piece}
                    if idx == 0:
                        for k in ("answer", "keywords", "attachment_ids"):
                            if seg.get(k):
                                item[k] = seg[k]
                    else:
                        log.warning(
                            "dify add_segments: 拆分出的第 %d 段未携带元数据 "
                            "(attachment_ids/keywords 仅保留在首段)",
                            idx + 1,
                            extra={"step": "dify", "status": "split_meta_dropped", "document_id": document_id},
                        )
                    prepared.append(item)
            else:
                prepared.append(seg)

        # 服务端有 segments 数量上限（默认 100），分批发送
        # ★ 2026-08-12：实测 Dify 对大批量请求（100 段/批，每段内容可能数千字符）
        #   会静默丢弃大部分分段。将默认批次从 100 降到 30，并添加诊断日志。
        chunk_size = settings.dify_segments_per_request
        all_results: List[DifySegment] = []
        total_batches = (len(prepared) + chunk_size - 1) // chunk_size
        for batch_idx, i in enumerate(range(0, len(prepared), chunk_size), start=1):
            batch = prepared[i : i + chunk_size]
            batch_results = self._add_segments_once(document_id, batch, user=user)
            log.info(
                "dify add_segments batch %d/%d: requested=%d created=%d",
                batch_idx, total_batches, len(batch), len(batch_results),
            )
            if len(batch_results) < len(batch):
                log.warning(
                    "dify add_segments batch %d/%d 丢失分段! requested=%d created=%d",
                    batch_idx, total_batches, len(batch), len(batch_results),
                )
            all_results.extend(batch_results)

        # ★ 写入后核对：返回成功但未落库的段（Dify 静默丢弃）必须暴露出来
        self._verify_segments_persisted(document_id, all_results, user=user)
        return all_results

    def _verify_segments_persisted(
        self,
        document_id: str,
        created: List[DifySegment],
        *,
        user: str = "ragsystem",
    ) -> None:
        """核对 add_segments 返回的段是否真正落库。

        Dify 对超长分段/大批量请求会出现「HTTP 200 + 返回 created id，
        但随后分段被删除」的静默丢失。此方法重新拉取全量分段，把已消失的
        返回 id 用 ERROR 日志暴露出来。
        """
        if not created:
            return
        try:
            current = self.list_segments(document_id, user=user)
        except DifyError:
            log.warning(
                "dify add_segments 核对失败（无法拉取分段列表），无法确认全部落库",
                extra={"step": "dify", "status": "verify_failed", "document_id": document_id},
            )
            return
        current_ids = {str(s.get("id") or s.get("segment_id")) for s in current}
        vanished = [seg for seg in created if seg.segment_id and seg.segment_id not in current_ids]
        if vanished:
            log.error(
                "dify add_segments 校验失败: %d/%d 段返回成功但未落库! "
                "（Dify 对超长/大批量分段静默丢弃）",
                len(vanished), len(created),
                extra={
                    "step": "dify",
                    "status": "segments_vanished",
                    "document_id": document_id,
                    "created": len(created),
                    "vanished": len(vanished),
                    "previews": [s.content[:60] for s in vanished[:10]],
                },
            )
        else:
            log.info(
                "dify add_segments 核对通过: %d 段全部落库",
                len(created),
                extra={"step": "dify", "status": "verify_ok", "document_id": document_id},
            )

    def update_segment(
        self,
        document_id: str,
        segment_id: str,
        *,
        content: Optional[str] = None,
        answer: Optional[str] = None,
        keywords: Optional[List[str]] = None,
        attachment_ids: Optional[List[str]] = None,
        enabled: Optional[bool] = None,
        regenerate_child_chunks: bool = False,
        user: str = "ragsystem",
    ) -> Dict[str, Any]:
        """更新单个分段。

        ★ 关键（2026-07-31 修复）：
        POST /datasets/{id}/documents/{doc}/segments/{seg} 端点（SegmentUpdateArgs）
        与 POST /segments 端点（SegmentCreateItemPayload）行为不同：
        - POST /segments: attachment_ids 静默丢弃
        - POST /segments/{id}: attachment_ids 正确持久化到 attachments 字段

        调用方必须在 add_segments 之后，对每个有 attachment_ids 的段调用一次
        update_segment，编辑器才能预览图片。

        Args:
            content: 新的段内容（None 不更新）
            attachment_ids: 新的 attachment 列表（None 不更新；[] 表示清空）
        """
        body: Dict[str, Any] = {"segment": {}}
        seg: Dict[str, Any] = {}
        if content is not None:
            seg["content"] = content
        if answer is not None:
            seg["answer"] = answer
        if keywords is not None:
            seg["keywords"] = keywords
        if attachment_ids is not None:
            seg["attachment_ids"] = attachment_ids
        if enabled is not None:
            seg["enabled"] = enabled
        seg["regenerate_child_chunks"] = regenerate_child_chunks
        body["segment"] = seg

        def _do() -> Dict[str, Any]:
            with httpx.Client(timeout=self.timeout) as client:
                resp = client.post(
                    f"{self.api_url}/datasets/{self.dataset_id}/documents/{document_id}/segments/{segment_id}",
                    headers=self._auth_headers(extra={"Content-Type": "application/json"}),
                    params={"user": user},
                    json=body,
                )
            return self._parse(resp, expect_json=True)

        return self._with_retry(_do, label=f"update_segment({document_id}/{segment_id})")

    # -------------------- 人工校验（plan §3.5）相关 --------------------

    def list_documents(
        self,
        *,
        page: int = 1,
        limit: int = 50,
        keyword: Optional[str] = None,
    ) -> Dict[str, Any]:
        """GET /datasets/{id}/documents — 列出数据集下所有文档（人工校验左侧栏）。

        Dify 端分页：默认 page=1, limit=50。返回 payload 通常为
        ``{"data": [Document, ...], "has_more": bool, "limit": int, "total": int, ...}``
        实际字段名以 Dify API 为准。"""
        params: Dict[str, Any] = {"page": page, "limit": limit}
        if keyword:
            params["keyword"] = keyword

        def _do() -> Dict[str, Any]:
            with httpx.Client(timeout=self.timeout) as client:
                resp = client.get(
                    f"{self.api_url}/datasets/{self.dataset_id}/documents",
                    headers=self._auth_headers(),
                    params=params,
                )
            return self._parse(resp, expect_json=True)

        log.info(
            "dify list_documents start",
            extra={"step": "dify", "status": "list_documents", "page": page, "limit": limit},
        )
        return self._with_retry(_do, label=f"list_documents(page={page})")

    def list_segments(
        self,
        document_id: str,
        *,
        keyword: Optional[str] = None,
        status: Optional[str] = None,
        page: int = 1,
        limit: Optional[int] = None,
        user: str = "ragsystem",
    ) -> List[Dict[str, Any]]:
        """GET /datasets/{id}/documents/{doc}/segments — 列出文档的所有分段（人工校验中间栏）。

        Args:
            keyword: 关键词过滤
            status: 分段状态过滤（如 "completed" / "error"）
            page: 页码（配合 limit 使用；默认 1）
            limit: 每页数量。None 表示**拉取全部分页**（默认行为，
                   与旧版「返回所有分段」语义一致）。传入数值则只拉单页。
            user: Dify user 标识

        返回 ``payload["data"]`` 列表（每项含 id/content/position/enabled/attachments ...）。
        失败时抛 DifyError。"""
        params: Dict[str, Any] = {"user": user}
        if keyword:
            params["keyword"] = keyword
        if status:
            params["status"] = status
        if limit is not None:
            params["limit"] = limit

        def _fetch(pg: int) -> Dict[str, Any]:
            params["page"] = pg
            with httpx.Client(timeout=self.timeout) as client:
                resp = client.get(
                    f"{self.api_url}/datasets/{self.dataset_id}/documents/{document_id}/segments",
                    headers=self._auth_headers(),
                    params=params,
                )
            return self._parse(resp, expect_json=True)

        log.info(
            "dify list_segments start",
            extra={"step": "dify", "status": "list_segments", "document_id": document_id},
        )
        if limit is not None:
            # 单页查询
            payload = self._with_retry(
                lambda: _fetch(page), label=f"list_segments({document_id})"
            )
            if isinstance(payload, list):
                return payload
            return payload.get("data") or []

        # 分页拉全量
        all_items: List[Dict[str, Any]] = []
        pg = page
        while True:
            payload = self._with_retry(
                lambda pg=pg: _fetch(pg), label=f"list_segments({document_id}, page={pg})"
            )
            if isinstance(payload, list):
                all_items.extend(payload)
                break
            data = payload.get("data") or []
            all_items.extend(data)
            if not payload.get("has_more"):
                break
            pg += 1
        return all_items

    # -------------------- 元数据相关（Metadata） --------------------

    def list_metadata_fields(self) -> List[Dict[str, Any]]:
        """GET /datasets/{id}/metadata — 列出知识库所有元数据字段（含内置）。"""
        def _do() -> Dict[str, Any]:
            with httpx.Client(timeout=self.timeout) as client:
                resp = client.get(
                    f"{self.api_url}/datasets/{self.dataset_id}/metadata",
                    headers=self._auth_headers(),
                )
            return self._parse(resp, expect_json=True)

        payload = self._with_retry(_do, label="list_metadata_fields")
        return payload.get("doc_metadata") or []

    def create_metadata_field(self, name: str, type_: str = "string") -> Dict[str, Any]:
        """POST /datasets/{id}/metadata — 创建自定义元数据字段。

        Args:
            name: 字段名（知识库内唯一，≤255 字符）
            type_: 字段类型 — "string" / "number" / "time"
        """
        body = {"name": name, "type": type_}

        def _do() -> Dict[str, Any]:
            with httpx.Client(timeout=self.timeout) as client:
                resp = client.post(
                    f"{self.api_url}/datasets/{self.dataset_id}/metadata",
                    headers=self._auth_headers(extra={"Content-Type": "application/json"}),
                    json=body,
                )
            return self._parse(resp, expect_json=True)

        return self._with_retry(_do, label=f"create_metadata_field({name})")

    def batch_update_document_metadata(
        self,
        operations: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """POST /datasets/{id}/documents/metadata — 批量更新文档元数据值。

        Args:
            operations: 每项包含:
                - document_id (str): Dify 文档 ID
                - metadata_list (list): [{id, name, value}, ...]
                - partial_update (bool): True 仅更新指定字段，保留其余
        """
        body = {"operation_data": operations}

        def _do() -> Dict[str, Any]:
            with httpx.Client(timeout=self.timeout) as client:
                resp = client.post(
                    f"{self.api_url}/datasets/{self.dataset_id}/documents/metadata",
                    headers=self._auth_headers(extra={"Content-Type": "application/json"}),
                    json=body,
                )
            return self._parse(resp, expect_json=True)

        return self._with_retry(_do, label=f"batch_update_document_metadata(n={len(operations)})")

    # -------------------- 内部 --------------------

    def _add_segments_once(
        self,
        document_id: str,
        segments: List[Dict[str, Any]],
        *,
        user: str,
    ) -> List[DifySegment]:
        body = {"segments": segments}

        def _do() -> Dict[str, Any]:
            with httpx.Client(timeout=self.timeout) as client:
                resp = client.post(
                    f"{self.api_url}/datasets/{self.dataset_id}/documents/{document_id}/segments",
                    headers=self._auth_headers(extra={"Content-Type": "application/json"}),
                    params={"user": user},
                    json=body,
                )
            return self._parse(resp, expect_json=True)

        payload = self._with_retry(
            _do, label=f"add_segments(doc={document_id}, n={len(segments)})"
        )
        data = payload.get("data") or []
        out: List[DifySegment] = []
        for idx, item in enumerate(data):
            out.append(
                DifySegment(
                    segment_id=item.get("id", ""),
                    document_id=item.get("document_id", document_id),
                    position=item.get("position", idx + 1),
                    content=item.get("content", ""),
                    word_count=int(item.get("word_count") or 0),
                    tokens=int(item.get("tokens") or 0),
                    status=item.get("status", "completed"),
                )
            )
        return out

    def _auth_headers(
        self,
        extra: Optional[Dict[str, str]] = None,
        *,
        use_app_key: bool = False,
    ) -> Dict[str, str]:
        """生成鉴权头。

        Args:
            extra: 额外的 header（如 Content-Type）
            use_app_key: True 时用 App API Key（仅 /files/upload 需要），
                         默认 False（用 Knowledge API Key）
        """
        if use_app_key:
            key = self.app_api_key
        else:
            key = self.api_key
        h = {"Authorization": f"Bearer {key}"}
        if extra:
            h.update(extra)
        return h

    def _parse(self, resp: httpx.Response, *, expect_json: bool) -> Dict[str, Any]:
        status = resp.status_code
        body_text = resp.text or ""
        if 200 <= status < 300:
            if not expect_json:
                return {}
            try:
                data = resp.json()
            except Exception as e:  # noqa: BLE001
                raise _RetryableDifyError(f"Dify 响应不是合法 JSON: {e}") from e
            if not isinstance(data, dict):
                raise _RetryableDifyError("Dify 响应不是 JSON object")
            return data
        body_snippet = body_text[:500]
        if 400 <= status < 500:
            raise _FatalDifyError(
                f"Dify 4xx: {status} {resp.reason_phrase}",
                status_code=status,
                body=body_snippet,
            )
        raise _RetryableDifyError(f"Dify 5xx: {status} {resp.reason_phrase}: {body_snippet}")

    def _with_retry(self, fn, *, label: str):
        """统一重试：4xx 不重试（_FatalDifyError），5xx/网络/JSON 错误重试。"""
        last_err: Optional[Exception] = None
        for attempt in range(1, self.max_retries + 1):
            try:
                return fn()
            except _FatalDifyError as e:
                # 客户端错误，立即失败（重试无意义）
                log.error(
                    "dify 4xx, no retry",
                    extra={
                        "step": "dify",
                        "status": "fatal",
                        "label": label,
                        "status_code": e.status_code,
                        "error_msg": e.body[:200],
                    },
                )
                raise DifyError(
                    str(e),
                    attempts=attempt,
                    status_code=e.status_code,
                    body=e.body,
                ) from e
            except Exception as e:  # noqa: BLE001
                last_err = e
                if attempt >= self.max_retries:
                    break
                sleep_s = self.backoff ** (attempt - 1)
                log.warning(
                    "dify call failed, retrying",
                    extra={
                        "step": "dify",
                        "status": "retry",
                        "label": label,
                        "attempt": attempt,
                        "max_retries": self.max_retries,
                        "sleep_s": sleep_s,
                        "error_msg": str(e)[:200],
                    },
                )
                time.sleep(sleep_s)
        raise DifyError(
            f"Dify 调用失败 {label}（重试 {self.max_retries} 次耗尽）: {last_err}",
            attempts=self.max_retries,
        )
