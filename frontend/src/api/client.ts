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

// ============ 配置中心（2026-08 新增）============

/** 可配置字段定义（后端 config_store.PROFILE_FIELDS 动态下发，前端据此渲染表单）。 */
export interface ConfigFieldDef {
  key: string;
  label: string;
  type: "int" | "float" | "bool" | "str" | "select_dataset" | "select_strategy";
  default: number | boolean | string;
  description?: string;
  min?: number;
  max?: number;
  step?: number;
  /** 该字段生效的切分策略列表；不填/空数组表示所有策略通用。 */
  strategies?: string[];
}

/** 一个配置方案 = 知识库 ID + 切分策略 + 全部切分参数。 */
export interface ConfigProfile {
  id: string;
  name: string;
  created_at: string;
  updated_at: string;
  config: Record<string, number | boolean | string>;
}

export interface ConfigProfilesResponse {
  profiles: ConfigProfile[];
  active_profile_id: string | null;
}

export interface ActiveConfigResponse {
  profile: ConfigProfile | null;
  fields: ConfigFieldDef[];
}

export interface ConfigSchemaResponse {
  fields: ConfigFieldDef[];
}

/** 列出所有配置方案 + 当前激活 id。 */
export const listConfigProfiles = () =>
  http<ConfigProfilesResponse>("/config/profiles");

/** 创建配置方案。 */
export const createConfigProfile = (
  name: string,
  config: Record<string, number | boolean | string>
) =>
  http<ConfigProfile>("/config/profiles", {
    method: "POST",
    body: JSON.stringify({ name, config }),
  });

/** 更新配置方案。 */
export const updateConfigProfile = (
  profileId: string,
  body: { name?: string; config?: Record<string, number | boolean | string> }
) =>
  http<ConfigProfile>(`/config/profiles/${encodeURIComponent(profileId)}`, {
    method: "PUT",
    body: JSON.stringify(body),
  });

/** 删除配置方案。 */
export const deleteConfigProfile = (profileId: string) =>
  http<{ ok: boolean; active_profile_id: string | null }>(
    `/config/profiles/${encodeURIComponent(profileId)}`,
    { method: "DELETE" }
  );

/** 激活配置方案。 */
export const activateConfigProfile = (profileId: string) =>
  http<ConfigProfile>(`/config/profiles/${encodeURIComponent(profileId)}/activate`, {
    method: "POST",
  });

/** 当前激活配置方案 + 字段定义（前端「当前配置」卡片用）。 */
export const getActiveConfig = () => http<ActiveConfigResponse>("/config/active");

/** 可配置字段定义（配置中心表单用）。 */
export const getConfigSchema = () => http<ConfigSchemaResponse>("/config/schema");

/** 一条处理配置记录：每次实际触发处理时落库的配置快照（process_config_log 表）。 */
export interface RunConfigLogItem {
  id: number;
  run_time?: string | null;
  source?: string | null;
  profile_id?: string | null;
  profile_name?: string | null;
  /** 本次实际写入的 Dify 知识库 ID（独立列，来自运行时快照）。 */
  dataset_id?: string | null;
  /** 本次实际使用的切分策略（独立列，来自运行时快照）。 */
  chunk_strategy?: string | null;
  /** 当时生效的全部配置项（API Key 已脱敏为 ******）。 */
  config: Record<string, number | boolean | string>;
  /** 本批处理的目标文件 stem 列表。 */
  target_stems: string[];
  status?: string | null;
  error?: string | null;
  duration_ms?: number | null;
}

export interface RunConfigLogsResponse {
  total: number;
  rows: RunConfigLogItem[];
}

/** 最近的处理配置记录（按时间倒序，默认 50 条）。 */
export const listRunConfigLogs = (limit = 50) =>
  http<RunConfigLogsResponse>(`/config/run-logs?limit=${limit}`);

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

export const getManifest = (limit = 50, offset = 0) =>
  http<ManifestPage>(`/manifest?limit=${limit}&offset=${offset}`);

/** PATCH /api/manifest/{filename} 可更新的元数据字段（web 端编辑，替代原 Excel 填列）。 */
export interface ManifestUpdateFields {
  seq?: number | null;
  category_l1?: string | null;
  category_l2?: string | null;
  keywords?: string | null;
  department?: string | null;
  effective_date?: string | null;
  verified?: string | null;
  process_note?: string | null;
}

/** 更新清单行元数据（仅更新传入字段，其余列保持不变）。 */
export const updateManifestRow = (
  filename: string,
  fields: ManifestUpdateFields
) =>
  http<ManifestRow>(`/manifest/${encodeURIComponent(filename)}`, {
    method: "PATCH",
    body: JSON.stringify(fields),
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
      // ★ 2026-08 起只支持「上传文档」驱动处理：
      //   scan 步骤默认禁用（不再扫描 input/ 目录），
      //   文档上传时已自动登记进清单，流水线直接解析/切分/入库。
      //   force 标志对所有阶段都生效，与手动单步的"强制"开关行为一致。
      scan: { enabled: false, dry_run: dryRun, force },
      parse: { enabled: true, dry_run: dryRun, force },
      chunk: { enabled: true, dry_run: dryRun, force, strategy: strategy ?? "" },
      dify: { enabled: true, dry_run: dryRun, force },
      stop_on_error: false,
    }),
  });

