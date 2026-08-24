# RAG 批量入库自动化

> 实现自 [plan.md](./plan.md) — 第一阶段：§3.1 文件读取与状态管理 + §3.2 MinerU 解析 + §3.3 自定义切分 + Web 框架骨架。

## 当前进度

- [x] **§3.1 文件读取与状态管理（Excel 驱动）** — 启动时只补列；扫描按钮触发时遍历 manifest，对「导入情况」为空的行，去 `input/` 找同名文件移到 `pending/`，更新 manifest。
- [x] **§4 Web 框架** — FastAPI 后端 + Vite + React + Ant Design 前端骨架。
- [x] **§3.2 调用 MinerU API 解析** — 对「import_status 非空 + parse 列为空」的行，调 `POST /file_parse` 解析，结果落到 `data/parsed/{stem}/`，失败文件移入 `data/error/`，更新 manifest 的 `parse` 列。
- [x] **§3.3 自定义切分（核心）** — 对「parse 非空 + chunks 列为空」的行，按 `cutrule.md` + `cutstrategy.md` 把 `data/parsed/{stem}/hybrid_auto/*.md` + `*_content_list_v2.json` 切分为 `data/chunks/{stem}/chunk_NNN_*.md` + `chunk_metadata.json` + `images/`，并按 `chunks` 列做幂等控制。
- [ ] §3.4 Dify 入库
- [ ] §3.5 人工校验
- [ ] §3.6 测试与报告

## 目录结构

```
ragsystem/
├── backend/                   # FastAPI 后端
│   ├── app/
│   │   ├── main.py            # FastAPI 入口
│   │   ├── config.py          # pydantic-settings
│   │   ├── logging_config.py  # JSON 日志 + 按天轮转
│   │   ├── api/               # health, files, manifest, scan, parse, chunk
│   │   ├── services/          # scanner, parser, mineru_client, chunker, manifest_store, hasher
│   │   └── models/            # Pydantic schemas
│   ├── tests/                 # pytest（83 单元 + 3 E2E 冒烟）
│   │   ├── test_scanner.py        # §3.1 扫描 16 项
│   │   ├── test_manifest_extend.py # 列扩展 8 项
│   │   ├── test_parser.py         # §3.2 解析 22 项
│   │   ├── test_chunker.py        # §3.3 切分 37 项
│   │   ├── e2e_bootstrap_smoke.py # §3.1 端到端冒烟
│   │   ├── e2e_parse_smoke.py     # §3.2 端到端冒烟
│   │   └── e2e_chunk_smoke.py     # §3.3 端到端冒烟
│   ├── conftest.py
│   ├── pytest.ini
│   ├── requirements.txt
│   └── .env.example
├── frontend/                  # Vite + React + AntD
│   ├── package.json
│   ├── vite.config.ts
│   └── src/
│       ├── App.tsx
│       ├── pages/             # ScanPage, ParsePage, ChunkPage
│       ├── components/        # ScanControl, ParseControl, ChunkControl, FileTable, ManifestTable, ParsedTable
│       └── api/client.ts
├── data/                      # 运行时数据
│   ├── input/                 # 源文件
│   ├── pending/               # 已扫描待处理
│   ├── parsed/                # 解析产物（§3.2 输出）
│   ├── chunks/                # 切分产物（§3.3 输出）— 每文档一目录
│   ├── error/                 # 失败文件（§3.2 失败时移入）
│   ├── manual_fix/
│   ├── logs/
│   └── manifest.xlsx          # 处理状态（自动生成，18 列）
├── ragsys/                    # Python venv（已存在）
├── plan.md
├── mdapi.md
├── 文件列表Excel示例.txt
├── run_dev.ps1
└── README.md
```

## 快速开始

### 1. 后端

仓库内已有 venv（`ragsys/`），依赖已安装。

```powershell
# 启动 FastAPI（开发模式，自动 reload）
.\run_dev.ps1
# 或：
ragsys\Scripts\python.exe -m uvicorn app.main:app --app-dir backend --reload --host 127.0.0.1 --port 8000
```

- API：<http://127.0.0.1:8000/api/health>
- OpenAPI 文档：<http://127.0.0.1:8000/docs>
- 日志：`data/logs/app.log`（JSON 一行一条）

### 2. 前端

需要先装 Node.js 18+。参见 [frontend/README.md](./frontend/README.md)。

```powershell
cd frontend
npm install
npm run dev
# → http://127.0.0.1:5173
```

### 3. 端到端冒烟测试

