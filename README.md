# RAG 批量入库系统

面向企业知识库的 **文档批量入库工具**：将 PDF / DOCX / 扫描件等文档，经 **扫描 → MinerU 解析 → 多策略切分 → Dify 入库** 全流程自动化，构建高质量 RAG 知识库。内置 **8 种切分策略**，支持单文件上传即点即用与批量台账流程。

## 核心功能

### 3.0 一键流水线（Pipeline）
将 **扫描 → 解析 → 切分 → Dify 入库** 四步串成一条流水线，前端一键触发。每步可独立启用 / 禁用，支持 `dry_run`（纯本地预检）、`stop_on_error`（失败即停）、`target_stems`（单文件白名单，配合单文件上传只处理当前文件）。

### 3.1 文件扫描（Scan）
扫描 `data/input` 目录，识别待处理文档，维护 PostgreSQL `manifest` 表（文件清单台账），支持增量更新。

### 3.2 MinerU 解析（Parse）
调用本地部署的 **MinerU API**（`POST /file_parse`）解析文档：
- 默认 `hybrid-engine`（VLM + 文本提取，高精度），可切换 `vlm-engine`（极致精度）或纯 OCR `pipeline`
- 以 ZIP 形式一次性取回 `.md` / `middle.json` / 页面图片 / layout 等全部产物，一文档一文件夹
- **长文档路由**：PDF 页数超阈值自动切到 `vlm-engine`，避免复杂 CMap / 编码 OOM
- **自适应重试**：指数退避（默认 30s/60s/120s），兼容 API 端 OOM 重启场景
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
| `llm` | LLM 切分 | 大模型自主决定切分点（默认关闭，需 `RAG_CHUNK_LLM_ENABLED=true` + Dify App Key） |

所有策略均保留 **特殊内容保护**（Markdown 表格 / LaTeX 公式 / 图片不可被切断），`cover/toc/preface/reference` 区域保持原有专用逻辑。

### 3.4 Dify 入库（Dify）
将切分产物批量上传至 Dify 知识库（Knowledge API）：文档级去重、批次上传、上传报告汇总；图片经 **阿里云 OSS** 托管为公网 URL 后随文档上传，避免内网 tunnel 链接失效。

### 3.5 人工校验（Verify）
汇总展示切分 / 入库结果，支持按文档查看 chunk 明细（正文、页号、字数、策略、图片引用），供人工抽查校验。

