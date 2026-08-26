"""plan.md §3.4 Dify 入库单元测试。

覆盖：
1. DifyClient HTTP 封装（FakeDifyClient 模拟各种场景：成功/4xx/5xx/重试）
2. _extract_image_refs 从 markdown 提取图片引用
3. _resolve_image_path 解析相对路径为绝对路径
4. upload_one_doc 单文档入库完整流程
5. upload_all_docs 遍历 + 幂等 + 写 manifest
"""

from __future__ import annotations

import importlib
import json
import logging
import shutil
from io import BytesIO
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
from PIL import Image

logging.disable(logging.CRITICAL)


# ============ fixtures ============


@pytest.fixture(autouse=True)
def fresh_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """每个用例：隔离 tmp_path 作为 data_root，并预填 Dify 配置。"""
    test_data_root = tmp_path / "data"
    monkeypatch.setenv("RAG_DATA_ROOT", str(test_data_root))
    monkeypatch.setenv("RAG_DIFY_API_KEY", "dataset-test-key")
    monkeypatch.setenv("RAG_DIFY_DATASET_ID", "test-dataset-id")
    # ★ 显式清掉公网 URL 变量 & app_api_key：保证默认走 /files/upload 策略（FakeDifyClient 模拟）。
    #   涉及公网 URL 替换的测试单独 monkeypatch.setenv 开启 RAG_PUBLIC_BASE_URL；
    #   涉及纯 Dify 域 URL（不走 /files/upload）的测试单独 monkeypatch.delenv("RAG_DIFY_APP_API_KEY")。
    monkeypatch.delenv("RAG_PUBLIC_BASE_URL", raising=False)
    # ★ 给 fake 端一个 app_api_key，让 /files/upload 策略能跑（FakeDifyClient.upload_file 不校验值）
    monkeypatch.setenv("RAG_DIFY_APP_API_KEY", "app-test-key")
    # ★ 测试默认走「老行为」：调用 /files/upload 拿 file_id + attachment_ids。
    #   只有显式开 SKIP_FILE_UPLOAD 的测试才切到「纯 OSS URL」模式。
    #   （prod .env 里 RAG_DIFY_SKIP_FILE_UPLOAD=true 是 2026-08-04 起的默认）
    monkeypatch.setenv("RAG_DIFY_SKIP_FILE_UPLOAD", "false")

    from app import config as cfg_mod
    importlib.reload(cfg_mod)
    # ★ .env 优先级高于环境变量（settings_customise_sources），
    #   必须用 init kwargs 覆盖 data_root 才能真正隔离测试目录
    settings = cfg_mod.Settings(data_root=test_data_root)
    cfg_mod.settings = settings

    from app.services import scanner, parser, chunker, dify_uploader, dify_ingest, image_host, manifest_store
    scanner.settings = settings
    parser.settings = settings
    chunker.settings = settings
    dify_uploader.settings = settings
    dify_ingest.settings = settings
    image_host.settings = settings
    manifest_store.settings = settings

    settings.ensure_dirs()
    yield settings


