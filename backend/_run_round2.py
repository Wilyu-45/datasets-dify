"""Run pipeline for remaining files."""
import httpx
import time

BASE = "http://localhost:8000"

def run_step(name: str, url: str, body: dict, timeout: int = 600):
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


# 1) Parse remaining
result = run_step("Step 1: Parse remaining files", "/api/parse", {"dry_run": False, "force": False}, timeout=1800)
if result:
    print(f"  scanned={result['scanned']}, parsed={result['parsed']}, skipped={result['skipped_done']}, failed={result['failed']}")
    for a in result.get("actions", []):
        if a["action"] == "failed":
            print(f"    FAILED: {a['filename']} - {a.get('error')}")

# 2) Chunk
result = run_step("Step 2: Chunk", "/api/chunk", {"dry_run": False, "force": False}, timeout=600)
if result:
    print(f"  scanned={result['scanned']}, chunked={result['chunked']}, skipped={result['skipped_done']}, failed={result['failed']}")

# 3) Dify Upload
result = run_step("Step 3: Dify Upload", "/api/dify/upload", {"dry_run": False, "force": False}, timeout=1800)
if result:
    print(f"  scanned={result['scanned']}, uploaded={result['uploaded']}, skipped={result['skipped_done']}, failed={result['failed']}")
    for a in result.get("actions", []):
        if a.get("error"):
            print(f"    ERROR: {a['stem']} - {a['error']}")

print(f"\n{'='*60}")
print("  Round 2 complete!")
print(f"{'='*60}")