```powershell
# 准备两个测试 PDF
"Hello PDF A" | Out-File -Encoding ascii data\input\a.pdf
"Hello PDF B" | Out-File -Encoding ascii data\input\b.pdf

# 试运行（不移动）
curl -X POST http://127.0.0.1:8000/api/scan -H "Content-Type: application/json" -d '{"dry_run":true}'

# 正式扫描
curl -X POST http://127.0.0.1:8000/api/scan -H "Content-Type: application/json" -d '{}'

# 查看 manifest
curl "http://127.0.0.1:8000/api/manifest?limit=50"

# 验证文件已移动
Get-ChildItem data\input
Get-ChildItem data\pending
```

## manifest.xlsx 列结构

### 用户原 11 列（可任意顺序）

| # | 列名 | 来源 | 说明 |
|---|------|------|------|
| 1 | 序号 | 示例 | 人工填 |
| 2 | 文件名称 | 示例 | **主键**（与磁盘文件名一致） |
| 3 | 一级分类 | 示例 |  |
| 4 | 二级分类 | 示例 |  |
| 5 | 关键词标签 | 示例 |  |
| 6 | 适用科室 | 示例 |  |
| 7 | 生效日期 | 示例 |  |
| 8 | 导入情况 | 示例 | 系统在 §3.1 完成后写入 `已移入待处理` / `已移入待处理(重命名)` / `移入失败` |
| 9 | 处理情况 | 示例 | 人工备注（**不是** 管线状态）；系统各步骤完成后会覆盖 |
| 10 | 校对 | 示例 |  |
| 11 | 处理说明 | 示例 | 系统默认写入 `md5=<前12位>…` 或错误信息 |

### 系统自动追加 6 列（始终在末尾）

| # | 列名 | 说明 |
|---|------|------|
| 12 | **status** | 管线 FSM：`new`/`pending`/`scanning`/`parsing`/`chunking`/`uploading`/`done`/`error` |
| 13 | **md5** | 32 字符 hex |
| 14 | **create_time** | YYYY-MM-DD HH:MM:SS |
| 15 | **update_time** | 同上 |
| 16 | **error_msg** | 失败原因 |
| 17 | **parse** | §3.2 解析状态/路径（成功 = 解析目录绝对路径；失败 = 失败描述；试运行 = `试运行-已识别`） |

> **注意**：`处理情况`（第 9 列）保留为人工备注，**不要**用它来表示管线状态 —— 用 `status`（第 12 列）。

## 用户自定义 Excel 自动补列

用户可以把任意版本的 `manifest.xlsx` 直接放入 `data/` 目录：

| 用户 Excel 列数 | 系统行为 |
|---|---|
| 0（文件不存在） | 启动时自动创建 16 列空表 |
| 11 列（与示例一致） | 启动时按用户列顺序保留前 11 位，末尾追加 5 个系统列 |
| 任意中间状态（例如只放了『文件名称』『导入情况』） | 启动时只追加缺失列，原有列与原数据完全不动 |
| 已 16 列 | 不动（幂等） |
| 列顺序打乱 | 按表头名匹配读写，列顺序无关 |

**关键设计**：
- 系统按 **表头名**（不是位置）匹配列；用户列顺序任意。
- 启动时只补缺失列，**不动用户数据**。
- 缺失列占位为 `None`；后续每完成一步都会同步更新对应的用户列（`导入情况`/`处理情况`/`处理说明`）和系统列（`status`/`md5`/时间戳）。

## §3.1 行为规范（Excel 驱动）

> **重要变更**：扫描逻辑从「扫描 input/」改为「读取 manifest.xlsx」。启动时只补列，**不移动任何文件**；只有点击前端「扫描」按钮才会执行实际的文件操作。

### 启动（main.lifespan）

1. 读取 `data/manifest.xlsx`，检查列数；
2. 缺失的列（含 5 个系统列）追加到表尾，**不动用户数据**；
3. **不**扫描 `input/`，**不**移动任何文件，**不**更新 manifest 的内容（仅表头）。

### 扫描（用户点击「扫描」按钮触发）

1. 读取 `data/manifest.xlsx` 的每一行；
2. 对每一行检查 **「导入情况」列**：
   - **非空**（已标记） → 跳过（计入 `skipped_done`），保证幂等；
   - **为空**（未导入） → 继续；
3. 在 `data/input/` 找同名文件（`row.filename`）：
   - **找不到** → 记 `MISSING`，不更新 manifest（让用户补文件后再次扫描）；
   - **找到**（支持扩展名自动补全，见下） → 算 MD5，处理 `pending/` 冲突（同名 MD5 一致则跳过；不一致则重命名为 `_<6hex>`），`shutil.move` 到 `data/pending/`，更新 manifest；
