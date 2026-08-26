"""Pydantic 模型 — API 请求/响应与服务间数据结构。"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import List, Literal, Optional

from pydantic import BaseModel, Field


# ============ 状态枚举 ============


class ProcessStatus(str, Enum):
    """manifest 表（PostgreSQL）中 `status` 列的取值。"""

    NEW = "new"
    PENDING = "pending"
    SCANNING = "scanning"
    PARSING = "parsing"
    PARSED = "parsed"          # §3.2 解析完成
    CHUNKING = "chunking"
    CHUNKED = "chunked"        # §3.3 切分完成
    UPLOADING = "uploading"
    DONE = "done"
    ERROR = "error"


# ============ 文件相关 ============


class FileItem(BaseModel):
    """input/ 或 pending/ 目录下的一个文件。"""

    name: str
    size: int
    mtime: datetime
    md5: Optional[str] = None
    status: Optional[str] = None  # 若 manifest 中存在


class FileAction(str, Enum):
    """单文件扫描动作。"""

    STAGED = "staged"          # 成功从 input/ 移到 pending/
    NEW = "new"                # 新增（无 manifest 行，已插入）
    SKIPPED_DONE = "skipped"   # 已是 done，不动
    COLLISION_RENAMED = "renamed"  # pending/ 同名但 md5 不同，已重命名
    MISSING = "missing"        # manifest 有但 input 缺失（仅日志）
    FAILED = "failed"          # 移动失败
    DRY_RUN = "dry_run"        # 试运行，不实际移动


class FileActionRecord(BaseModel):
    """单文件的扫描动作明细。"""

    filename: str
    action: FileAction
    md5: Optional[str] = None
    from_path: Optional[str] = None
    to_path: Optional[str] = None
    error: Optional[str] = None
    duration_ms: Optional[int] = None


# ============ 解析相关（plan.md §3.2）============


class ParseAction(str, Enum):
    """单文件 MinerU 解析动作。"""

    PARSED = "parsed"                  # 成功解析
    SKIPPED_DONE = "skipped_parsed"    # 已解析过（parse 列非空）
    PARSE_FAILED = "parse_failed"      # 重试耗尽仍失败
    DRY_RUN = "dry_run_parse"          # 试运行（仅识别，不调 API）
    NO_PENDING = "no_pending"          # manifest 中无对应行 / 文件已移走


class ParseActionRecord(BaseModel):
    """单文件解析动作明细。"""

    filename: str
    action: ParseAction
    parse_dir: Optional[str] = None     # 成功时 = data/parsed/{stem}/
    md: Optional[str] = None            # .md 路径
    json_path: Optional[str] = None     # .json 路径
    error: Optional[str] = None
    duration_ms: Optional[int] = None
    attempts: Optional[int] = None       # 实际调用 API 次数（含重试）
    # ★ 2026-08-07：MinerU 解析进度（0-100），用于前端进度条展示
    progress: Optional[int] = None
    progress_msg: Optional[str] = None


class ParseReport(BaseModel):
    """POST /api/parse 的返回。"""

    dry_run: bool
    api_url: str
    scanned: int
    parsed: int
    skipped_done: int
    failed: int
    actions: List[ParseActionRecord] = Field(default_factory=list)


class ParseRequest(BaseModel):
    dry_run: bool = False
    # ★ 2026-08 修复（流水线一致性）：与 chunker / dify 一样支持 force 强制重解析。
    #   force=True 时清空旧 parsed/{stem}/ 目录，重新调 MinerU（仍会触发 PyMuPDF fallback）。
    #   默认 False 保持幂等（parse 列非空 → 跳过）。
    force: bool = False


# ============ 切分相关（plan.md §3.3）============


class ChunkAction(str, Enum):
    """单文件切分动作。"""

    CHUNKED = "chunked"                  # 成功切分
    SKIPPED_DONE = "skipped_chunked"     # 已切分过（chunks 列非空）
    CHUNK_FAILED = "chunk_failed"        # 切分失败
    NO_PARSED = "no_parsed"              # 解析结果缺失
    DRY_RUN = "dry_run_chunk"            # 试运行（仅识别，不实际切分）


class ChunkActionRecord(BaseModel):
    """单文件切分动作明细。"""

    filename: str
    action: ChunkAction
    chunks_dir: Optional[str] = None     # 成功时 = data/chunks/{stem}/
    chunk_count: Optional[int] = None    # 生成的 chunk 文件数
    total_chars: Optional[int] = None    # 所有 chunk 累计字符数
    image_count: Optional[int] = None    # 拷贝到 chunks/images/ 的图片数
    error: Optional[str] = None
    duration_ms: Optional[int] = None


class ChunkReport(BaseModel):
    """POST /api/chunk 的返回。"""

    dry_run: bool
    scanned: int
    chunked: int
    skipped_done: int
    failed: int
    actions: List[ChunkActionRecord] = Field(default_factory=list)


class ChunkRequest(BaseModel):
    dry_run: bool = False
    force: bool = False  # 强制重切（即使 chunks 列已有内容）
    # ★ 2026-08-07：支持指定文件列表（用于批量处理 0807 等已解析文件）
    target_stems: Optional[List[str]] = None
    # ★ 2026-08-24 多策略切分：structure/recursive/fixed/sentence/semantic/
    #   parent_child/late_chunking/llm；空 → 使用配置默认
    strategy: str = ""


class ChunkStrategyOption(BaseModel):
    """GET /api/chunk/strategies 的单条策略。"""

    key: str
    name: str
    desc: str
    default: bool = False


class ChunkStrategyListResponse(BaseModel):
    strategies: List[ChunkStrategyOption]
    default: str


class ChunkSummary(BaseModel):
    """GET /api/chunks 的单条记录。"""

    stem: str
    dir: str
    chunk_count: int
    image_count: int
    total_size: int
    file_count: int


class ChunkFile(BaseModel):
    """GET /api/chunks/{stem}/files 的单条记录。"""

    name: str
    rel_path: str
    size: int
    ext: str
    kind: str  # "chunk" | "image" | "metadata" | "other"


class ChunkMeta(BaseModel):
    """GET /api/chunks/{stem}/chunks 的单条 chunk 元数据。"""

    chunk_id: str            # e.g. "chunk_001"
    file_name: str
    title_path: str          # 完整标题路径
    chunk_type: str          # "cover" | "toc" | "preface" | "body" | "appendix" | "reference" | "single"
    char_count: int
    image_refs: List[str] = Field(default_factory=list)
    is_split: bool = False   # 是否为长章节二次切分
    # ★ 2026-08-24 多策略切分
    strategy: str = ""       # 生成该 chunk 时使用的切分策略
    parent_id: Optional[str] = None  # 父-子切分时子块指向的父块标识


# ============ 扫描报告 ============


class ScanReport(BaseModel):
    """POST /api/scan 的返回。"""

    dry_run: bool
    scanned: int
    staged: int
    new: int
    skipped_done: int
    renamed: int
    missing_on_disk: int
    failed: int
    actions: List[FileActionRecord] = Field(default_factory=list)


class ScanRequest(BaseModel):
    dry_run: bool = False
    # ★ 2026-08 修复（流水线一致性）：与 chunker / parse / dify 一样支持 force 强制重扫描。
    #   force=True 时重新移动 input/ → pending/（已 staged 的会跳过，仅处理新文件）。
    #   默认 False 保持幂等（import_status 非空 → 跳过）。
    force: bool = False


# ============ Manifest ============


class ManifestRow(BaseModel):
    """manifest 表（PostgreSQL）的单行（对应 manifest 表 20 列）。

    字段命名与表头一一对应：用户原 11 列（按用户给的顺序可任意）+
    系统 5 列 + plan.md §3.2 新增 1 列 `parse` + plan.md §3.3 新增 1 列 `chunks`。
    """

    seq: Optional[int] = None
    filename: str
    category_l1: Optional[str] = None
    category_l2: Optional[str] = None
    keywords: Optional[str] = None
    department: Optional[str] = None
    effective_date: Optional[str] = None
    import_status: Optional[str] = None
    process_status: Optional[str] = None
    verified: Optional[str] = None
    process_note: Optional[str] = None
    status: Optional[str] = None
    md5: Optional[str] = None
    create_time: Optional[str] = None
    update_time: Optional[str] = None
    error_msg: Optional[str] = None
    # §3.2 新增：解析状态/路径
    #  - 解析成功：写入 data/parsed/{stem}/ 目录
    #  - 解析失败：写入失败原因
    #  - 试运行：写入"试运行-已识别"
    parse: Optional[str] = None
    # §3.3 新增：切分状态/路径
    #  - 切分成功：写入 data/chunks/{stem}/ 目录
    #  - 切分失败：写入失败原因
    #  - 试运行：写入"试运行-已切分"
    chunks: Optional[str] = None
    # §3.4 新增：Dify 入库状态
    #  - dify_doc_id:  Dify 文档 ID（成功后写入）
    #  - dify_status:  done / error / 空
    dify_doc_id: Optional[str] = None
    dify_status: Optional[str] = None


class ManifestPage(BaseModel):
    total: int
    limit: int
    offset: int
    rows: List[ManifestRow]


class ManifestUpdate(BaseModel):
    """PATCH /api/manifest/{filename} 可更新字段（均为可选，仅更新显式传入的字段）。

    这些列原本靠用户在 Excel 清单里维护（已删除 Excel 依赖），现改为 web 端直接编辑：
    一级分类 / 二级分类 / 关键词 / 部门 / 生效日期，另支持序号、已核对、处理备注。
    """

    seq: Optional[int] = None
    category_l1: Optional[str] = None
    category_l2: Optional[str] = None
    keywords: Optional[str] = None
    department: Optional[str] = None
    effective_date: Optional[str] = None
    verified: Optional[str] = None
    process_note: Optional[str] = None


# ============ 通用响应 ============


class MinerUHealthInfo(BaseModel):
    healthy: bool
    version: Optional[str] = None
    status: str = "unknown"  # "healthy" / "unreachable" / "error"
    detail: str = ""


class HealthInfo(BaseModel):
    status: Literal["ok"] = "ok"
    version: str
    data_root: str
    manifest_exists: bool
    mineru: Optional[MinerUHealthInfo] = None  # MinerU API 健康状态


# ============ Dify 入库（plan.md §3.4）============


class DifyUploadRequest(BaseModel):
    dry_run: bool = False
    force: bool = False  # 强制重传已 done 的文档（默认跳过）
    # ★ 2026-08-07：支持指定文件列表（用于批量处理 0807 等已解析文件）
    target_stems: Optional[List[str]] = None


class DifyActionRecord(BaseModel):
    """单文档入库动作明细。"""

    stem: str
    action: Literal["uploaded", "skipped_done", "failed", "dry_run"]
    dify_doc_id: Optional[str] = None
    chunks_dir: Optional[str] = None
    note: Optional[str] = None
    error: Optional[str] = None
    duration_ms: Optional[int] = None


class DifyUploadReport(BaseModel):
    """POST /api/dify/upload 的返回。"""

    dry_run: bool
    api_url: str
    dataset_id: str
    scanned: int
    uploaded: int
    skipped_done: int
    failed: int
    actions: List[DifyActionRecord] = Field(default_factory=list)


class DifyConfigInfo(BaseModel):
    """GET /api/dify/config 返回当前 Dify 配置（不含 key 全量）。"""

    api_url: str
    dataset_id: str
    has_api_key: bool
    indexing_technique: str
    doc_form: str
    chunks_dir: str
    output_dir: str
    chunk_dir_count: int
    output_dir_count: int = 0  # 已归档（已入库）的目录数


class DifyTestResult(BaseModel):
    """GET /api/dify/test 返回 Dify 连通性测试结果。"""

    ok: bool
    api_url: str
    dataset_id: str
    dataset_name: Optional[str] = None
    doc_count: Optional[int] = None
    elapsed_ms: int = 0
    error: Optional[str] = None
    error_code: Optional[int] = None  # HTTP 状态码


class DifyDatasetItem(BaseModel):
    """Dify 知识库列表条目（GET /api/dify/datasets，供用户选择目标知识库）。"""

    id: str
    name: str
    description: str = ""
    permission: str = "only_me"
    indexing_technique: str = ""
    document_count: int = 0
    created_at: Optional[int] = None


class DifyConfigUpdate(BaseModel):
    """POST /api/dify/config 入参（切换目标知识库）。"""

    dataset_id: Optional[str] = None
