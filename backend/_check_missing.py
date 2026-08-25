"""检查手卫生文件夹所有文件，对比 manifest 找出缺失文档。"""
import os, sys
from pathlib import Path

# 强制 UTF-8 输出
sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(r"d:\programmtools\tools\ragsystem\data\转\手卫生")
PENDING = Path(r"d:\programmtools\tools\ragsystem\data\pending")

# 1) 递归列出手卫生文件夹下所有文件
print("=== 手卫生文件夹下所有文件 ===")
all_files = []
for f in sorted(ROOT.rglob("*")):
    if f.is_file() and not f.name.startswith("~$") and f.name != ".gitkeep":
        all_files.append(f)
        rel = f.relative_to(ROOT)
        print(f"  {rel}  ({f.stat().st_size} bytes)")

print(f"\n总计: {len(all_files)} 个文件")

# 2) 列出 pending/ 下所有文件
print("\n=== pending/ 下所有文件 ===")
pending_files = []
if PENDING.exists():
    for f in sorted(PENDING.iterdir()):
        if f.is_file() and f.name != ".gitkeep":
            pending_files.append(f)
            print(f"  {f.name}  ({f.stat().st_size} bytes)")

print(f"\n总计: {len(pending_files)} 个文件")

# 3) 对比：哪些手卫生文件夹的文件不在 pending/ 中
print("\n=== 不在 pending/ 中的文件 ===")
pending_names = {f.name for f in pending_files}
pending_stems = {f.stem for f in pending_files}
missing = []
for f in all_files:
    if f.name not in pending_names:
        # 也检查 stem 匹配（可能 pending 中文件名略有不同）
        stem_match = any(f.stem in ps or ps in f.stem for ps in pending_stems)
        if not stem_match:
            missing.append(f)
            print(f"  MISSING: {f.relative_to(ROOT)}")

print(f"\n缺失: {len(missing)} 个文件")

# 4) 也检查 manifest（PostgreSQL）中的文件
from app.services import manifest_store

manifest = manifest_store.load()
manifest_files = set(manifest.keys())
print(f"\n=== manifest 中有 {len(manifest_files)} 个文件 ===")

print("\n=== 不在 manifest 中的手卫生文件 ===")
for f in all_files:
    if f.name not in manifest_files:
        # 也检查 stem 匹配
        stem_in_manifest = any(f.stem in mf or mf in f.stem for mf in manifest_files)
        if not stem_in_manifest:
            print(f"  NOT IN MANIFEST: {f.relative_to(ROOT)}")
