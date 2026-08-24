"""应用配置。

通过 pydantic-settings 从 `backend/.env` 或环境变量加载。
所有路径均解析为绝对路径，确保无论从哪里启动都行为一致。

★ 加载优先级（自定义，见 Settings.settings_customise_sources）：
    1) 进程环境变量（仅作为“启动调试”兑底）
    2) backend/.env 文件（主配置：业务运行时调这里）
    3) init kwargs（测试 / 代码构造）

背景：pydantic-settings 默认优先级是 环境变量 > .env。
      这导致如果在某个 PowerShell / 终端里 `$env:RAG_DIFY_DATASET_ID=xxx`
      临时设过，即使改了 .env 也不会生效（要重启+关闭那个 shell）。
      为贴合用户直觉“改 .env 就生效”，这里反之：.env 优先于环境变量。
      如果需要环境变量临时覆盖某个 .env 字段，可以在 .env 改后重启 shell。
"""

from __future__ import annotations

from pathlib import Path
from typing import Tuple

from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)


# 仓库根目录：<repo>/backend/app/config.py -> <repo>
REPO_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    """运行时配置。"""

    # ---- 数据目录 ----
    data_root: Path = REPO_ROOT / "data"
    input_dirname: str = "input"
    pending_dirname: str = "pending"
    output_dirname: str = "output"
    parsed_dirname: str = "parsed"
    chunks_dirname: str = "chunks"
    error_dirname: str = "error"
    manual_fix_dirname: str = "manual_fix"
    logs_dirname: str = "logs"

    manifest_filename: str = "manifest.xlsx"

    # ---- 行为 ----
    scan_chunk_size: int = 65536  # 64 KB
    # 扩展名按优先级排序：.pdf / .docx 最常用，放最前。
    # 用户在 Excel 中填「文件名称」时常省略后缀，扫描时按此顺序在 input/ 中尝试补全。
    allowed_extensions: tuple[str, ...] = (
        ".pdf", ".docx", ".doc",
        ".pptx", ".xlsx",
        ".png", ".jpg", ".jpeg", ".tiff", ".tif",
    )

    # ---- 日志 ----
    log_level: str = "INFO"
    log_retention_days: int = 15

    # ---- MinerU API（plan.md §3.2）----
    # MinerU 自带 FastAPI 服务：mineru-api 3.x。
    # 同步解析接口：POST {mineru_api_url}/file_parse（multipart/form-data 上传）
    # 重要参数：response_format_zip=true 一次性返回所有产物的 ZIP
    mineru_api_url: str = "http://192.168.31.165:7860"
    # 是否以 ZIP 形式返回所有产物（推荐 true：一个文档一个文件夹，直接解压）
    mineru_response_format_zip: bool = True
    # 解析后端（推荐 vlm-engine / hybrid-engine / vlm-http-client / hybrid-http-client；
    #              不推荐 pipeline：纯 OCR 流水线，对扫描件 / 复杂版面效果差）
    # ★ 2026-07 起强制高质量模型：对扫描件 OCR 效果排序：
    #     vlm-engine   > hybrid-engine（VLM + 文本） > pipeline
    mineru_backend: str = "hybrid-engine"
    # hybrid-engine 的解析强度（medium / high）。默认 high：保证效果，启用 image analysis。
    #   - medium：默认，速度更快，但不支持 image analysis
    #   - high  ：极致精度 + image analysis，慢一些但效果最好
    mineru_backend_effort: str = "high"
    # 是否强制高质量后端。开启后 MinerUClient 会校验 backend：
    #   - 不在白名单（vlm-engine / hybrid-engine / vlm-http-client / hybrid-http-client）
    #     时打印 WARNING 并自动切换到 hybrid-engine，避免误用 pipeline。
    mineru_enforce_high_quality: bool = True
    # 语言列表（pipeline 后端使用，影响 OCR 准确率）
    mineru_lang_list: tuple[str, ...] = ("ch",)
    # 是否启用公式 / 表格解析
    mineru_formula_enable: bool = True
    mineru_table_enable: bool = True
    # ---- ★ 输出产物开关（关键）----
    # MinerU 默认只 return_md=true，其余 4 项都是 false。
    # 我们的目标是拿到所有产物（md + middle.json + content_list + images + ...），
    # 所以全部默认开启。可在 .env 中按需关闭。
    mineru_return_md: bool = True
    mineru_return_middle_json: bool = True        # 页面/块级结构（切分依赖）
    mineru_return_model_output: bool = True       # 模型原始输出（debug 用）
    mineru_return_content_list: bool = True       # 结构化内容列表
    mineru_return_images: bool = True             # 提取的图片
    # 单次请求超时（秒）。MinerU 解析大文件可能较久。
    mineru_api_timeout: int = 108000
    # ---- ★ 2026-08-06 重试策略：适应 API 端自杀后自动重启的场景 ----
    # MinerU 在解析超长文档时服务端会自杀（本地部署常见，依赖
    # mineru-router / supervisor 自动拉起服务）。重试时不能太短，
    # 否则连续失败 3 次（1+5+25s）服务端还没起来。
    # 重试退避公式：wait = min(initial * factor^(attempt-1), max_wait)
    #  默认 30s / 2.0x / 300s → 30s, 60s, 120s（最多 3 次）
    mineru_max_retries: int = 3
    mineru_retry_initial_wait: float = 15   # 首次重试等 30s（给 API 重启时间）
    mineru_retry_backoff_factor: float = 2.0  # 指数退避倍数
    mineru_retry_max_wait: float = 300.0      # 单次重试最大等待（5 分钟，防无限等）
    # 兼容保留旧字段：以前的 5.0 倍重试公式；新代码不再读该字段
    mineru_retry_backoff: float = 5.0  # noqa: F811 保留向后兼容
    # ---- ★ 2026-08-06 长文档路由：超长页数自动切换为精准解析 API ----
    # 本地部署配置 24G 内存 + RTX 3080 可以跑 vlm-engine；
    # vlm-engine 在超长文档场景下比 hybrid-engine 稳定（不走文本路径，避免
    # 某些复杂 CMap/编码问题诱发 API 自杀）。
    # 路由逻辑：当 PDF 页数 ≥ threshold 时，自动用 long_doc_backend
    # 替代默认 backend。0 = 关闭路由（所有文档都用默认 backend）。
    mineru_long_doc_pages_threshold: int = 15
    mineru_long_doc_backend: str = "vlm-engine"   # 精准解析 API（推荐 vlm-engine）
    mineru_long_doc_effort: str = "high"          # 仅 hybrid-engine 生效；vlm-engine 会忽略
    # 是否在请求中带 Authorization Bearer 头（可选，由 .env 提供）
    mineru_api_token: str = ""
    # 是否启用 .doc 旧 OLE 格式预检测（MinerU 不支持 .doc 旧格式；开启后客户端会
    # 读取文件 magic bytes 提前拒绝，给用户更友好错误，而不是让 MinerU 返回 400）
    mineru_reject_legacy_doc: bool = True

    # ---- 切分（plan.md §3.3）----
    # 主阈值：单个 chunk 的目标字符上限（参考 cutrule.md：1500 中文字符）
    chunk_target_chars: int = 1500
    # 硬上限：超过则强制切分（防极端长段落）
    chunk_hard_limit: int = 1800
    # 句号/分号二次切分时，每个子块目标字符（cutrule.md 3.4 情况二：800~1500）
    chunk_split_target: int = 1200
    # 二次切分的 overlap 字符数（cutrule.md 3.4 情况二：overlap 100）
    chunk_overlap: int = 100
    # 附录贪心合并阈值（cutrule.md 4.1：≤1500）
    chunk_appendix_threshold: int = 1500
    # 单段最大图片数（cutrule.md 3.5 / 4.3）
    # ★ 与 Dify 端 SINGLE_CHUNK_ATTACHMENT_LIMIT（默认 10）对齐：
    #   切分阶段控制单段 attachment_ids ≤ 此值，避免 add_segments 报 400
    #   `Exceeded maximum attachment limit of N`。
    #   自托管环境如调大了 Dify 端限制，可同步调大此值。
    chunk_max_images_per_segment: int = 10
    # ★ 2026-08-13 表格独立成段：行数超过此阈值的表格自动拆分为多段
    chunk_table_row_threshold: int = 20
    # ★ 2026-08-20 表格独立成段字符兜底：可见文本超过此值（即使行数很少）
    #   也按行拆分。背景：WS 628-2 附录 B 表格仅 3 行但 11662 字符，
    #   行数阈值不触发 → 整表超大 → Dify 静默丢弃分段。取与
    #   dify_max_segment_chars 相同的 5000 保守值。
    chunk_table_max_chars: int = 5000
    # 参考文献条目识别正则：[1] / [2] / [J1] / 1) 等
    chunk_ref_pattern: str = r"^\s*[\[【\(（]\s*\d+[\]】\)）]"

    # ---- 切分策略（chunk_strategies.py）----
    # 默认切分策略：structure / recursive / fixed / sentence /
    #             semantic / parent_child / late_chunking / llm
    chunk_strategy: str = "structure"
    # fixed 固定长度切分：单块目标字符数 + overlap
    chunk_fixed_size_chars: int = 800
    chunk_fixed_overlap_chars: int = 100
    # semantic / late_chunking 语义断裂相似度阈值（相邻句子/句子与文档向量
    # 相似度低于该值时视为语义转折 → 切分）。0~1，越小越不易切分。
    chunk_semantic_threshold: float = 0.78
    # 自定义 embedding 端点（可选）。留空时使用 Dify /embeddings/text-embedding。
    # 支持 OpenAI 兼容返回（data[].embedding）或 Dify 格式（embeddings）。
    chunk_embedding_api_url: str = ""
    chunk_embedding_api_key: str = ""
    # parent_child 父-子切分：父块（上下文）与子块（检索单元）目标字符数
    chunk_parent_size_chars: int = 1500
    chunk_child_size_chars: int = 400
    # LLM 切分开关（默认关闭：成本高、速度慢，仅用于小规模高质量文档）
    chunk_llm_enabled: bool = False
    # LLM 切分提示词（让模型返回 JSON 数组：切分后的段落列表）
    chunk_llm_chunk_prompt: str = (
        "你是一名文档切分专家。请把下面文档切成语义完整、主题集中的片段。"
        "要求：1) 每个片段围绕单一主题；2) 不要切断表格、公式、代码块；"
        "3) 保留原文顺序，不遗漏、不改写原文；4) 片段长度 300~800 字。"
        "只输出一个 JSON 数组，数组元素是切分后的原文段落字符串，不要输出其他内容。"
    )

    # ---- Dify 入库（plan.md §3.4）----
    # Dify 服务地址（Dify Cloud 默认 https://api.dify.ai/v1；自托管请改为自己的实例）。
    dify_api_url: str = "https://api.dify.ai/v1"
    # Dify Knowledge API Key（dataset-xxx 开头，用于 /datasets/... 知识库写操作）。
    dify_api_key: str = "dataset-c0aDelJrCtEjgLMhRqc5SRBG"
    # ★ Dify App API Key（app-xxx 开头，用于 /files/upload 上传图片）。
    #   Knowledge API Key 没有 /files/upload 权限，必须用 App API Key。
    #   留空则降级到公网 URL 策略（cloudflared/OSS）。
    dify_app_api_key: str = ""
    # 目标知识库 ID。
    dify_dataset_id: str = "b2c4f340-97c9-474c-bfb2-0fdb71e23250"
    # 索引技术：high_quality（embedding）/ economy（关键词）
    dify_indexing_technique: str = "high_quality"
    # 文档形态：text_model / hierarchical_model / qa_model
    dify_doc_form: str = "text_model"
    # HTTP 超时（秒）
    dify_timeout: int = 60
    # 等待文档 indexing_status=completed 的最长秒数
    dify_indexing_wait_timeout: int = 120
    # 轮询间隔（秒）
    dify_indexing_poll_interval: float = 2.0
    # 单次 add_segments 最大分段数（Dify 服务端有上限，默认 100）
    # ★ 2026-08-12：实测大批量（100/批）会导致 Dify 静默丢弃分段，降到 30
    dify_segments_per_request: int = 30
    # 单段安全字符上限（Dify 会静默丢弃超长分段，且同批中其它段一起被回滚）
    # ★ 2026-08-20：实测 HTML 内容 5637 字符可持久化、11000+ 被丢弃；
    #   纯文本 11000 字符可持久化。取 5000 保守兜底，超长段先转 markdown 再拆分。
    dify_max_segment_chars: int = 5000
    # 失败重试次数（4xx 不重试，5xx/网络重试）
    dify_max_retries: int = 3
    dify_retry_backoff: float = 2.0

    # ---- 公网图片托管（§3.4 内联 URL 策略）----
    # 用于把 chunk 里的 `![image](images/xxx.jpg)` 替换成 Dify 可访问的公网 URL。
    # Dify 知识库 API（dataset-xxx key）无法调用 /files/upload（属于 App API，
    # 见 https://docs.dify.ai/zh/api-reference/），所以图片不能走 Dify 自家托管。
    # 折中：放在公网可访问的位置（tunnel / OSS），Dify 渲染时直接拉。
    # ---- 图片托管后端选择 ----
    # ★ 2026-08-04 切换为 oss（阿里云 OSS 永久外链，tunnel 模式被废弃）：
    #   - tunnel: 本地 8000 + cloudflared/ngrok 暴露 /static/output/...（已废弃，URL 失效）
    #   - oss:    阿里云 OSS 公网 bucket，Dify 直接拉（永久外链，推荐）
    # 新增后端步骤见 app/services/image_host.py。
    image_host_backend: str = "oss"
    # ---- 留空 = 关闭 URL 替换（用 attachment_ids 上传图片）----
    # 例：
    #   - ngrok:      https://abc-123.ngrok.app
    #   - cloudflared: https://abc.trycloudflare.com
    #   - 公网 IP+端口: http://203.0.113.10:8000
    # 注意末尾不要带 / 也不要带 /static，Dify 入库时会自动拼 /static/output/{stem}/images/...
    public_base_url: str = ""

    # ---- OSS 后端（占位，正式启用时填）----
    # OSS region endpoint，例：https://oss-cn-hangzhou.aliyuncs.com
    oss_endpoint: str = ""
    # OSS bucket 名
    oss_bucket: str = ""
    # OSS AccessKey（RAM 子账号，建议只授 PutObject 权限）
    oss_access_key_id: str = ""
    oss_access_key_secret: str = ""
    # OSS 对象 key 前缀（默认 static/output，与 tunnel 模式路径模板一致，方便切换）
    oss_object_prefix: str = "static/output"
    # 可选 CDN / 自定义域名；空则用 endpoint+bucket 拼
    oss_public_domain: str = ""

    # ---- ★ 2026-08-04：Dify /files/upload 跳过开关 ----
    # 默认 False：保留旧行为（调用 /files/upload 拿 file_id + attachment_ids）
    # 设为 True：完全跳过 /files/upload，content 里**只**写 OSS 永久 URL，**不带 attachment_ids**
    #   适用场景：Dify 端 RAG_DIFY_APP_API_KEY 配了，但 /files/upload 拿到的 5min 签名 URL
    #   在 content 里反复过期导致图片不显示。让 Dify 自己从公网 URL 拉图存为 attachment
    #   （参考 Dify 官方行为：当 segment 文本里出现可公网访问的图片 URL 时，Dify 索引时
    #   会拉取并作为 attachment 内部存储，content 里的 URL 仅作为显示标识）。
    #   即使 Dify 拉图失败，content 里的 OSS URL 仍是永久的，召回时前端能直接拉。
    dify_skip_file_upload: bool = False

    # ---- 文档元数据（Excel → Dify Metadata）----
    # 文档元数据 Excel 文件路径（相对于 data_root 或绝对路径）。
    # Excel 格式：第 1 行英文字段名，第 2 行中文列名（跳过），第 3 行起数据。
    # 第 1 列为文件 stem（不含后缀），用于与 manifest / chunks 目录关联。
    doc_metadata_excel_filename: str = "doc_metadata.xlsx"

    # ---- 应用元数据 ----
    app_name: str = "RAG Batch Ingestion"
    app_version: str = "0.4.0"

    model_config = SettingsConfigDict(
        env_file=str(REPO_ROOT / "backend" / ".env"),
        env_file_encoding="utf-8",
        env_prefix="RAG_",
        extra="ignore",
    )

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> Tuple[PydanticBaseSettingsSource, ...]:
        """★ 反转 pydantic-settings 默认顺序：让 .env 优先于进程环境变量。

        默认顺序：init → env → dotenv → secrets
                     ↑ 环境变量会覆盖 .env ✗ 改 .env 看似无效

        本类调整后：init → dotenv → env → secrets
                                   ↑ 环境变量仅作兑底
        含义：
            - 业务上以 .env 为准（它是仓库版本控制的唯一来源）
            - 进程环境变量只用于“临时调试”（启动前 export 一次）
        """
        return (
            init_settings,
            dotenv_settings,
            env_settings,
            file_secret_settings,
        )

    # ---- 派生路径（始终绝对） ----
    @property
    def input_dir(self) -> Path:
        return (self.data_root / self.input_dirname).resolve()

    @property
    def pending_dir(self) -> Path:
        return (self.data_root / self.pending_dirname).resolve()

    @property
    def output_dir(self) -> Path:
        return (self.data_root / self.output_dirname).resolve()

    @property
    def parsed_dir(self) -> Path:
        return (self.data_root / self.parsed_dirname).resolve()

    @property
    def chunks_dir(self) -> Path:
        return (self.data_root / self.chunks_dirname).resolve()

    @property
    def error_dir(self) -> Path:
        return (self.data_root / self.error_dirname).resolve()

    @property
    def manual_fix_dir(self) -> Path:
        return (self.data_root / self.manual_fix_dirname).resolve()

    @property
    def logs_dir(self) -> Path:
        return (self.data_root / self.logs_dirname).resolve()

    @property
    def doc_metadata_excel_path(self) -> Path:
        return (self.data_root / self.doc_metadata_excel_filename).resolve()

    @property
    def manifest_path(self) -> Path:
        return (self.data_root / self.manifest_filename).resolve()

    def ensure_dirs(self) -> None:
        """启动时创建所有数据目录（幂等）。"""
        for d in (
            self.input_dir,
            self.pending_dir,
            self.output_dir,
            self.parsed_dir,
            self.chunks_dir,
            self.error_dir,
            self.manual_fix_dir,
            self.logs_dir,
        ):
            d.mkdir(parents=True, exist_ok=True)


settings = Settings()
