"""公网图片托管后端注册中心（plan.md §3.4）。

为什么需要这个模块：
- Dify 知识库 API（dataset-xxx key）**无法**调用 `/files/upload`（属于 App API）。
- 因此我们不能把图片上传到 Dify 自家服务器，段里也就拿不到 Dify 的
  `https://dify.xxx/files/{file_id}/file-preview` 格式 URL。
- 折中方案：把图片放在公网可访问的位置（自己起的 tunnel / OSS bucket），
  Dify 段里写完整外链，渲染时直接拉。

本模块把"如何生成图片公网 URL"抽象成可插拔后端：
- `tunnel` = 本地 8000 + cloudflared / ngrok 暴露 /static/output/...
  （2026-08-04 标记为废弃：quick tunnel URL 重启就失效，Dify 召回时图片 404）
- `oss`   = 阿里云 OSS 直传 + 永久公网外链（★ 2026-08-04 启用，依赖 oss2 SDK）

添加新后端的步骤：
1. 在 `ImageHostBackend` 加成员；
2. 写一个 `_build_xxx_url(stem, ref) -> str`；
3. 在 `_BUILDERS` 注册；
4. 如果需要上传（如 OSS），在 oss_uploader.py 加对应逻辑 + 暴露 `prepare_chunks_images`。
不需要改 `dify_ingest.py`。
"""

from __future__ import annotations

import logging
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict

from app.config import settings

log = logging.getLogger("ragsystem.image_host")


class ImageHostBackend(str, Enum):
    """已知的图片托管后端。"""

    TUNNEL = "tunnel"
    OSS = "oss"


# 已知后端集合（与 ImageHostBackend 成员保持一致；用于快速校验）。
KNOWN_BACKENDS = frozenset(b.value for b in ImageHostBackend)


# 单个 builder 的签名：接收 (stem, ref) 返回完整公网 URL。
# 失败时返回空串或抛 NotImplementedError（被 build_image_url 兜底）。
ImageUrlBuilder = Callable[[str, str], str]


# ---- 各后端实现 ----


def _build_tunnel_url(stem: str, ref: str) -> str:
    """tunnel 后端：从 settings.public_base_url 拼出公网 URL。

    路径模板：`{public_base_url}/static/output/{stem}/{ref}`，与 main.py
    里挂载的 StaticFiles(directory=output_dir) 路径一致。

    public_base_url 为空时返回空串（让调用方走 /files/upload 旧路径）。

    ref 规范化：
    - `\\` → `/`（Windows 路径）
    - 去掉前导 `/`、前导 `./`（让 ./images/x.jpg 变 images/x.jpg）
    """
    base = settings.public_base_url.rstrip("/")
    if not base:
        return ""
    ref_clean = ref.replace("\\", "/")
    # 反复去掉前导的 ./ 或 /
    while ref_clean.startswith(("./", "/")):
        ref_clean = ref_clean[2:] if ref_clean.startswith("./") else ref_clean[1:]
    return f"{base}/static/output/{stem}/{ref_clean}"


def _build_oss_url(stem: str, ref: str) -> str:
    """OSS 后端：生成永久公网外链（不依赖 oss2 SDK，可独立调用）。

    ★ 2026-08-04 启用 + 重构：
        - 阿里云 OSS 公网 bucket（ACL=public-read），Dify 可匿名拉取
        - 不依赖本地服务存活（解决 cloudflared quick tunnel 重启失效问题）
        - bucket 配置见 RAG_OSS_* 环境变量
        - 关键：URL 拼装走 `build_oss_public_url` / `build_oss_object_key` 独立函数，
          即使 oss2 SDK 缺失/版本不兼容（导致 OssUploader 实例化失败），
          仍能返回正确的 OSS 永久外链 —— 这是兜底保证：
          "Dify 段里写出的图片 URL 一定是 OSS URL，不是 Dify 5min 签名 URL"。
    """
    # ★ 不再 import / 实例化 OssUploader，只调独立 URL 拼装函数
    from app.services.oss_uploader import build_oss_object_key, build_oss_public_url

    ref_clean = (ref or "").replace("\\", "/")
    while ref_clean.startswith(("./", "/")):
        ref_clean = ref_clean[2:] if ref_clean.startswith("./") else ref_clean[1:]
    key = build_oss_object_key(
        object_prefix=settings.oss_object_prefix,
        stem=stem,
        ref=ref_clean,
    )
    return build_oss_public_url(
        endpoint=settings.oss_endpoint,
        bucket=settings.oss_bucket,
        public_domain=settings.oss_public_domain,
        key=key,
    )


# ---- 注册表 ----

_BUILDERS: Dict[str, ImageUrlBuilder] = {
    ImageHostBackend.TUNNEL.value: _build_tunnel_url,
    ImageHostBackend.OSS.value: _build_oss_url,
}


# ---- 公开 API ----


