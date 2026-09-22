# RAG 批量入库系统 · API 文档（部署方对接）

> 适用版本：v0.4.0+（含 2026-09 租户隔离）
> 本文档面向**部署方 / 接入方**：在只使用后端、使用自有前端与用户体系时，如何通过 API 完成
> 文档上传 → 解析 → 切分 → 入库（Dify 知识库），并保证**多租户数据隔离**。

## 目录

- [1. 总览](#1-总览)
- [2. 鉴权与租户隔离模型](#2-鉴权与租户隔离模型)
- [3. 租户管理 API（管理员）](#3-租户管理-api管理员)
- [4. 业务 API（租户）](#4-业务-api租户)
- [5. 运维 API（全局，不参与租户隔离）](#5-运维-api全局不参与租户隔离)
- [6. 典型接入流程](#6-典型接入流程)
- [7. 存储布局与数据归属](#7-存储布局与数据归属)
- [8. 常见问题（FAQ）](#8-常见问题faq)

---

## 1. 总览

| 项 | 值 |
| :--- | :--- |
| 基地址 | `http://<部署主机>:<端口>`（Docker 默认宿主端口 `18000` → 容器 `8000`） |
| 请求体 | `multipart/form-data`（上传）或 `application/json`（其余） |
| 交互式文档 | 后端启动后访问 `GET /docs`（Swagger UI）与 `GET /openapi.json` |
| 健康检查 | `GET /api/health`（无需鉴权，返回 MinerU provider 等运行时信息） |

系统有两套**互不干扰**的身份体系，部署方自有用户系统只需在**网关/后端侧**持有密钥、转发请求头即可：

| 身份 | 请求头 | 用途 |
| :--- | :--- | :--- |
| 租户 | `X-API-Key: rt-xxxxxxxx` | 业务接口：上传、台账、文件、元数据、Dify 校验 |
| 管理员 | `X-Admin-Key: <RAG_ADMIN_API_KEY>` | 管理接口：`/api/tenants` 租户的增删改查与 Key 轮换 |

> **匿名调用 = `default` 租户**：不带 `X-API-Key` 的请求按内置 `default` 租户处理（全局数据，兼容存量调用）。
> 带了 Key 但无效 → `401`（不会静默降级为 default，避免"以为隔离了实则没有"）。

---

## 2. 鉴权与租户隔离模型

### 2.1 隔离维度

一个租户（`tenant_id`，如 `acme`）在四个层面被隔离：

| 维度 | default 租户 | 命名租户 |
| :--- | :--- | :--- |
| 台账行（`manifest` 表） | 主键 `(default, filename)`，API 可见**全部**租户数据 | 主键 `(tenant_id, filename)`，API 只可见**本租户**行 |
| 文档元数据（`doc_metadata` 表） | 同上 | 同上 |
| 文件目录 | 直接位于 `data/{pending,parsed,chunks,output,error,input,single_uploads}/` 根下 | 位于各自 `data/.../{tenant_id}/` 子目录 |
| Dify 知识库 | 全局 dataset（`RAG_DIFY_DATASET_ID`） | 租户创建时绑定的 `dify_dataset_id`（**强制路由**，无法越权访问其他库） |

补充说明：

- **Dify 路由是强制的**：命名租户调用 `GET /api/dify/documents` 等接口时，服务端忽略请求传入的
  `dataset_id`，强制使用该租户绑定的知识库；返回的文档永远只来自本租户的库。
- **图片托管隔离**：入库到 Dify 的图片 URL（OSS / 隧道）路径带 `{tenant_id}/{stem}/` 前缀，租户间不冲突。
- **同一 Dify 工作空间**：所有租户的 dataset 属于同一个 Dify 账号/工作空间，由系统统一用 `RAG_DIFY_API_KEY` 访问；
  租户侧的可见性由 Dify 控制台的知识库权限设置管理（推荐：每个 dataset 只授权给对应的 Dify 应用/成员）。
- **文件名可重复**：不同租户可存在同名文件（如都叫 `规范.pdf`），互不影响。

### 2.2 状态码约定

| 状态码 | 含义 |
| :--- | :--- |
| `200` / `201` / `204` | 成功（创建 201、删除租户 204） |
| `400` | 参数校验失败（如 `tenant_id` 非法、`dify_dataset_id` 为空、重复创建、status 取值非法） |
| `401` | 鉴权失败：`X-API-Key` 无效/被禁用，或 `X-Admin-Key` 错误 |
| `403` | 管理面未开启：服务端未配置 `RAG_ADMIN_API_KEY` |
| `404` | 资源不存在（租户不存在；或租户下不存在该文件，如 `ingest` 触发他人文档） |
| `422` | 请求体格式错误（FastAPI 校验，如缺字段、类型不符） |

---

## 3. 租户管理 API（管理员）

以下接口全部要求请求头 `X-Admin-Key: <RAG_ADMIN_API_KEY>`；服务端未配置该变量时统一返回 `403`。

### 3.1 创建租户

```
POST /api/tenants
```

| 字段 | 类型 | 必填 | 说明 |
| :--- | :--- | :--- | :--- |
| `tenant_id` | string | 是 | 租户标识：小写字母/数字/`-`/`_`，以字母或数字开头，最长 64 字符；`default` 为保留字 |
| `name` | string | 否 | 显示名 |
| `dify_dataset_id` | string | 是 | 绑定的 Dify 知识库 ID（在 Dify 控制台创建知识库后获取） |

```bash
curl -X POST http://<host>:18000/api/tenants \
  -H "X-Admin-Key: $RAG_ADMIN_KEY" \
  -H "Content-Type: application/json" \
  -d '{"tenant_id":"acme","name":"ACME 公司","dify_dataset_id":"b2c4f340-97c9-474c-bfb2-0fdb71e23250"}'
```

响应 `201`：

```json
{
  "tenant": {
    "tenant_id": "acme",
    "name": "ACME 公司",
    "dify_dataset_id": "b2c4f340-97c9-474c-bfb2-0fdb71e23250",
    "status": "active",
    "create_time": "2026-09-22 10:00:00",
    "update_time": "2026-09-22 10:00:00"
  },
  "api_key": "rt-B1zsqPFH8BJPTos-ZYJH40hqGQKrrgppIFeE9bGN"
}
```

> ⚠️ **`api_key` 明文仅在本响应中出现一次**（库中只存 sha256）。请立即安全保存并交付给对应租户。
> 若遗失，用 [3.5 轮换 Key](#35-轮换租户-api-key)。

### 3.2 租户列表

```
GET /api/tenants
```

```bash
curl http://<host>:18000/api/tenants -H "X-Admin-Key: $RAG_ADMIN_KEY"
```

响应 `200`：`{"tenants":[{"tenant_id":"default",...}, ...]}`（不含密钥哈希；内置 `default` 租户恒在列）。

### 3.3 查询单个租户

```
GET /api/tenants/{tenant_id}
```

响应 `200` 为单个租户对象；不存在 → `404`。

### 3.4 更新租户

```
PATCH /api/tenants/{tenant_id}
```

可更新字段（均为可选，只更新显式传入的字段）：

| 字段 | 说明 |
| :--- | :--- |
| `name` | 显示名 |
| `dify_dataset_id` | 换绑知识库（后续上传/校验即路由到新库，历史数据不迁移） |
| `status` | `active`（启用）/ `disabled`（禁用；其 Key 立即失效，请求返回 401） |

```bash
# 禁用某租户
curl -X PATCH http://<host>:18000/api/tenants/acme \
  -H "X-Admin-Key: $RAG_ADMIN_KEY" -H "Content-Type: application/json" \
  -d '{"status":"disabled"}'
```

### 3.5 轮换租户 API Key

```
POST /api/tenants/{tenant_id}/rotate-key
```

响应同创建接口：`{"tenant": {...}, "api_key": "rt-新key"}`。**旧 Key 立即失效**。不存在 → `404`。

### 3.6 删除租户

```
DELETE /api/tenants/{tenant_id}
```

- 成功 → `204 No Content`；不存在 → `404`；试图删除 `default` → `400`。
- 删除仅移除租户身份（Key 立即失效）。**其名下的台账行、文档元数据、文件目录保留**，成为孤儿数据；
  如需彻底清理，请在删除后另行清理数据库行与 `data/.../{tenant_id}/` 目录（或先记下目录，删除后手工移除）。
- 不会删除 Dify 侧的知识库（dataset 归属 Dify 控制台管理）。

---

## 4. 业务 API（租户）

以下接口均支持 `X-API-Key`；不带则为 `default` 租户。所有返回的数据范围严格限定在当前租户内。

### 4.1 上传与入库

#### 单文件上传（可自动全流程入库）

```
POST /api/upload/single        (multipart/form-data)
```

| 参数 | 位置 | 必填 | 说明 |
| :--- | :--- | :--- | :--- |
| `file` | form-data | 是 | 文件本体（PDF / DOCX / DOC / PPTX / XLSX / HTML） |
| `auto_ingest` | form-data | 否 | 默认 `true`：上传后自动执行 parse → chunk → dify（仅本文件） |
| `profile_id` | form-data | 否 | 配置方案 ID；留空使用当前激活方案（未配置任何方案时拒绝处理） |

```bash
curl -X POST http://<host>:18000/api/upload/single \
  -H "X-API-Key: $TENANT_KEY" \
  -F "file=@./规范.pdf" \
  -F "auto_ingest=true"
```

响应 `200`（上传阶段结果）：

```json
{
  "ok": true,
  "filename": "规范.pdf",
  "stem": "规范",
  "md5": "7cddbc92d61cdeaa23faaee99c5bc1d5",
  "size": 6391,
  "saved_path": "/opt/ragsystem/data/pending/acme/规范.pdf",
  "manifest_row_added": true,
  "error": null
}
```

`auto_ingest=true` 时，响应中还会带流水线报告（`pipeline` 字段），含各阶段耗时与结果：

```json
{
  "ok": true, "...": "...",
  "pipeline": {
    "status": "ok",
    "duration_ms": 91166,
    "step_timings_ms": {"scan": 0, "parse": 32739, "chunk": 51, "dify": 58372},
    "target_stems": ["规范"],
    "parse": {"parsed": 1, "skipped_done": 0, "failed": 0, "actions": [{"filename": "规范.pdf", "action": "parsed", "duration_ms": 32661}]},
    "chunk": {"chunked": 1, "chunk_count": 21, "total_chars": 18342, "image_count": 3},
    "dify":  {"uploaded": 1, "actions": [{"stem": "规范", "action": "uploaded", "dify_doc_id": "883b2fab-8950-4cff-b1dc-baacdd1e5ce0"}]}
  }
}
```

- 同名文件重复上传：覆盖台账行并按新文件重新入库（按 MD5 去重/重命名）。
- 首次入库成功后，切分产物归档至 `output/{tenant}/`，Dify 侧生成文档（可用 `dify_doc_id` 定位）。

#### 批量上传

```
POST /api/upload/batch         (multipart/form-data)
```

参数同单文件：`files`（多个，最多 600 个）、`auto_ingest`、`profile_id`。逐个文件独立处理，单个失败不影响其他文件。

#### 对已上传文件重跑入库

```
POST /api/upload/single/ingest?filename=规范.pdf[&profile_id=xxx]
```

用于上传时关闭了 `auto_ingest`、或需要重跑全流程的场景。

- 仅处理该文件（不扫描其他文件）。
- 命名租户调用时，**若该文件不属于本租户 → `404`**（防止跨租户触发他人文档）。

### 4.2 台账（manifest）

#### 查询台账

```
GET /api/manifest?limit=100&offset=0
```

| 租户 | 可见范围 |
| :--- | :--- |
| `default`（匿名） | 全部租户的行 |
| 命名租户 | 仅本租户的行 |

响应 `200`：

```json
{
  "total": 1, "limit": 100, "offset": 0,
  "rows": [{
    "seq": null,
    "filename": "规范.pdf",
    "category_l1": null, "category_l2": null, "keywords": null,
    "department": null, "effective_date": null,
    "import_status": "已上传", "process_status": "已入库",
    "verified": null, "process_note": "上传，大小 6391 字节",
    "status": "done",
    "md5": "7cddbc92d61cdeaa23faaee99c5bc1d5",
    "create_time": "2026-09-22 10:00:00"
  }]
}
```

关键字段：`status`（`new` / `parsed` / `chunked` / `done` / `failed`）、`dify_doc_id`（入库后回填）、`chunks`（切分段数）。

#### 更新台账行

```
PATCH /api/manifest/{filename}
```

可更新字段：`seq`、`category_l1`、`category_l2`、`keywords`、`department`、`effective_date`、`verified`、`process_note`
（只更新显式传入的字段；命名租户只能更新本租户的行，否则 `404`）。

### 4.3 文件列表

```
GET /api/files?dir=pending        # 或 dir=input
```

返回当前租户目录下的文件清单：`[{"name":"规范.pdf","size":6391,"mtime":"...","md5":"...","status":"done"}]`。

### 4.4 文档元数据（11 字段）

```
GET  /api/doc-metadata                 # 列表（本租户）
GET  /api/doc-metadata/{stem}          # 单文档
PUT  /api/doc-metadata/{stem}          # 写入/更新
```

字段：`doc_type_primary`、`doc_type_secondary`、`topic_primary`、`topic_secondary`、`core_summary`、
`entity_label`、`attribute_label`、`applicable_scenarios`、`effective_date`、`priority`、`status`。

```bash
curl -X PUT http://<host>:18000/api/doc-metadata/规范 \
  -H "X-API-Key: $TENANT_KEY" -H "Content-Type: application/json" \
  -d '{"doc_type_primary":"行业标准","effective_date":"2026-09-01","priority":1}'
```

> 这些字段会随入库同步为 Dify 文档元数据（由系统在租户自己的 dataset 中懒创建字段）。

### 4.5 Dify 知识库校验（人工校验）

#### 文档列表

```
GET /api/dify/documents?page=1&limit=50&keyword=xxx
```

- 命名租户：`dataset_id` 参数被**忽略**，强制返回本租户绑定的知识库文档。
- `default` 租户：可传 `dataset_id` 指定知识库，不传则用全局默认库。

响应 `200`：

```json
[{
  "id": "883b2fab-8950-4cff-b1dc-baacdd1e5ce0",
  "name": "规范",
  "indexing_status": "completed",
  "enabled": true,
  "word_count": 719,
  "created_at": 1790048498,
  "metadata": [{"id": "built-in", "name": "document_name", "type": "string", "value": "规范"}]
}]
```

#### 分段（chunk）列表

```
GET /api/dify/documents/{doc_id}/segments?keyword=xxx&status=xxx
```

#### 编辑分段（写回 Dify）

```
POST /api/dify/documents/{doc_id}/segments/{seg_id}
Body: {"content": "新的分段文本"}     # 可另含 answer / keywords / enabled 等字段
```

跨租户访问他人 `doc_id` 会被 Dify 侧拒绝（系统只用本租户 dataset 的 Key 调用），返回非 2xx 错误。

### 4.6 解析进度

```
GET /api/parse/progress
```

查询后台解析任务的进度（全局视图，供运维/前端展示）。

---

## 5. 运维 API（全局，不参与租户隔离）

以下接口为**部署/运维**用途，语义上操作全局资源（跨租户），不按 `X-API-Key` 隔离：

| 模块 | 端点 | 说明 |
| :--- | :--- | :--- |
| 流水线 | `POST /api/pipeline/run` · `POST /api/pipeline/dry` | 一键执行 扫描→解析→切分→入库；预演 |
| 扫描 | `POST /api/scan` | 扫描 `input/`（含全部租户目录）更新台账 |
| 解析 | `POST /api/parse` | 批量解析待处理文档（可 `target_stems` 限定） |
| 切分 | `POST /api/chunk` | 批量切分（body 含 `strategy` / `force`） |
| 切分策略 / 配置 | `GET /api/chunk/strategies` · `GET/POST /api/chunk/config` | 策略与参数 |
| Dify 配置 | `GET/POST /api/dify/config` · `GET /api/dify/test` · `GET /api/dify/datasets` | 连通性 / 知识库列表 |
| Dify 批量入库 | `POST /api/dify/upload` | 把切分产物批量推送到知识库（**自动按租户 dataset 路由**） |
| Dify 元数据 | `POST /api/dify/metadata/sync` 等 | 跨租户元数据同步工具（显式传 `dataset_id`） |
| 配置中心 | `/api/config/profiles*` | 切分/处理方案管理 |
| 网站抓取 | `/api/webscrape/*` | 抓取 → 预览 → 确认入库 |
| 清理 | `POST /api/cleanup` · `GET /api/cleanup/status` | 中间产物清理 |
| 健康 | `GET /api/health` | 服务与 MinerU 状态 |

> **生产安全建议**：若后端直接暴露公网，请在 Nginx/网关层对上述运维路径做来源限制
> （仅内网/白名单 IP），或仅将第 3、4 节接口开放给外部租户系统。
> 示例（Nginx）：`location ~ ^/api/(pipeline|scan|parse|chunk|cleanup|config|webscrape)/ { allow 10.0.0.0/8; deny all; }`

---

## 6. 典型接入流程

### 步骤 1：在 Dify 控制台为租户创建知识库

登录 Dify → 知识库 → 创建（如"ACME 知识库"）→ 记下 `dataset_id`。
如需限制可见性，在知识库/成员权限中将该库仅授权给对应应用。

### 步骤 2：在主系统创建租户并领取 Key

```bash
curl -X POST http://<host>:18000/api/tenants \
  -H "X-Admin-Key: $RAG_ADMIN_KEY" -H "Content-Type: application/json" \
  -d '{"tenant_id":"acme","name":"ACME 公司","dify_dataset_id":"<dataset_id>"}'
# → 保存响应中的 api_key（仅显示一次）
```

### 步骤 3：租户侧接入上传 + 入库

在自有后端（不要把 Key 放进浏览器前端）用 `X-API-Key` 调用：

```python
import requests
BASE = "http://<host>:18000"
KEY = "rt-xxxxxxxx"          # 该租户的 Key

r = requests.post(
    f"{BASE}/api/upload/single",
    headers={"X-API-Key": KEY},
    files={"file": ("规范.pdf", open("规范.pdf", "rb"))},
    data={"auto_ingest": "true"},
    timeout=1800,             # 解析+入库耗时较长，按需放大
)
report = r.json()
print(report["pipeline"]["status"], report["pipeline"]["dify"]["actions"])
```

### 步骤 4：查询进度与台账

```python
rows = requests.get(f"{BASE}/api/manifest", headers={"X-API-Key": KEY}).json()["rows"]
# rows[0]["status"] == "done" 表示已入库
# rows[0]["dify_doc_id"] 对应该租户 Dify 库中的文档
```

### 步骤 5（可选）：人工校验

```python
docs = requests.get(f"{BASE}/api/dify/documents", headers={"X-API-Key": KEY}).json()
segs = requests.get(f"{BASE}/api/dify/documents/{docs[0]['id']}/segments",
                    headers={"X-API-Key": KEY}).json()
```

---

## 7. 存储布局与数据归属

```
data/                                  # 宿主机挂载卷（Docker: ./data → /opt/ragsystem/data）
├── pending/                           # 待解析（default 租户）
│   └── acme/                          # 命名租户 → 子目录
├── input/         └── acme/           # 待扫描
├── parsed/        └── acme/{stem}/    # 解析产物（永久保留）
├── chunks/        └── acme/{stem}/    # 切分产物（默认保留 7 天）
├── output/        └── acme/{stem}/    # 入库归档 + 图片静态托管（默认保留 7 天）
├── error/         └── acme/           # 处理失败（默认保留 30 天）
└── single_uploads/└── acme/           # 上传中转
```

数据库表（PostgreSQL / MySQL，应用启动时自动建表迁移）：

| 表 | 租户列 | 主键 |
| :--- | :--- | :--- |
| `manifest` | `tenant_id`（默认 `default`） | `(tenant_id, filename)` |
| `doc_metadata` | `tenant_id` | `(tenant_id, filename)` |
| `tenants` | `tenant_id` | `tenant_id` |
| `webscrape_records` 等 | — | 全局 |

> **存量升级**：升级前已有的数据自动归属 `default` 租户（迁移脚本自动补 `tenant_id` 列并重建主键），
> 原有匿名调用行为完全不变。

---

## 8. 常见问题（FAQ）

**Q1：调用返回 `403 管理接口未启用`？**
服务端未配置 `RAG_ADMIN_API_KEY`。在 `backend/.env` 中配置并重启后端（见 `deploy/README.md`）。

**Q2：租户上传后 `GET /api/manifest` 看不到文件？**
先确认请求确实带了该租户的 `X-API-Key`；命名租户只能看到 `tenant_id` 等于自己的行。用管理员接口
`GET /api/tenants/{tenant_id}` 核对绑定关系。

**Q3：能否用租户 Key 读取其他租户的知识库？**
不能。命名租户的 Dify 路由是服务端强制的：`dataset_id` 请求参数被忽略，且只使用该租户绑定的库；
上传、台账、目录同理。跨租户 `ingest` 会返回 `404 当前租户下不存在文件`。

**Q4：Key 泄露怎么办？**
`POST /api/tenants/{tenant_id}/rotate-key` 立即作废旧 Key 并签发新 Key；或 `PATCH` 将 `status` 置为
`disabled` 临时停用。

**Q5：如何给租户改名/换绑知识库？**
`PATCH /api/tenants/{tenant_id}`，传 `name` 或 `dify_dataset_id`。换绑只影响后续上传与查询路由，
历史文档仍在旧库中（如需迁移请在 Dify 控制台操作）。

**Q6：删除租户后数据还在吗？**
在。删除只失效身份 Key；台账/元数据行与文件目录保留。如需彻底删除，请在删除租户后清理
`data/.../{tenant_id}/` 与数据库对应行（`WHERE tenant_id = '...'`）。

**Q7：`auto_ingest=true` 请求超时？**
解析（尤其扫描件）可能耗时数分钟。建议：客户端超时设置 ≥ 30 分钟，或改用
`auto_ingest=false` 先上传、再用 `POST /api/upload/single/ingest` 触发并异步轮询
`GET /api/manifest` 的 `status` 字段。
