"""
检查 manifest（PostgreSQL）中的重复条目与缺失文件。
"""
import sys
from collections import Counter
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

from app.services import manifest_store

DATA_DIR = Path(r"d:\programmtools\tools\ragsystem\data")

manifest = manifest_store.load()
entries = list(manifest.values())
print(f"总条目: {len(entries)}")

# filename 为 PG 主键，正常不会重复
fname_counts = Counter(e.filename for e in entries)
duplicates = {fn: c for fn, c in fname_counts.items() if c > 1}
if duplicates:
    print(f"\n=== 重复文件名（主键约束下不应出现）===")
    for fn, count in duplicates.items():
        print(f"  {fn}: {count} 次")
else:
    print("\n没有重复文件名")

# 检查 pending 文件夹中的实际文件
PENDING = DATA_DIR / "pending"
pending_files = sorted(
    f.name for f in PENDING.iterdir() if f.is_file() and f.name != ".gitkeep"
)
print(f"\npending/ 文件数: {len(pending_files)}")

pending_set = set(pending_files)
not_in_pending = [e for e in entries if e.filename not in pending_set]
if not_in_pending:
    print(f"\n=== manifest 条目但文件不在 pending/ 中 ({len(not_in_pending)}) ===")
    for e in not_in_pending:
        print(f"  {e.filename} (status={e.status})")
