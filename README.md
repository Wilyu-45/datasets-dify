# RAG 批量入库系统

面向企业知识库的 **文档批量入库工具**：将 PDF / DOCX / 扫描件等文档，经 **上传 → MinerU 解析 → 多策略切分 → Dify 入库** 全流程自动化，构建高质量 RAG 知识库。内置 **8 种切分策略**，支持单文件 / 批量上传即点即用（上传自动登记台账并全流程入库）。

## 核心功能

### 3.0 入库工作台（Pipeline，默认首页）
**上传即处理**：前端批量选择文件 → 后端保存到 `data/single_uploads/` 中转区 → 自动登记 manifest 台账 → 移入 `data/pending/` → 自动触发 **解析 → 切分 → Dify 入库** 全流程（以 `target_stems` 白名单只处理本批文件，1 个文件失败不影响其他）。处理前需在「配置中心」配置知识库 ID 与切分策略并激活配置方案，未配置则拒绝处理并提示。

> 后端 `POST /api/pipeline/run` 仍保留四步独立开关（`scan` 步骤默认不再使用）、`dry_run`（纯本地预检）、`stop_on_error`（失败即停）、`target_stems`（文件白名单）。

### 3.1 文件扫描（Scan）
扫描 `data/input` 目录，识别待处理文档，增量更新 PostgreSQL `manifest` 表（文件清单台账）。主流程已改为 **上传驱动**（见 3.0，上传时自动登记台账），`scan` 作为辅助能力保留，前端上传链路默认关闭该步骤。

### 3.2 MinerU 解析（Parse）
调用本地部署的 **MinerU API**（`POST /file_parse`）解析文档：
- 默认 `hybrid-engine`（VLM + 文本提取，高精度），可切换 `vlm-engine`（极致精度）或纯 OCR `pipeline`
- 以 ZIP 形式一次性取回 `.md` / `middle.json` / 页面图片 / layout 等全部产物，一文档一文件夹
- **长文档路由**：PDF 页数超阈值自动切到 `vlm-engine`，避免复杂 CMap / 编码 OOM
- **强制高质量**：`enforce_high_quality` 开启时自动把 `pipeline` 后端升级为 `hybrid-engine`
- **解析细节可配**：语言列表、公式 / 表格开关、输出产物开关（.md / middle.json / content_list / 图片）
- **超时与自适应重试**：单次请求超时、最大重试次数、指数退避初始等待 / 倍数 / 上限均可配置，兼容 API 端 OOM 重启场景
- **`.doc` 预检测**：读取 magic bytes 提前拒绝旧 OLE 格式，给出清晰报错

### 3.3 文档切分（Chunk）
内置 **8 种切分策略**，可全局配置或按请求指定（`/api/chunk` 请求体 `strategy` 字段）：

| 策略 key | 名称 | 说明 |
| :--- | :--- | :--- |
| `structure` | 结构切分（默认） | 按标题层级 + 段落贪心合并 + 句号二次切分，成熟稳健，适合大多数结构化文档 |
| `recursive` | 递归切分 | 段落 → 句子递归分隔符切分，语义完整性与块大小平衡好 |
| `fixed` | 固定长度切分 | 按固定字符数硬切（可配 overlap），适合日志/代码或基准测试 |
| `sentence` | 句子级切分 | 按句末标点切分，保留最自然的语义边界 |
| `semantic` | 语义切分 | 基于 Embedding 相邻相似度，在主题转变处切分（需 Dify embedding，失败自动降级为句子级） |
| `parent_child` | 父-子切分 | 父块（完整上下文）+ 子块（精准检索单元），子块通过 `parent_id` 关联父块 |
| `late_chunking` | 晚切分 | 先整文 Embedding 感知全局上下文，再按句子主题相关度切分（需 Dify embedding） |
| `llm` | LLM 切分 | 大模型自主决定切分点（默认关闭，需 `RAG_CHUNK_LLM_ENABLED=true` + `RAG_LLM_API_BASE_URL`/`RAG_LLM_API_KEY`/`RAG_LLM_MODEL`） |

