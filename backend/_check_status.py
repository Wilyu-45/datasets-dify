"""
检查当前 manifest（PostgreSQL manifest 表）状态，统计各状态文件数。
"""
import sys
from collections import Counter

sys.stdout.reconfigure(encoding="utf-8")

from app.services import manifest_store

manifest = manifest_store.load()
entries = list(manifest.values())

print(f"manifest 总行数: {len(entries)}")

stats = {"total": len(entries)}
for e in entries:
    status = str(e.status or "").strip()
    parse_status = str(e.parse or "").strip()
    dify_id = str(e.dify_doc_id or "").strip()

    if dify_id and dify_id != "None":
        stats["has_dify_id"] = stats.get("has_dify_id", 0) + 1
    else:
        stats["no_dify_id"] = stats.get("no_dify_id", 0) + 1

    if status == "done":
        stats["done"] = stats.get("done", 0) + 1
    elif status == "error":
        stats["error"] = stats.get("error", 0) + 1
    elif status == "chunked":
        stats["chunked"] = stats.get("chunked", 0) + 1
    elif status == "parsed" or parse_status == "done":
        stats["parsed"] = stats.get("parsed", 0) + 1
    elif status == "parsing":
        stats["parsing"] = stats.get("parsing", 0) + 1
    else:
        stats["pending"] = stats.get("pending", 0) + 1

print(f"\n=== Manifest 状态统计 ===")
print(f"总计: {stats['total']}")
print(f"  done (已上传 Dify): {stats.get('done', 0)}")
print(f"  chunked (已切分待上传): {stats.get('chunked', 0)}")
print(f"  parsed (已解析待切分): {stats.get('parsed', 0)}")
print(f"  parsing (解析中): {stats.get('parsing', 0)}")
print(f"  pending (待处理): {stats.get('pending', 0)}")
print(f"  error (错误): {stats.get('error', 0)}")
print(f"\n有 Dify ID: {stats.get('has_dify_id', 0)}")
print(f"无 Dify ID: {stats.get('no_dify_id', 0)}")

pending_files = [e.filename for e in entries if (e.status or "").strip() == ""]
error_files = [e.filename for e in entries if (e.status or "").strip() == "error"]

if pending_files:
    print(f"\n=== 待处理文件 ({len(pending_files)}) ===")
    for fn in pending_files[:20]:
        print(f"  {fn}")
    if len(pending_files) > 20:
        print(f"  ... 还有 {len(pending_files) - 20} 个")

if error_files:
    print(f"\n=== 错误文件 ({len(error_files)}) ===")
    for fn in error_files:
        print(f"  {fn}")
