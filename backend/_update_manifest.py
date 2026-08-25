"""
更新 manifest（PostgreSQL manifest 表），添加 pending/ 中新增的文件（从手卫生文件夹复制过来的）。
"""
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

from app.models.schemas import ManifestRow
from app.services import manifest_store

DATA_DIR = Path(r"d:\programmtools\tools\ragsystem\data")
PENDING = DATA_DIR / "pending"

manifest = manifest_store.load()
print(f"当前 manifest 文件数: {len(manifest)}")

# 2) 列出 pending/ 下所有文件
pending_files = sorted(
    f for f in PENDING.iterdir() if f.is_file() and f.name != ".gitkeep"
)
print(f"pending/ 文件数: {len(pending_files)}")

# 3) 找出 pending/ 中不在 manifest 中的文件
missing = [f for f in pending_files if f.name not in manifest]
print(f"\n不在 manifest 中的文件: {len(missing)} 个")
for f in missing:
    print(f"  {f.name}")

# 4) 添加缺失文件到 manifest
if missing:
    max_seq = max((row.seq or 0 for row in manifest.values()), default=0)
    rows = []
    for f in missing:
        max_seq += 1
        rows.append(
            ManifestRow(
                seq=max_seq,
                filename=f.name,
                create_time=manifest_store.now_iso(),
                update_time=manifest_store.now_iso(),
            )
        )
        print(f"  Added: {f.name} (seq={max_seq})")

    manifest_store.bulk_upsert(rows)
    print(f"\n已更新 PostgreSQL manifest，新增 {len(missing)} 条记录")
else:
    print("\n没有需要添加的文件")

# 5) 显示最终统计
print(f"\n最终 manifest 文件数: {manifest_store.count()}")