所有策略均保留 **特殊内容保护**（Markdown 表格 / LaTeX 公式 / 图片不可被切断），`cover/toc/preface/reference` 区域保持原有专用逻辑。通用保护参数：单 chunk 绝对硬切上限、附录并入阈值、单段图片数量上限、大表格行列压缩阈值等（见配置说明）。

### 3.4 Dify 入库（Dify）
将切分产物批量上传至 Dify 知识库（Knowledge API）：文档级去重、批次上传（默认 30 段/批）、上传报告汇总；图片经 **阿里云 OSS** 托管为公网 URL 后随文档上传，避免内网 tunnel 链接失效。
- 图片附件上传使用 **App API Key**（`app-` 前缀；Knowledge Key 无 `/files/upload` 权限）；`dify_skip_file_upload=true` 时可完全跳过附件上传，正文只写 OSS 永久 URL
- 写入 Dify 后**轮询等待索引完成**（超时 / 间隔可配置），单段超长内容保护性截断后再写入

### 3.5 人工校验（Verify）
从 Dify 知识库拉取已入库文档与分段，三栏布局供人工抽查校验，编辑结果**写回 Dify**：
- **左栏**：文档列表（搜索文档名 / ID / 状态，禁用标记）；
- **中栏**：分段列表（字数、含图标记、内容摘要、按内容 / ID 搜索）；
- **右栏**：分段详情，支持**编辑** `content`（Markdown 原文）与 `enabled` 开关，保存后写回 Dify；
  - **渲染预览**：Markdown 实时渲染，正文中的 `![](url)` 图片**直接渲染显示**（不再需要单独的图片附件页）；
  - 元数据：Segment / Document ID、Position、字数、Tokens、状态、附件数、复制原文。

### 3.6 配置中心（Config）
- **多配置方案**：可创建多套「知识库 ID + 切分策略 + 全部切分参数」方案，选择其一**激活**后，上传 / 流水线 / 切分自动使用激活方案（也可在请求中显式指定方案）。
- **按策略动态配置**：选择不同切分策略时，表单只显示该策略相关的配置项（例如 `fixed` 只显示固定长度与重叠，`parent_child` 只显示父子块大小，`llm` 只显示 LLM API 地址 / API Key / 模型名 / 切分提示词），切换策略已填参数保留。
- 处理入口（工作台上传 / 各产物页）会展示当前使用的配置方案；未配置方案时上传会提示先到配置中心配置并激活。
- **处理配置记录**：每次实际触发处理（单文件 / 批量上传入库、重跑入库、`/api/pipeline/run`）时，把当时生效的配置快照（配置方案 ID / 名称 + 全部配置项 JSONB + 目标文件 + 结果状态 + 耗时）写入 PostgreSQL `process_config_log` 表，配置方案事后修改不影响已落库的快照；API Key 类字段（`llm_api_key` / `chunk_embedding_api_key`）落库前脱敏。前端配置中心新增「处理配置记录」卡片，后端提供 `GET /api/config/run-logs?limit=50` 查询，用于追溯「这批文档当时是用什么配置处理的」。

### 单文件上传 + 一键入库
`/api/upload/single` / `/api/upload/batch` 直接上传 PDF / DOCX / DOC / PPTX / XLSX / HTML 等，随即触发全流程入库（`target_stems` 白名单），只处理本批文件，不影响 manifest / chunks 中其他文档。
- `auto_ingest`：默认 `true` 自动触发流水线；`false` 则只保存到 `pending/` 不触发
- `profile_id`：指定配置方案 ID；为空使用当前激活方案，未配置任何方案则 400 拒绝
- 单个文件失败不影响同批其他文件；`/api/upload/single/ingest` 可对已上传文件单独重跑入库

## 技术栈