4. 更新 manifest 的列：
   - **「文件名称」列**自动更新为带扩展名的真实文件名（这样重复扫描是直接精确匹配，无需再探测）；
   - 用户列：`导入情况`/`处理情况`/`处理说明`；
   - 系统列：`status`/`md5`/`create_time`/`update_time`/`error_msg`。

### 文件名扩展名自动补全

用户在 Excel 的「文件名称」列常省略扩展名（`国标-001`），但实际文件总是带扩展名（`国标-001.pdf`、`国标-001.docx`）。系统在 `input/` 中按以下顺序查找：

1. **精确匹配**（用户写出了扩展名时优先）
2. 按 `allowed_extensions` 顺序追加扩展名：
   ```python
   allowed_extensions = (".pdf", ".docx", ".doc", ".pptx", ".xlsx",
                         ".png", ".jpg", ".jpeg", ".tiff", ".tif")
   ```
3. 同名多扩展名（如同时有 `.pdf` 和 `.docx`）→ 命中列表中**靠前的扩展名**（即 `.pdf` 优先）。

找到文件后，扫描结果会用**带扩展名的真实文件名**作为 manifest 的「文件名称」列的 key（即 `bulk_upsert` 会按 stem 匹配已有行并就地更新 filename 字段），保证后续扫描直接精确匹配。

### 关键不变量

- **启动幂等**：多次启动只会确保列齐全，不会移动文件；
- **扫描幂等**：第二次扫描应 `staged=0`、`skipped_done=N`（N = 第一次成功移入的行数）；
- **missing 不污染**：文件找不到时**不**自动写 manifest，由用户补文件后再次扫描；
- **并发**：所有写 `manifest` 的操作都通过模块级 `threading.Lock` 串行化。

## API 一览

| 方法 | 路径 | 说明 |
|------|------|------|
| GET  | `/api/health` | 健康检查 |
| GET  | `/api/files?dir=input` | 列出 `data/input/` |
| GET  | `/api/files?dir=pending` | 列出 `data/pending/` |
| GET  | `/api/manifest?limit=&offset=` | 分页读 manifest |
| POST | `/api/scan` | 执行 §3.1 扫描（body: `{"dry_run": false}`） |
| POST | `/api/parse` | 执行 §3.2 MinerU 解析（body: `{"dry_run": false}`） |
| GET  | `/api/parsed` | 列出 `data/parsed/` 下所有解析目录 |
| GET  | `/api/parsed/{stem}/files` | 列出某文档解析产物里的所有文件 |

完整 schema 见 <http://127.0.0.1:8000/docs>。

## §3.2 MinerU 解析规范

> **入口**：用户点击前端「3.2 MinerU 解析」页的「执行解析」按钮 → `POST /api/parse`。
> **前置**：必须先执行过 §3.1 扫描（`import_status` 已非空）。

### MinerU API 契约（`POST /file_parse`）

部署的是 **mineru-api 3.x 自带 FastAPI 服务**（实测可达 `http://192.168.31.165:7860`）。本系统通过 `POST /file_parse` 同步解析。

| 项 | 值 |
|---|---|
| **URL** | `{RAG_MINERU_API_URL}/file_parse`（默认 `http://192.168.31.165:7860/file_parse`） |
| **方法** | `POST`（**同步**） |
| **Content-Type** | `multipart/form-data`（**注意：不是 JSON body**） |
| **必填** | `files`：上传的二进制文件（支持 PDF / PNG / JPG / DOCX / PPTX / XLSX） |
| **可选 form 字段** | `backend`、`lang_list`、`formula_enable`、`table_enable`、`response_format_zip`、`return_md` 等（详见 `/openapi.json`） |
| **Auth** | 可选 Bearer Token（`RAG_MINERU_API_TOKEN`） |
| **响应（zip 模式）** | 200 + `application/zip` → **包含所有产物**（.md / .json / 图片 / layout / content_list 等） |
| **响应（json 模式）** | 200 + `application/json` → `{ task_id, status, results: { stem: { md_content, ... } } }` |
| **超时** | 600s（`RAG_MINERU_API_TIMEOUT`） |

> ⭐ **关键开关：`response_format_zip=true`** — 让 MinerU 一次性返回所有产物的 ZIP，客户端解压到 `data/parsed/{stem}/` 即可拿到**全部**输出文件（而非只拿 .md/.json）。

### 行为

