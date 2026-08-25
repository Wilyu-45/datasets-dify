/**
 * 后端 API 客户端 — 所有请求都走 /api/...（Vite 代理到 :8000）。
 */

const BASE = "/api";

async function http<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    headers: { "Content-Type": "application/json" },
    ...init,
  });
  if (!res.ok) {
    const text = await res.text().catch(() => "");
    throw new Error(`${res.status} ${res.statusText} ${text}`);
  }
  return (await res.json()) as T;
}

// ============ 类型 ============

export interface FileItem {
  name: string;
  size: number;
  mtime: string;
  md5?: string | null;
  status?: string | null;
}

export type FileAction =
  | "staged"
  | "new"
  | "skipped"
  | "renamed"
  | "missing"
  | "failed"
  | "dry_run";

export interface FileActionRecord {
  filename: string;
  action: FileAction;
  md5?: string | null;
  from_path?: string | null;
  to_path?: string | null;
  error?: string | null;
  duration_ms?: number | null;
}

export interface ScanReport {
  dry_run: boolean;
  scanned: number;
  staged: number;
  new: number;
  skipped_done: number;
  renamed: number;
  missing_on_disk: number;
  failed: number;
  actions: FileActionRecord[];
}

export interface ManifestRow {
  seq?: number | null;
  filename: string;
  category_l1?: string | null;
  category_l2?: string | null;
  keywords?: string | null;
  department?: string | null;
  effective_date?: string | null;
  import_status?: string | null;
  process_status?: string | null;
  verified?: string | null;
  process_note?: string | null;
  status?: string | null;
  md5?: string | null;
  create_time?: string | null;
  update_time?: string | null;
  error_msg?: string | null;
  parse?: string | null;
  // §3.3 新增：切分列
  chunks?: string | null;
  // §3.4 新增：Dify 入库列
  dify_doc_id?: string | null;
  dify_status?: string | null;
}

export interface ManifestPage {
  total: number;
  limit: number;
  offset: number;
  rows: ManifestRow[];
}

export interface HealthInfo {
  status: "ok";
  version: string;
  data_root: string;
  manifest_exists: boolean;
}

// ============ §3.2 解析相关 ============

export type ParseAction =
  | "parsed"
  | "skipped_parsed"
  | "parse_failed"
  | "dry_run_parse"
  | "no_pending";

export interface ParseActionRecord {
  filename: string;
  action: ParseAction;
  parse_dir?: string | null;
  md?: string | null;
  json_path?: string | null;
  error?: string | null;
  duration_ms?: number | null;
  attempts?: number | null;
  /** ★ 2026-08-07：MinerU 解析进度（0-100），用于前端进度条展示 */
  progress?: number | null;
  /** ★ 2026-08-07：MinerU 解析进度描述（如"调用 API 中..."、"解压产物..."） */
  progress_msg?: string | null;
}

/** ★ 2026-08-07：解析进度查询（实时进度条） */
export interface ParseProgressItem {
  progress: number;  // 0-100
  msg: string;       // 进度描述
  status: "parsing" | "done" | "failed";
}

export type ParseProgressMap = Record<string, ParseProgressItem>;

/** ★ 2026-08-07：轮询解析进度 */
export const getParseProgress = async (): Promise<ParseProgressMap> => {
  return http<ParseProgressMap>("/parse/progress");
};

export interface ParseReport {
  dry_run: boolean;
  api_url: string;
  scanned: number;
  parsed: number;
  skipped_done: number;
  failed: number;
  actions: ParseActionRecord[];
}

export interface ParsedDirItem {
  stem: string;
  dir: string;
  md: string | null;
  json: string | null;
  image_count: number;
  total_size: number;
  file_count: number;
}

export interface ParsedFileItem {
  name: string;
  rel_path: string;
  size: number;
  ext: string;
}

// ============ 接口 ============

export const health = () => http<HealthInfo>("/health");

export const listFiles = (dir: "input" | "pending") =>
  http<FileItem[]>(`/files?dir=${dir}`);

export const getManifest = (limit = 50, offset = 0) =>
  http<ManifestPage>(`/manifest?limit=${limit}&offset=${offset}`);

export const triggerScan = (dryRun: boolean, force = false) =>
  http<ScanReport>("/scan", {
    method: "POST",
    body: JSON.stringify({ dry_run: dryRun, force }),
  });

export const triggerParse = (dryRun: boolean, force = false) =>
  http<ParseReport>("/parse", {
    method: "POST",
    body: JSON.stringify({ dry_run: dryRun, force }),
  });