- **后端**：Python 3 + FastAPI + Pydantic v2 + psycopg3（PostgreSQL）+ oss2（阿里云 OSS）+ PyMuPDF + httpx
- **前端**：React 18 + TypeScript + Vite + Ant Design 5
- **解析引擎**：MinerU（本地 FastAPI 服务，hybrid-engine / vlm-engine）
- **知识库**：Dify（Cloud API / Knowledge API）

## 目录结构

```
ragsystem/
├── backend/                    # FastAPI 后端
│   ├── app/
│   │   ├── main.py             # 应用入口（注册全部路由 + 静态托管）
│   │   ├── config.py           # 配置（RAG_ 前缀环境变量，见 .env.example）
│   │   ├── db.py               # PostgreSQL 连接池（psycopg3）
│   │   ├── logging_config.py   # 日志配置
│   │   ├── models/schemas.py   # Pydantic 请求/响应模型
│   │   ├── api/                # 路由：health / files / manifest / scan / parse /
│   │   │                       #       parse_progress / chunk / config / dify /
│   │   │                       #       pipeline / upload
│   │   └── services/           # 业务逻辑
│   │       ├── scanner.py          # 3.1 扫描
│   │       ├── mineru_client.py    # 3.2 MinerU API 客户端
│   │       ├── parser.py           # 3.2 解析编排（长文档路由 / 重试）
│   │       ├── pdf_fallback.py     # 3.2 PDF 切分降级（PyMuPDF）
│   │       ├── parse_progress.py   # 3.2 解析进度
│   │       ├── chunker.py          # 3.3 结构切分（默认策略）
│   │       ├── chunk_strategies.py # 3.3 多策略切分引擎（8 策略）
│   │       ├── config_store.py     # 3.6 配置中心（多方案持久化）
│   │       ├── config_run_log.py   # 3.6 处理配置记录（process_config_log 表）
│   │       ├── dify_ingest.py      # 3.4 Dify 入库编排
│   │       ├── dify_uploader.py    # 3.4 Dify 上传（分段 / 索引轮询）
│   │       ├── image_host.py       # 3.4 图片托管抽象
│   │       ├── oss_uploader.py     # 3.4 阿里云 OSS 上传
│   │       ├── hasher.py           # 文件 MD5
│   │       ├── doc_metadata.py     # 文档元数据（PostgreSQL）
│   │       ├── manifest_store.py   # manifest 台账（PostgreSQL）
│   │       └── pipeline.py         # 3.0 入库工作台流水线
│   ├── requirements.txt
│   └── .env.example            # 环境变量模板
├── frontend/                   # React 前端
│   └── src/
│       ├── App.tsx             # 布局与路由（5 个页面）
│       ├── pages/              # Pipeline(入库工作台) / Parse(解析产物) /
│       │                       # Chunk(切分产物) / Verify(人工校验) / Config(配置中心)
│       └── components/         # ActiveConfigCard / BatchFileUpload / ChunkDetail /
│                               # ChunksTable / DifyReportTable / ManifestTable /
│                               # MarkdownPreview / ParsedTable
├── data/                       # 运行时数据（git 忽略）
│   ├── input/                  # 待处理文档（辅助扫描）
│   ├── single_uploads/         # 上传中转区
│   ├── pending/                # 已登记待解析
│   ├── parsed/                 # MinerU 解析产物
│   ├── chunks/                 # 切分产物
│   ├── output/                 # 入库输出（图片静态托管 /static/output）
│   ├── error/                  # 处理失败文件
│   ├── manual_fix/             # 人工修复产物
│   ├── configs/                # 配置方案（profiles.json）
│   └── logs/                   # 运行日志
├── run_dev.ps1                 # Windows 开发启动脚本
└── README.md
```

## 快速开始

### 1. 环境准备
- Python 3.10+
- Node.js 18+
- PostgreSQL 12+（manifest 台账 / 文档元数据存储，配置见 `RAG_PG_*`）
- 本地 MinerU API 服务（`hybrid-engine` 或 `vlm-engine`）
- Dify 知识库（Cloud API Key）+ 可选阿里云 OSS（图片托管默认 OSS）

