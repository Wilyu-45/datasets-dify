
---

# 基于 MinerU 解析产物的切分策略（图片内联版）

## 一、核心原则

| 原则 | 说明 |
|------|------|
| **图片不独立成块** | 图片作为内联元素，嵌入到其上下文所在的文本 chunk 中，保留 MD 原生图片语法 `![](images/xxx)` 内联标记 |
| **保留阅读顺序** | 严格按照页面顺序，图片出现在其原文位置（文字 → 图片 → 文字） |
| **标题层级传递** | 每个 chunk 携带完整的 `title_path`，追溯性强 |
| **长度可控** | 每个 chunk 在1500中文字符以下 |
| **★ 图片数可控（2026-07 新增）** | **每个 chunk 内 `image_refs` 数量 ≤ settings.chunk_max_images_per_segment（默认 10），与 Dify `SINGLE_CHUNK_ATTACHMENT_LIMIT` 对齐，避免 `add_segments` 400 错误** |

---

## 二、数据源选择

| 文件 | 用途 |
|------|------|
| `_content_list_v2.json` | **主解析数据**，提供类型、层级、图片路径、标题栈 |
| `.md` 文件 | 纯文本内容来源（备用，以防 JSON 中文本截断） |
| `images/` 文件夹 | 图片文件，入库时需上传并关联到 chunk 的元数据中 |

> **推荐**：以 `_content_list_v2.json` 为主数据源，其已按页面顺序扁平化并标注了类型。

---

## 三、切分策略详细步骤

### 步骤 1：遍历并构建“有序块序列”

按页面顺序遍历 `_content_list_v2.json` 中每一页的元素，生成一个有序的**块列表**，每个块包含：

| 块类型 | 关键字段 | 说明 |
|--------|----------|------|
| `title` | `content.title_content` + `level` | 页面标题（含层级） |
| `paragraph` | `content.paragraph_content` | 纯文本段落 |
| `image` | `img_path` + `image_caption` | 图片及其图注 |

**丢弃**：`page_header`、`page_footer`、`page_number`（噪音）。

---

### 步骤 2：维护标题栈，确定当前归属

- 遇到 `title` 时，根据其 `level` 更新 `title_stack`：
  - `level=1`：重置栈为 `[title]`
  - `level=2`：追加到栈中（替换同层级的最后一个）
- 后续所有 `paragraph` 和 `image` 均继承当前的 `title_stack`。

示例：
```
title_stack = ["4", "4.2", "4.2.1"]  → 后续内容属于 "4.2.1"
下一个 title level=2: "4.3 候诊区"   → 栈变为 ["4", "4.3"]
```

---

### 步骤 3：构建 Chunk 缓冲区

维护一个 `chunk_buffer`，包含：
- `text_parts`：文本片段列表（含普通文本 + 图片占位符）
- `image_refs`：图片路径列表（用于后续上传）
- `title_path`：当前章节路径（字符串）
- `page_num`：当前页面
- `char_count`：累计字符数（不含占位符）

```python
chunk_buffer = {
    "text_parts": [],        # ["正文内容", "![](images/60de63a2...jpg)", "后续正文"]
    "image_paths": [],       # ["images/xxx.jpg"]
    "title_path": "4.2 入口外观",
    "page_num": 4,
    "char_count": 0
}
```

---

### 步骤 4：填充缓冲区与切分触发

遍历“有序块序列”，按以下规则处理：

#### 4.1 遇到 `paragraph`

1. 提取纯文本内容。
2. 若 `char_count + len(text) > 600`，先**落盘当前 chunk**，再新建缓冲区。
3. 将文本加入 `text_parts`，更新 `char_count`。

#### 4.2 遇到 `image`

1. **不落盘**，而是向缓冲区插入 **MD 原生图片语法**：`![](images/{图片文件名})`（不替换为 `[图: ...]` 占位符；图注 text 已由 MinerU 输出为相邻正文，保持原样）。
2. 将 `img_path` 加入 `image_paths` 列表（用于入库时上传）。
3. **`char_count` 不增加**（图片语法不计入文本长度阈值，避免图片导致误切）。
4. **★ 2026-07 增加**：`image_paths_count` 单独累计。

#### 4.3 遇到 `title`

1. 先检查当前缓冲区是否为空：
   - 若非空，先**落盘**当前 chunk。
2. 更新 `title_stack`，新缓冲区继承新栈。
3. 若 `title` 本身有文本内容（如 `4.1 基本原则`），将其作为新 chunk 的第一段文本加入缓冲区。

---

