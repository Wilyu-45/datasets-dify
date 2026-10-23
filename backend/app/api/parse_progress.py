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

from fastapi import APIRouter, Depends

from app.api.auth import require_default_tenant
from app.services.parse_progress import get_tracker

log = logging.getLogger("ragsystem.parse_progress_api")

# ★ 2026-10：补上全局接口守卫。进度表内容是全量文件名，属于跨租户信息；
#   而 POST /api/parse 本身就挂 require_default_tenant（命名租户无法触发解析），
#   所以这个只读端点没有理由不收。前端始终匿名调用（不带 X-API-Key），
#   仍走 default 租户，行为不变。
router = APIRouter(
    prefix="/parse", tags=["parse"], dependencies=[Depends(require_default_tenant)]
)


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