### 2. 后端

```powershell
# 创建虚拟环境并安装依赖
python -m venv ragsys
ragsys\Scripts\pip install -r backend\requirements.txt

# 配置环境变量
Copy-Item backend\.env.example backend\.env   # 按需修改 MinerU / Dify / 切分配置

# 启动（或直接运行 run_dev.ps1）
.\run_dev.ps1
# 或：ragsys\Scripts\python -m uvicorn app.main:app --app-dir backend --port 8000 --reload
```

### 3. 前端

```powershell
cd frontend
npm install
npm run dev        # 默认 http://localhost:5173
```

浏览器打开前端页面，默认进入 **「入库工作台」**。先在 **「配置中心」** 配置知识库 ID 与切分策略并激活方案，然后拖入文件即可自动完成 解析 → 切分 → 入库；「解析产物 / 切分产物 / 人工校验」页可查看各阶段产物并人工修正。

## 配置说明（backend/.env）

关键环境变量（全部以 `RAG_` 前缀加载）：

| 变量 | 默认值 | 说明 |
| :--- | :--- | :--- |
| `RAG_MINERU_API_URL` | - | MinerU 服务地址，如 `http://192.168.31.165:7860` |
| `RAG_MINERU_API_TOKEN` | `""` | MinerU 鉴权 token（可选，开启鉴权时填写） |
| `RAG_MINERU_BACKEND` | `hybrid-engine` | 解析后端：`hybrid-engine` / `vlm-engine` / `pipeline` |
| `RAG_MINERU_BACKEND_EFFORT` | `high` | 解析强度（仅 hybrid-engine 生效） |
| `RAG_MINERU_RESPONSE_FORMAT_ZIP` | `true` | ZIP 返回全部产物（.md / json / 图片 / layout） |
| `RAG_MINERU_LONG_DOC_PAGES_THRESHOLD` | `15` | 超长 PDF 自动切换到 `vlm-engine` 的页数阈值（0=禁用） |
| `RAG_MINERU_LONG_DOC_BACKEND` / `RAG_MINERU_LONG_DOC_EFFORT` | `vlm-engine` / `high` | 长文档路由目标后端与解析强度 |
| `RAG_MINERU_ENFORCE_HIGH_QUALITY` | `true` | 强制高质量后端（`pipeline` 自动升级为 `hybrid-engine`） |
| `RAG_MINERU_LANG_LIST` | `["ch"]` | 解析语言列表 |
| `RAG_MINERU_FORMULA_ENABLE` / `RAG_MINERU_TABLE_ENABLE` | `true` | 公式 / 表格解析开关 |
| `RAG_MINERU_RETURN_*` | `true` | 输出产物开关（md / middle_json / model_output / content_list / images） |
| `RAG_MINERU_API_TIMEOUT` | `1080000` | 单次解析请求超时（秒） |
| `RAG_MINERU_MAX_RETRIES` | `3` | 最大重试次数 |
| `RAG_MINERU_RETRY_INITIAL_WAIT` / `RAG_MINERU_RETRY_BACKOFF_FACTOR` / `RAG_MINERU_RETRY_MAX_WAIT` | `15` / `2.0` / `60` | 指数退避：初始等待（秒）/ 倍数 / 上限（秒） |
| `RAG_MINERU_REJECT_LEGACY_DOC` | `true` | `.doc` 旧 OLE 格式预检测拒绝 |
| `RAG_CHUNK_STRATEGY` | `structure` | 默认切分策略（structure/recursive/fixed/sentence/semantic/parent_child/late_chunking/llm） |
| `RAG_CHUNK_TARGET_CHARS` | `1500` | 单 chunk 目标字符数（贪心合并阈值） |
| `RAG_CHUNK_SPLIT_TARGET` | `1200` | 超长时按句号二次切分的阈值 |
| `RAG_CHUNK_OVERLAP` | `100` | 句号切分时的 overlap 字符数 |
| `RAG_CHUNK_REF_PATTERN` | `^\s*[\[【\(（]\s*\d+...` | 参考文献条目行识别正则 |
| `RAG_CHUNK_HARD_LIMIT` | `1800` | 单 chunk 绝对字符上限（超长硬切） |
| `RAG_CHUNK_APPENDIX_THRESHOLD` | `1500` | 附录合并到前一 chunk 的阈值 |
| `RAG_CHUNK_MAX_IMAGES_PER_SEGMENT` | `10` | 单段图片数量上限 |
| `RAG_CHUNK_TABLE_ROW_THRESHOLD` | `20` | 大表格压缩的最小行数 |
| `RAG_CHUNK_TABLE_MAX_CHARS` | `5000` | 大表格压缩后的字符上限 |
| `RAG_CHUNK_EMBEDDING_API_URL` / `RAG_CHUNK_EMBEDDING_API_KEY` | `""` | 语义切分 / 晚切分调用的 Embedding API |
| `RAG_CHUNK_FIXED_SIZE_CHARS` | `800` | 固定长度切分的块大小 |
| `RAG_CHUNK_FIXED_OVERLAP_CHARS` | `100` | 固定长度切分的 overlap |
| `RAG_CHUNK_PARENT_SIZE_CHARS` | `1500` | 父-子切分的父块大小 |
| `RAG_CHUNK_CHILD_SIZE_CHARS` | `400` | 父-子切分的子块大小 |
| `RAG_CHUNK_SEMANTIC_THRESHOLD` | `0.78` | 语义切分 / 晚切分的相似度阈值 |
| `RAG_CHUNK_LLM_ENABLED` | `false` | 是否启用 LLM 切分 |
| `RAG_CHUNK_LLM_CHUNK_PROMPT` | （内置提示词） | LLM 切分提示词（要求模型输出切分后段落 JSON 数组） |
| `RAG_LLM_API_BASE_URL` | - | LLM 切分调用的模型 API 地址（OpenAI 兼容 Chat Completions，如 `https://api.deepseek.com/v1`） |
| `RAG_LLM_API_KEY` | - | 调用大模型接口的 API Key |
| `RAG_LLM_MODEL` | - | 模型名（如 `deepseek-chat` / `gpt-4o-mini`） |
| `RAG_DIFY_API_URL` / `RAG_DIFY_API_KEY` | - | Dify 平台 / Knowledge API |
| `RAG_DIFY_APP_API_KEY` | `""` | App API Key（具备 `/files/upload` 权限，图片附件上传用） |
| `RAG_DIFY_SKIP_FILE_UPLOAD` | `false` | 跳过附件上传，正文只写 OSS 公网 URL |
| `RAG_DIFY_SEGMENTS_PER_REQUEST` | `30` | 每批写入的段数 |
| `RAG_DIFY_MAX_SEGMENT_CHARS` | `5000` | 单段字符上限（超长截断） |
| `RAG_DIFY_INDEXING_WAIT_TIMEOUT` / `RAG_DIFY_INDEXING_POLL_INTERVAL` | `120` / `2.0` | 索引完成等待超时（秒）/ 轮询间隔（秒） |
| `RAG_DIFY_MAX_RETRIES` / `RAG_DIFY_RETRY_BACKOFF` | `3` / `2.0` | 上传重试次数 / 退避等待（秒） |
| `RAG_IMAGE_HOST_BACKEND` | `oss` | 图片托管后端（`oss` / `tunnel`） |
| `RAG_PUBLIC_BASE_URL` | `""` | 公网基地址（skip_file_upload 时拼 OSS 图片 URL） |
| `RAG_OSS_*` | - | 阿里云 OSS 图片托管配置（endpoint / bucket / AK / SK / 前缀 / 自定义域名） |
| `RAG_PG_*` | - | PostgreSQL 连接与连接池（`RAG_PG_POOL_TIMEOUT` 默认 30s） |