### 步骤 4A：图片超限切分触发条件 ★ 2026-07-31 新增

**核心变更**：步骤 4 的落盘判定从**单维度**（仅字符数）升级为**双维度**（字符数 + 图片数）。

#### 4A.1 触发条件

任意一个满足即触发落盘：

| 维度 | 阈值 | 来源 |
|------|------|------|
| 字符数 | `chunk_target_chars = 1500` | `settings.chunk_target_chars` |
| 图片数 | `chunk_max_images_per_segment = 10` | `settings.chunk_max_images_per_segment`（与 Dify `SINGLE_CHUNK_ATTACHMENT_LIMIT` 对齐） |

判定伪代码：

```python
def should_finalize(buffer, next_block):
    return (
        buffer.char_count + len(next_block.text) > 1500
        or
        buffer.image_count + next_block.image_count > 10
    )
```

#### 4A.2 适用阶段

| 阶段 | 是否启用 | 说明 |
|------|----------|------|
| 正文 L1（一级标题间）整体合并 | ✅ | 4.1 整体 1 段的条件新增图片数维度 |
| 正文 L2 贪心合并 | ✅ | 步骤 4.3 title 触发的合并条件新增图片数 |
| 正文 L3 贪心合并 | ✅ | 同 L2 |
| 附录贪心合并 | ✅ | 附录按图片数独立切分（cutrule.md 4.3） |
| 封面/目录/前言/参考文献 | ⚠️ 部分 | 整体 1 段，若图片数 > 10 也强制切分（罕见场景） |

#### 4A.3 单组就超 max_images 的处理

**原则**：**不强行拆图**——避免破坏"图片与上下文同段"的规则（cutrule.md 5.2）。

- 单组（L2 / L3 / 附录）本身就 > 10 张图 → 原样保留为 1 段。
- 入库阶段（`dify_ingest`）兜底：若 `add_segments` 报 400，则把超出 10 张上限的图片降级为 URL-only 模式（仅在 markdown 里写 URL，不进 `attachment_ids`），保证入库不中断。

#### 4A.4 典型场景示例

**场景 A**：WST 809 `4.11 卫生间` 共 13 张图
- 旧逻辑：13 张图全在 1 个 L3 段，字符数 950 ✅ → 入库时 Dify 400 ❌
- 新逻辑：贪心合并时累计图片数，第 6 张时落盘，第 10 张时再落盘 → 3 个段（6/4/3 张图）✅

**场景 B**：WST 809 附录 A~J，每个附录 3 张图
- 旧逻辑：附录 A~F 合并为 1 段（18 张图）→ Dify 400 ❌
- 新逻辑：附录 A~C 合并（9 张图）→ 落盘；附录 D~F 合并（9 张图）→ 落盘；附录 G~I 合并（9 张图）→ 落盘；附录 J 单段（2 张图）→ 4 段 ✅

#### 4A.5 与 cutrule.md 的对应关系

| 策略文档 | 切分规则文档 |
|----------|--------------|
| 本节 4A.1 触发条件 | 规则 3.5.2 / 4.3.1 合并条件 |
| 本节 4A.2 适用阶段 | 规则 3.5.5 与原有规则的关系 |
| 本节 4A.3 单组超限处理 | 规则 3.5.3 / 4.3.2 |
| 本节 4A.4 场景示例 | 规则 3.5.4 / 4.3.3 |

---

### 步骤 5：落盘（finalize）操作

当触发落盘时，将 `chunk_buffer` 转换成最终 chunk 结构：

```json
{
  "chunk_id": "WST809-4.2.1-001",
  "doc_id": "WST 809—2022",
  "title_path": "4.2 入口外观 > 4.2.1",
  "content": "在入口的醒目位置悬挂带有基层医疗卫生机构标识的牌匾。![](images/60de63a2...jpg)入口应造型简洁...",
  "image_paths": ["images/60de63a2...jpg"],
  "page_num": 4,
  "char_count": 312,
  "start_bbox": [112, 114, 631, 129],
  "end_bbox": [119, 231, 900, 563]
}
```

**关键点**：
- `content` 是拼接后的完整文本，其中图片以 MD 原生语法 `![](images/...)` 形式内联嵌入。
- `image_paths` 是该 chunk 中所有图片的路径列表，用于后续入库上传。

---

### 步骤 6：长章节的二次处理（优化）

若某个 chunk 的 `char_count` 仍超过 600，但内部没有标题可拆分（如 `4.11 卫生间` 有多条细则），则：
- 在最近的句号（`。`）或分号（`；`）处切分。
- 每个子块保留相同的 `title_path`，但增加后缀编号（如 `4.11-001`、`4.11-002`）。
- 子块之间添加 **overlap（重叠）**：上一个 chunk 的最后 50 字符重复到下一个 chunk 的开头。