1. 加载 manifest，筛选 `import_status` 非空 + `parse` 列为空的行；
2. 对每行：
   - 在 `pending/` 找原文件（按 stem + allowed_extensions 解析，与 §3.1 一致）；
   - 调 MinerU `POST {RAG_MINERU_API_URL}/file_parse`（**multipart/form-data** 上传 + `response_format_zip=true`）；
   - **成功** → 把 ZIP 整个解压到 `data/parsed/{stem}/`（含 .md / .json / images / layout / *_origin.pdf 等所有产物）；
   - **失败**（重试 N 次仍失败）→ 清理空目录，原文件移入 `data/error/`，manifest 标记 `status=error`、`parse=解析失败 → …`；
3. 更新 manifest：
   - `parse` 列 = `data/parsed/{stem}/`（成功）或 失败描述（失败）或 `试运行-已识别`（试运行）；
   - `status` 列 = `parsing_done`（成功）/ `error`（失败）；
   - `error_msg` 列 = 失败原因。

### MinerU 客户端行为（`app/services/mineru_client.py`）

- **请求**：`POST {api_url}/file_parse`，`multipart/form-data`：
  - `files`: 上传的二进制文件（自动按后缀选 MIME）
  - `lang_list`: 重复字段（dict-of-list 形式 →  `lang_list=ch&lang_list=en`）
  - `backend`: `hybrid-engine`（默认）/ `pipeline` / `vlm-engine` / `vlm-http-client` / `hybrid-http-client`（★ 默认走高质量模型）
  - `effort`: `high`（默认，hybrid-engine 极致精度 + image analysis）/ `medium` / `low`（仅 hybrid-engine 接受）
  - `formula_enable` / `table_enable`: bool
  - **`response_format_zip=true`** ← 默认开启，一次拿全
- **响应处理**（自动判定）：
  - `Content-Type: application/zip` → 整体解压到 `parsed/{stem}/`；
  - `Content-Type: application/json` → 按 `results[stem].md_content` / `middle_json` / `content_list` / `images` 分别落盘；
- **重试**：默认 3 次，指数退避（1s / 2s / 4s）；
- **4xx 错误**：不重试（参数问题，重试无意义）；
- **5xx / 网络 / 超时**：按 `max_retries` 重试；
- **失败清理**：解析失败时清理 `parsed/{stem}/` 空目录（避免污染）。

### 高质量模型强制（★ 推荐配置）

**目标**：所有解析调用都走 VLM 级别的高质量模型（vlm-engine / hybrid-engine），保证扫描件 / 复杂版面效果。

**实现位置**：[mineru_client.py](backend/app/services/mineru_client.py) 的 `_resolve_backend` 方法。

| 配置 | 默认值 | 含义 |
|------|--------|------|
| `RAG_MINERU_BACKEND` | `hybrid-engine` | 解析后端。**推荐**：vlm-engine（极致精度）/ hybrid-engine（VLM+文本，平衡） |
| `RAG_MINERU_BACKEND_EFFORT` | `high` | hybrid-engine 解析强度。`high`=极致精度+image analysis，**`medium`**=速度优先但无 image analysis |
| `RAG_MINERU_ENFORCE_HIGH_QUALITY` | `true` | 开启后：误填 `pipeline` 时**自动升级**到 `hybrid-engine` 并打印 WARNING |

**后端白名单**（`mineru_client._HIGH_QUALITY_BACKENDS`）：

- ✅ `vlm-engine`（极致精度，VLM 大模型）
- ✅ `hybrid-engine`（VLM + 文本提取，**默认**，兼顾速度与效果）
- ✅ `vlm-http-client` / `hybrid-http-client`（远程推理变种）
- ⚠️ `pipeline` / `pipeline-http-client`（**低质量**，纯 OCR，对扫描件效果差；开启强制后会被自动升级）
- ❓ 未知值：WARNING 但原样发送（让 MinerU 自己报错，避免静默切换隐藏配置错误）

**效果排序**（对扫描件 OCR 准确率）：`vlm-engine` > `hybrid-engine` > `pipeline`

### PyMuPDF fallback（★ 兜底，2026-07 新增）

**背景**：某些旧版 PDF（Acrobat PDFWriter 5.0 + Type0 字体 + `GBK-EUC-H` CMap），
MinerU 服务端**不能解码 GBK-EUC-H CMap**，仅识别到 ASCII 范围的年份数字，
导致解析产物过少（`v2+.md` 字符数 < 100）。

**实际案例**：`济宁市医疗卫生机构病死婴幼儿遗体处理暂行办法(1).pdf`（20 KB，6 页）

