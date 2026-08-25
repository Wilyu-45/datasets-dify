"""
清理 manifest（PostgreSQL）：移除不在 pending/ 中的条目。
（PG manifest 表以 filename 为主键，不存在重复行；重复检查见 _check_duplicates.py）
"""
import sys
from collections import Counter
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

from app.services import manifest_store

DATA_DIR = Path(r"d:\programmtools\tools\ragsystem\data")
PENDING = DATA_DIR / "pending"

# 1) 获取 pending/ 中的文件
pending_files = set(
    f.name for f in PENDING.iterdir() if f.is_file() and f.name != ".gitkeep"
)
print(f"pending/ 文件数: {len(pending_files)}")

# 2) 移除不在 pending/ 中的条目
manifest = manifest_store.load()
removed = []
for filename in list(manifest.keys()):
    if filename not in pending_files:
        print(f"  移除 (不在 pending/): {filename}")
        manifest_store.delete(filename)
        removed.append(filename)

print(f"\n移除 {len(removed)} 条，剩余 {manifest_store.count()} 条")

# 3) 显示统计
status_counts = Counter(
    (e.status or "").strip() for e in manifest.values() if e.filename not in removed
)
print(f"\n状态分布:")
for status, count in sorted(status_counts.items()):
    print(f"  {status or '(empty)'}: {count}")

dify_counts = sum(
    1
    for e in manifest.values()
    if e.filename not in removed and e.dify_doc_id and e.dify_doc_id != "None"
)
remaining = len(manifest) - len(removed)
print(f"\n有 Dify ID: {dify_counts}")
print(f"无 Dify ID: {remaining - dify_counts}")