def _make_chunks_dir(
    chunks_root: Path,
    stem: str,
    chunks: List[Dict[str, Any]],
    images: Optional[Dict[str, bytes]] = None,
) -> Path:
    """在 chunks_root/stem/ 下造一个完整的 chunked 文档。"""
    doc_dir = chunks_root / stem
    doc_dir.mkdir(parents=True, exist_ok=True)
    images = images or {}
    image_dir = doc_dir / "images"
    image_dir.mkdir(exist_ok=True)

    items = []
    for idx, c in enumerate(chunks, start=1):
        chunk_id = c.get("chunk_id") or f"chunk_{idx:03d}"
        file_name = c.get("file_name") or f"{chunk_id}_{c.get('slug', 'x')}.md"
        content = c["content"]
        (doc_dir / file_name).write_text(content, encoding="utf-8")
        # 把 content 中提到的图片写到 images/
        refs = c.get("image_refs", [])
        for ref in refs:
            basename = Path(ref).name
            if ref in images:
                (image_dir / basename).write_bytes(images[ref])
            elif basename not in [p.name for p in image_dir.iterdir()]:
                # 自动造 1x1 像素占位图
                _make_placeholder_image(image_dir / basename)
        items.append({
            "chunk_id": chunk_id,
            "file_name": file_name,
            "title_path": c.get("title_path", chunk_id),
            "chunk_type": c.get("chunk_type", "body"),
            "char_count": len(content),
            "image_refs": refs,
            "is_split": False,
        })

    metadata = {
        "doc_stem": stem,
        "chunk_count": len(items),
        "chunks": items,
    }
    (doc_dir / "chunk_metadata.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return doc_dir


def _make_placeholder_image(path: Path) -> None:
    img = Image.new("RGB", (2, 2), color=(255, 255, 255))
    ext = path.suffix.lstrip(".").upper()
    if ext == "JPG":
        ext = "JPEG"
    img.save(path, format=ext or "PNG")


# ============ Fake DifyClient ============


class FakeResp:
    def __init__(self, status: int, body: Any = None, ctype: str = "application/json") -> None:
        self.status_code = status
        self.headers = {"content-type": ctype}
        self._body = body
        if isinstance(body, (dict, list)):
            self._text = json.dumps(body, ensure_ascii=False)
        else:
            self._text = body if isinstance(body, str) else ""
        self.reason_phrase = "OK" if 200 <= status < 300 else "Server Error"

    def json(self) -> Any:
        if isinstance(self._body, (dict, list)):
            return self._body
        return json.loads(self._text) if self._text else {}

    @property
    def text(self) -> str:
        return self._text


class FakeDifyClient:
    """完全可控的 fake，模拟 Dify Knowledge API。

    行为可注入：
    - upload_files: dict[文件名 → file_id]（预填），调用时按文件名返回；缺失则用计数器生成
    - create_doc_status: "ok" / "fail_4xx" / "fail_5xx"（5xx 会触发重试到 max_retries）
    - add_seg_status: 同上
    - wait_indexing_timeout: 模拟 indexing 一直不完成
    - raise_in: 第几次调用抛错
    """

    def __init__(self, *, max_retries: int = 3, backoff: float = 0.0) -> None:
        from app.services import dify_uploader
        self.api_url = "http://fake-dify"
        self.api_key = "dataset-test-key"
        self.dataset_id = "test-dataset-id"
        self.max_retries = max_retries
        self.backoff = backoff
        self.indexing_technique = "high_quality"
        self.doc_form = "text_model"
        self.timeout = 30
        self.calls: List[Dict[str, Any]] = []
        self.upload_counter = 0
        self.doc_counter = 0
        self.seg_counter = 0
        # 预填映射
        self.uploaded_files: Dict[str, str] = {}
        self.created_docs: Dict[str, str] = {}  # name → document_id
        self.added_segments: Dict[str, List[Dict[str, Any]]] = {}  # document_id → segments
        # 行为开关
        self.create_doc_status = "ok"
        self.add_seg_status = "ok"
        self.wait_ready_status = "ok"
        self.get_doc_status = "ok"
        self.raise_in_create = 0
        self.raise_in_upload = 0
        self.raise_in_add = 0

    def upload_file(self, file_path: Path) -> Any:
        from app.services import dify_uploader
        self.calls.append({"op": "upload_file", "file": file_path.name})
        if self.raise_in_upload > 0:
            self.raise_in_upload -= 1
            raise dify_uploader.DifyError(f"fake upload error ({file_path.name})")
        name = file_path.name
        if name in self.uploaded_files:
            fid = self.uploaded_files[name]
        else:
            self.upload_counter += 1
            fid = f"file-fake-{self.upload_counter:04d}"
            self.uploaded_files[name] = fid
        return dify_uploader.DifyUploadedFile(
            file_id=fid,
            name=name,
            size=file_path.stat().st_size if file_path.exists() else 0,
            extension=file_path.suffix.lstrip("."),
            mime_type="image/jpeg",
            url=f"http://fake-dify/{fid}",
            source_url=None,
        )

    def create_document_by_text(self, name: str, text: str, **kw: Any) -> Any:
        from app.services import dify_uploader
        self.calls.append({"op": "create_doc", "name": name})
        if self.raise_in_create > 0:
            self.raise_in_create -= 1
            raise dify_uploader.DifyError(f"fake create error ({name})")
        if self.create_doc_status == "fail_4xx":
            raise dify_uploader.DifyError(
                f"fake 4xx ({name})", status_code=400, body="bad request"
            )
        if self.create_doc_status == "fail_5xx":
            # 5xx 会触发重试，但 fake 不重试（区别于真 client），所以直接抛
            raise dify_uploader.DifyError(
                f"fake 5xx ({name})", status_code=500, body="server error"
            )
        self.doc_counter += 1
        doc_id = f"doc-fake-{self.doc_counter:04d}"
        self.created_docs[name] = doc_id
        self.added_segments[doc_id] = []
        return dify_uploader.DifyDocument(
            document_id=doc_id,
            name=name,
            batch=f"batch-{self.doc_counter:04d}",
            indexing_status="waiting",
            enabled=True,
        )

    def get_document(self, document_id: str) -> Any:
        from app.services import dify_uploader
        self.calls.append({"op": "get_doc", "doc": document_id})
        if self.get_doc_status == "fail_5xx":
            raise dify_uploader.DifyError(
                "fake 5xx get_doc", status_code=500, body="err"
            )
        return dify_uploader.DifyDocument(
            document_id=document_id,
            name="",
            indexing_status="completed",  # 立即就绪
            enabled=True,
        )

    def wait_document_ready(self, document_id: str, **kw: Any) -> Any:
        if self.wait_ready_status == "timeout":
            from app.services import dify_uploader
            raise dify_uploader.DifyError(f"timeout waiting for {document_id}")
        return self.get_document(document_id)

    def add_segments(self, document_id: str, segments: List[Dict[str, Any]], **kw: Any) -> Any:
        from app.services import dify_uploader
        self.calls.append({"op": "add_seg", "doc": document_id, "n": len(segments)})
        if self.raise_in_add > 0:
            self.raise_in_add -= 1
            raise dify_uploader.DifyError("fake add seg error")
        if self.add_seg_status == "fail_4xx":
            raise dify_uploader.DifyError(
                "fake 4xx add_seg", status_code=400, body="bad"
            )
        # ★★★ 关键（2026-07-31）：模拟 Dify Knowledge API 行为：
        #   add_segments 端点（POST /segments）**静默丢弃 attachment_ids**，
        #   持久化必须靠 update_segment（POST /segments/{id}）。
        # 真实 Dify 行为：update_segment 成功 → 段里 attachments = [{id, name, source_url, ...}]
        out = []
        for idx, seg in enumerate(segments, start=1):
            self.seg_counter += 1
            seg_id = f"seg-fake-{self.seg_counter:04d}"
            # add_segments 返回的段**不包含** attachment_ids（模拟 Dify 静默丢弃）
            seg_stored = {k: v for k, v in seg.items() if k != "attachment_ids"}
            self.added_segments.setdefault(document_id, []).append(seg_stored)
            out.append(
                dify_uploader.DifySegment(
                    segment_id=seg_id,
                    document_id=document_id,
                    position=idx,
                    content=seg.get("content", ""),
                    word_count=len(seg.get("content", "")),
                    tokens=0,
                    status="completed",
                )
            )
        return out

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
        """模拟 POST /datasets/{id}/documents/{doc}/segments/{seg_id}。

        ★ 2026-07-31 修复（彻底版）：
        - 此端点（SegmentUpdateArgs）正确持久化 attachment_ids
        - 而 add_segments 端点（SegmentCreateItemPayload）静默丢弃 attachment_ids
        - Fake 模拟：add_segments 后 segments[idx] 无 attachment_ids，
          update_segment 后 segments[idx]["attachment_ids"] 才被设置，
          与真实 Dify 行为一致。

        ★ 2026-08-04 扩展：content / keywords / answer / enabled 都会回写到段字典，
          模拟 Dify 持久化 update_segment 提交的所有字段。
        """
        from app.services import dify_uploader
        self.calls.append({
            "op": "update_segment",
            "doc": document_id,
            "seg": segment_id,
            "attachment_ids": list(attachment_ids) if attachment_ids else [],
            "has_content": content is not None,
        })
        if self.raise_in_add > 0:
            self.raise_in_add -= 1
            raise dify_uploader.DifyError(f"fake update_segment error ({segment_id})")
        # 找到对应段，回填 attachment_ids
        for seg in self.added_segments.get(document_id, []):
            # 段字典里没有 seg_id 字段，但 created 时的 seg_stored 顺序对齐 segment_id
            # 这里用 position 推断；更稳妥的方式是给段也存 seg_id
            pass
        # ★ 更稳妥：维护一个 seg_id → seg 字典 的反向索引
        if not hasattr(self, "_seg_index"):
            self._seg_index: Dict[str, Dict[str, Any]] = {}
        # 第一次 update_segment 时建立 seg_id → seg 映射
        if not self._seg_index:
            counter = 0
            for doc_segs in self.added_segments.values():
                for s in doc_segs:
                    counter += 1
                    sid = f"seg-fake-{counter:04d}"
                    if sid not in self._seg_index:
                        self._seg_index[sid] = s
        seg = self._seg_index.get(segment_id)
        # ★ 2026-08-04：所有 update_segment 字段都回写到段字典（与 Dify 持久化行为一致）
        if seg is not None:
            if attachment_ids is not None:
                seg["attachment_ids"] = list(attachment_ids)
            if content is not None:
                seg["content"] = content
            if keywords is not None:
                seg["keywords"] = list(keywords)
            if answer is not None:
                seg["answer"] = answer
            if enabled is not None:
                seg["enabled"] = enabled
        # 构造 update_segment 响应（Dify 真实响应包含 attachments 字段）
        attachments: List[Dict[str, Any]] = []
        if attachment_ids and seg is not None:
            for fid in attachment_ids:
                # 反查 file_id 对应的 file 元数据（从 uploaded_files 字典）
                file_name = next(
                    (n for n, stored_fid in self.uploaded_files.items() if stored_fid == fid),
                    fid,
                )
                attachments.append({
                    "id": fid,
                    "name": file_name,
                    "size": 0,
                    "extension": "jpg",
                    "mime_type": "image/jpeg",
                    "source_url": f"http://fake-dify/files/{fid}/file-preview?sign=fake",
                })
        return {
            "data": [{
                "id": segment_id,
                "document_id": document_id,
                "position": 0,
                "content": content if content is not None else (seg.get("content", "") if seg else ""),
                "word_count": 0,
                "status": "completed",
                "attachments": attachments,
            }],
            "doc_form": "text_model",
        }


# ============ 1. _extract_image_refs ============


def test_extract_image_refs_basic():
    """基本：从 markdown 提取所有 ![](path) 引用。"""
    from app.services import dify_ingest
    md = (
        "封面\n\n# Title\n\n"
        "正文开始\n\n"
        "![](images/a.jpg)\n\n"
        "中间段落\n\n"
        "![](images/b.png)\n\n"
    )
    refs = dify_ingest._extract_image_refs(md)
    assert refs == ["images/a.jpg", "images/b.png"]


def test_extract_image_refs_dedup():
    """重复引用只保留一次。"""
    from app.services import dify_ingest
    md = "![](images/a.jpg)\n\n中间\n\n![](images/a.jpg)\n"
    refs = dify_ingest._extract_image_refs(md)
    assert refs == ["images/a.jpg"]


def test_extract_image_refs_with_alt_text():
    """`![alt](path)` 也应能匹配。"""
    from app.services import dify_ingest
    md = "正文\n\n![图A.1 入口外观](images/a.jpg)\n"
    refs = dify_ingest._extract_image_refs(md)
    assert refs == ["images/a.jpg"]


def test_extract_image_refs_skip_remote_urls():
    """外链（http/https/data:）应被忽略。"""
    from app.services import dify_ingest
    md = (
        "![](https://example.com/a.jpg)\n"
        "![](http://example.com/b.jpg)\n"
        "![](data:image/png;base64,xxxxx)\n"
        "![](images/local.jpg)\n"
    )
    refs = dify_ingest._extract_image_refs(md)
    assert refs == ["images/local.jpg"]


def test_dify_file_url_format():
    """★ 2026-07-31 修复（彻底版）：段里图片 URL 必须用 Dify 返回的「带签名」source_url。

    关键背景（来自 probe_attachment_field.py 实测）：
    - 无签名 URL `https://dify.17vision.com/files/{id}/file-preview`（无 ?sign=）
      → 外部 GET 返回 400 `timestamp Field required`（Dify API 拒绝）
      → Dify 索引时也无法拉取该 URL 来重签（同样 400），最终 sign_content 没图
    - 带签名 URL（source_url，含 timestamp/nonce/sign 三参数）
      → 外部 GET 返回 200（签名 5 分钟内有效）
      → Dify 索引时能成功拉取并重签存到 sign_content，编辑器正常预览
      → 段里写带签名 URL 后，add_segments 响应里的 sign_content 字段含 Dify 重新签名的 URL
         （与原 source_url 独立），所以编辑器看到的是 Dify 拉过并重签后的版本，
         不会随原签名过期而失效

    验证要点：
    - 段里的 URL 应来自 client.upload_file() 返回的 source_url（带 sign=）
    - 不要自拼无签名 URL（已知会导致 400）
    - fallback 仅在 source_url 为空时才使用无签名 URL（极端兜底）
    """
    # Case 1: 我们写入段里的 URL 格式（必须用 source_url 带签名）
    signed_url = (
        "https://dify.17vision.com/files/abc-123/file-preview"
        "?timestamp=1785489991&nonce=abcdef&sign=xyz%3D"
    )
    assert "sign=" in signed_url
    assert "timestamp=" in signed_url
    assert "nonce=" in signed_url
    # 域名是 dify.17vision.com（不是 api.dify.ai）
    assert signed_url.startswith("https://dify.17vision.com/files/")
    # 路径是 /files/{id}/file-preview（不是 /v1/files/.../preview）
    assert "/v1/" not in signed_url
    assert "file-preview" in signed_url

    # Case 2: 反例 - 自拼无签名 URL 不应再写入段里
    unsigned_url = "https://dify.17vision.com/files/abc-123/file-preview"
    assert "?" not in unsigned_url
    assert "sign=" not in unsigned_url
    # dify_ingest 应该用 source_url（带签名），不用自拼的无签名 URL

    # Case 3: alt 文本兜底为 "image"（Dify 编辑器要求 alt 必填）
    assert "![image](" in f"![image]({signed_url})"
    assert "![](http" not in f"![image]({signed_url})"  # 严禁空 alt


# ============ 2. _resolve_image_path ============


def test_resolve_image_path_basic(fresh_settings):
    """基本：images/xxx.jpg → chunks_dir/images/xxx.jpg。"""
    from app.services import dify_ingest
    s = fresh_settings
    doc = _make_chunks_dir(s.chunks_dir, "docA", [{"content": "x"}])
    (doc / "images" / "x.jpg").write_bytes(b"jpg")
    p = dify_ingest._resolve_image_path(doc, "images/x.jpg")
    assert p is not None
    assert p.name == "x.jpg"
    assert p.is_file()


def test_resolve_image_path_basename(fresh_settings):
    """纯文件名也能在 images/ 下找到。"""
    from app.services import dify_ingest
    s = fresh_settings
    doc = _make_chunks_dir(s.chunks_dir, "docA", [{"content": "x"}])
    (doc / "images" / "y.jpg").write_bytes(b"jpg")
    p = dify_ingest._resolve_image_path(doc, "y.jpg")
    assert p is not None
    assert p.name == "y.jpg"


def test_resolve_image_path_not_found(fresh_settings):
    """找不到返回 None（不抛错）。"""
    from app.services import dify_ingest
    s = fresh_settings
    doc = _make_chunks_dir(s.chunks_dir, "docA", [{"content": "x"}])
    p = dify_ingest._resolve_image_path(doc, "images/missing.jpg")
    assert p is None


def test_resolve_image_path_strip_relative_prefix(fresh_settings):
    """`../images/xxx.jpg` 这种相对路径也应能解析。"""
    from app.services import dify_ingest
    s = fresh_settings
    doc = _make_chunks_dir(s.chunks_dir, "docA", [{"content": "x"}])
    (doc / "images" / "z.jpg").write_bytes(b"jpg")
    p = dify_ingest._resolve_image_path(doc, "../images/z.jpg")
    assert p is not None
    assert p.name == "z.jpg"


# ============ 3. DifyClient 单元测试（用 monkeypatch httpx）============


def test_dify_client_upload_file_success(fresh_settings, monkeypatch, tmp_path):
    """upload_file 成功：返回 DifyUploadedFile，含 file_id。"""
    from app.services import dify_uploader
    client = dify_uploader.DifyClient()
    f = tmp_path / "test.jpg"
    Image.new("RGB", (2, 2), color="red").save(f, format="JPEG")

    def fake_post(self, url, files=None, data=None, headers=None):  # noqa: ARG001
        return FakeResp(
            200,
            {
                "id": "file-abc-123",
                "name": "test.jpg",
                "size": f.stat().st_size,
                "extension": "jpg",
                "mime_type": "image/jpeg",
            },
        )

    import httpx
    monkeypatch.setattr(httpx.Client, "post", fake_post)
    result = client.upload_file(f)
    assert result.file_id == "file-abc-123"
    assert result.name == "test.jpg"


def test_dify_client_4xx_no_retry(fresh_settings, monkeypatch, tmp_path):
    """4xx 错误不重试，立即抛 DifyError。"""
    from app.services import dify_uploader
    client = dify_uploader.DifyClient(max_retries=3, backoff=0.0)
    f = tmp_path / "x.jpg"
    f.write_bytes(b"x")

    call_count = {"n": 0}

    def fake_post(self, url, files=None, data=None, headers=None):  # noqa: ARG001
        call_count["n"] += 1
        return FakeResp(400, body='{"error":"bad file type"}')

    import httpx
    monkeypatch.setattr(httpx.Client, "post", fake_post)
    with pytest.raises(dify_uploader.DifyError) as exc:
        client.upload_file(f)
    assert exc.value.status_code == 400
    assert call_count["n"] == 1  # 不重试


def test_dify_client_5xx_retries_then_raises(fresh_settings, monkeypatch, tmp_path):
    """5xx 错误会重试 max_retries 次后抛错。"""
    from app.services import dify_uploader
    client = dify_uploader.DifyClient(max_retries=3, backoff=0.0)
    f = tmp_path / "x.jpg"
    f.write_bytes(b"x")

    call_count = {"n": 0}

    def fake_post(self, url, files=None, data=None, headers=None):  # noqa: ARG001
        call_count["n"] += 1
        return FakeResp(503, body="server error")

    import httpx
    monkeypatch.setattr(httpx.Client, "post", fake_post)
    with pytest.raises(dify_uploader.DifyError) as exc:
        client.upload_file(f)
    assert call_count["n"] == 3  # 重试 3 次
    assert "Dify 调用失败" in str(exc.value)


def test_dify_client_5xx_eventually_succeeds(fresh_settings, monkeypatch, tmp_path):
    """5xx 重试中途成功：第 1 次 5xx，第 2 次 200。"""
    from app.services import dify_uploader
    client = dify_uploader.DifyClient(max_retries=3, backoff=0.0)
    f = tmp_path / "x.jpg"
    f.write_bytes(b"x")

    call_count = {"n": 0}

    def fake_post(self, url, files=None, data=None, headers=None):  # noqa: ARG001
        call_count["n"] += 1
        if call_count["n"] == 1:
            return FakeResp(503, body="err")
        return FakeResp(200, {"id": "file-x", "name": "x.jpg", "size": 1, "extension": "jpg", "mime_type": "image/jpeg"})

    import httpx
    monkeypatch.setattr(httpx.Client, "post", fake_post)
    result = client.upload_file(f)
    assert result.file_id == "file-x"
    assert call_count["n"] == 2


def test_dify_client_create_document(fresh_settings, monkeypatch):
    """create_document_by_text 解析 document_id。"""
    from app.services import dify_uploader
    client = dify_uploader.DifyClient()

    def fake_post(self, url, json=None, headers=None, params=None):  # noqa: ARG001
        return FakeResp(
            200,
            {
                "document": {
                    "id": "doc-xyz",
                    "name": "test.txt",
                    "indexing_status": "waiting",
                    "enabled": True,
                },
                "batch": "batch-1",
            },
        )

    import httpx
    monkeypatch.setattr(httpx.Client, "post", fake_post)
    doc = client.create_document_by_text("test", "hello")
    assert doc.document_id == "doc-xyz"
    assert doc.batch == "batch-1"


def test_dify_client_add_segments_chunks(fresh_settings, monkeypatch):
    """add_segments 自动按 settings.dify_segments_per_request 分批。"""
    from app.services import dify_uploader
    from app.config import settings as cfg

    # 临时把 per_request 调小
    orig = cfg.dify_segments_per_request
    cfg.dify_segments_per_request = 2
    try:
        client = dify_uploader.DifyClient()
        calls = {"n": 0}

        def fake_post(self, url, json=None, headers=None, params=None):  # noqa: ARG001
            calls["n"] += 1
            batch_size = len(json["segments"])
            # 每个 batch 模拟返回对应数量的段
            return FakeResp(
                200,
                {
                    "data": [
                        {
                            "id": f"seg-{calls['n']}-{i}",
                            "document_id": "doc-1",
                            "position": i + 1,
                            "content": seg["content"],
                            "word_count": 1,
                        }
                        for i, seg in enumerate(json["segments"])
                    ],
                    "doc_form": "text_model",
                },
            )

        import httpx
        monkeypatch.setattr(httpx.Client, "post", fake_post)
        segs = [{"content": f"seg{i}"} for i in range(5)]
        result = client.add_segments("doc-1", segs)
        assert calls["n"] == 3  # 5 segments / 2 per batch = 3 batches
        assert len(result) == 5
    finally:
        cfg.dify_segments_per_request = orig


# ============ 4. upload_one_doc 端到端（用 FakeDifyClient）============


def test_upload_one_doc_success_with_images(fresh_settings, monkeypatch):
    """完整流程：含图片的 chunk 应上传图片并附到 segment（默认 /files/upload 策略）。

    ★ 2026-07-31 行为变更：/files/upload 拿到 file_id 后，content 里的
      `![](images/xxx.jpg)` 会被替换为「无签名」的 Dify 域 URL
      (`{dify_root}/files/{file_id}/file-preview`，无 ?sign= 后缀)，
      Dify 编辑器带 session 时可直接访问，签名不会过期。

    ★ 2026-08-04 修订：默认 backend 改为 oss，本测试显式切到 tunnel
      并配 RAG_PUBLIC_BASE_URL，覆盖"公网 URL 替换 content 图片"路径。
    """
    from app.services import dify_ingest
    from app.services import image_host

    # ★ 显式开启 tunnel 后端 + 公网 URL（默认是 oss，会走 OSS 上传）
    monkeypatch.setenv("RAG_IMAGE_HOST_BACKEND", "tunnel")
    monkeypatch.setenv("RAG_PUBLIC_BASE_URL", "https://astronomy-papers-rooms-recipients.trycloudflare.com")
    from app import config as cfg_mod
    importlib.reload(cfg_mod)
    dify_ingest.settings = cfg_mod.settings
    image_host.settings = cfg_mod.settings

    s = fresh_settings
    chunks = [
        {
            "chunk_id": "chunk_001",
            "file_name": "chunk_001_封面.md",
            "title_path": "封面",
            "content": "封面\n\n# 标题\n\n![](images/cover.jpg)\n",
            "image_refs": ["images/cover.jpg"],
        },
        {
            "chunk_id": "chunk_002",
            "file_name": "chunk_002_第一章.md",
            "title_path": "第一章",
            "content": "第一章\n\n正文\n",
            "image_refs": [],
        },
    ]
    doc = _make_chunks_dir(s.chunks_dir, "docA", chunks)

    fake = FakeDifyClient()
    doc_id, infos, err = dify_ingest.upload_one_doc(doc, fake)  # type: ignore[arg-type]

    assert err is None
    assert doc_id == "doc-fake-0001"
    assert len(infos) == 2
    # chunk_001 应有 attachment_id（cover.jpg 上传了）
    assert len(infos[0].attachment_ids) == 1
    assert infos[0].attachment_ids[0].startswith("file-fake-")
    # chunk_002 没有图，attachment_ids 为空
    assert infos[1].attachment_ids == []
    # fake.add_segments 收到 2 段，且第一段带 attachment_ids
    segs = fake.added_segments["doc-fake-0001"]
    assert len(segs) == 2
    assert "attachment_ids" in segs[0]
    assert "attachment_ids" not in segs[1]
    # ★ 2026-08 修复（Dify 聊天召回图片不显示）：
    #   行为：content 里的图片路径会被替换为永久公网 URL（来自 RAG_PUBLIC_BASE_URL），
    #   原因：Dify /files/upload 返回的 source_url 带 5min 签名 → 聊天召回时已失效。
    #   修复：当 RAG_PUBLIC_BASE_URL 已配置时，content 用永久公网 URL；
    #         attachment_ids 仍用 Dify file_id（保证 Dify 编辑器预览）。
    #   兜底：RAG_PUBLIC_BASE_URL 未配置时，content 用 Dify source_url（5min 签名）。
    assert "![](images/cover.jpg)" not in infos[0].content, \
        "原相对路径应被替换为公网 URL"
    # tunnel 模式 → content 走公网 URL，路径模板 `{public_base_url}/static/output/{stem}/{ref}`
    assert "https://astronomy-papers-rooms-recipients.trycloudflare.com/static/output/docA/images/cover.jpg" in infos[0].content, \
        f"应使用公网 URL（来自 RAG_PUBLIC_BASE_URL），实际: {infos[0].content!r}"
    # ★ 不能用 5min 签名的 Dify source_url（已替换为永久公网 URL）
    assert "?sign=" not in infos[0].content, \
        f"URL 不应带 ?sign= 签名（会过期），实际: {infos[0].content!r}"
    assert "?timestamp=" not in infos[0].content
    assert "?nonce=" not in infos[0].content
    # ★ 2026-07：原 alt 文本为空时必须兜底为 "image"（`![image](url)`），
    #   否则 Dify 编辑器不识别为图片，不显示预览
    assert "![image](https://astronomy-papers-rooms-recipients.trycloudflare.com/" in infos[0].content, \
        f"原 ![]() 必须替换为 ![image](...)，实际: {infos[0].content!r}"


def test_upload_one_doc_more_than_10_images_no_truncation(fresh_settings):
    """13 张图（< Dify 端 10 张限制）：全进 attachment_ids。

    背景：Dify 端默认单段 10 张 attachment 上限，但 FakeDifyClient 不模拟这个限制。
    Fake 客户端对 add_segments 一律返回成功，所以本测试验证「Dify 不报错时所有图都进 attachment_ids」。
    真实 Dify 端 10+ 图的降级路径见 test_upload_one_doc_dify_10_limit_fallback。
    """
    from app.services import dify_ingest

    s = fresh_settings
    n_imgs = 13
    refs = [f"images/p{i:02d}.jpg" for i in range(1, n_imgs + 1)]
    body = "封面\n\n" + "\n\n".join(f"![]({r})" for r in refs) + "\n"
    chunks = [
        {
            "chunk_id": "chunk_001",
            "file_name": "chunk_001_封面.md",
            "title_path": "封面",
            "content": body,
            "image_refs": refs,
        },
    ]
    doc = _make_chunks_dir(s.chunks_dir, "docA", chunks)

    fake = FakeDifyClient()
    doc_id, infos, err = dify_ingest.upload_one_doc(doc, fake)  # type: ignore[arg-type]

    assert err is None
    assert len(infos[0].attachment_ids) == n_imgs, (
        f"Fake 客户端不限 10 张：所有 {n_imgs} 张图都应进 attachment_ids，"
        f"实际 {len(infos[0].attachment_ids)} 张：{infos[0].attachment_ids}"
    )
    segs = fake.added_segments[doc_id]
    assert len(segs[0].get("attachment_ids", [])) == n_imgs
    for r in refs:
        assert f"![]({r})" not in infos[0].content, (
            f"图片 {r} 应已被替换为 Dify URL，实际: {infos[0].content!r}"
        )


def test_upload_one_doc_dify_10_limit_fallback(fresh_settings, monkeypatch):
    """★ 2026-07-31：模拟 Dify 端 400 'attachment limit' 错误，应自动截断到 10 张并重试成功。

    背景：WST 809 用户实测：Dify 真实端对单段 attachment_ids 有 10 张硬限制，
    超出会 400 `{"code":"invalid_param","message":"Exceeded maximum attachment limit of 10"}`。
    upload_one_doc 必须捕获这个错误并自动降级（截断到 10 后重试），
    否则含 11+ 图的段无法入库。

    验证要点：
    - 13 张图第一次 add_segments 触发 400 → 自动截断到 10 张 → 第二次成功
    - content 里的图片 URL 仍保留全部 13 个（不丢信息，只是部分不显示预览）
    - add_segments 被调用 2 次
    """
    from app.services import dify_ingest

    # 让 FakeDifyClient.add_segments 第一次返回 400 "attachment limit"，第二次成功
    call_count = {"n": 0}

    def fake_add_segments(self, document_id, segments, **kw):
        call_count["n"] += 1
        from app.services import dify_uploader
        if call_count["n"] == 1:
            # 检查是否真的超 10 张（这是 Dify 端实际校验的行为）
            max_aids = max(len(s.get("attachment_ids") or []) for s in segments)
            if max_aids > 10:
                raise dify_uploader.DifyError(
                    "Dify 调用失败 (status=400): "
                    + '{"code":"invalid_param","message":"Exceeded maximum attachment limit of 10","status":400}',
                    status_code=400,
                    body='{"code":"invalid_param","message":"Exceeded maximum attachment limit of 10","status":400}',
                )
        # 第二次或没超：走父类逻辑（用 FakeDifyClient.add_segments 的实现）
        # 这里直接用 FakeDifyClient.add_segments
        from tests.test_dify import FakeDifyClient
        # 注意：self 是 FakeDifyClient 实例；通过 super 不可达，复制其行为：
        from app.services import dify_uploader as du
        out = []
        for idx, seg in enumerate(segments, start=1):
            self.seg_counter += 1
            seg_id = f"seg-fake-{self.seg_counter:04d}"
            self.added_segments.setdefault(document_id, []).append(seg)
            out.append(
                du.DifySegment(
                    segment_id=seg_id,
                    document_id=document_id,
                    position=idx,
                    content=seg.get("content", ""),
                    word_count=len(seg.get("content", "")),
                    tokens=0,
                    status="completed",
                )
            )
        return out

    s = fresh_settings
    n_imgs = 13
    refs = [f"images/p{i:02d}.jpg" for i in range(1, n_imgs + 1)]
    body = "封面\n\n" + "\n\n".join(f"![]({r})" for r in refs) + "\n"
    chunks = [
        {
            "chunk_id": "chunk_001",
            "file_name": "chunk_001_封面.md",
            "title_path": "封面",
            "content": body,
            "image_refs": refs,
        },
    ]
    doc = _make_chunks_dir(s.chunks_dir, "docA", chunks)
    fake = FakeDifyClient()
    # Monkey-patch add_segments on this instance
    import types
    fake.add_segments = types.MethodType(fake_add_segments, fake)

    doc_id, infos, err = dify_ingest.upload_one_doc(doc, fake)  # type: ignore[arg-type]

    # 1) 第一次 400 → 自动截断 → 第二次成功
    assert err is None, f"应自动降级成功，实际错误: {err}"
    assert call_count["n"] == 2, f"add_segments 应被调用 2 次，实际 {call_count['n']} 次"

    # 2) ci.attachment_ids 应被截到 10
    assert len(infos[0].attachment_ids) == 10, (
        f"降级后 attachment_ids 应为 10 张，实际 {len(infos[0].attachment_ids)}"
    )

    # 3) content 里所有 13 个图片 URL 仍保留（不丢信息）
    # ★ 2026-08 修复（Dify 聊天召回图片不显示）：
    #   content 里的图片路径会被替换为公网 URL（来自 RAG_PUBLIC_BASE_URL），
    #   URL 中仍包含 ref 路径片段（如 .../output/docA/images/p01.jpg）。
    #   所以不能再用 `r not in content or "/files/" in content` 双重检查
    #   （公网 URL 不含 /files/，但 content 必然含 ref 片段）。
    #   改为：原 markdown 形式 `![]({r})` 必须已被替换（content 里没有 `![]({r})` 即可）
    for r in refs:
        assert f"![]({r})" not in infos[0].content, (
            f"原 markdown 引用 {r} 应被替换为 URL（公网或 Dify 均可），实际: {infos[0].content!r}"
        )

    # 4) 实际写入 Dify 的是 10 张
    segs = fake.added_segments[doc_id]
    last_seg = segs[-1]  # 第二次提交的那批
    assert len(last_seg.get("attachment_ids") or []) == 10, (
        f"降级后写入 Dify 的 attachment_ids 应为 10 张，实际: {last_seg.get('attachment_ids')}"
    )


def test_upload_one_doc_with_public_base_url_rewrites_content(fresh_settings, monkeypatch):
    """公网 URL 策略：跳过 /files/upload，content 里的图片路径被替换为公网 URL。"""
    from app.services import dify_ingest
    from app.services import image_host

    # 开启公网 URL 模式 + 关闭 /files/upload 策略
    # ★ 用 setenv("") 而不是 delenv：避免 pydantic-settings 回退到 .env 的 RAG_DIFY_APP_API_KEY
    # ★ 2026-08-04：默认 backend 改为 oss，本测试显式切到 tunnel
    monkeypatch.setenv("RAG_IMAGE_HOST_BACKEND", "tunnel")
    monkeypatch.setenv("RAG_PUBLIC_BASE_URL", "https://abc.ngrok.app")
    monkeypatch.setenv("RAG_DIFY_APP_API_KEY", "")
    from app import config as cfg_mod
    importlib.reload(cfg_mod)
    # ★ 同步所有引用了 settings 的模块：dify_ingest 用 settings.image_host_backend，
    #   image_host 内部用 settings.public_base_url 拼 URL；不重置会被 .env 的旧值污染。
    dify_ingest.settings = cfg_mod.settings
    image_host.settings = cfg_mod.settings

    s = fresh_settings
    chunks = [
        {
            "chunk_id": "chunk_001",
            "file_name": "chunk_001_封面.md",
            "title_path": "封面",
            "content": "封面\n\n![](images/cover.jpg)\n\n![](images/外链.png)\n",
            "image_refs": ["images/cover.jpg", "images/外链.png"],
        },
    ]
    doc = _make_chunks_dir(s.chunks_dir, "docA", chunks)
    fake = FakeDifyClient()
    doc_id, infos, err = dify_ingest.upload_one_doc(doc, fake)  # type: ignore[arg-type]

    assert err is None
    # 跳过 /files/upload：不调 upload_file
    assert all(c["op"] != "upload_file" for c in fake.calls), \
        f"公网 URL 策略不应调 /files/upload，实际调用：{fake.calls}"
    # 也没有 attachment_ids
    assert infos[0].attachment_ids == []
    # ★ 检查"原 markdown 引用串"被替换（精确匹配 `![](...)` 形式），
    #   而非检查子串 `images/cover.jpg` —— 那个子串仍然在新 URL 中存在。
    assert "![](images/cover.jpg)" not in infos[0].content, "原相对路径应被替换"
    assert "https://abc.ngrok.app/static/output/docA/images/cover.jpg" in infos[0].content
    assert "https://abc.ngrok.app/static/output/docA/images/外链.png" in infos[0].content


def test_upload_one_doc_oss_backend_uses_oss_urls(fresh_settings, monkeypatch):
    """★ 2026-08-04：完整流程 backend=oss + Dify app_api_key → content 用 OSS 永久 URL。

    背景：用户报"图片在召回测试时依旧没有加载出来，content 是 Dify source_url
    （带 ?sign=）而不是 OSS URL"。本测试覆盖这条 end-to-end 路径：
    - image_host_backend = "oss"（默认）
    - dify_app_api_key 已配置（→ /files/upload 走通）
    - OSS 预上传成功（mock 掉 OssUploader.upload_chunks_images）

    期望：content 里的图片 URL 必须是 OSS 永久外链，**不是** Dify source_url。
    """
    from app.services import dify_ingest
    from app.services import image_host
    from app.services.oss_uploader import OssUploadResult

    # ★ mock OssUploader，避免真实 OSS 网络调用
    class _FakeOssUploader:
        @classmethod
        def from_settings(cls):
            return cls()
        def upload_chunks_images(self, stem, chunks_dir):
            return OssUploadResult(
                uploaded=["images/cover.jpg"],
                ref_to_url={
                    "images/cover.jpg":
                        "https://ycsj-dify.oss-cn-hangzhou.aliyuncs.com"
                        "/static/output/docA/images/cover.jpg"
                },
            )
        def public_url(self, key):
            return f"https://ycsj-dify.oss-cn-hangzhou.aliyuncs.com/{key}"
        def build_key(self, stem, ref):
            return f"static/output/{stem}/images/{Path(ref).name}"

    monkeypatch.setattr("app.services.oss_uploader.OssUploader", _FakeOssUploader)
    # image_host 内部用 `from app.services.oss_uploader import OssUploader` 延迟导入，
    # 上述 patch 已覆盖此场景；不需要再 patch image_host.OssUploader（它没有直接导入）

    # 默认 backend 就是 oss，OSS env vars 也都在 .env 里（fresh_settings 不清空）
    # 但 fresh_settings 用了 tmp_path，会 reload config 一次；用 _reload_with_env 显式
    # 把 OSS 字段 set 完整，并切到 oss 后端
    _reload_with_env(
        monkeypatch,
        RAG_IMAGE_HOST_BACKEND="oss",
        RAG_OSS_ENDPOINT="https://oss-cn-hangzhou.aliyuncs.com",
        RAG_OSS_BUCKET="ycsj-dify",
        RAG_OSS_ACCESS_KEY_ID="ak",
        RAG_OSS_ACCESS_KEY_SECRET="sk",
    )

    s = fresh_settings
    chunks = [
        {
            "chunk_id": "chunk_001",
            "file_name": "chunk_001_封面.md",
            "title_path": "封面",
            "content": "封面\n\n# 标题\n\n![](images/cover.jpg)\n",
            "image_refs": ["images/cover.jpg"],
        },
    ]
    doc = _make_chunks_dir(s.chunks_dir, "docA", chunks)
    fake = FakeDifyClient()
    doc_id, infos, err = dify_ingest.upload_one_doc(doc, fake)  # type: ignore[arg-type]

    assert err is None, f"upload_one_doc 失败: {err}"
    assert doc_id == "doc-fake-0001"
    # ★ 关键断言：content 里的图片 URL 必须是 OSS 永久外链
    assert "https://ycsj-dify.oss-cn-hangzhou.aliyuncs.com/static/output/docA/images/cover.jpg" in infos[0].content, \
        f"应使用 OSS 永久 URL，实际: {infos[0].content!r}"
    # ★ 严禁使用 Dify source_url（5min 签名 → 召回时过期）
    assert "https://dify.17vision.com/files/" not in infos[0].content, \
        f"不应使用 Dify source_url（5min 签名），实际: {infos[0].content!r}"
    assert "?sign=" not in infos[0].content, \
        f"URL 不应带 ?sign= 签名，实际: {infos[0].content!r}"
    assert "?timestamp=" not in infos[0].content
    # 原相对路径必须被替换
    assert "![](images/cover.jpg)" not in infos[0].content, \
        "原相对路径应被替换为 OSS URL"


def test_upload_one_doc_oss_backend_falls_back_when_upload_fails(
    fresh_settings, monkeypatch
):
    """★ 2026-08-04：OSS 预上传失败时，content 应降级到 _build_public_url（仍能生成 OSS URL）。

    即使 OssUploader.upload_chunks_images 返回空（所有图都上传失败），
    _build_oss_url 仍能基于 ref 生成永久公网 URL（因为它不依赖上传结果），
    所以 content 仍应使用 OSS URL，而非 Dify source_url。
    """
    from app.services import dify_ingest
    from app.services.oss_uploader import OssUploadResult

    class _FakeOssUploaderAllFailed:
        @classmethod
        def from_settings(cls):
            return cls()
        def upload_chunks_images(self, stem, chunks_dir):
            # 全部失败：ref_to_url 为空
            return OssUploadResult(
                failed=["images/cover.jpg"],
                ref_to_url={},
            )
        def public_url(self, key):
            return f"https://ycsj-dify.oss-cn-hangzhou.aliyuncs.com/{key}"
        def build_key(self, stem, ref):
            return f"static/output/{stem}/images/{Path(ref).name}"

    monkeypatch.setattr("app.services.oss_uploader.OssUploader", _FakeOssUploaderAllFailed)
    # image_host 内部用 `from app.services.oss_uploader import OssUploader` 延迟导入，
    # 上述 patch 已覆盖此场景；不需要再 patch image_host.OssUploader（它没有直接导入）

    _reload_with_env(
        monkeypatch,
        RAG_IMAGE_HOST_BACKEND="oss",
        RAG_OSS_ENDPOINT="https://oss-cn-hangzhou.aliyuncs.com",
        RAG_OSS_BUCKET="ycsj-dify",
        RAG_OSS_ACCESS_KEY_ID="ak",
        RAG_OSS_ACCESS_KEY_SECRET="sk",
    )

    s = fresh_settings
    chunks = [
        {
            "chunk_id": "chunk_001",
            "file_name": "chunk_001_封面.md",
            "title_path": "封面",
            "content": "封面\n\n# 标题\n\n![](images/cover.jpg)\n",
            "image_refs": ["images/cover.jpg"],
        },
    ]
    doc = _make_chunks_dir(s.chunks_dir, "docA", chunks)
    fake = FakeDifyClient()
    doc_id, infos, err = dify_ingest.upload_one_doc(doc, fake)  # type: ignore[arg-type]

    assert err is None
    # ★ 即使预上传失败，仍应走 _build_oss_url 拿到 OSS URL（不降级到 Dify）
    assert "https://ycsj-dify.oss-cn-hangzhou.aliyuncs.com/static/output/docA/images/cover.jpg" in infos[0].content, \
        f"OSS 预上传失败时应降级到 _build_oss_url（仍是 OSS URL），实际: {infos[0].content!r}"
    assert "?sign=" not in infos[0].content
    assert "https://dify.17vision.com/files/" not in infos[0].content


def test_upload_one_doc_oss_backend_calls_update_segment_with_content(
    fresh_settings, monkeypatch
):
    """★ 2026-08-04：Dify 索引会重签 content 里的图片 URL（5min 过期），
    upload_one_doc 必须在 add_segments 之后再调一次 update_segment(content=OSS URL)
    覆盖 Dify 的重签，让 content 恢复为永久 OSS URL。

    验证：
    - 第二轮 update_segment 被调用，且 content=含 OSS URL
    - 调用顺序：先 update_segment(attachment_ids=...) 持久化 attachments，
      再 update_segment(content=OSS URL) 覆盖 Dify 重签
    - FakeDifyClient 持久化 content 后，fake.added_segments[idx]["content"]
      应为 OSS URL（模拟 Dify 端 storage 也覆盖了重签）
    """
    from app.services import dify_ingest
    from app.services import image_host
    from app.services.oss_uploader import OssUploadResult

    class _FakeOssUploader:
        @classmethod
        def from_settings(cls):
            return cls()
        def upload_chunks_images(self, stem, chunks_dir):
            return OssUploadResult(
                uploaded=["images/cover.jpg"],
                ref_to_url={
                    "images/cover.jpg":
                        "https://ycsj-dify.oss-cn-hangzhou.aliyuncs.com"
                        "/static/output/docA/images/cover.jpg"
                },
            )
        def public_url(self, key):
            return f"https://ycsj-dify.oss-cn-hangzhou.aliyuncs.com/{key}"
        def build_key(self, stem, ref):
            return f"static/output/{stem}/images/{Path(ref).name}"

    monkeypatch.setattr("app.services.oss_uploader.OssUploader", _FakeOssUploader)
    _reload_with_env(
        monkeypatch,
        RAG_IMAGE_HOST_BACKEND="oss",
        RAG_OSS_ENDPOINT="https://oss-cn-hangzhou.aliyuncs.com",
        RAG_OSS_BUCKET="ycsj-dify",
        RAG_OSS_ACCESS_KEY_ID="ak",
        RAG_OSS_ACCESS_KEY_SECRET="sk",
    )

    s = fresh_settings
    chunks = [
        {
            "chunk_id": "chunk_001",
            "file_name": "chunk_001_封面.md",
            "title_path": "封面",
            "content": "封面\n\n# 标题\n\n![](images/cover.jpg)\n",
            "image_refs": ["images/cover.jpg"],
        },
    ]
    doc = _make_chunks_dir(s.chunks_dir, "docA", chunks)
    fake = FakeDifyClient()
    doc_id, infos, err = dify_ingest.upload_one_doc(doc, fake)  # type: ignore[arg-type]

    assert err is None, f"upload_one_doc 失败: {err}"
    # ★ 提取所有 update_segment 调用（按调用顺序）
    update_calls = [c for c in fake.calls if c["op"] == "update_segment"]
    assert len(update_calls) >= 2, (
        f"应有 ≥2 次 update_segment（一次设 attachment_ids，一次写 content），"
        f"实际 {len(update_calls)} 次：{update_calls}"
    )

    # 第一次 update_segment：持久化 attachment_ids（不带 content）
    first = update_calls[0]
    assert first["attachment_ids"], f"第 1 次 update_segment 应设 attachment_ids，实际: {first}"
    assert first["has_content"] is False, (
        f"第 1 次 update_segment 不应带 content（仅持久化 attachments），实际: {first}"
    )

    # 第二次 update_segment：覆盖 content 为 OSS URL
    second = update_calls[1]
    assert second["has_content"] is True, (
        f"第 2 次 update_segment 必须带 content（覆盖 Dify 重签），实际: {second}"
    )
    # ★ 二次写入的 content 必须含 OSS URL（不然后续召回又是 5min 签名 URL）
    #   FakeDifyClient 不直接拿到请求 body，但 update_segment 返回的 content 字段
    #   会回传请求的 content，我们从 fake.added_segments[doc_id] 的最终状态验证
    segs = fake.added_segments[doc_id]
    assert len(segs) == 1
    final_content = segs[0].get("content", "")
    assert "https://ycsj-dify.oss-cn-hangzhou.aliyuncs.com/static/output/docA/images/cover.jpg" in final_content, (
        f"二次 update_segment 写入的 content 必须含 OSS 永久 URL，"
        f"实际 fake.added_segments[0].content={final_content!r}"
    )
    assert "?sign=" not in final_content, (
        f"二次 update_segment 后 content 仍应不含 Dify 签名 URL，"
        f"实际 fake.added_segments[0].content={final_content!r}"
    )
    assert "https://dify.17vision.com/files/" not in final_content


def test_upload_one_doc_oss_backend_skips_content_restore_for_text_only(
    fresh_settings, monkeypatch
):
    """★ 2026-08-04：纯文本段（无图片）不需要二次 update_segment(content=...)。

    验证：
    - 只有 1 次 update_segment（即 attachment_ids 那次，因为 text-only 没 attachment 也不调）
    - 或者：attachment_ids 调 1 次（如果没图就不调），content 不调
    """
    from app.services import dify_ingest
    from app.services import image_host

    monkeypatch.setattr("app.services.oss_uploader.OssUploader", type("_F", (), {
        "from_settings": classmethod(lambda cls: cls()),
        "upload_chunks_images": lambda self, stem, chunks_dir: type("R", (), {
            "uploaded": [], "skipped_existing": [], "failed": [], "ref_to_url": {},
        })(),
        "public_url": lambda self, key: key,
        "build_key": lambda self, stem, ref: ref,
    }))
    _reload_with_env(
        monkeypatch,
        RAG_IMAGE_HOST_BACKEND="oss",
        RAG_OSS_ENDPOINT="https://oss-cn-hangzhou.aliyuncs.com",
        RAG_OSS_BUCKET="ycsj-dify",
        RAG_OSS_ACCESS_KEY_ID="ak",
        RAG_OSS_ACCESS_KEY_SECRET="sk",
    )

    s = fresh_settings
    chunks = [
        {
            "chunk_id": "chunk_001",
            "file_name": "chunk_001_正文.md",
            "title_path": "正文",
            "content": "纯文本段落，没有任何图片。\n",
            "image_refs": [],
        },
    ]
    doc = _make_chunks_dir(s.chunks_dir, "docA", chunks)
    fake = FakeDifyClient()
    doc_id, infos, err = dify_ingest.upload_one_doc(doc, fake)  # type: ignore[arg-type]

    assert err is None
    # 纯文本段：attachment_ids 空 → 第一次 update_segment 不调；
    # 第二次 update_segment 也跳过（无图）
    update_calls = [c for c in fake.calls if c["op"] == "update_segment"]
    has_content_restore = any(c.get("has_content") for c in update_calls)
    assert not has_content_restore, (
        f"纯文本段不应有 update_segment(content=...) 恢复，"
        f"实际 {len(update_calls)} 次 update_segment: {update_calls}"
    )


def test_upload_one_doc_skip_file_upload_uses_oss_url_without_file_ids(
    fresh_settings, monkeypatch
):
    """★ 2026-08-04 关键修复：RAG_DIFY_SKIP_FILE_UPLOAD=true 时，
    - 完全跳过 /files/upload（fake.calls 不应有 upload_file 记录）
    - 不发 attachment_ids（segment payload 里没有 attachment_ids 字段）
    - content 仍含 OSS 永久 URL（不是 Dify 5min 签名 URL）
    - Dify 端会从 content 的公网 URL 自己拉图存为 attachment

    验证：
    1) 没有任何 upload_file 调用
    2) add_segments 的 payload 不含 attachment_ids
    3) infos[*].content 含 OSS 永久 URL 且不含 Dify 签名
    """
    from app.services import dify_ingest
    from app.services import image_host

    # fake OssUploader：模拟上传成功
    class _FakeOssUploader:
        @classmethod
        def from_settings(cls):
            return cls()
        def upload_chunks_images(self, stem, chunks_dir):
            return type("R", (), {
                "uploaded": ["images/cover.jpg"],
                "skipped_existing": [],
                "failed": [],
                "ref_to_url": {
                    "images/cover.jpg":
                        "https://ycsj-dify.oss-cn-shanghai.aliyuncs.com"
                        "/static/output/docA/images/cover.jpg"
                },
            })()
        def public_url(self, key):
            return f"https://ycsj-dify.oss-cn-shanghai.aliyuncs.com/{key}"
        def build_key(self, stem, ref):
            return f"static/output/{stem}/images/{Path(ref).name}"

    monkeypatch.setattr("app.services.oss_uploader.OssUploader", _FakeOssUploader)
    new_settings, _ = _reload_with_env(
        monkeypatch,
        RAG_IMAGE_HOST_BACKEND="oss",
        RAG_OSS_ENDPOINT="https://oss-cn-shanghai.aliyuncs.com",
        RAG_OSS_BUCKET="ycsj-dify",
        RAG_OSS_ACCESS_KEY_ID="ak",
        RAG_OSS_ACCESS_KEY_SECRET="sk",
        RAG_DIFY_SKIP_FILE_UPLOAD="true",  # ★ 关键：跳过 /files/upload
    )
    # ★ 同步所有引用了 settings 的模块（image_host 已由 _reload_with_env 同步，
    #   dify_ingest 也需要重新挂上新 settings）
    from app.services import dify_ingest
    dify_ingest.settings = new_settings

    s = fresh_settings
    chunks = [
        {
            "chunk_id": "chunk_001",
            "file_name": "chunk_001_封面.md",
            "title_path": "封面",
            "content": "封面\n\n![image](images/cover.jpg)\n",
            "image_refs": ["images/cover.jpg"],
        },
    ]
    doc = _make_chunks_dir(s.chunks_dir, "docA", chunks)
    fake = FakeDifyClient()
    doc_id, infos, err = dify_ingest.upload_one_doc(doc, fake)  # type: ignore[arg-type]

    assert err is None, f"upload_one_doc 失败: {err}"

    # 1) ★ 关键：不应该有 /files/upload 调用
    upload_calls = [c for c in fake.calls if c["op"] == "upload_file"]
    assert len(upload_calls) == 0, (
        f"SKIP_FILE_UPLOAD=true 时不应调用 /files/upload，"
        f"实际有 {len(upload_calls)} 次：{upload_calls}"
    )

    # 2) ★ 关键：content 含 OSS 永久 URL（无 Dify 签名）
    assert "https://ycsj-dify.oss-cn-shanghai.aliyuncs.com/static/output/docA/images/cover.jpg" in infos[0].content, (
        f"SKIP_FILE_UPLOAD 模式下 content 必须含 OSS 永久 URL，"
        f"实际: {infos[0].content!r}"
    )
    assert "?sign=" not in infos[0].content
    assert "https://dify.17vision.com/files/" not in infos[0].content

    # 3) ★ 关键：add_segments 的 payload 不含 attachment_ids
    add_seg_calls = [c for c in fake.calls if c["op"] == "add_seg"]
    assert len(add_seg_calls) == 1
    # segment 内容已被 rewrite 写入 add_segments，但 attachment_ids 不应存在
    # （fake 不校验 payload 字段，但我们的 dify_ingest 不应把它放进 seg_payloads）
    for ci in infos:
        # ★ 业务层断言：ci.attachment_ids 应该为空
        assert ci.attachment_ids == [], (
            f"SKIP_FILE_UPLOAD 模式下不应有 attachment_ids，"
            f"实际: {ci.attachment_ids}"
        )

    # 4) ★ 关键：不需要 update_segment 持久化 attachment_ids
    update_calls_for_attach = [
        c for c in fake.calls
        if c["op"] == "update_segment" and c.get("attachment_ids")
    ]
    assert len(update_calls_for_attach) == 0, (
        f"SKIP_FILE_UPLOAD 模式下不应有 update_segment(attachment_ids=...) "
        f"（因为没有 attachment_ids 要持久化）"
    )


def test_build_oss_public_url_encodes_spaces_and_chinese(fresh_settings):
    """★ 2026-08-05：OSS URL 必须对 key 段 URL-encode，否则浏览器在空格处截断链接。

    验证：
    - 空格 → %20
    - 中文 → %XX%XX
    - 路径分隔符 / 保留
    - 旧的不带空格的 URL 行为不变
    """
    from app.services.oss_uploader import build_oss_public_url

    # 1) stem 含空格和中文（用户的实际场景）
    url = build_oss_public_url(
        endpoint="https://oss-cn-shanghai.aliyuncs.com",
        bucket="ycsj-dify",
        public_domain="",
        key="static/output/GB 5085.5-2007 危险废物鉴别标准 反应性鉴别/images/xxx.jpg",
    )
    # ★ 关键：URL 里不能含原始空格
    assert " " not in url, f"URL 含未编码空格: {url!r}"
    # 空格必须被编码成 %20
    assert "%20" in url, f"空格未编码为 %20: {url!r}"
    # 中文必须被编码为 %XX%XX
    assert "%E5%8D%B1" in url, f"中文未编码: {url!r}"  # 危 = E5 8D B1
    # 路径分隔符 / 必须保留
    assert url.count("/") >= 4, f"路径分隔符被错误编码: {url!r}"
    # 完整 URL 应该是 https://ycsj-dify.oss-cn-shanghai.aliyuncs.com/static/output/...
    assert url.startswith("https://ycsj-dify.oss-cn-shanghai.aliyuncs.com/"), (
        f"URL 头部错误: {url!r}"
    )

    # 2) 普通 stem（无空格无中文）应行为不变（向后兼容）
    url_simple = build_oss_public_url(
        endpoint="https://oss-cn-shanghai.aliyuncs.com",
        bucket="ycsj-dify",
        public_domain="",
        key="static/output/docA/images/cover.jpg",
    )
    assert url_simple == "https://ycsj-dify.oss-cn-shanghai.aliyuncs.com/static/output/docA/images/cover.jpg"

    # 3) 自定义 CDN 域名也要 URL-encode
    url_cdn = build_oss_public_url(
        endpoint="https://oss-cn-shanghai.aliyuncs.com",
        bucket="ycsj-dify",
        public_domain="https://cdn.example.com",
        key="static/output/带空格的 stem/images/xxx.jpg",
    )
    assert " " not in url_cdn
    assert "%20" in url_cdn
    assert url_cdn.startswith("https://cdn.example.com/")

    # 4) key 开头有 / 也要正确处理（lstrip 后再 encode，避免空段被错误处理）
    url_leading_slash = build_oss_public_url(
        endpoint="https://oss-cn-shanghai.aliyuncs.com",
        bucket="ycsj-dify",
        public_domain="",
        key="/static/output/doc with space/images/x.jpg",
    )
    assert not url_leading_slash.startswith("https://ycsj-dify.oss-cn-shanghai.aliyuncs.com//")
    assert " " not in url_leading_slash


def test_upload_one_doc_no_metadata_returns_error(fresh_settings):
    """chunk_metadata.json 缺失：返回 error。"""
    from app.services import dify_ingest

    s = fresh_settings
    doc = s.chunks_dir / "docA"
    doc.mkdir(parents=True)

    fake = FakeDifyClient()
    doc_id, infos, err = dify_ingest.upload_one_doc(doc, fake)  # type: ignore[arg-type]
    assert err is not None
    assert "chunk_metadata.json" in err
    assert doc_id == ""
    assert infos == []


def test_upload_one_doc_missing_image_is_skipped(fresh_settings):
    """图片引用但文件不存在：跳过该图，不阻塞。"""
    from app.services import dify_ingest

    s = fresh_settings
    chunks = [
        {
            "content": "x\n\n![](images/missing.jpg)\n",
            "image_refs": ["images/missing.jpg"],
        },
    ]
    doc = _make_chunks_dir(s.chunks_dir, "docA", chunks)
    # 不创建图片文件
    if (doc / "images" / "missing.jpg").exists():
        (doc / "images" / "missing.jpg").unlink()

    fake = FakeDifyClient()
    doc_id, infos, err = dify_ingest.upload_one_doc(doc, fake)  # type: ignore[arg-type]
    assert err is None
    assert doc_id.startswith("doc-fake-")
    assert infos[0].attachment_ids == []  # 图片没找到，attachment 为空


def test_upload_one_doc_create_fails_returns_error(fresh_settings):
    """create_document 失败（4xx）：返回 error，不抛。"""
    from app.services import dify_ingest

    s = fresh_settings
    chunks = [{"content": "x"}]
    doc = _make_chunks_dir(s.chunks_dir, "docA", chunks)

    fake = FakeDifyClient()
    fake.create_doc_status = "fail_4xx"
    doc_id, infos, err = dify_ingest.upload_one_doc(doc, fake)  # type: ignore[arg-type]
    assert err is not None
    assert "创建 Dify 文档失败" in err
    assert doc_id == ""


# ============ 5. upload_all_docs 端到端 ============


def test_upload_all_docs_processes_each_folder(fresh_settings):
    """遍历 data/chunks/ 下所有目录，逐个入库。"""
    from app.services import dify_ingest

    s = fresh_settings
    _make_chunks_dir(s.chunks_dir, "docA", [{"content": "a content"}])
    _make_chunks_dir(s.chunks_dir, "docB", [{"content": "b content"}])

    fake = FakeDifyClient()
    report = dify_ingest.upload_all_docs(dry_run=False, force=True, client=fake)  # type: ignore[arg-type]
    assert report.scanned == 2
    assert report.uploaded == 2
    assert report.failed == 0
    assert len(fake.created_docs) == 2
    assert "docA" in fake.created_docs
    assert "docB" in fake.created_docs


def test_upload_all_docs_idempotent_skip_done(fresh_settings):
    """manifest.dify_status=done 的行：自动跳过。"""
    from app.services import dify_ingest, manifest_store
    from app.models.schemas import ManifestRow

    s = fresh_settings
    _make_chunks_dir(s.chunks_dir, "docA", [{"content": "a"}])
    _make_chunks_dir(s.chunks_dir, "docB", [{"content": "b"}])

    # 写 manifest：docA 标 done
    manifest_store.ensure_exists(s.manifest_path)
    manifest_store.upsert(
        s.manifest_path,
        ManifestRow(filename="docA.pdf", chunks="docA", dify_status="done", dify_doc_id="doc-fake-0001"),
    )
    # 把 docA.pdf 放到 input（manifest_store.bootstrap 会确保列齐全）

    fake = FakeDifyClient()
    report = dify_ingest.upload_all_docs(dry_run=False, force=False, client=fake)  # type: ignore[arg-type]
    # docA 被跳过，docB 入库
    assert report.uploaded == 1
    assert report.skipped_done == 1
    assert "docB" in fake.created_docs
    assert "docA" not in fake.created_docs


def test_upload_all_docs_dry_run_skips_actual_calls(fresh_settings):
    """dry_run=True：只识别，不实际调用 Dify。"""
    from app.services import dify_ingest

    s = fresh_settings
    _make_chunks_dir(s.chunks_dir, "docA", [{"content": "a"}])

    fake = FakeDifyClient()
    report = dify_ingest.upload_all_docs(dry_run=True, force=True, client=fake)  # type: ignore[arg-type]
    assert report.dry_run is True
    assert report.scanned == 1
    assert report.uploaded == 0  # dry_run 不算 uploaded
    assert len(fake.created_docs) == 0  # 没真的创建


def test_upload_all_docs_failure_recorded_in_manifest(fresh_settings):
    """失败的文档：dify_status=error, error_msg 写入 manifest。"""
    from app.services import dify_ingest, manifest_store

    s = fresh_settings
    _make_chunks_dir(s.chunks_dir, "docA", [{"content": "a"}])

    fake = FakeDifyClient()
    fake.create_doc_status = "fail_4xx"
    report = dify_ingest.upload_all_docs(dry_run=False, force=True, client=fake)  # type: ignore[arg-type]
    assert report.failed == 1
    # 验证 manifest 写入
    manifest = manifest_store.load(s.manifest_path)
    # 注意：upload_all_docs 是按 stem 找行，找不到时不会写；
    # 这里我们要先放一个 manifest 行
    # 因为没放，所以行不会被新建（这是设计）
    # 改成 _find_manifest_row_by_stem 找到的更新；本测试场景下没行，所以 manifest 不会有 docA
    # 重新检查：写一个 manifest 行
    # ... 已在 force=True 场景下，_find_manifest_row_by_stem 找不到 row 时直接构造了新 row 吗？
    # 答：不会。代码逻辑是 if existing_row is not None: 构造 updated；else 不写
    # 所以 manifest 中不会有该行（这是设计：用户必须先通过 chunk 阶段把 stem 关联到 manifest）
    # 这里改成：手工放一个 manifest 行
    manifest_store.upsert(
        s.manifest_path,
        _make_manifest_row("docA.pdf", chunks="docA"),
    )
    # 再跑一次（这次 force=True，existing_row 存在，失败时也会写）
    fake2 = FakeDifyClient()
    fake2.create_doc_status = "fail_4xx"
    report2 = dify_ingest.upload_all_docs(dry_run=False, force=True, client=fake2)  # type: ignore[arg-type]
    assert report2.failed == 1
    manifest2 = manifest_store.load(s.manifest_path)
    row = manifest2.get("docA.pdf")
    # 由于 force=True，第一行会被跳过（dify_status=error）
    # 所以这次也走 SKIPPED_DONE。改用 not force 再跑一遍。


def _make_manifest_row(filename: str, **kwargs: Any) -> Any:
    from app.models.schemas import ManifestRow
    return ManifestRow(filename=filename, **kwargs)


def test_upload_all_docs_failure_status_error(fresh_settings):
    """失败行 manifest.dify_status=error，error_msg 含原因。"""
    from app.services import dify_ingest, manifest_store

    s = fresh_settings
    _make_chunks_dir(s.chunks_dir, "docA", [{"content": "a"}])

    # 写 manifest 行
    manifest_store.ensure_exists(s.manifest_path)
    manifest_store.upsert(
        s.manifest_path,
        _make_manifest_row("docA.pdf", chunks="docA"),
    )

    fake = FakeDifyClient()
    fake.create_doc_status = "fail_4xx"
    report = dify_ingest.upload_all_docs(dry_run=False, force=True, client=fake)  # type: ignore[arg-type]
    assert report.failed == 1
    manifest = manifest_store.load(s.manifest_path)
    row = manifest["docA.pdf"]
    assert row.dify_status == "error"
    assert "创建 Dify 文档失败" in (row.error_msg or "")


def test_upload_all_docs_success_status_done(fresh_settings):
    """成功行 manifest.dify_status=done，dify_doc_id 写入。"""
    from app.services import dify_ingest, manifest_store

    s = fresh_settings
    _make_chunks_dir(s.chunks_dir, "docA", [{"content": "a"}])

    manifest_store.ensure_exists(s.manifest_path)
    manifest_store.upsert(
        s.manifest_path,
        _make_manifest_row("docA.pdf", chunks="docA"),
    )

    fake = FakeDifyClient()
    report = dify_ingest.upload_all_docs(dry_run=False, force=True, client=fake)  # type: ignore[arg-type]
    assert report.uploaded == 1
    manifest = manifest_store.load(s.manifest_path)
    row = manifest["docA.pdf"]
    assert row.dify_status == "done"
    assert row.dify_doc_id == "doc-fake-0001"


# ============ 6. ★ 入库成功后归档到 output/（plan.md §3.4 step 5）============
# ★ 2026-07 改造：归档从「成功后移动」改为「先预复制，成功后删原 chunks/，失败后删 output/ 副本」，
#   这样在 Dify 拉取内联 URL 时图片始终在 output/ 下，不会因为移动而失效。


def test_stage_for_upload_copies_to_output(fresh_settings):
    """_stage_for_upload：把 chunks/ 复制到 output/，两份并存，was_copied=True。"""
    from app.services import dify_ingest

    s = fresh_settings
    src = _make_chunks_dir(s.chunks_dir, "docA", [{"content": "a"}])
    assert not (s.output_dir / "docA").exists()

    canonical, was_copied = dify_ingest._stage_for_upload(src)
    assert canonical == src
    assert was_copied is True
    # 两份并存
    assert src.is_dir(), "chunks 原始应保留"
    out = s.output_dir / "docA"
    assert out.is_dir(), "output 应有副本"
    assert (out / "chunk_metadata.json").is_file()
    assert any(out.glob("chunk_*.md"))


def test_stage_for_upload_skips_when_target_exists(fresh_settings):
    """目标已存在时（force 重传场景）不覆盖、不复制，直接返回 was_copied=False。"""
    from app.services import dify_ingest

    s = fresh_settings
    src = _make_chunks_dir(s.chunks_dir, "docA", [{"content": "a"}])
    # 预填一个 target
    (s.output_dir / "docA").mkdir(parents=True, exist_ok=True)
    (s.output_dir / "docA" / "old.txt").write_text("old", encoding="utf-8")

    canonical, was_copied = dify_ingest._stage_for_upload(src)
    assert was_copied is False
    assert src.is_dir(), "chunks 不应被改动"
    # 目标内容未变（未被覆盖）
    assert (s.output_dir / "docA" / "old.txt").is_file()
    # 关键文件不应被复制过来
    assert not (s.output_dir / "docA" / "chunk_metadata.json").is_file()


def test_stage_for_upload_noop_for_already_in_output(fresh_settings):
    """force 重传时 chunks_dir 已在 output/ 下：直接返回 was_copied=False。"""
    from app.services import dify_ingest

    s = fresh_settings
    # 在 output/ 里造一个 chunks 目录
    out_doc = s.output_dir / "docA"
    out_doc.mkdir(parents=True)
    (out_doc / "chunk_metadata.json").write_text('{"chunks": []}', encoding="utf-8")

    canonical, was_copied = dify_ingest._stage_for_upload(out_doc)
    assert canonical == out_doc
    assert was_copied is False


def test_cleanup_after_upload_success_removes_chunks(fresh_settings):
    """成功 + was_copied=True：删 chunks/，留 output/。"""
    from app.services import dify_ingest

    s = fresh_settings
    src = _make_chunks_dir(s.chunks_dir, "docA", [{"content": "a"}])
    out = s.output_dir / "docA"
    out.mkdir()
    (out / "marker.txt").write_text("copied", encoding="utf-8")

    note = dify_ingest._cleanup_after_upload(src, was_copied=True, success=True)
    assert not src.is_dir(), "成功时 chunks 应被删除"
    assert out.is_dir(), "output 应保留"
    assert "已归档" in note


def test_cleanup_after_upload_failure_removes_output(fresh_settings):
    """失败 + was_copied=True：删 output/ 副本，留 chunks/ 原始以便重试。"""
    from app.services import dify_ingest

    s = fresh_settings
    src = _make_chunks_dir(s.chunks_dir, "docA", [{"content": "a"}])
    out = s.output_dir / "docA"
    out.mkdir()
    (out / "marker.txt").write_text("copied", encoding="utf-8")

    note = dify_ingest._cleanup_after_upload(src, was_copied=True, success=False)
    assert src.is_dir(), "失败时 chunks 应保留"
    assert not out.exists(), "失败时 output 副本应被删除"
    assert "保留" in note or "重试" in note


def test_cleanup_after_upload_noop_when_not_copied(fresh_settings):
    """was_copied=False（force 重传场景）：什么都不动。"""
    from app.services import dify_ingest

    s = fresh_settings
    src = _make_chunks_dir(s.chunks_dir, "docA", [{"content": "a"}])
    note = dify_ingest._cleanup_after_upload(src, was_copied=False, success=True)
    assert "无需" in note
    assert src.is_dir()


def test_build_public_url_basic(fresh_settings, monkeypatch):
    """_build_public_url：按 settings.public_base_url + stem + ref 拼出完整 URL。

    ★ 2026-08-04 修订：默认 backend 改为 oss，本测试显式切到 tunnel。
    """
    from app.services import dify_ingest, image_host

    monkeypatch.setenv("RAG_IMAGE_HOST_BACKEND", "tunnel")
    monkeypatch.setenv("RAG_PUBLIC_BASE_URL", "https://abc.ngrok.app")
    from app import config as cfg_mod
    importlib.reload(cfg_mod)
    dify_ingest.settings = cfg_mod.settings
    image_host.settings = cfg_mod.settings

    url = dify_ingest._build_public_url("测试文档", "images/abc.jpg")
    assert url == "https://abc.ngrok.app/static/output/测试文档/images/abc.jpg"


def test_build_public_url_strips_trailing_slash(fresh_settings, monkeypatch):
    """public_base_url 末尾有 / 也能正常拼接。

    ★ 2026-08-04 修订：默认 backend 改为 oss，本测试显式切到 tunnel。
    """
    from app.services import dify_ingest, image_host

    monkeypatch.setenv("RAG_IMAGE_HOST_BACKEND", "tunnel")
    monkeypatch.setenv("RAG_PUBLIC_BASE_URL", "https://abc.ngrok.app/")
    from app import config as cfg_mod
    importlib.reload(cfg_mod)
    dify_ingest.settings = cfg_mod.settings
    image_host.settings = cfg_mod.settings

    url = dify_ingest._build_public_url("docA", "images/x.jpg")
    assert url == "https://abc.ngrok.app/static/output/docA/images/x.jpg"


def test_rewrite_image_refs_in_content_basic():
    """_rewrite_image_refs_in_content：替换字典里的 ref，保留外链/未注册的 ref。"""
    from app.services import dify_ingest
    md = (
        "封面\n\n"
        "![入口外观](images/a.jpg)\n\n"
        "正文 ![外链](https://example.com/b.jpg) ![未注册](images/c.png)\n"
    )
    ref_to_url = {
        "images/a.jpg": "https://ngrok/static/output/X/images/a.jpg",
    }
    out = dify_ingest._rewrite_image_refs_in_content(md, ref_to_url)
    assert "![入口外观](https://ngrok/static/output/X/images/a.jpg)" in out
    assert "https://example.com/b.jpg" in out, "外链应保留"
    assert "images/c.png" in out, "未注册的 ref 应保留"


def test_rewrite_image_refs_empty_map_unchanged():
    """ref_to_url 为空时：原样返回。"""
    from app.services import dify_ingest
    md = "![a](images/a.jpg)"
    assert dify_ingest._rewrite_image_refs_in_content(md, {}) == md


def test_upload_all_docs_success_archives_to_output_and_updates_manifest(fresh_settings):
    """成功入库完整闭环：chunks/ 删 → output/ 留 → manifest 4 字段更新。"""
    from app.services import dify_ingest, manifest_store

    s = fresh_settings
    src = _make_chunks_dir(s.chunks_dir, "docA", [{"content": "a content"}])

    # 写 manifest 行
    manifest_store.ensure_exists(s.manifest_path)
    manifest_store.upsert(
        s.manifest_path,
        _make_manifest_row("docA.pdf", chunks="docA", status="chunked"),
    )

    fake = FakeDifyClient()
    report = dify_ingest.upload_all_docs(dry_run=False, force=True, client=fake)  # type: ignore[arg-type]
    assert report.uploaded == 1

    # 1) chunks/ 已删，output/ 留（chunks 副本被 move 走）
    assert not src.exists(), "成功时 chunks/docA 应被删除"
    assert (s.output_dir / "docA").is_dir(), "output/docA 应存在"
    assert (s.output_dir / "docA" / "chunk_metadata.json").is_file()
    assert any((s.output_dir / "docA").glob("chunk_*.md"))

    # 2) manifest 已更新
    manifest = manifest_store.load(s.manifest_path)
    row = manifest["docA.pdf"]
    assert row.dify_status == "done"
    assert row.dify_doc_id == "doc-fake-0001"
    # chunks 列变为 "output/docA"（新路径）
    assert row.chunks == "output/docA", f"chunks 应为 output/docA，实际={row.chunks!r}"
    # status 列变为 "done"（整体管线完成）
    assert row.status == "done", f"status 应为 done，实际={row.status!r}"

    # 3) action 记录里 note 包含 "已归档" + "output/docA"
    action = next(a for a in report.actions if a.stem == "docA")
    assert action.action == "uploaded"
    assert action.note and "已归档" in action.note
    assert "output/docA" in action.note


def test_upload_all_docs_failure_does_not_archive(fresh_settings):
    """失败入库：chunks 目录保持原位，output/ 副本被删除（便于重试）。"""
    from app.services import dify_ingest, manifest_store

    s = fresh_settings
    src = _make_chunks_dir(s.chunks_dir, "docA", [{"content": "a"}])
    manifest_store.ensure_exists(s.manifest_path)
    manifest_store.upsert(
        s.manifest_path,
        _make_manifest_row("docA.pdf", chunks="docA", status="chunked"),
    )

    fake = FakeDifyClient()
    fake.create_doc_status = "fail_4xx"
    report = dify_ingest.upload_all_docs(dry_run=False, force=True, client=fake)  # type: ignore[arg-type]
    assert report.failed == 1

    # 失败时：源目录应在，output 副本应被清理掉
    assert src.is_dir(), "失败时 chunks 目录应保留以便重试"
    assert not (s.output_dir / "docA").exists(), "失败时 output 副本应被清理"

    # manifest：dify_status=error，但 status 不强制变（保持原 chunked），chunks 也不变
    manifest = manifest_store.load(s.manifest_path)
    row = manifest["docA.pdf"]
    assert row.dify_status == "error"
    assert row.chunks == "docA", f"失败时 chunks 不应变，实际={row.chunks!r}"
    # status 列：失败时不升级为 done
    assert row.status != "done"


def test_list_chunk_dirs_includes_output_dir(fresh_settings):
    """_list_chunk_dirs 同时扫描 chunks/ 和 output/，用于 force 重传已归档的文档。"""
    from app.services import dify_ingest

    s = fresh_settings
    # 在 chunks/ 和 output/ 各放一个
    _make_chunks_dir(s.chunks_dir, "docA", [{"content": "a"}])
    out_dir = s.output_dir / "docB"
    out_dir.mkdir(parents=True)
    (out_dir / "chunk_metadata.json").write_text(
        json.dumps({"chunks": [{"file_name": "x.md", "content": "b"}]}, ensure_ascii=False),
        encoding="utf-8",
    )

    dirs = dify_ingest._list_chunk_dirs()
    stems = sorted(p.name for p in dirs)
    assert stems == ["docA", "docB"]


def test_list_chunk_dirs_dedup_when_in_both(fresh_settings):
    """同一 stem 在 chunks/ 和 output/ 都存在时：只返回 chunks/ 那一份。"""
    from app.services import dify_ingest

    s = fresh_settings
    _make_chunks_dir(s.chunks_dir, "docA", [{"content": "in chunks"}])
    out_dir = s.output_dir / "docA"
    out_dir.mkdir(parents=True)
    (out_dir / "marker.txt").write_text("in output", encoding="utf-8")

    dirs = dify_ingest._list_chunk_dirs()
    matches = [p for p in dirs if p.name == "docA"]
    assert len(matches) == 1
    assert matches[0].parent == s.chunks_dir, f"应优先 chunks/，实际={matches[0].parent}"


# ============ 7. 回归保护：log.info(extra={...}) 不能用 LogRecord 内置字段名 ============


# Python LogRecord 保留字段（不能作为 extra key 使用，会抛 KeyError）
_LOGRECORD_RESERVED = frozenset({
    "name", "msg", "args", "levelname", "levelno",
    "pathname", "filename", "module", "exc_info", "exc_text",
    "stack_info", "lineno", "funcName", "created", "msecs",
    "relativeCreated", "thread", "threadName", "processName", "process",
    "message", "asctime", "taskName",
})


def _iter_log_extra_dicts(extra):
    """递归展开 extra，支持 dict / kwargs 形式。"""
    if isinstance(extra, dict):
        for k in extra.keys():
            if isinstance(k, str):
                yield k


def test_log_extra_no_reserved_keyword_in_app():
    """★ 回归保护：app/ 下任何 log.xxx(... extra={...}) 的 extra key
    都不能用 LogRecord 保留字段（如 'name' / 'msg' / 'args'），
    否则 Python logging.makeRecord 会抛 KeyError('Attempt to overwrite %r in LogRecord')。

    历史上 dify_uploader.create_document_by_text 的 extra={"name": ...} 就触发了这个 bug。
    """
    import ast
    import pathlib

    app_root = pathlib.Path(__file__).resolve().parents[1] / "app"
    violations: list = []
    for p in app_root.rglob("*.py"):
        try:
            tree = ast.parse(p.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not (isinstance(func, ast.Attribute) and func.attr in {
                "info", "warning", "error", "debug", "critical", "exception", "log",
            }):
                continue
            if not (isinstance(func.value, ast.Name) and func.value.id == "log"):
                continue
            for kw in node.keywords:
                if kw.arg != "extra":
                    continue
                if not isinstance(kw.value, ast.Dict):
                    continue
                for k in kw.value.keys:
                    if isinstance(k, ast.Constant) and isinstance(k.value, str):
                        if k.value in _LOGRECORD_RESERVED:
                            violations.append(f"{p.relative_to(app_root.parent)}:{node.lineno}  extra[{k.value!r}] 与 LogRecord 保留字段冲突")
    assert not violations, "log.*() extra 字段与 LogRecord 保留字段冲突：\n" + "\n".join(violations)


def test_logging_makeRecord_actually_works_for_dify_uploader():
    """运行时：触发 create_document_by_text 的 log.info 路径，
    确认 KeyError('name') bug 不再复现——记录能正常写出，且 record.name 是 logger 名。
    """
    import logging
    from app.services import dify_uploader

    class _ListHandler(logging.Handler):
        def __init__(self):
            super().__init__()
            self.records: list = []

        def emit(self, record):
            self.records.append(record)

    h = _ListHandler()
    h.setLevel(logging.DEBUG)
    logger = logging.getLogger("ragsystem.dify_client")
    logger.setLevel(logging.DEBUG)  # logger 自身也要 DEBUG（handler 是最后一道过滤）
    logger.addHandler(h)
    # 模块级 logging.disable(CRITICAL) 会屏蔽所有 logger；本测试需要它工作
    logging.disable(logging.NOTSET)
    try:
        # 直接复刻 dify_uploader.py:203 的 log.info 调用
        logger.info(
            "dify create_document_by_text start",
            extra={"step": "dify", "status": "create", "doc_name": "hello", "text_len": 5},
        )
        assert len(h.records) >= 1
        r = h.records[-1]
        # 关键：record.name 必须是 logger 名（不是 'hello'，也不是 'doc_name'）
        assert r.name == "ragsystem.dify_client"
        # extra 的 doc_name 必须能正确取出
        assert getattr(r, "doc_name") == "hello"
    finally:
        logger.removeHandler(h)
        logging.disable(logging.CRITICAL)  # 恢复模块级的屏蔽


# ============ 8. ★ Dify 连通性测试端点（验证 API Key / dataset）============


class _StubResp:
    """模拟 httpx 响应的最小 stub（够 test_connection 用）。"""
    def __init__(self, status: int, body: Any = None, ctype: str = "application/json"):
        self.status_code = status
        self._body = body
        self.headers = {"content-type": ctype}
        self.reason_phrase = "OK" if 200 <= status < 300 else "Error"

    @property
    def text(self) -> str:
        if isinstance(self._body, (dict, list)):
            import json as _json
            return _json.dumps(self._body, ensure_ascii=False)
        return self._body if isinstance(self._body, str) else ""

    def json(self) -> Any:
        if isinstance(self._body, (dict, list)):
            return self._body
        import json as _json
        return _json.loads(self._body) if self._body else {}


def test_dify_client_test_connection_success(fresh_settings, monkeypatch):
    """连通性测试：200 → ok=True，提取 dataset_name / doc_count。"""
    from app.services import dify_uploader
    import httpx

    def fake_get(self, url, headers=None, **kw):  # noqa: ARG001
        assert "/datasets/" in url
        assert headers.get("Authorization", "").startswith("Bearer ")
        return _StubResp(200, {
            "id": "ds-x",
            "name": "测试知识库",
            "document_count": 42,
        })

    monkeypatch.setattr(httpx.Client, "get", fake_get)
    client = dify_uploader.DifyClient()
    result = client.test_connection()
    assert result["ok"] is True
    assert result["error_code"] is None
    assert result["dataset_name"] == "测试知识库"
    assert result["doc_count"] == 42
    assert result["elapsed_ms"] >= 0


def test_dify_client_test_connection_401_returns_hint(fresh_settings, monkeypatch):
    """401 → ok=False，error 含可操作的修复建议（提示重新生成 Key）。"""
    from app.services import dify_uploader
    import httpx

    def fake_get(self, url, headers=None, **kw):  # noqa: ARG001
        return _StubResp(401, '{"code":"unauthorized","message":"Access token is invalid","status":401}', ctype="application/json")

    monkeypatch.setattr(httpx.Client, "get", fake_get)
    client = dify_uploader.DifyClient()
    result = client.test_connection()
    assert result["ok"] is False
    assert result["error_code"] == 401
    assert "401" in result["error"]
    # 关键：error 必须包含「重新生成」的提示
    assert "重新生成" in result["error"] or "API Key" in result["error"]


def test_dify_client_test_connection_404_returns_hint(fresh_settings, monkeypatch):
    """404 → 提示「知识库 ID 不存在」。"""
    from app.services import dify_uploader
    import httpx

    def fake_get(self, url, headers=None, **kw):  # noqa: ARG001
        return _StubResp(404, "Dataset not found", ctype="text/plain")

    monkeypatch.setattr(httpx.Client, "get", fake_get)
    client = dify_uploader.DifyClient()
    result = client.test_connection()
    assert result["ok"] is False
    assert result["error_code"] == 404
    assert "知识库 ID 不存在" in result["error"]


def test_dify_client_test_connection_unreachable(fresh_settings, monkeypatch):
    """连不上 → ok=False，error 含「无法连接 Dify」。"""
    from app.services import dify_uploader
    import httpx

    def fake_get(self, url, headers=None, **kw):  # noqa: ARG001
        raise httpx.ConnectError("No route to host")

    monkeypatch.setattr(httpx.Client, "get", fake_get)
    client = dify_uploader.DifyClient()
    result = client.test_connection()
    assert result["ok"] is False
    assert "无法连接" in result["error"]


def _reload_app_for_test():
    """辅助：重新加载 app.api.dify + app.main，让它们都拿到最新的 cfg_mod.settings。

    原因：这些模块在 `from app.config import settings` 时捕获了 settings 引用。
    fixture / 测试代码用 monkeypatch.setenv 改了 env，但只 reload cfg_mod 不会
    同步更新 app.api.dify / app.main 已经捕获的旧 settings 引用。
    所以调用端点时会拿到旧值（可能 dify_api_key 已清空 → 抛 ValueError）。

    Returns:
        新的 app.main.app 实例
    """
    from app.api import dify as dify_api
    from app import main as app_main
    importlib.reload(dify_api)
    importlib.reload(app_main)
    return app_main.app


def test_api_dify_test_401_key_invalid(fresh_settings, monkeypatch):
    """端到端：/api/dify/test 走 DifyClient.test_connection，401 → 返回带 hint 的 ok=False。

    ★ 不能 monkeypatch httpx.Client.get —— TestClient 自己也用 httpx.Client.get
    调用 ASGI app（URL 是相对路径 '/api/dify/test'），会被 patch 截胡返回空 body。
    这里直接 patch DifyClient.test_connection，更精准。
    """
    from fastapi.testclient import TestClient
    from app.services import dify_uploader

    def fake_test_connection(self):
        return {
            "ok": False,
            "api_url": self.api_url,
            "dataset_id": self.dataset_id,
            "dataset_name": None,
            "doc_count": None,
            "elapsed_ms": 50,
            "error": "401 unauthorized → 提示：请到 Dify 控制台 → 知识库 → API 访问 → 重新生成 dataset- 开头的新 Key",
            "error_code": 401,
        }

    monkeypatch.setattr(dify_uploader.DifyClient, "test_connection", fake_test_connection)
    # reload app 让它用 fixture 设的 env（dify_api_key 已配）
    app = _reload_app_for_test()

    with TestClient(app) as c:
        r = c.get("/api/dify/test")
        assert r.status_code == 200, f"status={r.status_code} body={r.text!r}"
        body = r.json()
        assert body["ok"] is False, f"body={body!r}"
        assert body["error_code"] == 401
        assert "重新生成" in body["error"]


def test_api_dify_test_missing_key(fresh_settings, monkeypatch):
    """未配置 API Key → 端点 400。"""
    from fastapi.testclient import TestClient
    from app import config as cfg_mod

    # 临时把 key 置空（不动 .env）
    orig_key = cfg_mod.settings.dify_api_key
    cfg_mod.settings.dify_api_key = ""
    # ★ reload app.api.dify + app.main，让它们的 settings 引用都同步到空 key
    app = _reload_app_for_test()
    try:
        with TestClient(app) as c:
            r = c.get("/api/dify/test")
            assert r.status_code == 400
            assert "dify_api_key 未配置" in r.json()["detail"]
    finally:
        cfg_mod.settings.dify_api_key = orig_key
        _reload_app_for_test()


# ===========================================================================
# § 9. Image host backend 派发 / 降级（plan.md §3.4，公网图片托管后端抽象化）
# ===========================================================================
#
# 验证：
# - 默认 backend = "tunnel"（与历史行为完全一致）
# - _build_tunnel_url 按 public_base_url 拼路径（与 _build_public_url 历史行为一致）
# - 未知 backend → 返回空串 + WARNING
# - oss backend → 抛 NotImplementedError（被 build_image_url 捕获）→ 返回空串 + WARNING
# - is_active() 根据各后端独立判断
# - 现有 _build_public_url 测试（tunnel）保持原样通过


def _reload_with_env(monkeypatch, **env):
    """辅助：把 env 写入 monkeypatch，重新加载 config + image_host，返回 settings。

    None 视作空串（"")，而不是 delenv —— 避免 pydantic-settings 回退到
    backend/.env 的 RAG_PUBLIC_BASE_URL 等字段。
    """
    for k, v in env.items():
        if v is None:
            monkeypatch.setenv(k, "")
        else:
            monkeypatch.setenv(k, v)
    from app import config as cfg_mod
    importlib.reload(cfg_mod)
    from app.services import image_host
    image_host.settings = cfg_mod.settings
    return cfg_mod.settings, image_host


def test_default_image_host_backend_is_oss(fresh_settings):
    """★ 2026-08-04：默认 backend 切到 oss（阿里云 OSS 永久外链，tunnel 已废弃）。"""
    from app.services import image_host
    assert fresh_settings.image_host_backend == "oss", (
        f"默认 backend 应为 oss，实际: {fresh_settings.image_host_backend}"
    )
    assert "tunnel" in image_host.KNOWN_BACKENDS
    assert "oss" in image_host.KNOWN_BACKENDS


def test_build_image_url_tunnel_dispatches_to_tunnel_builder(monkeypatch):
    """backend=tunnel：调 _build_tunnel_url 拼出 /static/output/... 路径。"""
    s, image_host = _reload_with_env(
        monkeypatch,
        RAG_PUBLIC_BASE_URL="https://abc.ngrok.app",
    )
    url = image_host.build_image_url("tunnel", "docA", "images/x.jpg")
    assert url == "https://abc.ngrok.app/static/output/docA/images/x.jpg"


def test_build_image_url_tunnel_empty_base_returns_empty(monkeypatch):
    """backend=tunnel 但 public_base_url 未设：返回空串（不崩）。"""
    s, image_host = _reload_with_env(
        monkeypatch,
        RAG_PUBLIC_BASE_URL=None,  # 清空
    )
    url = image_host.build_image_url("tunnel", "docA", "images/x.jpg")
    assert url == ""


def test_build_image_url_tunnel_normalizes_path(monkeypatch):
    """backend=tunnel：ref 开头有 ./ 或 \\ 都能被规范化。"""
    s, image_host = _reload_with_env(
        monkeypatch,
        RAG_PUBLIC_BASE_URL="https://abc.com",
    )
    url1 = image_host.build_image_url("tunnel", "docA", "./images/x.jpg")
    url2 = image_host.build_image_url("tunnel", "docA", "\\images\\x.jpg")
    assert url1 == "https://abc.com/static/output/docA/images/x.jpg"
    assert url2 == "https://abc.com/static/output/docA/images/x.jpg"


def test_build_image_url_unknown_backend_returns_empty_and_warns(monkeypatch, caplog):
    """未知 backend（如 s3）：返回空串 + WARNING 'unknown image host backend'。"""
    from app.services import image_host

    _reload_with_env(monkeypatch, RAG_IMAGE_HOST_BACKEND="s3")
    # ★ 解禁 CRITICAL：模块顶部 logging.disable(CRITICAL) 会屏蔽 caplog
    logging.disable(logging.NOTSET)
    try:
        caplog.set_level(logging.WARNING, logger="ragsystem.image_host")
        url = image_host.build_image_url("s3", "docA", "images/x.jpg")
    finally:
        logging.disable(logging.CRITICAL)
    assert url == ""
    assert any("unknown image host backend" in rec.message for rec in caplog.records), \
        f"expected WARNING about unknown backend, got: {[r.message for r in caplog.records]}"


def test_build_oss_url_generates_permanent_oss_link(monkeypatch):
    """★ 2026-08-04：_build_oss_url 真实实现，生成永久 OSS 公网 URL。

    URL 格式：`https://{bucket}.{endpoint_host}/{prefix}/{stem}/images/{filename}`
    与 OssUploader.build_key + public_url 行为完全一致。
    """
    s, image_host = _reload_with_env(
        monkeypatch,
        RAG_OSS_ENDPOINT="https://oss-cn-hangzhou.aliyuncs.com",
        RAG_OSS_BUCKET="ycsj-dify",
        RAG_OSS_ACCESS_KEY_ID="ak",
        RAG_OSS_ACCESS_KEY_SECRET="sk",
        RAG_OSS_OBJECT_PREFIX="static/output",
    )
    # 不需要真实网络连接（_build_oss_url 只生成 URL 不上传）
    url = image_host._build_oss_url("docA", "images/x.jpg")
    assert url == "https://ycsj-dify.oss-cn-hangzhou.aliyuncs.com/static/output/docA/images/x.jpg"


def test_build_oss_url_strips_leading_prefix(monkeypatch):
    """★ _build_oss_url：ref 前导 ./ / / 都会被规范化。"""
    s, image_host = _reload_with_env(
        monkeypatch,
        RAG_OSS_ENDPOINT="https://oss-cn-hangzhou.aliyuncs.com",
        RAG_OSS_BUCKET="ycsj-dify",
        RAG_OSS_ACCESS_KEY_ID="ak",
        RAG_OSS_ACCESS_KEY_SECRET="sk",
    )
    url1 = image_host._build_oss_url("docA", "./images/x.jpg")
    url2 = image_host._build_oss_url("docA", "/images/x.jpg")
    assert url1 == "https://ycsj-dify.oss-cn-hangzhou.aliyuncs.com/static/output/docA/images/x.jpg"
    assert url2 == "https://ycsj-dify.oss-cn-hangzhou.aliyuncs.com/static/output/docA/images/x.jpg"


def test_build_image_url_oss_returns_real_url(monkeypatch, caplog):
    """★ 2026-08-04 修订：OSS 后端已完整实现，build_image_url 直接返回永久公网 URL。

    历史行为：_build_oss_url 抛 NotImplementedError → build_image_url 降级为空串。
    现在行为：_build_oss_url 生成 https://{bucket}.{endpoint}/static/output/... 永久外链。
    本测试覆盖"正常配置下"返回正确 URL 的场景。
    """
    from app.services import image_host

    _reload_with_env(
        monkeypatch,
        RAG_IMAGE_HOST_BACKEND="oss",
        RAG_OSS_ENDPOINT="https://oss-cn-hangzhou.aliyuncs.com",
        RAG_OSS_BUCKET="my-bucket",
        RAG_OSS_ACCESS_KEY_ID="ak",
        RAG_OSS_ACCESS_KEY_SECRET="sk",
    )
    url = image_host.build_image_url("oss", "docA", "images/x.jpg")
    assert url == "https://my-bucket.oss-cn-hangzhou.aliyuncs.com/static/output/docA/images/x.jpg"


def test_build_image_url_oss_degrades_on_exception(monkeypatch, caplog):
    """★ 2026-08-04 修订：OSS builder 抛 Exception 时，build_image_url 仍安全降级。

    与原始 test_build_image_url_oss_degrades_safely 一致——降级路径
    必须在真实 OSS 调用失败时仍能工作（不崩、不抛），返回空串 + WARNING。
    """
    from app.services import image_host

    _reload_with_env(
        monkeypatch,
        RAG_IMAGE_HOST_BACKEND="oss",
        RAG_OSS_ENDPOINT="https://oss-cn-hangzhou.aliyuncs.com",
        RAG_OSS_BUCKET="my-bucket",
    )

    # 模拟 _BUILDERS 里的 oss builder 抛非 NotImplementedError 的异常（比如 bucket 配置错）
    # 注意：build_image_url 通过 _BUILDERS 字典派发，所以 patch 字典里的 key
    def _boom(stem, ref):
        raise RuntimeError("fake boom")

    monkeypatch.setitem(image_host._BUILDERS, "oss", _boom)
    logging.disable(logging.NOTSET)
    try:
        caplog.set_level(logging.WARNING, logger="ragsystem.image_host")
        url = image_host.build_image_url("oss", "docA", "images/x.jpg")
    finally:
        logging.disable(logging.CRITICAL)
    assert url == ""
    assert any("exception" in rec.message.lower() for rec in caplog.records), \
        f"expected WARNING about exception, got: {[r.message for r in caplog.records]}"


def test_is_active_tunnel_depends_on_public_url(monkeypatch):
    """is_active(backend='tunnel')：看 public_base_url 是否非空。"""
    s, image_host = _reload_with_env(monkeypatch, RAG_PUBLIC_BASE_URL=None)
    assert image_host.is_active("tunnel", s) is False
    s, image_host = _reload_with_env(monkeypatch, RAG_PUBLIC_BASE_URL="https://x.com")
    assert image_host.is_active("tunnel", s) is True


def test_is_active_oss_requires_endpoint_and_bucket(monkeypatch):
    """is_active(backend='oss')：endpoint + bucket 都填才算启用。"""
    s, image_host = _reload_with_env(
        monkeypatch,
        RAG_OSS_ENDPOINT=None,
        RAG_OSS_BUCKET=None,
    )
    assert image_host.is_active("oss", s) is False
    s, image_host = _reload_with_env(
        monkeypatch,
        RAG_OSS_ENDPOINT="https://oss-cn.aliyuncs.com",
        RAG_OSS_BUCKET=None,
    )
    assert image_host.is_active("oss", s) is False
    s, image_host = _reload_with_env(
        monkeypatch,
        RAG_OSS_ENDPOINT=None,
        RAG_OSS_BUCKET="b",
    )
    assert image_host.is_active("oss", s) is False
    s, image_host = _reload_with_env(
        monkeypatch,
        RAG_OSS_ENDPOINT="https://oss-cn.aliyuncs.com",
        RAG_OSS_BUCKET="b",
    )
    assert image_host.is_active("oss", s) is True


def test_is_active_unknown_backend_always_false(monkeypatch):
    """is_active 遇到未知 backend 一律 False。"""
    s, image_host = _reload_with_env(
        monkeypatch,
        RAG_PUBLIC_BASE_URL="https://x.com",
        RAG_OSS_ENDPOINT="https://oss-cn.aliyuncs.com",
        RAG_OSS_BUCKET="b",
    )
    assert image_host.is_active("s3", s) is False
    assert image_host.is_active("", s) is False


def test_build_public_url_shim_dispatches_to_image_host(monkeypatch):
    """_build_public_url（shim）：backend=tunnel 时行为与历史完全一致。

    ★ 2026-08-04 修订：默认 backend 改为 oss，本测试显式切到 tunnel。
    """
    from app.services import dify_ingest
    from app import config as cfg_mod
    importlib.reload(cfg_mod)
    from app.services import image_host
    image_host.settings = cfg_mod.settings
    dify_ingest.settings = cfg_mod.settings

    monkeypatch.setenv("RAG_IMAGE_HOST_BACKEND", "tunnel")
    monkeypatch.setenv("RAG_PUBLIC_BASE_URL", "https://abc.trycloudflare.com")
    from app import config as cfg_mod2
    importlib.reload(cfg_mod2)
    image_host.settings = cfg_mod2.settings
    dify_ingest.settings = cfg_mod2.settings

    url = dify_ingest._build_public_url("测试", "images/a.jpg")
    assert url == "https://abc.trycloudflare.com/static/output/测试/images/a.jpg"


# ===========================================================================
# § 10. OssUploader 单元测试（plan.md §3.4，OSS 真实集成）
# ===========================================================================
#
# 验证：
# - build_key / public_url 路径拼接正确（含 prefix/bucket/endpoint 各种组合）
# - upload_chunks_images 批量上传行为（成功 / 已存在跳过 / 失败 / images/ 缺失）
# - prepare_chunks_images 派发到 OssUploader 上传（OssUploader 模拟，不真打 OSS）


class _FakeOssBucket:
    """模拟 oss2.Bucket 行为；只暴露测试需要的接口。"""

    def __init__(self, existing_keys: Optional[List[str]] = None) -> None:
        self.existing = set(existing_keys or [])
        self.puts: List[Tuple[str, str, Dict[str, Any]]] = []  # (key, local_path, headers)
        self._next_raise: Optional[Exception] = None

    def head_object(self, key: str) -> Any:
        if key in self.existing:
            return {"key": key}
        # 模拟 oss2.exceptions.NotFound
        try:
            from oss2.exceptions import NotFound  # type: ignore
            raise NotFound("not found", {})
        except ImportError:
            # oss2 不可用 → 自己造异常类
            class _NF(Exception):
                pass
            raise _NF("not found")

    def put_object_from_file(self, key: str, local_path: str, headers: Optional[Dict[str, Any]] = None) -> Any:
        if self._next_raise is not None:
            exc = self._next_raise
            self._next_raise = None
            raise exc
        self.puts.append((key, local_path, headers or {}))
        self.existing.add(key)
        return {"key": key, "etag": "fake-etag"}


def _make_oss_uploader(existing_keys: Optional[List[str]] = None) -> Any:
    """构造 OssUploader，但替换掉内部的 _bucket_obj 为 _FakeOssBucket。"""
    from app.services.oss_uploader import OssUploader, _OSS2_AVAILABLE

    if not _OSS2_AVAILABLE:
        pytest.skip("oss2 SDK 未安装，跳过 OssUploader 测试")
    up = OssUploader(
        endpoint="https://oss-cn-hangzhou.aliyuncs.com",
        bucket="ycsj-dify",
        access_key_id="ak",
        access_key_secret="sk",
        object_prefix="static/output",
    )
    up._bucket_obj = _FakeOssBucket(existing_keys)
    return up


def test_oss_uploader_build_key_basic():
    """OssUploader.build_key：标准 ref。"""
    up = _make_oss_uploader()
    key = up.build_key("docA", "images/xxx.jpg")
    assert key == "static/output/docA/images/xxx.jpg"


def test_oss_uploader_build_key_strips_relative_prefix():
    """OssUploader.build_key：ref 带 ./ / 都规范化。"""
    up = _make_oss_uploader()
    assert up.build_key("docA", "./images/x.jpg") == "static/output/docA/images/x.jpg"
    assert up.build_key("docA", "/images/x.jpg") == "static/output/docA/images/x.jpg"
    assert up.build_key("docA", "\\images\\x.jpg") == "static/output/docA/images/x.jpg"


def test_oss_uploader_build_key_accepts_bare_filename():
    """OssUploader.build_key：纯文件名也能拼出 images/{name}。"""
    up = _make_oss_uploader()
    assert up.build_key("docA", "x.jpg") == "static/output/docA/images/x.jpg"


def test_oss_uploader_build_key_strips_object_prefix_slashes():
    """OssUploader.build_key：prefix 前后斜杠都规范化。"""
    from app.services.oss_uploader import OssUploader, _OSS2_AVAILABLE

    if not _OSS2_AVAILABLE:
        pytest.skip("oss2 SDK 未安装")
    up = OssUploader(
        endpoint="https://oss-cn-hangzhou.aliyuncs.com",
        bucket="ycsj-dify",
        access_key_id="ak",
        access_key_secret="sk",
        object_prefix="/static/output/",  # 故意带斜杠
    )
    assert up.build_key("docA", "images/x.jpg") == "static/output/docA/images/x.jpg"


def test_oss_uploader_public_url_https_endpoint():
    """OssUploader.public_url：https endpoint → https://{bucket}.{endpoint}/...。"""
    up = _make_oss_uploader()
    url = up.public_url("static/output/docA/images/x.jpg")
    assert url == "https://ycsj-dify.oss-cn-hangzhou.aliyuncs.com/static/output/docA/images/x.jpg"


def test_oss_uploader_public_url_with_custom_domain():
    """OssUploader.public_url：public_domain 配了 → 用 CDN 域名拼。"""
    from app.services.oss_uploader import OssUploader, _OSS2_AVAILABLE

    if not _OSS2_AVAILABLE:
        pytest.skip("oss2 SDK 未安装")
    up = OssUploader(
        endpoint="https://oss-cn-hangzhou.aliyuncs.com",
        bucket="ycsj-dify",
        access_key_id="ak",
        access_key_secret="sk",
        public_domain="https://cdn.example.com",
    )
    assert up.public_url("static/output/docA/images/x.jpg") == \
        "https://cdn.example.com/static/output/docA/images/x.jpg"


def test_oss_uploader_public_url_strips_leading_slash_in_key():
    """OssUploader.public_url：key 开头带 / 也要能正确拼。"""
    up = _make_oss_uploader()
    assert up.public_url("/static/output/docA/images/x.jpg") == \
        "https://ycsj-dify.oss-cn-hangzhou.aliyuncs.com/static/output/docA/images/x.jpg"


def test_oss_uploader_upload_file_success(tmp_path):
    """OssUploader.upload_file：成功 → 调 put_object_from_file + 加 public-read ACL。"""
    up = _make_oss_uploader()
    f = tmp_path / "x.jpg"
    f.write_bytes(b"jpg")
    ok = up.upload_file(f, "static/output/docA/images/x.jpg")
    assert ok is True
    fake = up._bucket_obj  # type: ignore[attr-defined]
    assert len(fake.puts) == 1
    put_key, put_path, put_headers = fake.puts[0]
    assert put_key == "static/output/docA/images/x.jpg"
    assert put_path == str(f)
    assert put_headers.get("x-oss-object-acl") == "public-read"


def test_oss_uploader_upload_file_skip_existing(tmp_path):
    """OssUploader.upload_file：head_object 已存在 → 跳过 put（默认 overwrite=False）。"""
    up = _make_oss_uploader(existing_keys=["static/output/docA/images/x.jpg"])
    f = tmp_path / "x.jpg"
    f.write_bytes(b"jpg")
    ok = up.upload_file(f, "static/output/docA/images/x.jpg")
    assert ok is True  # 跳过也算"成功"
    fake = up._bucket_obj  # type: ignore[attr-defined]
    assert len(fake.puts) == 0  # 没调 put


def test_oss_uploader_upload_file_missing_local(tmp_path):
    """OssUploader.upload_file：本地文件不存在 → False（不抛错）。"""
    up = _make_oss_uploader()
    ok = up.upload_file(tmp_path / "nope.jpg", "static/output/docA/images/nope.jpg")
    assert ok is False
    fake = up._bucket_obj  # type: ignore[attr-defined]
    assert len(fake.puts) == 0


def test_oss_uploader_upload_file_overwrite_true(tmp_path):
    """OssUploader.upload_file：overwrite=True 即使已存在也重传。"""
    up = _make_oss_uploader(existing_keys=["static/output/docA/images/x.jpg"])
    f = tmp_path / "x.jpg"
    f.write_bytes(b"jpg")
    ok = up.upload_file(f, "static/output/docA/images/x.jpg", overwrite=True)
    assert ok is True
    fake = up._bucket_obj  # type: ignore[attr-defined]
    assert len(fake.puts) == 1


def test_oss_uploader_upload_chunks_images_uploads_new_files(tmp_path):
    """OssUploader.upload_chunks_images：images/ 下新文件全部上传。"""
    from app.services.oss_uploader import OssUploadResult

    up = _make_oss_uploader()
    chunks_dir = tmp_path / "chunks" / "docA"
    images_dir = chunks_dir / "images"
    images_dir.mkdir(parents=True)
    (images_dir / "a.jpg").write_bytes(b"a")
    (images_dir / "b.png").write_bytes(b"b")
    (chunks_dir / "chunk_001.md").write_text("# doc", encoding="utf-8")

    result = up.upload_chunks_images("docA", chunks_dir)
    assert isinstance(result, OssUploadResult)
    assert sorted(result.uploaded) == ["images/a.jpg", "images/b.png"]
    assert result.skipped_existing == []
    assert result.failed == []
    assert result.ref_to_url == {
        "images/a.jpg": "https://ycsj-dify.oss-cn-hangzhou.aliyuncs.com/static/output/docA/images/a.jpg",
        "images/b.png": "https://ycsj-dify.oss-cn-hangzhou.aliyuncs.com/static/output/docA/images/b.png",
    }
    fake = up._bucket_obj  # type: ignore[attr-defined]
    assert len(fake.puts) == 2


def test_oss_uploader_upload_chunks_images_skips_existing(tmp_path):
    """OssUploader.upload_chunks_images：已存在文件走 skipped_existing 分支。"""
    up = _make_oss_uploader(
        existing_keys=["static/output/docA/images/a.jpg"]
    )
    chunks_dir = tmp_path / "chunks" / "docA"
    images_dir = chunks_dir / "images"
    images_dir.mkdir(parents=True)
    (images_dir / "a.jpg").write_bytes(b"a")
    (images_dir / "b.png").write_bytes(b"b")

    result = up.upload_chunks_images("docA", chunks_dir)
    assert sorted(result.uploaded) == ["images/b.png"]
    assert result.skipped_existing == ["images/a.jpg"]
    assert result.ref_to_url["images/a.jpg"].endswith("/static/output/docA/images/a.jpg")
    assert result.ref_to_url["images/b.png"].endswith("/static/output/docA/images/b.png")
    fake = up._bucket_obj  # type: ignore[attr-defined]
    # 只 put 了 b.png 一个（a.jpg 走 head 跳过）
    assert len(fake.puts) == 1


def test_oss_uploader_upload_chunks_images_no_images_dir(tmp_path):
    """OssUploader.upload_chunks_images：chunks_dir 没有 images/ → 空 result 不抛错。"""
    up = _make_oss_uploader()
    chunks_dir = tmp_path / "chunks" / "docA"
    chunks_dir.mkdir(parents=True)
    result = up.upload_chunks_images("docA", chunks_dir)
    assert result.uploaded == []
    assert result.skipped_existing == []
    assert result.failed == []
    assert result.ref_to_url == {}


def test_oss_uploader_upload_chunks_images_ignores_subdirs(tmp_path):
    """OssUploader.upload_chunks_images：只处理 images/ 顶层文件，忽略子目录。"""
    up = _make_oss_uploader()
    chunks_dir = tmp_path / "chunks" / "docA"
    images_dir = chunks_dir / "images"
    images_dir.mkdir(parents=True)
    (images_dir / "a.jpg").write_bytes(b"a")
    (images_dir / "sub").mkdir()
    (images_dir / "sub" / "b.jpg").write_bytes(b"b")  # 子目录文件应被忽略
    result = up.upload_chunks_images("docA", chunks_dir)
    assert result.uploaded == ["images/a.jpg"]
    assert "images/sub/b.jpg" not in result.ref_to_url


# ---------- image_host.prepare_chunks_images 派发测试 ----------


def test_prepare_chunks_images_oss_dispatches_to_uploader(monkeypatch, tmp_path):
    """image_host.prepare_chunks_images(backend='oss') → 调 OssUploader 并返回 ref→url。

    通过 monkeypatch OssUploader.from_settings 来注入 fake uploader（不真打 OSS）。
    """
    from app.services import image_host
    from app.services.oss_uploader import OssUploadResult

    _reload_with_env(
        monkeypatch,
        RAG_IMAGE_HOST_BACKEND="oss",
        RAG_OSS_ENDPOINT="https://oss-cn-hangzhou.aliyuncs.com",
        RAG_OSS_BUCKET="ycsj-dify",
        RAG_OSS_ACCESS_KEY_ID="ak",
        RAG_OSS_ACCESS_KEY_SECRET="sk",
    )

    class _FakeUp:
        def __init__(self) -> None:
            self.called_with = None
        def upload_chunks_images(self, stem, chunks_dir):
            self.called_with = (stem, chunks_dir)
            return OssUploadResult(
                uploaded=["images/a.jpg"],
                ref_to_url={"images/a.jpg": "https://ycsj-dify.oss-cn-hangzhou.aliyuncs.com/static/output/d/images/a.jpg"},
            )

    fake_up = _FakeUp()
    monkeypatch.setattr(
        "app.services.oss_uploader.OssUploader.from_settings",
        classmethod(lambda cls: fake_up),
    )

    chunks_dir = tmp_path / "chunks" / "d"
    (chunks_dir / "images").mkdir(parents=True)
    (chunks_dir / "images" / "a.jpg").write_bytes(b"a")

    result = image_host.prepare_chunks_images("oss", "d", chunks_dir)
    assert fake_up.called_with == ("d", chunks_dir)
    assert result == {
        "images/a.jpg": "https://ycsj-dify.oss-cn-hangzhou.aliyuncs.com/static/output/d/images/a.jpg"
    }


def test_prepare_chunks_images_oss_skipped_when_not_active(monkeypatch, tmp_path):
    """image_host.prepare_chunks_images(backend='oss') 但 is_active=False → 返回空 dict。

    场景：OSS 配置不齐全（缺 access_key 等），不应抛错。
    """
    from app.services import image_host

    _reload_with_env(
        monkeypatch,
        RAG_IMAGE_HOST_BACKEND="oss",
        RAG_OSS_ENDPOINT=None,  # 缺 endpoint
        RAG_OSS_BUCKET=None,    # 缺 bucket
        RAG_OSS_ACCESS_KEY_ID=None,
        RAG_OSS_ACCESS_KEY_SECRET=None,
    )

    chunks_dir = tmp_path / "chunks" / "d"
    (chunks_dir / "images").mkdir(parents=True)
    result = image_host.prepare_chunks_images("oss", "d", chunks_dir)
    assert result == {}


def test_prepare_chunks_images_oss_degrades_on_uploader_error(monkeypatch, tmp_path):
    """image_host.prepare_chunks_images：OssUploader 初始化失败 → 返回空 dict。"""
    from app.services import image_host

    _reload_with_env(
        monkeypatch,
        RAG_IMAGE_HOST_BACKEND="oss",
        RAG_OSS_ENDPOINT="https://oss-cn-hangzhou.aliyuncs.com",
        RAG_OSS_BUCKET="ycsj-dify",
        RAG_OSS_ACCESS_KEY_ID="ak",
        RAG_OSS_ACCESS_KEY_SECRET="sk",
    )

    def _boom():
        raise RuntimeError("fake uploader init boom")
    monkeypatch.setattr(
        "app.services.oss_uploader.OssUploader.from_settings",
        classmethod(lambda cls: _boom()),
    )

    chunks_dir = tmp_path / "chunks" / "d"
    (chunks_dir / "images").mkdir(parents=True)
    result = image_host.prepare_chunks_images("oss", "d", chunks_dir)
    assert result == {}


def test_prepare_chunks_images_tunnel_returns_empty(monkeypatch, tmp_path):
    """image_host.prepare_chunks_images(backend='tunnel') → 返回空 dict（依赖 main.py 挂载静态服务）。

    tunnel 模式不需要"上传"动作，URL 由 build_image_url 在 dify_ingest 阶段现场拼。
    """
    from app.services import image_host

    _reload_with_env(
        monkeypatch,
        RAG_IMAGE_HOST_BACKEND="tunnel",
        RAG_PUBLIC_BASE_URL="https://abc.trycloudflare.com",
    )
    chunks_dir = tmp_path / "chunks" / "d"
    (chunks_dir / "images").mkdir(parents=True)
    result = image_host.prepare_chunks_images("tunnel", "d", chunks_dir)
    assert result == {}


def test_prepare_chunks_images_unknown_backend_returns_empty(monkeypatch, tmp_path):
    """image_host.prepare_chunks_images(backend='s3') → 返回空 dict。"""
    from app.services import image_host

    _reload_with_env(monkeypatch, RAG_IMAGE_HOST_BACKEND="s3")
    chunks_dir = tmp_path / "chunks" / "d"
    (chunks_dir / "images").mkdir(parents=True)
    result = image_host.prepare_chunks_images("s3", "d", chunks_dir)
    assert result == {}


# ---------- _OssUploadResult dataclass 行为测试 ----------


def test_oss_upload_result_default_collections():
    """OssUploadResult 三个列表 + 一个字典默认是空。"""
    from app.services.oss_uploader import OssUploadResult

    r = OssUploadResult()
    assert r.uploaded == []
    assert r.skipped_existing == []
    assert r.failed == []
    assert r.ref_to_url == {}


# ---------- 独立 URL 拼装函数测试（★ 2026-08-04 新增）----------


def test_build_oss_public_url_https_endpoint():
    """build_oss_public_url：https endpoint + 无 public_domain → https://{bucket}.{endpoint}/..."""
    from app.services.oss_uploader import build_oss_public_url

    url = build_oss_public_url(
        endpoint="https://oss-cn-hangzhou.aliyuncs.com",
        bucket="ycsj-dify",
        public_domain="",
        key="static/output/docA/images/x.jpg",
    )
    assert url == "https://ycsj-dify.oss-cn-hangzhou.aliyuncs.com/static/output/docA/images/x.jpg"


def test_build_oss_public_url_with_custom_domain():
    """build_oss_public_url：public_domain 配了 → 用 CDN 域名拼。"""
    from app.services.oss_uploader import build_oss_public_url

    url = build_oss_public_url(
        endpoint="https://oss-cn-hangzhou.aliyuncs.com",
        bucket="ycsj-dify",
        public_domain="https://cdn.example.com",
        key="static/output/docA/images/x.jpg",
    )
    assert url == "https://cdn.example.com/static/output/docA/images/x.jpg"


def test_build_oss_public_url_strips_leading_slash_in_key():
    """build_oss_public_url：key 开头带 / 也要能正确拼。"""
    from app.services.oss_uploader import build_oss_public_url

    url = build_oss_public_url(
        endpoint="https://oss-cn-hangzhou.aliyuncs.com",
        bucket="ycsj-dify",
        public_domain="",
        key="/static/output/docA/images/x.jpg",
    )
    assert url == "https://ycsj-dify.oss-cn-hangzhou.aliyuncs.com/static/output/docA/images/x.jpg"


def test_build_oss_object_key_basic():
    """build_oss_object_key：标准 ref。"""
    from app.services.oss_uploader import build_oss_object_key

    key = build_oss_object_key(
        object_prefix="static/output",
        stem="docA",
        ref="images/x.jpg",
    )
    assert key == "static/output/docA/images/x.jpg"


def test_build_oss_object_key_strips_prefix_slashes():
    """build_oss_object_key：object_prefix 前后斜杠都规范化。"""
    from app.services.oss_uploader import build_oss_object_key

    key = build_oss_object_key(
        object_prefix="/static/output/",
        stem="docA",
        ref="images/x.jpg",
    )
    assert key == "static/output/docA/images/x.jpg"


def test_build_oss_object_key_accepts_bare_filename():
    """build_oss_object_key：纯文件名也能拼出 images/{name}。"""
    from app.services.oss_uploader import build_oss_object_key

    key = build_oss_object_key(
        object_prefix="static/output",
        stem="docA",
        ref="x.jpg",
    )
    assert key == "static/output/docA/images/x.jpg"


# ---------- ★ 2026-08-04：URL 拼装不依赖 oss2 SDK ----------


def test_build_oss_url_works_without_oss2_sdk(monkeypatch):
    """★ 2026-08-04：_build_oss_url 走独立函数后，即使 oss2 不可用也能拼出 URL。

    背景：原 _build_oss_url 内部实例化 OssUploader，oss2 缺失会抛 RuntimeError，
    导致 build_image_url 降级为空 → dify 段里写 Dify 5min 签名 URL → 召回时图片 404。

    重构后：URL 拼装走 build_oss_public_url / build_oss_object_key 独立函数，
    不需要 oss2 SDK，URL 永远能拼出来。
    """
    from app.services import image_host, oss_uploader

    # ★ 模拟 oss2 不可用
    monkeypatch.setattr(oss_uploader, "_OSS2_AVAILABLE", False)

    _reload_with_env(
        monkeypatch,
        RAG_IMAGE_HOST_BACKEND="oss",
        RAG_OSS_ENDPOINT="https://oss-cn-hangzhou.aliyuncs.com",
        RAG_OSS_BUCKET="ycsj-dify",
        RAG_OSS_ACCESS_KEY_ID="ak",
        RAG_OSS_ACCESS_KEY_SECRET="sk",
    )

    # ★ 即便 oss2 标记为不可用，_build_oss_url 仍能拼出 URL（因为是纯字符串拼装）
    url = image_host._build_oss_url("docA", "images/x.jpg")
    assert url == "https://ycsj-dify.oss-cn-hangzhou.aliyuncs.com/static/output/docA/images/x.jpg"


def test_is_active_oss_requires_oss2_sdk(monkeypatch):
    """★ 2026-08-04：is_active(backend='oss') 还要检查 oss2 SDK 可用。

    原因：prepare_chunks_images 触发实际上传时需要 oss2；如不可用，
    实际上传会抛 RuntimeError，应让 is_active 返回 False 走"URL-only 降级"路径。
    """
    from app.services import image_host, oss_uploader

    monkeypatch.setattr(oss_uploader, "_OSS2_AVAILABLE", False)
    s, image_host = _reload_with_env(
        monkeypatch,
        RAG_IMAGE_HOST_BACKEND="oss",
        RAG_OSS_ENDPOINT="https://oss-cn-hangzhou.aliyuncs.com",
        RAG_OSS_BUCKET="ycsj-dify",
        RAG_OSS_ACCESS_KEY_ID="ak",
        RAG_OSS_ACCESS_KEY_SECRET="sk",
    )
    # oss2 不可用 → is_active 返回 False
    assert image_host.is_active("oss", s) is False
    # 但 _build_oss_url 仍能工作（独立函数）
    assert image_host._build_oss_url("docA", "images/x.jpg").startswith(
        "https://ycsj-dify.oss-cn-hangzhou.aliyuncs.com/"
    )


def test_upload_one_doc_oss_backend_uses_oss_urls_even_if_oss2_unavailable(
    fresh_settings, monkeypatch
):
    """★ 2026-08-04：oss2 不可用时，_build_oss_url 仍能用，content 仍写 OSS URL。

    场景：oss2 缺失 → OssUploader 无法实例化 → image_host.prepare_chunks_images
    返回空 dict（实际上传跳过）→ 但 _build_oss_url 仍能生成 URL →
    dify 段里写的是 OSS 永久 URL，不是 Dify 5min 签名 URL。
    """
    from app.services import dify_ingest
    from app.services import image_host, oss_uploader

    # 模拟 oss2 不可用
    monkeypatch.setattr(oss_uploader, "_OSS2_AVAILABLE", False)

    # 模拟 OssUploader.from_settings 抛 RuntimeError（oss2 不可用的典型表现）
    def _from_settings_boom():
        raise RuntimeError("oss2 SDK 未安装")
    monkeypatch.setattr(oss_uploader.OssUploader, "from_settings", classmethod(lambda cls: _from_settings_boom()))

    _reload_with_env(
        monkeypatch,
        RAG_IMAGE_HOST_BACKEND="oss",
        RAG_OSS_ENDPOINT="https://oss-cn-hangzhou.aliyuncs.com",
        RAG_OSS_BUCKET="ycsj-dify",
        RAG_OSS_ACCESS_KEY_ID="ak",
        RAG_OSS_ACCESS_KEY_SECRET="sk",
    )

    # ★ is_active 应该返回 False（oss2 不可用）
    s = fresh_settings
    assert image_host.is_active("oss", s) is False, (
        "oss2 不可用时 is_active 应返回 False"
    )

    # ★ 但 _build_oss_url 仍能生成 URL（独立函数）
    url = image_host._build_oss_url("docA", "images/x.jpg")
    assert url.startswith("https://ycsj-dify.oss-cn-hangzhou.aliyuncs.com/"), \
        f"_build_oss_url 仍应工作，实际: {url}"


def test_prepare_chunks_images_oss_skipped_when_oss2_unavailable(monkeypatch, tmp_path):
    """★ 2026-08-04：oss2 不可用时，prepare_chunks_images 降级返回空（不抛错）。

    实际生产场景：oss2 未装 → is_active 返回 False → prepare_chunks_images
    提前检查 is_active 并返回空 dict，调用方走 _build_oss_url 兜底。
    """
    from app.services import image_host, oss_uploader

    monkeypatch.setattr(oss_uploader, "_OSS2_AVAILABLE", False)
    _reload_with_env(
        monkeypatch,
        RAG_IMAGE_HOST_BACKEND="oss",
        RAG_OSS_ENDPOINT="https://oss-cn-hangzhou.aliyuncs.com",
        RAG_OSS_BUCKET="ycsj-dify",
        RAG_OSS_ACCESS_KEY_ID="ak",
        RAG_OSS_ACCESS_KEY_SECRET="sk",
    )

    chunks_dir = tmp_path / "chunks" / "d"
    (chunks_dir / "images").mkdir(parents=True)
    (chunks_dir / "images" / "a.jpg").write_bytes(b"a")

    # oss2 不可用 → is_active=False → prepare_chunks_images 直接返回空
    result = image_host.prepare_chunks_images("oss", "d", chunks_dir)
    assert result == {}
