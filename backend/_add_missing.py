"""
对比 pending/ 与 手卫生/ 文件夹，找出缺失文件并复制到 pending/。
"""
import sys, shutil
from pathlib import Path

sys.stdout.reconfigure(encoding='utf-8')

PENDING = Path(r"d:\programmtools\tools\ragsystem\data\pending")
ROOT = Path(r"d:\programmtools\tools\ragsystem\data\转\手卫生")

# 1) 列出手卫生文件夹下所有文件（排除临时文件）
print("=== 手卫生文件夹下所有文件 ===")
all_files = []
for f in sorted(ROOT.rglob("*")):
    if f.is_file() and not f.name.startswith("~$") and f.name != ".gitkeep":
        all_files.append(f)
        print(f"  {f.relative_to(ROOT)}")

print(f"\n总计: {len(all_files)} 个文件")

# 2) 列出 pending/ 下所有文件
print("\n=== pending/ 下所有文件 ===")
pending_files = []
for f in sorted(PENDING.iterdir()):
    if f.is_file() and f.name != ".gitkeep":
        pending_files.append(f)
        print(f"  {f.name}")

print(f"\n总计: {len(pending_files)} 个文件")

# 3) 对比：哪些手卫生文件夹的文件不在 pending/ 中
print("\n=== 不在 pending/ 中的文件 ===")
pending_names = {f.name for f in pending_files}
pending_stems = {f.stem.strip() for f in pending_files}

missing = []
for f in all_files:
    if f.name in pending_names:
        continue
    # 检查 stem 匹配
    stem = f.stem.strip()
    stem_match = any(stem in ps or ps in stem for ps in pending_stems)
    if stem_match:
        print(f"  STEM MATCH (skip): {f.name}")
        continue
    missing.append(f)
    print(f"  MISSING: {f.relative_to(ROOT)}")

print(f"\n缺失: {len(missing)} 个文件")

# 4) 复制缺失文件到 pending/
if missing:
    print("\n=== 复制缺失文件到 pending/ ===")
    for f in missing:
        dst = PENDING / f.name
        shutil.copy2(f, dst)
        print(f"  Copied: {f.name}")
    print(f"\n已复制 {len(missing)} 个文件到 pending/")
else:
    print("\n没有缺失文件")
