"""Reset manifest (PostgreSQL) + clean parsed for Jining PDF."""
import shutil
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

from app.services import manifest_store

# 1. Clean parsed dir
parsed_dir = Path(r"D:\programmtools\tools\ragsystem\data\parsed\济宁市医疗卫生机构病死婴幼儿遗体处理暂行办法(1)")
if parsed_dir.exists():
    shutil.rmtree(parsed_dir)
    print(f"cleaned: {parsed_dir}")
else:
    print(f"not exists: {parsed_dir}")

# 2. Reset manifest parse column (PostgreSQL)
manifest_store.bootstrap()
manifest = manifest_store.load()
for filename, row in manifest.items():
    if filename and "济宁" in filename:
        old_parse = row.parse
        old_status = row.status
        row.parse = ""
        row.status = "pending"
        row.update_time = manifest_store.now_iso()
        manifest_store.upsert(row)
        print(f"reset Jining: parse={old_parse!r} → '', status={old_status!r} → pending")
        break
else:
    print("Jining row not found")
print("manifest updated (PostgreSQL)")
