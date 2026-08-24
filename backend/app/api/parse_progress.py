"""★ 2026-08-07：解析进度查询 API（实时进度条）。

背景：
  批量上传时，前端需要实时展示每个文件的 MinerU 解析进度。
  本接口提供轮询端点，返回所有文件的当前进度。

接口：
  GET /api/parse/progress
    返回：{
      "file1.pdf": {"progress": 50, "msg": "调用 MinerU API 中...", "status": "parsing"},
      "file2.pdf": {"progress": 100, "msg": "解析完成", "status": "done"},
    }
"""

from __future__ import annotations

import logging

from fastapi import APIRouter

from app.services.parse_progress import get_tracker

log = logging.getLogger("ragsystem.parse_progress_api")

router = APIRouter(prefix="/parse", tags=["parse"])


@router.get("/progress")
async def get_parse_progress():
    """获取所有文件的解析进度。

    返回结构：
        {
            "file1.pdf": {
                "progress": 50,      # 0-100
                "msg": "调用 MinerU API 中...",
                "status": "parsing"  # parsing / done / failed
            },
            ...
        }
    """
    tracker = get_tracker()
    # 先清理旧进度（完成超过 30 秒的）
    tracker.cleanup_old()
    return tracker.get_all()
