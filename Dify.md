# Dify 集成说明（本系统）

本系统通过 **Dify Cloud / Knowledge API** 完成文档入库与人工校验，所有逻辑在
`backend/app/services/dify_uploader.py` / `dify_ingest.py` 与
`backend/app/api/dify.py` 中实现。

## 1. 配置（.env，见 backend/.env.example）

| 环境变量 | 说明 |
| --- | --- |
| `RAG_DIFY_API_URL` | Dify 服务地址，`https://xxx/v1`（须 HTTPS，HTTP 会 301） |
| `RAG_DIFY_API_KEY` | **Dataset/Knowledge API Key**（`dataset-` 前缀），用于知识库写操作 |
| `RAG_DIFY_APP_API_KEY` | **App API Key**（`app-` 前缀），仅用于 `/files/upload` 上传图片（Knowledge Key 无此权限） |
| `RAG_DIFY_DATASET_ID` | 目标知识库 ID |
| `RAG_DIFY_INDEXING_TECHNIQUE` | 索引技术：`high_quality`（embedding）/ `economy`（关键词） |
| `RAG_DIFY_DOC_FORM` | 文档形态：`text_model`（按段）/ `hierarchical_model`（父子）/ `qa_model`（问答对） |
| `RAG_DIFY_TIMEOUT` | HTTP 超时（默认 60s） |
| `RAG_DIFY_INDEXING_WAIT_TIMEOUT` | 等待 indexing 完成超时（默认 120s） |
| `RAG_DIFY_INDEXING_POLL_INTERVAL` | 索引轮询间隔（默认 2s） |
| `RAG_DIFY_SEGMENTS_PER_REQUEST` | 批量 add_segments 每批条数（默认 30） |
| `RAG_DIFY_MAX_SEGMENT_CHARS` | 单段字符上限保护性截断（默认 5000，对齐 Dify 分段上限） |
| `RAG_DIFY_MAX_RETRIES` / `RAG_DIFY_RETRY_BACKOFF` | 重试次数 / 退避因子（4xx 不重试，5xx / 网络重试） |
| `RAG_DIFY_SKIP_FILE_UPLOAD` | `true` 时跳过 `/files/upload`，图片以公网 URL 直接写入 markdown（需 `RAG_PUBLIC_BASE_URL`） |

## 2. 入库流程（dify_uploader.DifyClient）

1. **创建文档**：`POST /datasets/{ds}/document/create_by_text` 创建空文档。
2. **等待索引**：轮询 `indexing_status` 直到 `completed`（`wait_document_ready`），
   完成后才可 `add_segments`。
3. **批量写分段**：`add_segments` 按 `RAG_DIFY_SEGMENTS_PER_REQUEST` 分批提交；
   - 超长分段（> `RAG_DIFY_MAX_SEGMENT_CHARS`）先按句号拆分为多段；
   - 每段携带 `keywords`、`attachment_ids` 等元数据（拆分出的后续段不重复携带）；
   - 提交后**核对落库**（拉取分段列表比对数量），发现 Dify 静默丢弃时告警。
4. **图片处理**：
   - `RAG_DIFY_SKIP_FILE_UPLOAD=false`：先 `POST /files/upload`（需 App API Key）
     拿 `file_id`，`add_segments` 时写入 `attachment_ids`；
   - `RAG_DIFY_SKIP_FILE_UPLOAD=true`（默认）：跳过上传，图片以
     `![](公网URL)` 直接内嵌在 markdown 中（Dify 索引时会拉取并内嵌）；
   - 对带 `attachment_ids` 的段，入库后调用 `update_segment` 以便校验页预览图片。
5. **元数据**：`list_metadata_fields` / `create_metadata_field` /
   `batch_update_document_metadata` 维护知识库元数据字段。

## 3. 人工校验接口（backend/app/api/dify.py）

| 接口 | 用途 |
| --- | --- |
| `GET /api/dify/config` · `POST /api/dify/config` | 校验页展示 / 保存 Dify 连接信息 |
| `GET /api/dify/test` | 连通性测试 |
| `GET /api/dify/datasets` | 数据集列表（供配置选择） |
| `POST /api/dify/upload` | 上传 chunks 到 Dify 知识库 |
| `GET /api/dify/documents` | 校验页：文档列表（分页 / 关键字） |
| `GET /api/dify/documents/{doc_id}/segments` | 校验页：文档的分段列表 |
| `POST /api/dify/documents/{doc_id}/segments/{seg_id}` | 校验页：编辑分段 content / enabled 后写回 |
| `GET /api/dify/metadata/fields` · `POST /api/dify/metadata/init-fields` · `POST /api/dify/metadata/sync` | 元数据字段管理 |

> 人工校验页面（前端 `VerifyPage`）从 Dify 实时拉取文档 / 分段，
> 分段正文中的 `![](url)` 图片在「渲染预览」中直接显示；编辑保存写回 Dify。
