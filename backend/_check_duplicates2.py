"""
检查 pending/ 中可能的重复文件。
"""
import sys
from pathlib import Path
from collections import defaultdict

sys.stdout.reconfigure(encoding='utf-8')

PENDING = Path(r"d:\programmtools\tools\ragsystem\data\pending")

# 列出所有文件
files = []
for f in sorted(PENDING.iterdir()):
    if f.is_file() and f.name != ".gitkeep":
        files.append(f)

# 按 stem 分组
stem_groups = defaultdict(list)
for f in files:
    stem = f.stem.strip()
    stem_groups[stem].append(f)

# 找出可能的重复
print("=== 可能的重复文件 ===")
for stem, group in stem_groups.items():
    if len(group) > 1:
        print(f"\n{stem}:")
        for f in group:
            size = f.stat().st_size
            print(f"  {f.name} ({size} bytes)")

# 检查相似文件名
print("\n=== 相似文件名 ===")
for i, f1 in enumerate(files):
    for f2 in files[i+1:]:
        # 检查是否包含相同关键词
        if "手卫生规范" in f1.name and "手卫生规范" in f2.name:
            print(f"  {f1.name} <-> {f2.name}")
        elif "WS 628" in f1.name and "WS 628" in f2.name:
            print(f"  {f1.name} <-> {f2.name}")
        elif "QBT 5997" in f1.name and "QBT 5997" in f2.name:
            print(f"  {f1.name} <-> {f2.name}")