export const listParsed = () => http<ParsedDirItem[]>("/parsed");

export const listParsedFiles = (stem: string) =>
  http<ParsedFileItem[]>(`/parsed/${encodeURIComponent(stem)}/files`);

// ============ §3.3 切分相关 ============

export type ChunkAction =
  | "chunked"
  | "skipped_chunked"
  | "chunk_failed"
  | "no_parsed"
  | "dry_run_chunk";

export interface ChunkActionRecord {
  filename: string;
  action: ChunkAction;
  chunks_dir?: string | null;
  chunk_count?: number | null;
  total_chars?: number | null;
  image_count?: number | null;
  error?: string | null;
  duration_ms?: number | null;
}

export interface ChunkReport {
  dry_run: boolean;
  scanned: number;
  chunked: number;
  skipped_done: number;
  failed: number;
  actions: ChunkActionRecord[];
}

export interface ChunkSummary {
  stem: string;
  dir: string;
  chunk_count: number;
  image_count: number;
  total_size: number;
  file_count: number;
}

export interface ChunkFile {
  name: string;
  rel_path: string;
  size: number;
  ext: string;
  kind: string; // "chunk" | "image" | "metadata" | "other"
}

export interface ChunkMeta {
  chunk_id: string;
  file_name: string;
  title_path: string;
  chunk_type: string; // cover / toc / preface / body / appendix / reference / single / parent
  char_count: number;
  image_refs: string[];
  is_split: boolean;
  // ★ 2026-08-24 多策略切分
  strategy: string; // structure / recursive / fixed / sentence / semantic / parent_child / late_chunking / llm
  parent_id?: string | null; // 父-子切分时子块指向的父块
}

export interface ChunkStrategyOption {
  key: string;
  name: string;
  desc: string;
  default: boolean;
}

export interface ChunkStrategyListResponse {
  strategies: ChunkStrategyOption[];
  default: string;
}

export const listChunkStrategies = () =>
  http<ChunkStrategyListResponse>("/chunk/strategies");

/** 切分策略及相关配置变量（来自 backend/.env 集中配置）。 */
export interface ChunkConfig {
  strategy: string;
  target_chars: number;
  split_target: number;
  overlap: number;
  hard_limit: number;
  appendix_threshold: number;
  max_images_per_segment: number;
  table_row_threshold: number;
  table_max_chars: number;
  fixed_size_chars: number;
  fixed_overlap_chars: number;
  semantic_threshold: number;
  parent_size_chars: number;
  child_size_chars: number;
  llm_enabled: boolean;
}

/** 查看当前切分策略配置。 */
export const getChunkConfig = () => http<ChunkConfig>("/chunk/config");

/** 保存默认切分策略（写回 backend/.env 的 RAG_CHUNK_STRATEGY 并热更新）。 */
export const saveChunkConfig = (strategy: string) =>
  http<{ strategy: string }>("/chunk/config", {
    method: "POST",
    body: JSON.stringify({ strategy }),
  });

export interface ChunkPreview {
  stem: string;
  chunk_id: string;
  file_name: string;
  content: string;
}

export const triggerChunk = (
  dryRun: boolean,
  force: boolean,
  strategy?: string
) =>
  http<ChunkReport>("/chunk", {
    method: "POST",
    body: JSON.stringify({ dry_run: dryRun, force, strategy: strategy ?? "" }),
  });

export const listChunks = () => http<ChunkSummary[]>("/chunks");

export const listChunkFiles = (stem: string) =>
  http<ChunkFile[]>(`/chunks/${encodeURIComponent(stem)}/files`);

export const listChunkMeta = (stem: string) =>
  http<ChunkMeta[]>(`/chunks/${encodeURIComponent(stem)}/chunks`);

export const previewChunk = (stem: string, chunkId: string) =>
  http<ChunkPreview>(
    `/chunks/${encodeURIComponent(stem)}/preview/${encodeURIComponent(chunkId)}`
  );

// ============ §3.4 Dify 入库相关 ============

export type DifyAction = "uploaded" | "skipped_done" | "failed" | "dry_run";

export interface DifyActionRecord {
  stem: string;
  action: DifyAction;
  dify_doc_id?: string | null;
  chunks_dir?: string | null;
  note?: string | null;
  error?: string | null;
  duration_ms?: number | null;
}

export interface DifyUploadReport {
  dry_run: boolean;
  api_url: string;
  dataset_id: string;
  scanned: number;
  uploaded: number;
  skipped_done: number;
  failed: number;
  actions: DifyActionRecord[];
}