| 方案 | 字符数 | 完整条款 | 页码/页脚 |
|------|--------|----------|-----------|
| 旧 pipeline | 6 | ❌ 仅年份数字 | ❌ 保留 |
| hybrid-engine (effort=high) | 6 | ❌ 仍失败 | ❌ 保留 |
| vlm-engine（最强大模型，直接读 PDF）| 6 | ❌ 仍失败 | ❌ 保留 |
| Tier 2：PyMuPDF 纯文本 | 2103 | ✅ 完整 6 条正文 | ❌ 保留（`- 1 -` 等） |
| **★ Tier 1：PyMuPDF 转图 + VLM 读图** | **3865** | ✅ **完整 + 准确结构** | ✅ **自动删除** |

**★ Tier 1 推荐（2026-07 新增）**：

1. PyMuPDF 渲染原 PDF 每页为 200 DPI PNG；
2. 打包为新的「图片型 PDF」（每页是全屏图片）；
3. 上传给 MinerU `vlm-engine` 走视觉路径；
4. VLM 自动识别版式、删除页码/页脚、给出准确结构（`#` 章节、`第一条` 条款）。

**自动触发 fallback 链**（见 [parser.py](backend/app/services/parser.py)）：

```
MinerU 解析成功 → 检查 v2+.md 字符数
   ↓ < 100
启动 fallback 链
   ↓ Tier 1: PyMuPDF 渲染 + MinerU vlm-engine 读图
   ↓ 失败？
   ↓ Tier 2: PyMuPDF 纯文本提取
   ↓
更新 manifest.parse 列加后缀：[vlm-image-fallback 修复] 或 [pymupdf-fallback 修复]
```

**输出结构**（与 MinerU 一致，下游 chunker 不需修改）：

```
data/parsed/{stem}/
    {stem}_img/                            ← vlm-engine 风格（图片 PDF 解析后）
        vlm/
            {stem}_img.md
            {stem}_img_content_list_v2.json
            {stem}_img_middle.json
            {stem}_img_model.json
    或 hybrid_auto/                        ← hybrid-engine 风格
        {stem}.md
        {stem}_content_list_v2.json
```

**依赖**：项目已自带 PyMuPDF（`pymupdf==1.28.0`），无需额外安装。

### 落盘目录结构

每个文档对应一个 `parsed/{stem}/` 目录，**MinerU 自身的产物结构** 原样保留：

```
data/parsed/
├── 国标-001/
│   └── hybrid_auto/                  # ← MinerU 按后端类型分的子目录（保留原结构）
│       ├── 国标-001.md               # 主 markdown
│       ├── 国标-001.json             # 结构化内容（blocks / 表格 / 阅读顺序）
│       ├── 国标-001_origin.pdf       # （可选）处理后的原文件
│       ├── layout.json               # 版面分析
│       ├── middle.json               # 中间结构
│       └── images/                   # 解析出的图片
│           ├── image_0.png
│           └── image_1.jpg
├── 团标-002/
│   └── ...
```

### Manifest 字段更新规则

| 场景 | `parse` 列 | `status` 列 | `error_msg` 列 |
|------|-----------|------------|----------------|
| 成功 | `D:\...\data\parsed\国标-001`（绝对路径） | `parsing_done` | `""`（清空） |
| 失败（重试耗尽） | `解析失败 → 失败-003.pdf` | `error` | `mineru 调用失败(尝试3次): 5xx ...` |
| 试运行 | `试运行-已识别` | `pending` | `""`（清空） |
| 已解析过 | （不变） | （不变） | （不变） |

### 配置

在 `backend/.env` 中调整：

```ini
# MinerU 服务地址
RAG_MINERU_API_URL=http://192.168.31.165:7860
# 是否以 ZIP 形式返回（推荐 true：拿到所有产物）
RAG_MINERU_RESPONSE_FORMAT_ZIP=true
# 解析后端（★ 推荐用高质量：vlm-engine / hybrid-engine，对扫描件效果更好）
# pipeline 是低质量后端（纯 OCR），开启 RAG_MINERU_ENFORCE_HIGH_QUALITY 后会被自动升级
RAG_MINERU_BACKEND=hybrid-engine
# hybrid-engine 解析强度（high=极致精度+image analysis / medium=速度优先 / low=最快）
RAG_MINERU_BACKEND_EFFORT=high
# 是否强制高质量后端（true=自动升级 pipeline 到 hybrid-engine）
RAG_MINERU_ENFORCE_HIGH_QUALITY=true
# 语言列表（JSON 数组字符串）
RAG_MINERU_LANG_LIST=["ch"]
# 公式 / 表格解析
RAG_MINERU_FORMULA_ENABLE=true
RAG_MINERU_TABLE_ENABLE=true
# 超时 / 重试
RAG_MINERU_API_TIMEOUT=600
RAG_MINERU_MAX_RETRIES=3
RAG_MINERU_RETRY_BACKOFF=2.0
# 可选：Bearer token
# RAG_MINERU_API_TOKEN=
```