def build_image_url(backend: str, stem: str, ref: str) -> str:
    """派发到对应 builder；未知后端 / NotImplementedError → 记 WARNING + 返回空串。

    返回空串的语义是"无法生成公网 URL"，调用方应保留原 `images/xxx.jpg` 相对路径，
    让 Dify 走它自己的降级逻辑（不显示图片但段仍可入库）。

    ★ 注意：OSS 后端调用本函数不会真正上传图片（仅生成 URL）。
    上传动作由 prepare_chunks_images 在 dify_ingest 阶段触发。
    """
    b = (backend or ImageHostBackend.TUNNEL.value).lower().strip()
    fn = _BUILDERS.get(b)
    if fn is None:
        log.warning(
            "unknown image host backend, falling back to no-op",
            extra={
                "step": "image_host",
                "status": "unknown_backend",
                "backend": b,
                "stem": stem,
            },
        )
        return ""
    try:
        return fn(stem, ref)
    except NotImplementedError as e:
        log.warning(
            "image host backend not implemented, falling back to no-op: %s",
            e,
            extra={
                "step": "image_host",
                "status": "not_implemented",
                "backend": b,
                "stem": stem,
            },
        )
        return ""
    except Exception as e:  # noqa: BLE001
        log.warning(
            "image host backend exception, falling back to no-op: %s",
            e,
            extra={
                "step": "image_host",
                "status": "exception",
                "backend": b,
                "stem": stem,
            },
        )
        return ""


def is_active(backend: str, cfg: Any = None) -> bool:
    """判断指定后端是否"启用"（即配置齐全可生成 URL）。

    - tunnel: public_base_url 非空
    - oss:    oss_endpoint + oss_bucket + access_key 都非空
              且 oss2 SDK 可用（否则 `_build_oss_url` 走纯字符串拼装可工作，
              但 prepare_chunks_images 的实际上传动作会失败 —— 这种情况应让
              is_active 返回 False，避免触发上传流程，统一走"生成 URL 但不上传"降级路径）
    - 其他:   永远 False
    """
    cfg = cfg or settings
    b = (backend or "").lower().strip()
    if b == ImageHostBackend.TUNNEL.value:
        return bool(cfg.public_base_url)
    if b == ImageHostBackend.OSS.value:
        # ★ 2026-08-04 增强：除配置齐全外，还要 oss2 SDK 可用
        #  - 用途 1：判断"OSS 后端是否启用"以决定 content 是否写永久公网 URL
        #  - 用途 2：判断"是否调用 prepare_chunks_images 触发实际上传"
        #  - 即便 oss2 缺失，`_build_oss_url` 仍能独立拼出 URL（不阻断 URL 生成），
        #    但 is_active 应返回 False 以避免准备上传阶段抛 RuntimeError。
        from app.services.oss_uploader import _OSS2_AVAILABLE
        if not _OSS2_AVAILABLE:
            log.warning(
                "oss2 SDK 未安装，OSS 后端标记为未激活（仅 URL 拼装降级可用）",
                extra={"step": "image_host", "status": "oss2_missing"},
            )
            return False
        return bool(
            cfg.oss_endpoint
            and cfg.oss_bucket
            and cfg.oss_access_key_id
            and cfg.oss_access_key_secret
        )
    return False


def prepare_chunks_images(
    backend: str,
    stem: str,
    chunks_dir: Path,
) -> Dict[str, str]:
    """为 chunks_dir/images/ 下的所有图片准备公网 URL 映射。

    按后端派发：
    - `oss` 后端：调用 OssUploader.upload_chunks_images（真实上传 OSS）
    - `tunnel` 后端：依赖 main.py 挂载的 /static/output/ 静态服务（不需要上传）
    - 其他后端：返回空字典

    Args:
        backend: 后端名（"tunnel" / "oss"）
        stem: 文档 stem
        chunks_dir: chunks 目录（含 images/ 子目录）

    Returns:
        ref→public_url 字典（如 {"images/x.jpg": "https://..."}）。
        失败的图不会出现在字典里（让 dify_ingest 保留原相对路径降级）。
    """
    b = (backend or "").lower().strip()

    if b == ImageHostBackend.OSS.value:
        # 延迟导入：避免 oss2 未装时 import image_host 失败
        from app.services.oss_uploader import OssUploader

        if not is_active(b):
            log.warning(
                "OSS 后端未配置齐全（endpoint/bucket/ak/sk），跳过上传: stem=%s",
                stem,
            )
            return {}
        try:
            up = OssUploader.from_settings()
        except Exception as e:  # noqa: BLE001
            log.error(
                "OSS uploader 初始化失败，降级为相对路径: stem=%s err=%s",
                stem, e,
            )
            return {}
        try:
            res = up.upload_chunks_images(stem, chunks_dir)
        except Exception as e:  # noqa: BLE001
            log.error(
                "OSS 批量上传异常，降级为相对路径: stem=%s err=%s",
                stem, e,
            )
            return {}
        if res.failed:
            log.warning(
                "OSS 部分图片上传失败（保留原相对路径，Dify 不会显示）: "
                "stem=%s failed=%s",
                stem, res.failed,
            )
        return res.ref_to_url

    # tunnel / 其他后端：返回空 dict（调用方走原始 build_image_url 逻辑）
    return {}
