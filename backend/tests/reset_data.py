"""清理 E2E 测试遗留的 data 目录，还原为干净的初始状态（按 文件列表Excel示例.txt）。

- 清空 manifest.xlsx
- 清空 input/
- 清空 pending/
- 重新创建 11 列的 manifest（启动时 bootstrap 会自动补 5 列）
"""

import shutil
from pathlib import Path

ROOT = Path("d:/programmtools/tools/ragsystem")
DATA = ROOT / "data"
INPUT = DATA / "input"
PENDING = DATA / "pending"
MANIFEST = DATA / "manifest.xlsx"

# 清空 input/pending
for f in INPUT.iterdir():
    if f.name != ".gitkeep":
        f.unlink()
for f in PENDING.iterdir():
    if f.name != ".gitkeep":
        f.unlink()

# 删除 manifest
if MANIFEST.exists():
    MANIFEST.unlink()

# 重新创建 manifest（仅 11 列 → 让启动时 bootstrap 自动补 5 列）
from openpyxl import Workbook

wb = Workbook()
ws = wb.active
ws.title = "Sheet1"
headers = [
    "序号", "文件名称", "一级分类", "二级分类", "关键词标签",
    "适用科室", "生效日期", "导入情况", "处理情况", "校对", "处理说明",
]
ws.append(headers)
# 来自 文件列表Excel示例.txt
data_rows = [
    [1, "DB23T 3925—2024医用空气加压氧舱安全管理指南", "地标", "医用氧舱", "医用空气加压氧舱、安全管理", "高压氧科", "2024/12/15", "已导入", "已处理", "是", "元数据OK，内容调整OK"],
    [2, "GB 50751-2012 医用气体工程技术规范", "国标", "医用气体", "医用气体、工程设计", "全院", "2012/8/1", "", "", "", "扫描件、大文件"],
    [3, "GBT 12130-2005 医用空气加压氧舱", "国标", "医用氧舱", "医用空气加压氧舱", "高压氧科", "2006/4/1", "已导入", "已处理", "", "元数据OK，内容调整OK"],
    [4, "GBT 12130-2020 氧舱", "国标", "医用氧舱", "氧舱、分类、试验方法", "高压氧科", "2021/4/1", "已导入", "已处理", "", "元数据OK，内容调整OK"],
    [5, "GBT 19284-2003 医用氧气加压舱", "国标", "医用氧舱", "医用氧气加压舱", "高压氧科", "2004/3/1", "", "", "", ""],
    [6, "TCAME 76—2025《高原微压氧舱》", "团标", "医用氧舱", "高原微压氧舱", "高压氧科", "2025/9/15", "", "", "", ""],
    [7, "TCHAS 10-2-23—2022 中国医院质量安全管理 第2-23部分：患者服务 高压氧治疗", "团标", "医用氧舱", "医院质量安全、高压氧治疗", "高压氧科", "2022/6/1", "", "", "", ""],
]
for r in data_rows:
    ws.append(r)
wb.save(MANIFEST)
wb.close()

print("✓ data 目录已重置")
print(f"  manifest.xlsx: 11 列（启动时 bootstrap 会自动补到 16 列）")
print(f"  input/: 空")
print(f"  pending/: 空")