## §3.3 自定义切分（核心）

> **入口**：用户点击前端「3.3 自定义切分」页的「执行切分」按钮 → `POST /api/chunk`。
> **前置**：必须先执行过 §3.2 解析（`parse` 列已非空 + `chunks` 列为空）。

### 设计目标

将 MinerU 解析产物（`.md` + `*_content_list_v2.json` + `images/`）按 [cutrule.md](./cutrule.md) 和 [cutstrategy.md](./cutstrategy.md) 切分为适合 RAG 召回的语义块：

- **语义优先**：按章节（`第一章`、`1 范围`、`4.1 基本原则`）切分，不切断完整语义。
- **长度可控**：目标 ≤ 1500 字符；超长则按三级标题 → 句号切分（带 overlap）。
- **结构完整**：保留标题层级、表格、图片内联引用。
- **幂等可重跑**：通过 manifest `chunks` 列控制；`force=true` 清空重切。

### 区域划分（`classify_regions`）

把 v2 扁平的 blocks 划分为六个区域：

| 区域 | 标识规则 | 切分策略 |
|------|---------|----------|
| `cover` 封面 | 首个 chapter-like 标题前 | 整体为 1 段，超长按句号切 |
| `toc` 目录 | 含「目录」「目 录」标题 | 整体为 1 段 |
| `preface` 前言 | 含「前言」「引言」标题 | 整体为 1 段 |
| `body` 正文 | 从第一个 level-1 章节开始 | 贪心合并 1级→2级→3级→句号 |
| `appendix` 附录 | 标题含「附 录」/`RE_APPENDIX` | 贪心合并（≤ 1500 字符） |
| `reference` 参考文献 | 标题含「参考文献」 | 整体为 1 段 |

**关键修复点**：
- **body 起点不能被 cover 吞掉**：先按 chapter-like 模式找 body 起点候选，倒推 cover 边界；
- **封面重复标题过滤**：WST 809 类文档 p4 重复 p1 文档名时，用 cover 区 title 集合过滤；
- **附录 paragraph 升级为 title**：v2 中附录 A 经常被标为 paragraph，扫描时按 `RE_APPENDIX` 升级。

### 标题层级推断

v2 标注常有错（如「1 范围」被标为 level-2），系统用 `_effective_level()` 综合判断：

```python
def _effective_level(b: Block) -> Optional[int]:
    # 1. 优先用文本模式正则推断（RE_CHAPTER / RE_ARTICLE / RE_NUMERIC_TITLE）
    inferred = _infer_block_level(b)
    if inferred is not None:
        return inferred
    # 2. 回退到 v2 标注
    return b.level
```

| 文本模式 | 推断 level |
|---------|-----------|
| `第一章 / 第二部分 / 第三章 范围` | 1 |
| `第一条 / 第六条` | 2 |
| `4.1 基本原则 / 4.2.1 详细` | 2 / 3 |
| `1 范围 / 2 规范性引用文件` | 1 |
| `附 录 A` | appendix 起点 |
| `参考文献` | reference 起点 |

### 正文切分算法（`chunk_body`）

```
body blocks
   │
   ├── 按 L1 标题分组（第一章/第二章/...）
   │
   ▼
L1 组（每组：1 个章节）
   │
   ├── 在组内按 L2 标题分组（4.1 / 4.2 / ...）
   │
   ▼
L2 子组
   │
   ├─ 累积字符数 ≤ 1500 → 合并
   │
   └─ 累积字符数 > 1500 → 关闭当前 chunk
                              │
                              ▼
                         单个 L2 子组
                              │
                              ├─ 字符数 ≤ 1500 → 1 个 chunk
                              │
                              └─ 字符数 > 1500 → 按 L3 标题再分
                                                  │
                                                  └─ 还超长 → 按"。"句号切，加 100 字符 overlap
```

### 图片处理

- **输出格式**：`![](images/xxx.jpg)`（MD 原生语法，无 caption 干扰）。
- **拷贝策略**：解析产物 `parsed/{stem}/images/` 下的图片**按需去重拷贝**到 `chunks/{stem}/images/`。
- **元数据记录**：`Chunk.image_refs` 字段记录每个 chunk 引用了哪些图片（用于后续 Dify 上传）。

