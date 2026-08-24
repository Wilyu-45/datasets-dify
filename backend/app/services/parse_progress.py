"""★ 2026-08-07：MinerU 解析进度跟踪（实时进度条）。

背景：
  批量上传时，前端需要实时展示每个文件的 MinerU 解析进度。
  由于当前架构是同步一次性请求，无法中途获取进度。

方案：
  1. 后端在解析过程中，把每个文件的进度写入内存字典
  2. 前端轮询 /api/parse/progress 接口获取实时进度
  3. 解析完成后，进度保留 30 秒供前端查询

进度结构：
  {
    "file1.pdf": {"progress": 50, "msg": "调用 MinerU API 中...", "status": "parsing"},
    "file2.pdf": {"progress": 100, "msg": "解析完成", "status": "done"},
  }
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Dict, Optional

log = logging.getLogger("ragsystem.parse_progress")


class ParseProgressTracker:
    """解析进度跟踪器（线程安全）。"""

    def __init__(self):
        self._progress: Dict[str, Dict[str, any]] = {}
        self._lock = threading.Lock()
        self._cleanup_time = 30  # 完成后保留 30 秒

    def update(
        self,
        filename: str,
        progress: int,
        msg: str = "",
        status: str = "parsing",
    ):
        """更新文件解析进度。

        Args:
            filename: 文件名
            progress: 0-100
            msg: 进度描述（如"调用 API 中..."、"解压产物..."）
            status: parsing / done / failed
        """
        with self._lock:
            self._progress[filename] = {
                "progress": progress,
                "msg": msg,
                "status": status,
                "update_time": time.time(),
            }
        log.debug(
            "parse progress: %s %d%% %s",
            filename,
            progress,
            msg,
        )

    def get(self, filename: str) -> Optional[Dict[str, any]]:
        """获取单个文件进度。"""
        with self._lock:
            return self._progress.get(filename)

    def get_all(self) -> Dict[str, Dict[str, any]]:
        """获取所有文件进度（用于 API 返回）。"""
        with self._lock:
            # 返回副本，避免外部修改
            return {k: v.copy() for k, v in self._progress.items()}

    def clear(self, filename: str):
        """清除单个文件进度。"""
        with self._lock:
            self._progress.pop(filename, None)

    def cleanup_old(self):
        """清理已完成的旧进度（超过 30 秒）。"""
        now = time.time()
        with self._lock:
            old_keys = [
                k
                for k, v in self._progress.items()
                if v.get("status") in ("done", "failed")
                and now - v.get("update_time", 0) > self._cleanup_time
            ]
            for k in old_keys:
                del self._progress[k]


# 全局单例
_tracker = ParseProgressTracker()


def get_tracker() -> ParseProgressTracker:
    """获取全局进度跟踪器。"""
    return _tracker