export interface DifyConfigInfo {
  api_url: string;
  dataset_id: string;
  has_api_key: boolean;
  indexing_technique: string;
  doc_form: string;
  chunks_dir: string;
  output_dir: string;
  chunk_dir_count: number;
  output_dir_count: number;
}

export const getDifyConfig = () => http<DifyConfigInfo>("/dify/config");

/** Dify 知识库条目（用户选择目标知识库用）。 */
export interface DifyDatasetItem {
  id: string;
  name: string;
  description: string;
  permission: string;
  indexing_technique: string;
  document_count: number;
  created_at: number | null;
}

export const listDifyDatasets = () => http<DifyDatasetItem[]>("/dify/datasets");

export const updateDifyDatasetId = (datasetId: string) =>
  http<DifyConfigInfo>("/dify/config", {
    method: "POST",
    body: JSON.stringify({ dataset_id: datasetId }),
  });

export const triggerDifyUpload = (dryRun: boolean, force: boolean) =>
  http<DifyUploadReport>("/dify/upload", {
    method: "POST",
    body: JSON.stringify({ dry_run: dryRun, force }),
  });

/** 别名：与 ParseReport/ChunkReport 命名风格保持一致 */
export type DifyReport = DifyUploadReport;

// ============ §3.0 一键流水线 ============

export type PipelineStatus = "ok" | "partial" | "failed" | "skipped" | "pending";

export interface PipelineReport {
  status: PipelineStatus;
  dry_run: boolean;
  duration_ms: number;
  step_timings_ms: Record<string, number>;
  scan?: ScanReport;
  parse?: ParseReport;
  chunk?: ChunkReport;
  dify?: DifyUploadReport;
  error?: string | null;
}

export interface PipelineStepIn {
  enabled: boolean;
  dry_run: boolean;
  force: boolean;
  strategy?: string; // chunk 阶段：切分策略（structure/fixed/semantic/parent_child 等）
}

export interface PipelineRunBody {
  scan: PipelineStepIn;
  parse: PipelineStepIn;
  chunk: PipelineStepIn;
  dify: PipelineStepIn;
  stop_on_error: boolean;
}

/** 触发一键流水线（前端 3.0 入口）。 */
export const triggerPipeline = (dryRun: boolean, force = false, strategy?: string) =>
  http<PipelineReport>("/pipeline/run", {
    method: "POST",
    body: JSON.stringify({
      // ★ 2026-08 修复（流水线一致性）：force 标志对所有阶段都生效
      //   scan/parse/chunk/dify 都传 force，与手动单步的"强制"开关行为一致
      scan: { enabled: true, dry_run: dryRun, force },
      parse: { enabled: true, dry_run: dryRun, force },
      chunk: { enabled: true, dry_run: dryRun, force, strategy: strategy ?? "" },
      dify: { enabled: true, dry_run: dryRun, force },
      stop_on_error: false,
    }),
  });

// ============ §3.5 人工校验相关 ============

export interface DifyDocumentItem {
  id: string;
  name: string;
  indexing_status: string;
  enabled: boolean;
  word_count?: number | null;
  created_at?: number | null;
  display_position?: number | null;
}

export interface DifySegmentAttachment {
  id: string;
  name?: string;
  source_url?: string;
  url?: string;
  mime_type?: string;
  size?: number;
  extension?: string;
}

export interface DifySegmentItem {
  id: string;
  document_id: string;
  position: number;
  content: string;
  word_count: number;
  tokens: number;
  status: string;
  enabled: boolean;
  attachments: DifySegmentAttachment[];
}

/** 列出 Dify 数据集的所有文档（人工校验左栏） */
export const listDifyDocuments = (
  params: { page?: number; limit?: number; keyword?: string } = {}
) => {
  const qs = new URLSearchParams();
  if (params.page) qs.set("page", String(params.page));
  if (params.limit) qs.set("limit", String(params.limit));
  if (params.keyword) qs.set("keyword", params.keyword);
  const q = qs.toString();
  return http<DifyDocumentItem[]>(`/dify/documents${q ? `?${q}` : ""}`);
};

/** 列出某文档的所有分段（人工校验中栏） */
export const listDifySegments = (
  docId: string,
  params: { keyword?: string; status?: string } = {}
) => {
  const qs = new URLSearchParams();
  if (params.keyword) qs.set("keyword", params.keyword);
  if (params.status) qs.set("status", params.status);
  const q = qs.toString();
  return http<DifySegmentItem[]>(
    `/dify/documents/${encodeURIComponent(docId)}/segments${q ? `?${q}` : ""}`
  );
};

