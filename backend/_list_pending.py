"""
列出 pending/ 中所有文件，标记哪些是从手卫生文件夹添加的。
"""
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding='utf-8')

PENDING = Path(r"d:\programmtools\tools\ragsystem\data\pending")
ROOT = Path(r"d:\programmtools\tools\ragsystem\data\转\手卫生")

# 获取手卫生文件夹下所有文件名
root_files = set()
for f in ROOT.rglob("*"):
    if f.is_file() and not f.name.startswith("~$") and f.name != ".gitkeep":
        root_files.add(f.name)

print(f"手卫生文件夹文件数: {len(root_files)}")

# 列出 pending/ 中所有文件
pending_files = []
for f in sorted(PENDING.iterdir()):
    if f.is_file() and f.name != ".gitkeep":
        pending_files.append(f)

print(f"pending/ 文件数: {len(pending_files)}")

# 标记哪些是从手卫生文件夹添加的
print("\n=== pending/ 文件列表 ===")
from_root = 0
not_from_root = 0

for f in pending_files:
    if f.name in root_files:
        print(f"  [手卫生] {f.name}")
        from_root += 1
    else:
        print(f"  [其他]   {f.name}")
        not_from_root += 1

print(f"\n来自手卫生: {from_root}")
print(f"非手卫生: {not_from_root}")
