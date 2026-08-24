"""JSON 结构化日志 + 按天轮转。"""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict

# plan.md §7 规定的固定字段
CANONICAL_FIELDS = (
    "timestamp",
    "level",
    "file_name",
    "step",
    "status",
    "duration_ms",
    "error_msg",
)


class JsonFormatter(logging.Formatter):
    """每条日志输出为单行 JSON，确保 plan.md §7 字段恒存在。"""

    def format(self, record: logging.LogRecord) -> str:
        # 标准字段
        payload: Dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created).strftime(
                "%Y-%m-%d %H:%M:%S"
            ),
            "level": record.levelname,
            "msg": record.getMessage(),
        }
        # canonical 字段（来自 extra）
        for field in CANONICAL_FIELDS:
            value = getattr(record, field, None)
            if field == "timestamp":
                continue
            if field in ("file_name", "step", "status", "error_msg"):
                payload[field] = value if value is not None else ""
            elif field == "duration_ms":
                payload[field] = value if value is not None else 0
        # 其它非标准字段（extra 中自定义的）
        std_keys = {
            "name",
            "msg",
            "args",
            "levelname",
            "levelno",
            "pathname",
            "filename",
            "module",
            "exc_info",
            "exc_text",
            "stack_info",
            "lineno",
            "funcName",
            "created",
            "msecs",
            "relativeCreated",
            "thread",
            "threadName",
            "processName",
            "process",
            "message",
            "asctime",
            *CANONICAL_FIELDS,
            "taskName",
        }
        for key, value in record.__dict__.items():
            if key in std_keys or key.startswith("_"):
                continue
            try:
                json.dumps(value)
            except TypeError:
                value = str(value)
            payload[key] = value

        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)

        return json.dumps(payload, ensure_ascii=False)


class _SafeTimedRotatingHandler(logging.handlers.TimedRotatingFileHandler):
    """Windows 安全的每日轮转 handler。

    标准 TimedRotatingFileHandler 在 doRollover 时用 os.rename()
    把 app.log → app.log.2026-08-07，但 Windows 上文件被进程持有
    时无法 rename，会报 PermissionError [WinError 32]。

    解决：用 copy + truncate 替代 rename：
      1) 关闭当前流
      2) 把当前日志复制到带日期的备份文件
      3) 截断原文件为空（保留同一文件句柄）
      4) 重新打开原文件继续写
    这样原文件路径不变，不会触发 Windows 文件锁。
    """

    def doRollover(self) -> None:
        # 关闭当前流
        if self.stream:
            self.stream.close()
            self.stream = None

        # 计算备份文件名（与父类逻辑一致）
        current_time = int(self.rolloverAt - self.interval)
        time_tuple = time.localtime(current_time)
        dest = self.baseFilename + "." + time.strftime(self.suffix, time_tuple)

        # copy 当前日志到备份文件
        try:
            if os.path.exists(self.baseFilename):
                shutil.copy2(self.baseFilename, dest)
        except OSError:
            pass  # 复制失败不影响主流程（日志可能丢失但服务不崩）

        # truncate 原文件（保留同一 inode / 文件句柄）
        try:
            with open(self.baseFilename, "w"):
                pass  # 清空文件内容
        except OSError:
            pass

        # 清理过期备份（保留 backupCount 个最近的）
        if self.backupCount > 0:
            files = []
            base_dir = os.path.dirname(self.baseFilename)
            base_name = os.path.basename(self.baseFilename)
            for f in os.listdir(base_dir):
                if f.startswith(base_name + "."):
                    files.append(os.path.join(base_dir, f))
            files.sort(key=os.path.getmtime, reverse=True)
            # 保留最新的 backupCount 个，删除其余
            for old_file in files[self.backupCount:]:
                try:
                    os.remove(old_file)
                except OSError:
                    pass

        # 重新打开原文件
        self.stream = self._open()

        # 更新下次轮转时间
        new_rv = current_time + self.interval
        while new_rv <= int(time.time()):
            new_rv += self.interval
        self.rolloverAt = new_rv


def setup(logs_dir: Path, level: str = "INFO", retention_days: int = 30) -> None:
    """初始化全局日志：stderr + 文件轮转。"""
    logs_dir.mkdir(parents=True, exist_ok=True)
    log_file = logs_dir / "app.log"

    root = logging.getLogger("ragsystem")
    root.setLevel(level.upper())
    # 幂等：避免重复 handler
    root.handlers.clear()

    formatter = JsonFormatter()

    # 文件 handler：每日轮转（Windows 安全）
    file_handler = _SafeTimedRotatingHandler(
        log_file,
        when="midnight",
        backupCount=retention_days,
        encoding="utf-8",
        utc=False,
    )
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    # stderr handler
    stream_handler = logging.StreamHandler(stream=sys.stderr)
    stream_handler.setFormatter(formatter)
    root.addHandler(stream_handler)

    root.propagate = False