---

## 四、完整流程图

```mermaid
flowchart TD
    A[遍历 _content_list_v2.json] --> B{元素类型}
    B -->|title| C[更新标题栈]\n若缓冲区非空则落盘
    B -->|paragraph| D[追加文本到缓冲区]\n若 chars > 1500\n或 images > 10\n则切分落盘
    B -->|image| E[内联 ![]() 图片语法到缓冲区]\n记录 image_path\n累加 image_count
    B -->|header/footer| F[丢弃]
    C --> G[继续遍历]
    D --> G
    E --> G
    F --> G
    G --> H{遍历结束?}
    H -->|是| I[落盘剩余缓冲区]
    I --> J[生成Chunk列表]
    J --> K[图片统一上传关联\nattachment_ids ≤ 10]
```

---

## 五、入库（Dify）时的图片处理（此为下一步处理，于此处为补充说明）

由于图片不独立成块，需在入库时**将图片与所属 chunk 关联**：

| 步骤 | 操作 |
|------|------|
| 1 | 遍历所有 chunk，收集 `image_paths` 列表 |
| 2 | 调用 Dify `upload_file` 接口上传图片，获取 `file_id` 映射 |
| 3 | 在调用 `add_segment` 时，通过 `metadata.attachment_ids` 字段携带 `file_ids` 列表 |
| 4 | 前端渲染时，根据 `![](images/...)` 占位符，结合 `metadata.attachment_ids` 渲染图片 |

**示例请求体**：
```json
{
  "content": "... ![image](https://dify.17vision.com/files/xxx/file-preview) ...",
  "metadata": {
    "title_path": "4.2 入口外观 > 4.2.1",
    "page_num": 4,
    "image_paths": ["images/60de63a2...jpg"],
    "attachment_ids": ["file_xxx", "file_yyy"]
  }
}
```

### 5.1 ★ 2026-07 attachment_ids 数量限制与降级策略

| 限制 | 值 | 来源 |
|------|----|------|
| `attachment_ids` 数量 | ≤ 10 | Dify 服务端 `SINGLE_CHUNK_ATTACHMENT_LIMIT`（默认 10） |
| 超出后果 | `add_segments` 返回 400 | `Exceeded maximum attachment limit of 10` |

**双层防御**：

1. **第一层（切分阶段）**：步骤 4A 把单段图片数控制在 ≤ 10，**99% 场景在此层已解决**。
2. **第二层（入库阶段兜底）**：`dify_ingest` 捕获 400 错误，对超出 10 张上限的图片**降级为 URL-only 模式**（仅在 markdown 里写带签名 URL，不进 `attachment_ids`），保证入库不中断。

---

## 六、切分效果预估

| 指标 | 预估值 |
|------|--------|
| 文本 chunk 数量 | 30~50 个（按章节/子章节切分） |
| 图片引用位置 | 保留在 chunk 内，不单独落盘 |
| 最大 chunk 长度 | 1500 中文字符 |
| 图片独立性 | **否**，完全嵌入文本 chunk 中 |
| **★ 单段最大图片数（2026-07 新增）** | **≤ 10 张，与 Dify `SINGLE_CHUNK_ATTACHMENT_LIMIT` 对齐** |
| **★ 图片超限分段的段数（2026-07 新增）** | **典型文档 +2~6 段（如 4.11 卫生间 13 图 → 3 段、附录 18 图 → 4 段）** |

---

## 七、后续优化方向

1. **图片 caption 缺失时**：可调用轻量级 VLM（如 BLIP）生成简短描述，追加到图片语法前后文本中（当前实现保留原样 `![](images/xxx)`）。
2. **表格内容**：`table` 类型保留 HTML `<table>` 文本与 caption，与普通文本混合；表格超过阈值（`chunk_table_row_threshold`，默认 20 行）时独立成段（见 cutrule.md 规则 5.7）。
3. **检索增强**：在 `![](images/...)` 图片语法周围，可主动加入图片所在章节的摘要，提升图片相关检索命中率。

---

## 八、多策略切分引擎 ★ 2026-08-24 新增

`backend/app/services/chunk_strategies.py` 实现了**可配置的多策略切分引擎**，把业界主流的 8 种切分策略全部融入系统。策略只影响 **body / appendix** 区域；封面/目录/前言/参考文献保持原逻辑。所有策略均保留**特殊内容保护**（Markdown 表格 / LaTeX 公式不被切断）。

