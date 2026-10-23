"""backend/.env 的持久化写入（★ 2026-10 优化）。

背景：切分策略、Dify 知识库等「前端保存即生效」的配置会写回 backend/.env。
原先 api/chunk.py 与 api/dify.py 各有一份完全相同的 `_persist_env`，且都是
``write_text()`` 直接覆盖：

1. 打开即截断，写入过程中掉电/异常会留下半截 .env —— 而 .env 是应用唯一的
   配置来源，损坏后服务直接起不来；
2. 两份实现各写各的，并发保存（切分策略 + 知识库同时提交）会互相丢更新；
3. 值里没有校验，将来若有调用方把用户文本写进 .env，换行会撕开配置。

本模块把写入收敛成一个入口，并做三件事：进程内加锁串行、临时文件 +
``os.replace`` 原子替换、替换前留一份 ``.env.bak``。
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import threading
from pathlib import Path
from typing import List, Optional

from app.config import settings

log = logging.getLogger("ragsystem.services.env_store")

# 与 config.py 的 env_prefix 保持一致（RAG_XXX）；宽松到允许小写以兼容手工加的键
_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

_WRITE_LOCK = threading.RLock()

_ENV_ENCODING = "utf-8"


class EnvWriteError(RuntimeError):
    """.env 无法定位或写入失败（调用方按需降级为「只在本进程生效」）。"""


def env_file_path() -> Optional[Path]:
    """.env 的真实路径——直接取 config.Settings.model_config，避免和加载侧漂移。

    测试/CI 用 RAG_DISABLE_DOTENV=1 关闭 dotenv 时 model_config["env_file"] 为
    None，此时没有可写的配置文件，返回 None 让调用方明确降级。
    """
    target = settings.model_config.get("env_file")
    if not target:
        return None
    path = Path(target)
    return path if path.is_absolute() else path.resolve()


def _split_line(line: str) -> Optional[tuple]:
    """解析一行配置。

    返回 ``(键名, 是否带 export 前缀)``；注释/空行/无法解析的行返回 None。
    保留 export 标志是为了原地替换时不改变写法（有些运维习惯写 ``export KEY=...``）。
    """
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return None
    is_export = False
    if stripped.startswith("export "):
        is_export = True
        stripped = stripped[len("export "):].strip()
    m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\s*=", stripped)
    return (m.group(1), is_export) if m else None


def set_env_value(key: str, value: str, *, path: Optional[Path] = None) -> Path:
    """把 ``KEY=value`` 写入 backend/.env（已有则原地替换，没有则追加）。

    Args:
        key: 变量名，需匹配 ``[A-Za-z_][A-Za-z0-9_]*``（含 RAG_ 前缀）
        value: 变量值，不允许含换行（会破坏 .env 结构）
        path: 显式目标文件（测试用）；默认取 env_file_path()

    Raises:
        ValueError: key/value 非法
        EnvWriteError: 找不到可写的 .env
    """
    if not _KEY_RE.match(key or ""):
        raise ValueError(f"非法的配置键名：{key!r}")
    text_value = "" if value is None else str(value)
    if "\n" in text_value or "\r" in text_value:
        raise ValueError(f"配置值不能包含换行：{key}={text_value!r}")

    target = Path(path) if path is not None else env_file_path()
    if target is None:
        raise EnvWriteError(
            "未找到可写的 backend/.env（RAG_DISABLE_DOTENV 生效或配置被禁用）"
        )

    with _WRITE_LOCK:
        lines: List[str] = []
        if target.exists():
            # utf-8-sig：容忍手工编辑留下的 BOM，避免第一个键被读成 "\ufeffRAG_X"
            lines = target.read_text(encoding="utf-8-sig").splitlines()

        replaced = 0
        for idx, line in enumerate(lines):
            parsed = _split_line(line)
            if parsed is not None and parsed[0] == key:
                prefix = "export " if parsed[1] else ""
                lines[idx] = f"{prefix}{key}={text_value}"
                replaced += 1
        if not replaced:
            if lines and lines[-1].strip():
                lines.append(f"{key}={text_value}")
            else:
                # 末尾留空行（或文件为空）时直接把键放到最后，避免出现空行夹在中间
                lines = lines[:-1] if lines else lines
                lines.append(f"{key}={text_value}")

        payload = "\n".join(lines) + "\n"
        _atomic_replace(target, payload)

    log.info(
        "backend/.env 已更新",
        extra={
            "step": "env_store",
            "status": "env_persisted",
            "key": key,
            "env_file": str(target),
            "replaced": replaced,
        },
    )
    return target


def _atomic_replace(target: Path, payload: str) -> None:
    """同目录临时文件 + fsync + os.replace，失败时不留损坏的目标文件。"""
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f"{target.name}.tmp.{os.getpid()}")
    try:
        with tmp.open("w", encoding=_ENV_ENCODING, newline="\n") as fp:
            fp.write(payload)
            fp.flush()
            os.fsync(fp.fileno())

        if target.exists():
            # 备份用“复制”而不是“改名”：改名会在 replace 之前留出一个
            # “.env 短暂不存在”的窗口，那时读到空配置的启动会静默走默认值。
            try:
                shutil.copyfile(target, target.with_name(target.name + ".bak"))
            except OSError as e:  # noqa: BLE001
                log.warning("备份 %s 失败（继续写入）: %s", target.name, e)

        os.replace(tmp, target)
    except Exception:
        # 任何异常都清掉临时文件，原 .env 保持不动
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:  # noqa: BLE001
            pass
        raise
