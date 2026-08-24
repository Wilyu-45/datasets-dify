"""Round 3: 重试错误文件 + 处理剩余文件。"""
import httpx
import time

BASE = "http://localhost:8000"

def run_step(name, url, body, timeout=600):
    print(f"\n{'='*60}")
    print(f"  {name}")
    print(f"{'='*60}")
    t0 = time.perf_counter()
    try:
        r = httpx.post(f"{BASE}{url}", json=body, timeout=timeout)
        data = r.json()
        dt = int((time.perf_counter() - t0) * 1000)
        print(f"  Status: {r.status_code} ({dt}ms)")
        if r.status_code != 200:
            print(f"  ERROR: {data}")
            return None
        return data
    except Exception as e:
        print(f"  EXCEPTION: {e}")
        return None


# 1) 重置错误文件（force=True 重新解析）
print("=== 重置错误文件 ===")
r = httpx.get(f"{BASE}/api/manifest?limit=100", timeout=10)
data = r.json()
error_files = [row for row in data.get("rows", []) if row.get("status") == "error"]
print(f"  找到 {len(error_files)} 个错误文件")
for ef in error_files:
    print(f"    - {ef['filename']}")

# 2) Parse (force=True 强制重新解析错误文件)
result = run_step("Step 1: Parse (force=True for errors)", "/api/parse", {"dry_run": False, "force": True}, timeout=1800)
if result:
    print(f"  scanned={result['scanned']}, parsed={result['parsed']}, skipped={result['skipped_done']}, failed={result['failed']}")
    for a in result.get("actions", []):
        if a["action"] == "failed":
            print(f"    FAILED: {a['filename']} - {a.get('error')}")

# 3) Chunk
result = run_step("Step 2: Chunk", "/api/chunk", {"dry_run": False, "force": False}, timeout=600)
if result:
    print(f"  scanned={result['scanned']}, chunked={result['chunked']}, skipped={result['skipped_done']}, failed={result['failed']}")

# 4) Dify Upload
result = run_step("Step 3: Dify Upload", "/api/dify/upload", {"dry_run": False, "force": False}, timeout=1800)
if result:
    print(f"  scanned={result['scanned']}, uploaded={result['uploaded']}, skipped={result['skipped_done']}, failed={result['failed']}")

print(f"\n{'='*60}")
print("  Round 3 complete!")
print(f"{'='*60}")
