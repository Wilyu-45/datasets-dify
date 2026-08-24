"""流式 MD5 工具。"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import BinaryIO, Union

PathLike = Union[str, Path]


def md5_of_file(path: PathLike, chunk_size: int = 65536) -> str:
    """计算文件 MD5（流式读取，64KB 块）。"""
    h = hashlib.md5()
    with open(path, "rb") as f:
        while True:
            block = f.read(chunk_size)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def md5_of_stream(stream: BinaryIO, chunk_size: int = 65536) -> str:
    """计算流对象的 MD5。流指针会被消费。"""
    h = hashlib.md5()
    while True:
        block = stream.read(chunk_size)
        if not block:
            break
        h.update(block)
    return h.hexdigest()