### 防御机制

1. **日期不被误识别为标题**（`_maybe_promote_to_title`）：
   - 4 位数字开头（年份 `2024`）不升级为 title。
   - 数字后紧跟 `年` / `月` / `日` 不升级为 title（如 `2024 年 11 月 12 日`、`11 月 12 日`）。
   - **典型场景**：单页通知文档（只有"国家卫健委关于印发...的通知"作为唯一标题 + 落款日期），整篇作为 cover，不会被切成 cover + body 两段。

2. **孤立标题不触发 body 起点**（`classify_regions` 兜底逻辑）：
   - 兜底选择 body 起点时，**仅在文档中存在 chapter-like 标题**（如"第一章"、"1 范围"、"4.1 基本原则"）时才生效。
   - **典型场景**：单页通知 / 标题 / 简单文档，整篇作为 cover 或 single。

3. **解析质量预检**（`_is_parse_content_trivial`）：
   - 在 `chunk_document` 之前检测 v2 块数 / title+paragraph 块数 / 文本字符数。
   - 阈值：v2 总块数 < 3、或 v2 无 title/paragraph 块、或 v2 文本 < 50 字符 → 标记为 `status=error`，`chunks` 列写"切分跳过 → MinerU 解析内容严重缺失：..."。
   - **典型场景**：扫描件 OCR 失败（v2 只有 page_number/header 块），不会产生空 chunk 文件。

### 落盘目录结构

```
data/chunks/
├── 国标-W809/
│   ├── chunk_001_封面.md
│   ├── chunk_002_目录.md
│   ├── chunk_003_前言.md
│   ├── chunk_004_1_范围.md
│   ├── chunk_007_4_功能单元视觉设计标准___4.1_基本原则_~_..._~_4.6_预防保健.md
│   ├── chunk_009_附_录_A_~_..._~_附_录_J.md
│   ├── chunk_metadata.json           # 所有 chunk 的元数据
│   └── images/                        # 引用的图片（去重拷贝）
│       ├── 07d380cd...jpg
│       └── ...
├── 规范-医院感染/
│   ├── chunk_001_封面.md
│   ├── chunk_002_第一章_总_则.md
│   ├── ...
│   └── chunk_metadata.json
```

**`chunk_metadata.json` 格式**：
```json
{
  "doc_stem": "国标-W809",
  "chunk_count": 9,
  "chunks": [
    {
      "chunk_id": "chunk_001",
      "file_name": "chunk_001_封面.md",
      "title_path": "封面",
      "chunk_type": "cover",
      "char_count": 166,
      "image_refs": [],
      "is_split": false
    },
    ...
  ]
}
```

### Manifest 字段更新规则

| 场景 | `chunks` 列 | `status` 列 | `error_msg` 列 |
|------|------------|------------|----------------|
| 成功 | `{stem}`（裸 stem，非路径） | `chunked` | `""`（清空） |
| 失败 | `切分失败 → 失败原因` | `error` | 失败原因 |
| 试运行 | （不变） | `chunked`（试运行标记） | `""` |
| 已切分过 | （不变） | （不变） | （不变） |

### 配置（`backend/.env`）

```ini
# 单 chunk 目标字符数（贪心合并阈值）
RAG_CHUNK_TARGET_CHARS=1500
# 句号切分阈值（仍超长时启用）
RAG_CHUNK_SPLIT_TARGET=1200
# overlap 字符数
RAG_CHUNK_OVERLAP=100
```