### 单文件上传 + 一键入库
`/api/upload` 直接上传单个 PDF / DOCX / 图片，随即触发流水线（`target_stems` 白名单），只处理该文件，不影响 manifest / chunks 中走批量流程的其他文档。

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
│   │   ├── main.py             # 应用入口（注册全部路由）
│   │   ├── config.py           # 配置（RAG_ 前缀环境变量，见 .env.example）
│   │   ├── models/schemas.py   # Pydantic 请求/响应模型
│   │   ├── api/                # 路由：health / manifest / scan / parse /
│   │   │                       #       parse_progress / chunk / dify /
│   │   │                       #       pipeline / upload / files
│   │   └── services/           # 业务逻辑
│   │       ├── scanner.py          # 3.1 扫描
│   │       ├── parser.py           # 3.2 MinerU 解析
│   │       ├── chunker.py          # 3.3 结构切分（默认策略）
│   │       ├── chunk_strategies.py # 3.3 多策略切分引擎（8 策略）
│   │       ├── dify_ingest.py      # 3.4 Dify 入库
│   │       └── pipeline.py         # 3.0 一键流水线
│   ├── requirements.txt
│   └── .env.example            # 环境变量模板
├── frontend/                   # React 前端
│   └── src/
│       ├── App.tsx             # 布局与路由（6 个页面）
│       ├── pages/              # Pipeline / Scan / Parse / Chunk / Dify / Verify
│       └── components/         # PipelineControl / ChunkControl / DifyControl 等
├── data/                       # 运行时数据（git 忽略）
│   ├── input/                  # 待处理文档
│   ├── parsed/                 # MinerU 解析产物
│   ├── chunks/                 # 切分产物
│   └── ...                     # 文件清单台账存于 PostgreSQL manifest 表
├── run_dev.ps1                 # Windows 开发启动脚本
└── README.md
```

## 快速开始

### 1. 环境准备
- Python 3.10+
- Node.js 18+
- 本地 MinerU API 服务（`hybrid-engine` 或 `vlm-engine`）
- Dify 知识库（Cloud API Key）+ 可选阿里云 OSS

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

浏览器打开前端页面，默认进入 **「3.0 一键流水线」**，选择步骤后点击运行即可；也可以在「3.1 扫描 → 3.2 解析 → 3.3 切分 → 3.4 入库」分步执行。

## 配置说明（backend/.env）

关键环境变量（全部以 `RAG_` 前缀加载）：

| 变量 | 默认值 | 说明 |
| :--- | :--- | :--- |
| `RAG_MINERU_API_URL` | - | MinerU 服务地址，如 `http://192.168.31.165:7860` |
| `RAG_MINERU_BACKEND` | `hybrid-engine` | 解析后端：`hybrid-engine` / `vlm-engine` / `pipeline` |
| `RAG_MINERU_BACKEND_EFFORT` | `high` | 解析强度（仅 hybrid-engine 生效） |
| `RAG_MINERU_RESPONSE_FORMAT_ZIP` | `true` | ZIP 返回全部产物（.md / json / 图片 / layout） |
| `RAG_MINERU_LONG_DOC_PAGES_THRESHOLD` | `15` | 超长 PDF 自动切换到 `vlm-engine` 的页数阈值（0=禁用） |
| `RAG_CHUNK_STRATEGY` | `structure` | 默认切分策略 |
| `RAG_CHUNK_TARGET_CHARS` | `1500` | 单 chunk 目标字符数（贪心合并阈值） |
| `RAG_CHUNK_SPLIT_TARGET` | `1200` | 超长时按句号二次切分的阈值 |
| `RAG_CHUNK_OVERLAP` | `100` | 句号切分时的 overlap 字符数 |
| `RAG_CHUNK_FIXED_SIZE_CHARS` | `800` | 固定长度切分的块大小 |
| `RAG_CHUNK_FIXED_OVERLAP_CHARS` | `100` | 固定长度切分的 overlap |
| `RAG_CHUNK_PARENT_SIZE_CHARS` | `1500` | 父-子切分的父块大小 |
| `RAG_CHUNK_CHILD_SIZE_CHARS` | `400` | 父-子切分的子块大小 |
| `RAG_CHUNK_SEMANTIC_THRESHOLD` | `0.78` | 语义切分 / 晚切分的相似度阈值 |
| `RAG_CHUNK_LLM_ENABLED` | `false` | 是否启用 LLM 切分（需 Dify App Key） |
| `RAG_DIFY_API_URL` / `RAG_DIFY_API_KEY` | - | Dify 平台 / Knowledge API |
| `RAG_OSS_*` | - | 阿里云 OSS 图片托管配置 |

完整配置项见 `backend/.env.example`。

## API 概览

| 模块 | 端点 | 说明 |
| :--- | :--- | :--- |
| 流水线 | `POST /api/pipeline/run` | 一键执行 scan → parse → chunk → dify |
| 扫描 | `POST /api/scan` | 扫描 input 并更新 manifest |
| 解析 | `POST /api/parse` | 调用 MinerU 解析待处理文档 |
| 解析进度 | `GET /api/parse_progress` | 解析进度查询 |
| 切分 | `POST /api/chunk` | 按策略切分已解析文档（body 含 `strategy`） |
| 切分策略 | `GET /api/chunk/strategies` | 获取可用策略列表 |
| Dify | `POST /api/dify/upload` | 上传 chunks 到 Dify 知识库 |
| 上传 | `POST /api/upload` | 单文件上传 + 一键入库 |
| 台账 | `GET/POST /api/manifest` | manifest 清单读写（PostgreSQL） |
| 文件 | `GET /api/files/...` | 切分 / 解析产物文件访问 |
| 健康 | `GET /api/health` | 健康检查 |

交互式文档：后端启动后访问 `http://localhost:8000/docs`。

## 运行时数据说明

所有运行时产物均在 `data/` 目录（已加入 `.gitignore`，不随仓库推送）：

```
data/
├── input/       # 待处理文档（拖入即可被扫描）
├── pending/     # 已扫描待解析
├── parsed/      # MinerU 解析产物（每文档一文件夹）
├── chunks/      # 切分产物（chunk 明细 + 报告）
└── logs/        # 运行日志

文件清单台账存于 PostgreSQL（`manifest` / `doc_metadata` 表），应用启动时自动建表，无需手工维护 Excel。
```
