"""检查当前解析进度"""
import sys
import httpx

sys.stdout.reconfigure(encoding='utf-8')

BASE_URL = "http://localhost:8000/api"

r = httpx.get(f"{BASE_URL}/parse/progress", timeout=30)
data = r.json()

print("=== 解析进度 ===")
for fname, info in data.items():
    if isinstance(info, dict):
        status = info.get('status', 'unknown')
        progress = info.get('progress', 0)
        msg = info.get('msg', '')
        print(f"  {fname}: {status} ({progress}%) - {msg}")
    else:
        print(f"  {fname}: {info}")