/** 更新单个分段（人工校验保存按钮） */
export const updateDifySegment = (
  docId: string,
  segId: string,
  body: { content?: string; enabled?: boolean }
) =>
  http<{ data: unknown; document: unknown }>(
    `/dify/documents/${encodeURIComponent(docId)}/segments/${encodeURIComponent(segId)}`,
    {
      method: "POST",
      body: JSON.stringify(body),
    }
  );

// ============ §3.x 单文件上传 + 一键入库 ============

export interface SingleUploadResponse {
  filename: string;
  stem: string;
  md5: string;
  size: number;
  saved_path: string;
  manifest_row_added: boolean;
  pipeline: PipelineReport | null;
  error: string | null;
}

/**
 * 单文件上传 + 一键入库（multipart/form-data）。
 *
 * @param file 待入库文件（PDF / DOCX / DOC / PPTX / XLSX）
 * @param auto_ingest 上传后是否自动触发 parse + chunk + dify 全流程
 * @returns 上传结果 + 流水线 Report
 */
export const uploadSingleFile = async (
  file: File,
  autoIngest: boolean
): Promise<SingleUploadResponse> => {
  const form = new FormData();
  form.append("file", file);
  form.append("auto_ingest", String(autoIngest));
  const res = await fetch(`${BASE}/upload/single`, {
    method: "POST",
    body: form,
  });
  if (!res.ok) {
    const text = await res.text().catch(() => "");
    throw new Error(`${res.status} ${res.statusText} ${text}`);
  }
  return (await res.json()) as SingleUploadResponse;
};

/** 对已上传文件触发全流程入库（不重新上传文件） */
export const ingestSingleFile = (filename: string) =>
  http<PipelineReport>(
    `/upload/single/ingest?filename=${encodeURIComponent(filename)}`,
    { method: "POST" }
  );

// ============ §3.x 批量文件上传 + 一键入库（2026-08 新增）============

/**
 * 批量上传时，每个文件对应的 per-file pipeline 摘要。
 *
 * 与 PipelineReport 不同：批量端点用一次 run_pipeline 跑所有文件，再按 stem 拆分。
 * 单文件 per-file summary 只需要保留核心状态：parse / chunk / dify 单条记录 + 整体状态。
 */
export interface BatchItemPipelineSummary {
  status: "ok" | "partial" | "failed" | "skipped" | "pending";
  parse: ParseActionRecord | null;
  chunk: ChunkActionRecord | null;
  dify: DifyActionRecord | null;
  error: string | null;
}

/** 批量上传时，items[i].pipeline 字段的类型。 */
export interface BatchItemResponse {
  filename: string;
  stem: string;
  md5: string;
  size: number;
  saved_path: string;
  manifest_row_added: boolean;
  pipeline: BatchItemPipelineSummary | null;
  error: string | null;
}

export interface BatchUploadResponse {
  total: number;
  succeeded: number;
  failed: number;
  duration_ms: number;
  items: BatchItemResponse[];
  /** 整批 PipelineReport（与 triggerPipeline 返回结构一致） */
  pipeline: PipelineReport | null;
}

/**
 * 批量文件上传 + 一键入库（multipart/form-data，2026-08 新增）。
 *
 * 一次上传多个文件，后端会把每个文件保存到 pending/，
 * 再用 target_stems=[s1, s2, ...] 一次跑 parse + chunk + dify 全流程。
 * 1 个文件保存/移动失败不影响其他文件（其他文件继续处理）。
 *
 * @param files 待入库文件列表（PDF / DOCX / DOC / PPTX / XLSX）
 * @param autoIngest 上传后是否自动触发 parse + chunk + dify 全流程入库
 * @returns 整批汇总结果 + 每个文件的 per-file summary + 整批 PipelineReport
 */
export const uploadBatchFiles = async (
  files: File[],
  autoIngest: boolean
): Promise<BatchUploadResponse> => {
  const form = new FormData();
  for (const f of files) {
    form.append("files", f);
  }
  form.append("auto_ingest", String(autoIngest));
  const res = await fetch(`${BASE}/upload/batch`, {
    method: "POST",
    body: form,
  });
  if (!res.ok) {
    const text = await res.text().catch(() => "");
    throw new Error(`${res.status} ${res.statusText} ${text}`);
  }
  return (await res.json()) as BatchUploadResponse;
};