### API

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/chunk` | 执行 §3.3 切分（body: `{"dry_run": false, "force": false}`） |
| GET  | `/api/chunks` | 列出 `data/chunks/` 下所有切分目录 |
| GET  | `/api/chunks/{stem}/files` | 列出某文档切分产物 |

## 测试

```powershell
ragsys\Scripts\python.exe -m pytest backend\tests -v
```

覆盖（`test_scanner.py`）：
- manifest 为空时 input/ 有文件 → 不动（避免误移）
- 「导入情况」为空 + input/ 有文件 → 移入 pending/
- 「导入情况」已非空 → 跳过（幂等）
- 旧 `status=done` + 新「导入情况」为空 → 仍会移入（新规则只看「导入情况」）
- 「导入情况」为空但 input/ 找不到 → MISSING，manifest 不动
- 二次扫描幂等
- dry_run 不移动、不写盘
- 混合：已导入 + 未导入
- pending/ 同名冲突（md5 不一致）→ 触发重命名
- bootstrap 启动时不会移动文件
- **Excel 文件名无扩展名** → 按 `allowed_extensions` 顺序探测（`.pdf` 优先）
- 同名多扩展名歧义 → 优先级高的胜出
- Excel 文件名已含扩展名 → 精确匹配
- 扫描后 manifest 的「文件名称」自动更新为带扩展名的真实文件名
- 试遍所有扩展名都找不到 → MISSING
- 第二次扫描时 manifest 已是带扩展名的 key → 直接精确匹配

覆盖（`test_manifest_extend.py`）：
- 用户放 11 列 Excel → bootstrap 自动补全为 17 列（11 用户 + 5 系统 + 1 parse），原列顺序保留
- 已 17 列 → 二次 bootstrap 幂等不动
- 用户 Excel 只有部分列 → 只补缺失列
- 列顺序打乱 → load 仍能正确读 `文件名/序号/处理说明`
- 补列后用户原数据不变
- 扫描后 `导入情况/处理情况/处理说明` 被正确更新
- dry_run 不写盘、不移动
- pending/ 同名冲突（md5 不一致）→ 触发重命名，列写 `已移入待处理(重命名)`

覆盖（`test_parser.py`，§3.2 解析 14 项）：
- 旧 16 列 manifest → 自动追加 `parse` 列到 17
- manifest 加载后 `ManifestRow.parse` 字段能正确读出
- 正常解析：pending/ → mineru API → 落盘到 `parsed/{stem}/`
- dry_run=True：不调 API、不动文件
- 第二次解析：parse 列非空 → 全部 SKIPPED_DONE（幂等）
- 解析失败：原文件移入 `error/`，manifest 标 `status=error`
- manifest 标记待解析但 pending/ 找不到 → NO_PENDING action
- 重试：前 N 次失败后第 N+1 次成功 → manifest 记录 attempts
- mineru_client 4xx → 不重试，立即抛错
- mineru_client 5xx → 重试 max_retries 次后抛错
- 混合：1 成功 + 1 失败 + 1 已解析 + 1 MISSING
- 客户端请求：URL=`/file_parse`、body 含 `file_path` + `output_format`
- 客户端：5xx 在内部 `_write_outputs` 抛 `_RetryableMinerUError`

覆盖（`test_chunker.py`，§3.3 切分 42 项）：
- `_infer_block_level`：第一章/第一条/4.1/4.2.1 等标题层级推断
- `_effective_level`：文本推断优先于 v2 标注
- `_block_to_text`：图片输出 `![](images/xxx.jpg)` 原生 MD 语法
- `classify_regions`：六区域划分（cover/toc/preface/body/appendix/reference）
- `classify_regions` 关键修复：body 起点不被 cover 吞掉、无目录/前言时 cover 边界正确
- `chunk_body` 贪心合并 L1→L2→L3 标题 + 句号切 + overlap
- `chunk_appendix` 附录合并
- `chunk_simple` cover/toc/preface 整段切
- `Chunk.image_refs` 字段正确传递
- 标题 slug 生成（中文/英文/特殊字符）
- `chunk_parsed` 入口：幂等、force、dry_run
- `_maybe_promote_to_title` 防御：日期/年份不被识别为标题（4 位年份 + 年/月/日）
- `classify_regions` 防御：孤立标题不触发 body 起点
- `_is_parse_content_trivial` 解析质量检测（v2 块数 + 文本字符数）

E2E 冒烟（覆盖完整链路）：

```powershell
# §3.1 扫描
ragsys\Scripts\python.exe backend\tests\e2e_bootstrap_smoke.py

# §3.2 解析（mock mineru API）
ragsys\Scripts\python.exe backend\tests\e2e_parse_smoke.py

# §3.3 切分（真实解析产物）
ragsys\Scripts\python.exe backend\tests\e2e_chunk_smoke.py
```

## 配置

复制 `backend/.env.example` 为 `backend/.env`，按需调整（`RAG_DATA_ROOT` 等）。

## 风险与限制

- **Excel 跨进程锁**：`openpyxl` 在多进程中同时写会冲突。当前未实现跨进程互斥（仅进程内 `threading.Lock`）。如果未来需要多 worker，请用 SQLite 或加文件锁。
- **大量文件 MD5**：`64KB` 流式分块，3.1 阶段 corpus 小（几十份标准文档）。若上千文件需引入 worker pool。
- **前端需手动装 Node**：当前环境无 Node.js；详见 [frontend/README.md](./frontend/README.md)。
