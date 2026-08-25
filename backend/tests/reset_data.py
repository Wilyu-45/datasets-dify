"""清理 E2E 测试遗留的 data 目录，还原为干净的初始状态（按 文件列表Excel示例.txt）。

- 清空 input/
- 清空 pending/
- 清空 manifest 表（PostgreSQL）
- 重新写入 7 行示例数据（来自 文件列表Excel示例.txt）
"""

import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path("d:/programmtools/tools/ragsystem")
DATA = ROOT / "data"
INPUT = DATA / "input"
PENDING = DATA / "pending"

# 清空 input/pending
for f in INPUT.iterdir():
    if f.name != ".gitkeep":
        f.unlink()
for f in PENDING.iterdir():
    if f.name != ".gitkeep":
        f.unlink()

# 清空 manifest 表并重新写入示例数据（PostgreSQL）
sys.path.insert(0, str(ROOT / "backend"))
from app.models.schemas import ManifestRow
from app.services import manifest_store

manifest_store.bootstrap()
manifest_store.clear()

data_rows = [
    ManifestRow(seq=1, filename="DB23T 3925—2024医用空气加压氧舱安全管理指南",
                category_l1="地标", category_l2="医用氧舱", keywords="医用空气加压氧舱、安全管理",
                department="高压氧科", effective_date="2024/12/15",
                import_status="已导入", process_status="已处理", verified="是",
                process_note="元数据OK，内容调整OK"),
    ManifestRow(seq=2, filename="GB 50751-2012 医用气体工程技术规范",
                category_l1="国标", category_l2="医用气体", keywords="医用气体、工程设计",
                department="全院", effective_date="2012/8/1", process_note="扫描件、大文件"),
    ManifestRow(seq=3, filename="GBT 12130-2005 医用空气加压氧舱",
                category_l1="国标", category_l2="医用氧舱", keywords="医用空气加压氧舱",
                department="高压氧科", effective_date="2006/4/1",
                import_status="已导入", process_status="已处理", process_note="元数据OK，内容调整OK"),
    ManifestRow(seq=4, filename="GBT 12130-2020 氧舱",
                category_l1="国标", category_l2="医用氧舱", keywords="氧舱、分类、试验方法",
                department="高压氧科", effective_date="2021/4/1",
                import_status="已导入", process_status="已处理", process_note="元数据OK，内容调整OK"),
    ManifestRow(seq=5, filename="GBT 19284-2003 医用氧气加压舱",
                category_l1="国标", category_l2="医用氧舱", keywords="医用氧气加压舱",
                department="高压氧科", effective_date="2004/3/1"),
    ManifestRow(seq=6, filename="TCAME 76—2025《高原微压氧舱》",
                category_l1="团标", category_l2="医用氧舱", keywords="高原微压氧舱",
                department="高压氧科", effective_date="2025/9/15"),
    ManifestRow(seq=7, filename="TCHAS 10-2-23—2022 中国医院质量安全管理 第2-23部分：患者服务 高压氧治疗",
                category_l1="团标", category_l2="医用氧舱", keywords="医院质量安全、高压氧治疗",
                department="高压氧科", effective_date="2022/6/1"),
]
manifest_store.bulk_upsert(data_rows)

print("✓ data 目录已重置（PostgreSQL）")
print(f"  manifest 表: {manifest_store.count()} 行")
print(f"  input/: 空")
print(f"  pending/: 空")
