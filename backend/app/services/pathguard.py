"""用户输入子路径的安全解析（防目录逃逸，★ 2026-10 统一收口）。

API 的 `{stem}` / `{chunk_id}` 等路径参数会被拼进 data/ 的子目录做读取，
必须校验「解析后的绝对路径仍在预期根目录内」。

⚠️ 不要用 `str(target).startswith(str(root))` 判断前缀：字符串前缀匹配会被
**同前缀的兄弟目录**绕过（`data/parsed_backup` 以 `data/parsed` 开头，
`../parsed_backup/x` 解析后同样以它开头）。正确做法是比较路径段，
即用 `Path.is_relative_to`（与 services.cleanup._within_data_root 同一思路）。
"""

from __future__ import annotations

from pathlib import Path
from typing import List, Tuple


def resolve_within(base: Path, sub: str) -> Path:
    """把用户传入的 `sub` 解析到 `base` 目录下，越界则抛 ValueError。

    Args:
        base: 预期根目录（如 settings.parsed_dir / settings.chunks_dir）
        sub:  用户传入的子路径（通常是文档 stem）

    Raises:
        ValueError: 解析结果不在 base 之内（含 `../` 逃逸、绝对路径等）
    """
    root = base.resolve()
    target = (root / sub).resolve()
    if not target.is_relative_to(root):
        raise ValueError(f"路径越界: {sub}")
    return target


def iter_files_tolerant(base: Path) -> List[Tuple[Path, int]]:
    """递归列出 ``base`` 下的普通文件，返回 ``(路径, 字节数)`` 列表，按文件名排序。

    ★ 2026-10：统一收口列表接口的文件遍历。清理任务可能在遍历时删掉某个文件，
    或存在权限不足 / 断链而不可访问的条目；原先 ``x.is_file()`` 直接抛错会让整个
    列表页 500。``Path.is_file()`` 只吞 ENOENT 类错误，其它 OSError 会向上抛，
    因此 is_file 与 stat 都要包 try，遇错跳过该条继续。

    注：本函数只做「列文件 + 取大小」；需要按目录结构分类计数（如 chunks/images）
    的调用方应自行遍历并在 is_file/stat 外包 try，口径与这里一致。
    """
    entries: List[Tuple[Path, int]] = []
    for f in base.rglob("*"):
        try:
            if not f.is_file():
                continue
            size = f.stat().st_size
        except OSError:  # noqa: BLE001 文件消失/不可访问，跳过
            continue
        entries.append((f, size))
    entries.sort(key=lambda t: t[0].name.lower())
    return entries