### 8.1 策略总览

| 策略 key | 名称 | 核心思想 | 依赖 | 适用场景 |
|:---|:---|:---|:---|:---|
| `structure` | 结构切分 | 标题层级 + 贪心合并 + 句号二次切分（**默认**，即原逻辑） | 无 | 大多数结构化文档 |
| `recursive` | 递归切分 | 段落（块）边界 → 句子边界，递归分隔符贪心合并 | 无 | 结构复杂的通用文档 |
| `fixed` | 固定长度切分 | 按固定字符数硬切，可配 overlap | 无 | 日志/代码，或基准测试 |
| `sentence` | 句子级切分 | 按句末标点切分，保留自然语义边界 | 无 | 普通文章、报告、FAQ |
| `semantic` | 语义切分 | Embedding 相似度低谷处切分（主题转变点） | **需 Embedding** | 专业领域高精度检索 |
| `parent_child` | 父-子切分 | 大父块（上下文）+ 小个子块（检索），metadata 用 `parent_id` 关联 | 无 | 需精确检索 + 完整上下文 |
| `late_chunking` | 晚切分 | 先整文 Embedding 感知全局上下文，再按主题相关度切分 | **需 Embedding** | 存在大量指代/歧义的长文档 |
| `llm` | LLM 切分 | 大模型自主决定切分点（JSON 数组返回） | **需 LLM API（OpenAI 兼容）** | 小规模高质量文档（默认关闭） |

### 8.2 选择建议

- **通用文档** → `structure`（默认）或 `recursive`，均衡稳健。
- **结构化文档（说明书/论文/标准）** → `structure`，保留标题层级可溯源。
- **普通文本 / FAQ** → `sentence`，语义边界最自然。
- **高精度检索场景** → `semantic` / `late_chunking`（需配置 Embedding）。
- **精确检索 + 丰富上下文** → `parent_child`（子块入库检索，父块提供完整上下文）。
- **小规模极致质量** → `llm`（成本高，显式开启）。

### 8.3 配置项（`backend/app/config.py`）

| 配置 | 默认 | 说明 |
|:---|:---|:---|
| `chunk_strategy` | `"structure"` | 默认切分策略 |
| `chunk_fixed_size_chars` | `800` | `fixed` 单块目标字符数 |
| `chunk_fixed_overlap_chars` | `100` | `fixed` 相邻块重叠字符数 |
| `chunk_semantic_threshold` | `0.78` | `semantic` / `late_chunking` 相似度阈值（低于则切分） |
| `chunk_parent_size_chars` | `1500` | `parent_child` 父块目标字符数 |
| `chunk_child_size_chars` | `400` | `parent_child` 子块目标字符数 |
| `chunk_llm_enabled` | `false` | `llm` 开关 |
| `chunk_llm_chunk_prompt` | … | `llm` 提示词 |
| `llm_api_base_url` | `""` | `llm` 调用的模型 API 地址（OpenAI 兼容 Chat Completions，如 `https://api.deepseek.com/v1`） |
| `llm_api_key` | `""` | `llm` 调用的模型 API Key |
| `llm_model` | `""` | `llm` 使用的模型名（如 `deepseek-chat` / `gpt-4o-mini`） |
| `chunk_embedding_api_url` | `""` | 自定义 Embedding 端点（OpenAI 兼容 / Dify 格式）；留空用 Dify |
| `chunk_embedding_api_key` | `""` | 自定义 Embedding Key；留空用 `dify_api_key` |

### 8.4 降级策略

- `semantic` / `late_chunking`：未配置 Embedding 或调用失败 → **自动降级为 `sentence`**。
- `llm`：未开启 / 缺少 `llm_api_base_url`·`llm_api_key`·`llm_model` / 调用失败 → **自动降级为 `structure`**。
- 未知策略名 → 归一化为 `structure`（不会报错）。

### 8.5 调用方式

1. **前端**：`切分策略` 下拉框（默认选中后端默认值），切换后执行切分。
2. **API**：`POST /api/chunk` body 增加 `"strategy": "recursive"`；`GET /api/chunk/strategies` 返回策略列表。
3. **命令行/脚本**：`chunker.chunk_parsed(..., strategy="parent_child")` 或 `chunker.chunk_document(parsed_dir, chunks_dir, strategy="fixed")`。
4. **元数据**：`chunk_metadata.json` 顶层 `strategy` 字段记录本次切分策略；`parent_child` 子块带 `parent_id` 关联父块；前端 Chunk 预览新增"策略"列。

---
