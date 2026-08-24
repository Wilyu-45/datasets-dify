"""
运行流水线处理所有待处理文件：scan → parse → chunk → dify upload
"""
import sys
import httpx
import time

sys.stdout.reconfigure(encoding='utf-8')

BASE_URL = "http://localhost:8000/api"

def scan_files():
    """扫描 input/ 文件夹，将新文件添加到 manifest"""
    print("\n=== Step 1: Scan Files ===")
    r = httpx.post(f"{BASE_URL}/scan", json={"dry_run": False, "force": False}, timeout=60)
    print(f"Status: {r.status_code}")
    data = r.json()
    print(f"Scanned: {data.get('scanned', 0)}, Staged: {data.get('staged', 0)}, Skipped: {data.get('skipped_done', 0)}")
    return data

def parse_pending():
    """解析所有待解析文件"""
    print("\n=== Step 2: Parse Pending Files ===")
    r = httpx.post(f"{BASE_URL}/parse", json={"dry_run": False, "force": False}, timeout=1800)
    print(f"Status: {r.status_code}")
    data = r.json()
    print(f"Scanned: {data.get('scanned', 0)}, Parsed: {data.get('parsed', 0)}, Skipped: {data.get('skipped_done', 0)}, Failed: {data.get('failed', 0)}")
    for a in data.get("actions", []):
        if a["action"] == "failed":
            print(f"  FAILED: {a['filename']} - {a.get('error')}")
    return data

def wait_for_parsing(timeout=600):
    """等待解析完成"""
    print("\n=== Waiting for parsing to complete ===")
    start = time.time()
    while time.time() - start < timeout:
        r = httpx.get(f"{BASE_URL}/parse/progress", timeout=30)
        data = r.json()
        parsing = data.get('parsing', 0)
        pending = data.get('pending', 0)
        done = data.get('done', 0)
        error = data.get('error', 0)
        
        print(f"  Parsing: {parsing}, Pending: {pending}, Done: {done}, Error: {error}")
        
        if parsing == 0:
            print("  All parsing completed!")
            return True
        time.sleep(10)
    
    print("  Timeout waiting for parsing!")
    return False

def chunk_parsed():
    """切分所有已解析文件"""
    print("\n=== Step 3: Chunk Parsed Files ===")
    r = httpx.post(f"{BASE_URL}/chunk", json={"dry_run": False, "force": False}, timeout=600)
    print(f"Status: {r.status_code}")
    data = r.json()
    print(f"Scanned: {data.get('scanned', 0)}, Chunked: {data.get('chunked', 0)}, Skipped: {data.get('skipped_done', 0)}, Failed: {data.get('failed', 0)}")
    for a in data.get("actions", []):
        if a["action"] == "failed":
            print(f"  FAILED: {a['filename']} - {a.get('error')}")
    return data

def upload_to_dify():
    """上传所有已切分文件到 Dify"""
    print("\n=== Step 4: Upload to Dify ===")
    r = httpx.post(f"{BASE_URL}/dify/upload", json={"dry_run": False, "force": False}, timeout=1800)
    print(f"Status: {r.status_code}")
    data = r.json()
    print(f"Scanned: {data.get('scanned', 0)}, Uploaded: {data.get('uploaded', 0)}, Skipped: {data.get('skipped_done', 0)}, Failed: {data.get('failed', 0)}")
    for a in data.get("actions", []):
        print(f"  {a['action']}: {a['stem']}")
        if a.get("error"):
            print(f"    ERROR: {a['error']}")
    return data

def check_status():
    """检查最终状态"""
    print("\n=== Final Status ===")
    r = httpx.get(f"{BASE_URL}/parse/progress", timeout=30)
    data = r.json()
    print(f"Parsing: {data.get('parsing', 0)}")
    print(f"Pending: {data.get('pending', 0)}")
    print(f"Done: {data.get('done', 0)}")
    print(f"Error: {data.get('error', 0)}")
    return data

if __name__ == "__main__":
    print("=== Running Pipeline ===")
    
    # Step 1: Scan
    scan_files()
    
    # Step 2: Parse
    parse_data = parse_pending()
    
    # Wait for parsing if needed
    if parse_data.get('parsed', 0) > 0:
        wait_for_parsing(timeout=600)
    
    # Step 3: Chunk
    chunk_data = chunk_parsed()
    
    # Step 4: Upload
    if chunk_data.get('chunked', 0) > 0:
        upload_to_dify()
    
    # Check final status
    check_status()
    
    print("\n=== Pipeline Complete ===")