// ============ §3.x 网站抓取（2026-08 新增：知识库内容外延，两步式） ============

/** 单个 URL 的抓取结果（任务项）。 */
export interface WebScrapeItem {
  url: string;
  ok: boolean;
  /** content=网页正文 / attachment=附件文件 */
  kind: string;
  /** content：页面标题；attachment：文件名 stem */
  title: string;
  /** attachment：原始文件名 */
  filename?: string | null;
  /** 相对 data/webscrape/{task_id}/ 的路径 */
  rel_path?: string | null;
  /** content：正文字符数 */
  char_count?: number | null;
  /** attachment：文件大小（字节） */
  size?: number | null;
  /** 正文是否超长截断 */
  truncated?: boolean;
  /** 是否已确认（confirm 后回填） */
  confirmed?: boolean;
  /** confirm 后：ok / error */
  ingest_status?: string | null;
  ingest_error?: string | null;
  /** 抓取失败原因 */
  error?: string | null;
}

/** 抓取任务（含 items 明细）。 */
export interface WebScrapeTask {
  id: string;
  created_at: string;
  updated_at?: string | null;
  profile_id?: string | null;
  profile_name?: string | null;
  /** 抓取网站的配置快照 */
  site_url?: string | null;
  /** pending=待确认 / confirmed=已确认并触发流水线 */
  status: string;
  confirm_time?: string | null;
  confirm_profile?: string | null;
  items: WebScrapeItem[];
  total: number;
  ok_count: number;
  confirmed_count: number;
}

/** 单项预览内容（网页正文全文 / 附件元信息）。 */
export interface WebScrapePreviewResponse {
  url: string;
  kind: string;
  title: string;
  filename?: string | null;
  content?: string | null;
  size?: number | null;
}

/**
 * 第一步：选配置 + URL 列表 → 抓取生成「待确认任务」（不登记 manifest、不入库）。
 *
 * 配置方案必填：其「抓取网站 URL」字段决定本批可抓取的网站（同域名白名单）；
 * 网页正文转 Markdown、附件文件下载，都先落在后端临时区，确认后才入库。
 */
export const runWebScrape = (
  profileId: string,
  urls: string[]
): Promise<{ task: WebScrapeTask; error: string | null }> =>
  http<{ task: WebScrapeTask; error: string | null }>("/webscrape/run", {
    method: "POST",
    body: JSON.stringify({ profile_id: profileId, urls }),
  });

/** 任务历史列表（不含 items 明细）。 */
export interface WebScrapeTaskListItem extends Omit<WebScrapeTask, "items"> {
  total: number;
  ok_count: number;
  confirmed_count: number;
}

export const listWebScrapeTasks = (limit = 20) =>
  http<{ total: number; tasks: WebScrapeTaskListItem[] }>(
    `/webscrape/tasks?limit=${limit}`
  );

/** 任务详情（含逐项状态，供预览页渲染）。 */
export const getWebScrapeTask = (taskId: string) =>
  http<WebScrapeTask>(`/webscrape/task/${encodeURIComponent(taskId)}`);

/** 预览任务中某一项（网页正文返回 Markdown 全文；附件返回文件信息）。 */
export const previewWebScrapeItem = (taskId: string, index: number) =>
  http<WebScrapePreviewResponse>(
    `/webscrape/task/${encodeURIComponent(taskId)}/preview/${index}`
  );

/**
 * 第二步：人为确认勾选内容 + 选择确认入库用的配置。
 * 后端把选中项落地（正文 → parsed/，附件 → pending/）并登记 manifest，
 * 再走 parse(MinerU) → chunk → dify 流水线。
 */
export const confirmWebScrapeTask = (
  taskId: string,
  urls: string[],
  profileId: string
): Promise<{
  task: WebScrapeTask;
  landed: {
    url: string;
    kind: string;
    stem?: string | null;
    filename?: string | null;
    ok: boolean;
    error?: string | null;
  }[];
  pipeline: PipelineReport | null;
  error: string | null;
}> =>
  http<{
    task: WebScrapeTask;
    landed: {
      url: string;
      kind: string;
      stem?: string | null;
      filename?: string | null;
      ok: boolean;
      error?: string | null;
    }[];
    pipeline: PipelineReport | null;
    error: string | null;
  }>(`/webscrape/task/${encodeURIComponent(taskId)}/confirm`, {
    method: "POST",
    body: JSON.stringify({ urls, profile_id: profileId }),
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
  autoIngest: boolean,
  profileId?: string
): Promise<SingleUploadResponse> => {
  const form = new FormData();
  form.append("file", file);
  form.append("auto_ingest", String(autoIngest));
  if (profileId) form.append("profile_id", profileId);
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
export const ingestSingleFile = (filename: string, profileId?: string) =>
  http<PipelineReport>(
    `/upload/single/ingest?filename=${encodeURIComponent(filename)}${
      profileId ? `&profile_id=${encodeURIComponent(profileId)}` : ""
    }`,
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
  autoIngest: boolean,
  profileId?: string
): Promise<BatchUploadResponse> => {
  const form = new FormData();
  for (const f of files) {
    form.append("files", f);
  }
  form.append("auto_ingest", String(autoIngest));
  if (profileId) form.append("profile_id", profileId);
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
