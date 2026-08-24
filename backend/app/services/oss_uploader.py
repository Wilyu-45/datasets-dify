"""公网图片 OSS 上传器（plan.md §3.4）。

★ 2026-08-04 启用阿里云 OSS 后端：
    - cloudflared tunnel 模式的 URL 失效（quick tunnel 是临时域名，重启就变），
      Dify 召回时图片 404，召回质量大幅下降。
    - 改用阿里云 OSS 公网 bucket 上传图片 + 永久公网外链：
      Dify 渲染时直接拉 https://ycsj-dify.oss-cn-shanghai.aliyuncs.com/static/output/{stem}/images/xxx.jpg
      图片永远可访问，不依赖本地服务存活。

★ 2026-08-05 修复：URL 里的 stem 含空格 / 中文（如 "GB 5085.5-2007 危险废物鉴别标准 ..."）
    不做 URL-encode 会导致：
      - 浏览器在第一个空格处截断链接 → 点击触发"下载"行为（其实是访问了不存在的路径）
      - Dify 端 fetch 时也因 URL 不合法拿不到图
    修复：build_oss_public_url 里对 key 段调用 urllib.parse.quote(safe='/')，
          空格 → %20、中文 → %E5%8D%B1... 但保留路径分隔符 /
    注：OSS 服务端会自动解码 URL-encoded 字符，**不需要重新上传图片**就能让旧图也能正常访问。

用法（由 image_host.prepare_chunks_images 内部调用，外部一般不直接用）：
    from app.services.oss_uploader import OssUploader
    up = OssUploader.from_settings()
    refs_to_url = up.upload_chunks_images(stem, chunks_dir)  # {"images/xxx.jpg": "https://..."}
    # → 上传失败时返回部分成功的映射（不阻断 dify 入库），失败图保留原相对路径
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import quote

log = logging.getLogger("ragsystem.oss_uploader")

# oss2 是阿里云官方 SDK，缺失时只影响 OSS 后端，不影响 tunnel/旧后端
try:
    import oss2  # type: ignore
    _OSS2_AVAILABLE = True
except ImportError:  # pragma: no cover
    oss2 = None  # type: ignore
    _OSS2_AVAILABLE = False


def build_oss_public_url(
    endpoint: str,
    bucket: str,
    public_domain: str,
    key: str,
) -> str:
    """根据 endpoint / bucket / key 拼出永久公网 URL（不依赖 oss2 SDK，可独立调用）。

    ★ 2026-08-04 新增：从 OssUploader.public_url 抽出独立函数，让
    `image_host._build_oss_url` 不再需要实例化 OssUploader。
    原因：oss2 SDK 缺失/版本不兼容时 OssUploader.__init__ 会抛 RuntimeError，
    但 URL 拼装只依赖字符串操作，根本不需要 oss2。把"生成 URL"和"上传对象"
    解耦后，URL 永远能拼出来（即使实际上传失败），保证 dify 段里仍写永久外链。

    ★ 2026-08-05 修复：URL-encode key 段。
    原因：key 里含空格 / 中文时（如 "static/output/GB 5085.5-2007 危险废物鉴别/images/xxx.jpg"），
    不 encode 的 URL 是非法的，浏览器在第一个空格处截断，Dify 端 fetch 也会失败。
    修复：用 urllib.parse.quote(key, safe='/') 把空格 / 中文都编码成 %XX，但保留 / 作为路径分隔符。
    OSS 服务端会自动解码 URL-encoded 字符查找对象，旧图不需要重传。
    """
    # ★ 关键：对 key 段 URL-encode（保留路径分隔符 /）
    encoded_key = quote(key.lstrip("/"), safe="/")
    if public_domain:
        return f"{public_domain.rstrip('/')}/{encoded_key}"
    ep = (endpoint or "").replace("https://", "").replace("http://", "").strip("/")
    if (endpoint or "").startswith("https://"):
        return f"https://{bucket}.{ep}/{encoded_key}"
    return f"http://{bucket}.{ep}/{encoded_key}"


def build_oss_object_key(object_prefix: str, stem: str, ref: str) -> str:
    """根据 object_prefix / stem / ref 拼出 OSS 对象 key（不依赖 oss2 SDK）。

    与 OssUploader.build_key 行为完全一致，抽出来让 image_host._build_oss_url 用。
    """
    ref_clean = (ref or "").replace("\\", "/").lstrip("/")
    while ref_clean.startswith("./"):
        ref_clean = ref_clean[2:]
    name = Path(ref_clean).name  # 兼容 "images/x.jpg" / "x.jpg"
    return f"{object_prefix.strip('/')}/{stem}/images/{name}"


@dataclass
class OssUploadResult:
    """单次 OSS 上传会话的汇总结果。"""

    uploaded: List[str] = None  # 成功上传的 ref 列表（相对 chunks_dir）
    skipped_existing: List[str] = None  # 已存在跳过的 ref
    failed: List[str] = None  # 失败的 ref + 原因
    ref_to_url: Dict[str, str] = None  # 成功的 ref → 永久公网 URL（供 dify 替换用）

    def __post_init__(self) -> None:
        if self.uploaded is None:
            self.uploaded = []
        if self.skipped_existing is None:
            self.skipped_existing = []
        if self.failed is None:
            self.failed = []
        if self.ref_to_url is None:
            self.ref_to_url = {}


class OssUploader:
    """阿里云 OSS 上传器（薄包装 oss2 SDK）。

    配置从 app.config.settings 读取（环境变量 RAG_OSS_*）：
    - oss_endpoint: region endpoint
    - oss_bucket: bucket 名
    - oss_access_key_id / oss_access_key_secret: RAM 子账号凭据
    - oss_object_prefix: 对象 key 前缀（默认 static/output）
    - oss_public_domain: 自定义 CDN 域名（空则用 endpoint+bucket 拼）
    """

    def __init__(
        self,
        endpoint: str,
        bucket: str,
        access_key_id: str,
        access_key_secret: str,
        object_prefix: str = "static/output",
        public_domain: str = "",
    ) -> None:
        if not _OSS2_AVAILABLE:
            raise RuntimeError(
                "oss2 SDK 未安装，请 `pip install oss2` 后再使用 OSS 后端。"
                "或切回 tunnel 模式：RAG_IMAGE_HOST_BACKEND=tunnel"
            )
        self.endpoint = endpoint
        self.bucket = bucket
        self.object_prefix = object_prefix.strip("/")
        self.public_domain = public_domain.rstrip("/") if public_domain else ""
        # 端点要去掉 https:// 前缀（oss2 SDK 内部要求）
        ep = endpoint.replace("https://", "").replace("http://", "").strip("/")
        self._auth = oss2.Auth(access_key_id, access_key_secret)
        self._bucket_obj = oss2.Bucket(self._auth, ep, bucket)

    @classmethod
    def from_settings(cls) -> "OssUploader":
        """从全局 settings 构造（依赖 app.config.settings 已经被 reload 过）。"""
        from app.config import settings

        return cls(
            endpoint=settings.oss_endpoint,
            bucket=settings.oss_bucket,
            access_key_id=settings.oss_access_key_id,
            access_key_secret=settings.oss_access_key_secret,
            object_prefix=settings.oss_object_prefix,
            public_domain=settings.oss_public_domain,
        )

    def public_url(self, key: str) -> str:
        """根据对象 key 拼接永久公网 URL（public-read bucket 匿名可访问）。

        优先用 public_domain（如 CDN 域名），否则用 endpoint+bucket 拼。
        ★ 2026-08-04 改为薄包装 `build_oss_public_url`（独立函数，不依赖 oss2 SDK）。
        """
        return build_oss_public_url(
            endpoint=self.endpoint,
            bucket=self.bucket,
            public_domain=self.public_domain,
            key=key,
        )

    def build_key(self, stem: str, ref: str) -> str:
        """构造对象 key：`{prefix}/{stem}/images/{filename}`（与 tunnel 路径模板一致）。

        ref 形如 `images/xxx.jpg` 或 `xxx.jpg`。
        ★ 2026-08-04 改为薄包装 `build_oss_object_key`（独立函数，不依赖 oss2 SDK）。
        """
        return build_oss_object_key(
            object_prefix=self.object_prefix,
            stem=stem,
            ref=ref,
        )

    def object_exists(self, key: str) -> bool:
        """检查对象是否已存在（用 head_object 避免覆盖）。"""
        try:
            self._bucket_obj.head_object(key)
            return True
        except oss2.exceptions.NotFound:  # type: ignore[attr-defined]
            return False
        except oss2.exceptions.OssError as e:  # type: ignore[attr-defined]
            log.warning(
                "OSS head_object 失败，按不存在处理（仍会尝试 put）: key=%s err=%s",
                key, e,
            )
            return False
        except Exception as e:  # noqa: BLE001
            log.warning(
                "OSS head_object 异常，按不存在处理: key=%s err=%s",
                key, e,
            )
            return False

    def upload_file(
        self,
        local_path: Path,
        key: str,
        *,
        overwrite: bool = False,
    ) -> bool:
        """上传单个本地文件到 OSS。返回是否成功。

        - overwrite=False（默认）：先用 head_object 检查，已存在则跳过
        - public-read（默认 ACL）：让 Dify 匿名拉取（生产环境应该用签名 URL + CDN 鉴权）
        """
        if not local_path.is_file():
            log.warning(
                "OSS 上传跳过（本地文件不存在）: key=%s local=%s",
                key, local_path,
            )
            return False

        if not overwrite and self.object_exists(key):
            log.info(
                "OSS 对象已存在，跳过上传: key=%s",
                key,
            )
            return True

        try:
            # ACL=public-read 让 Dify 匿名访问公网图片
            self._bucket_obj.put_object_from_file(
                key, str(local_path), headers={"x-oss-object-acl": "public-read"},
            )
            log.info(
                "OSS 上传成功: key=%s local=%s size=%d",
                key, local_path.name, local_path.stat().st_size,
            )
            return True
        except oss2.exceptions.OssError as e:  # type: ignore[attr-defined]
            log.error(
                "OSS 上传失败（OssError）: key=%s err=%s status=%d",
                key, e, getattr(e, "status", 0),
            )
            return False
        except Exception as e:  # noqa: BLE001
            log.error(
                "OSS 上传失败: key=%s err=%s",
                key, e,
            )
            return False

    def upload_chunks_images(
        self,
        stem: str,
        chunks_dir: Path,
        *,
        overwrite: bool = False,
    ) -> OssUploadResult:
        """批量上传 chunks/{stem}/images/ 下的所有图片到 OSS。

        Args:
            stem: 文档 stem（用于拼对象 key）
            chunks_dir: chunks 目录（含 images/ 子目录）
            overwrite: 是否覆盖已存在的对象（默认 False，幂等）

        Returns:
            OssUploadResult：成功/跳过/失败明细 + ref→public_url 映射（供 dify 替换用）
        """
        result = OssUploadResult()
        images_dir = chunks_dir / "images"
        if not images_dir.is_dir():
            log.info(
                "OSS 跳过：chunks_dir 没有 images/ 子目录: stem=%s",
                stem,
            )
            return result

        for img_path in sorted(images_dir.iterdir()):
            if not img_path.is_file():
                continue
            # 用 basename 形式 ref（与 chunk markdown 中的 `images/xxx.jpg` 引用一致）
            ref = f"images/{img_path.name}"
            key = self.build_key(stem, ref)
            try:
                # 单一图片上传（含已存在跳过）
                existed_before = self.object_exists(key)
                if existed_before and not overwrite:
                    result.skipped_existing.append(ref)
                    result.ref_to_url[ref] = self.public_url(key)
                    continue

                ok = self.upload_file(img_path, key, overwrite=overwrite)
                if ok:
                    result.uploaded.append(ref)
                    result.ref_to_url[ref] = self.public_url(key)
                else:
                    result.failed.append(ref)
            except Exception as e:  # noqa: BLE001
                log.exception(
                    "OSS 单张图片处理异常: ref=%s key=%s err=%s",
                    ref, key, e,
                )
                result.failed.append(ref)

        log.info(
            "OSS 批量上传完成: stem=%s uploaded=%d skipped=%d failed=%d",
            stem, len(result.uploaded), len(result.skipped_existing), len(result.failed),
        )
        return result


def prepare_chunks_images_for_dify(
    backend: str,
    stem: str,
    chunks_dir: Path,
) -> Dict[str, str]:
    """为 chunks_dir/images/ 准备公网 URL 映射（按 backend 派发到对应后端）。

    Args:
        backend: 后端名（"tunnel" / "oss" 等）
        stem: 文档 stem
        chunks_dir: chunks 目录

    Returns:
        ref→public_url 字典（如 {"images/x.jpg": "https://..."}）。
        - oss 后端：先上传再返回永久外链（失败图片不出现在字典里，调用方保留原 ref）
        - tunnel 后端：直接走 _build_tunnel_url，不上传（依赖 StaticFiles 暴露）
        - 其他后端：返回空字典
    """
    if backend == ImageHostBackendAlias.OSS:  # type: ignore[name-defined]
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


# 别名常量（避免循环 import image_host；image_host.py 在导入时也会引用本模块）
class ImageHostBackendAlias:
    OSS = "oss"
    TUNNEL = "tunnel"