> 切分参数既可在 `backend/.env` 配默认值，也可在前端「配置中心」创建/激活多套方案（推荐，方案优先于 `.env`）。完整配置项见 `backend/.env.example`。

## API 概览

| 模块 | 端点 | 说明 |
| :--- | :--- | :--- |
| 流水线 | `POST /api/pipeline/run` · `POST /api/pipeline/dry` | 一键执行解析 → 切分 → 入库（可选 `scan` / `dry_run` / `stop_on_error` / `target_stems`）；预演（只输出计划） |
| 扫描 | `POST /api/scan` | 扫描 input 并更新 manifest |
| 解析 | `POST /api/parse` | 调用 MinerU 解析待处理文档 |
| 解析进度 | `GET /api/parse/progress` | 解析进度查询 |
| 解析产物 | `GET /api/parsed` · `GET /api/parsed/{stem}/files` | 解析产物列表 / 单文档文件清单 |
| 切分 | `POST /api/chunk` | 按策略切分已解析文档（body 含 `strategy`） |
| 切分策略 | `GET /api/chunk/strategies` | 获取可用策略列表 |
| 切分配置 | `GET/POST /api/chunk/config` | 切分参数查询 / 保存 |
| 切分产物 | `GET /api/chunks` · `/api/chunks/{stem}/files` · `/api/chunks/{stem}/chunks` · `/api/chunks/{stem}/preview/{chunk_id}` | 切分产物列表 / 明细 / 预览 |
| Dify 配置 | `GET/POST /api/dify/config` · `GET /api/dify/test` · `GET /api/dify/datasets` | Dify 配置读写 / 连通性 / 数据集列表 |
| Dify 入库 | `POST /api/dify/upload` | 上传 chunks 到 Dify 知识库 |
| Dify 校验 | `GET /api/dify/documents` · `GET /api/dify/documents/{id}/segments` · `POST /api/dify/documents/{id}/segments/{seg_id}` | 人工校验页：文档 / 分段拉取、分段编辑写回 |
| Dify 元数据 | `GET /api/dify/metadata/fields` · `POST /api/dify/metadata/init-fields` · `POST /api/dify/metadata/sync` | 元数据字段管理 |
| 上传 | `POST /api/upload/single` · `POST /api/upload/batch` | 单文件 / 批量上传 + 一键入库（`auto_ingest` / `profile_id` 参数） |
| 上传 | `POST /api/upload/single/ingest` | 对已上传文件单独重跑入库（不重新上传） |
| 台账 | `GET /api/manifest` · `PATCH /api/manifest/{filename}` | manifest 分页清单 / 更新行（PostgreSQL） |
| 文件 | `GET /api/files?dir=input\|pending` | 待处理 / 待扫描文件访问 |
| 配置中心 | `GET/POST /api/config/profiles` · `PUT/DELETE /api/config/profiles/{id}` · `POST /api/config/profiles/{id}/activate` · `GET /api/config/active` · `GET /api/config/schema` | 配置方案管理（CRUD / 激活 / schema 字段） |
| 健康 | `GET /api/health` | 健康检查 |

交互式文档：后端启动后访问 `http://localhost:8000/docs`。

## 运行时数据说明

所有运行时产物均在 `data/` 目录（已加入 `.gitignore`，不随仓库推送）：

```
data/
├── input/           # 待处理文档（辅助扫描用；主流程为上传驱动）
├── single_uploads/  # 上传中转区（上传文件先落这里再移入 pending/）
├── pending/         # 已登记待解析
├── parsed/          # MinerU 解析产物（每文档一文件夹）
├── chunks/          # 切分产物（chunk 明细 + 报告）
├── output/          # 入库输出（图片经 /static/output 静态托管）
├── error/           # 处理失败文件（可人工修复）
├── manual_fix/      # 人工修复产物
├── configs/         # 配置方案（profiles.json）
└── logs/            # 运行日志

文件清单台账存于 PostgreSQL（`manifest` / `doc_metadata` 表），应用启动时自动建表，无需手工维护 Excel。
```
