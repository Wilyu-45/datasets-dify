"""pytest 全局配置。

测试期间必须禁用 backend/.env：项目自定义的配置优先级是
「.env 高于进程环境变量」，否则各用例的 monkeypatch.setenv 会被
本地 .env 静默覆盖，导致测试结果依赖开发者本机配置。
"""
from __future__ import annotations

import os

os.environ["RAG_DISABLE_DOTENV"] = "1"
