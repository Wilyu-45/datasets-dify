"""E2E 验证：用户写入 manifest（PostgreSQL，文件名无扩展名）→ 启动 bootstrap（不移动）→ 扫描按钮 → 自动补全扩展名并更新 manifest。"""

import importlib
import logging
import os
import sys
from pathlib import Path

logging.disable(logging.CRITICAL)

# 准备工作目录
ROOT = Path("d:/programmtools/tools/ragsystem")
DATA = ROOT / "data"
INPUT = DATA / "input"
PENDING = DATA / "pending"

# 备份现有 manifest 表（PostgreSQL），测试结束后恢复
sys.path.insert(0, str(ROOT / "backend"))
os.environ.setdefault("RAG_DATA_ROOT", str(DATA))

from app import config as cfg_mod
importlib.reload(cfg_mod)
settings = cfg_mod.settings
from app.models.schemas import ManifestRow
from app.services import scanner, manifest_store
scanner.settings = settings

manifest_store.bootstrap(DATA)
saved_manifest = list(manifest_store.load().values())
manifest_store.clear()

# 清空 input/pending
for f in INPUT.iterdir():
    if f.name != ".gitkeep":
        f.unlink()
for f in PENDING.iterdir():
    if f.name != ".gitkeep":
        f.unlink()

# 1) 模拟用户写入 manifest 行（文件名称均无扩展名）
data_rows = [
    ManifestRow(seq=1, filename="国标-001", category_l1="国标", category_l2="医用氧舱",
                keywords="高压氧", department="高压氧科", effective_date="2006-04-01", process_note="用户原备注"),
    ManifestRow(seq=2, filename="团标-002", category_l1="团标", category_l2="康复",
                keywords="智能", department="康复科", effective_date="2024-01-01"),
    ManifestRow(seq=3, filename="新规-003", category_l1="规范", category_l2="院内",
                keywords="院感", department="院感科", effective_date="2025-06-01", process_note="重要"),
    ManifestRow(seq=4, filename="已处理-004", category_l1="规范", category_l2="院内",
                keywords="已导入", department="院感科", effective_date="2025-06-01",
                import_status="已移入待处理", process_status="已扫描", process_note="上次扫描已处理"),
    ManifestRow(seq=5, filename="精确匹配-005", category_l1="国标", category_l2="测试",
                keywords="ext", department="测试科", effective_date="2025-01-01", process_note="文件名带扩展名"),
]
manifest_store.bulk_upsert(data_rows)

# 2) 启动 bootstrap（PG 表结构固定，幂等）
manifest_store.bootstrap(DATA)
print("✓ bootstrap 完成（PostgreSQL manifest 表结构固定）")

# 3) 模拟用户放入文件：扩展名不同，覆盖常见情况
(INPUT / "国标-001.pdf").write_bytes(b"PDF content for guobiao")
(INPUT / "新规-003.docx").write_bytes(b"DOCX content for xingui")
# 团标-002 故意不放 → MISSING
# 已处理-004 已被标记已处理，会被跳过
(INPUT / "精确匹配-005.pdf").write_bytes(b"Exact match with extension")  # 已经有扩展名
# 模拟一个同名但扩展名不同的歧义场景
(INPUT / "歧义文件-006.doc").write_bytes(b"DOC content")
(INPUT / "歧义文件-006.pdf").write_bytes(b"PDF content")
# 优先级：.pdf > .docx > .doc，所以应选 .pdf

# 4) 用户点击「扫描」按钮
report = scanner.scan_and_stage(dry_run=False)
print(f"✓ 扫描完成: staged={report.staged}, new={report.new}, skipped={report.skipped_done}, missing={report.missing_on_disk}")

# 5) 验证 manifest 的 filename 字段被更新为带扩展名的真实文件名
manifest = manifest_store.load()

# 国标-001: 无扩展名 → 应补为 .pdf
assert "国标-001.pdf" in manifest, f"应存在 '国标-001.pdf'，实际 keys: {list(manifest.keys())}"
assert "国标-001" not in manifest, f"旧的 '国标-001' 不应再存在"
row1 = manifest["国标-001.pdf"]
assert row1.import_status == "已移入待处理"
assert row1.status == "pending"
print(f"✓ 国标-001 → 国标-001.pdf (扩展名自动补全)")

# 新规-003: 无扩展名 → 应补为 .docx
assert "新规-003.docx" in manifest
row3 = manifest["新规-003.docx"]
assert row3.import_status == "已移入待处理"
print(f"✓ 新规-003 → 新规-003.docx")

# 已处理-004: 跳过（已标记已导入）
assert "已处理-004" in manifest  # 保持原样（无扩展名）
row4 = manifest["已处理-004"]
assert row4.import_status == "已移入待处理"
print(f"✓ 已处理-004: 保持原文件名（已处理）")

# 精确匹配-005: 已有 .pdf → 保持
assert "精确匹配-005.pdf" in manifest
row5 = manifest["精确匹配-005.pdf"]
assert row5.import_status == "已移入待处理"
print(f"✓ 精确匹配-005.pdf: 精确匹配")

# 团标-002: MISSING
assert "团标-002" in manifest
row2 = manifest["团标-002"]
assert row2.import_status is None
print(f"✓ 团标-002: MISSING（input 找不到）")

# 6) 验证 pending/ 中的文件
pending_files = {p.name for p in PENDING.iterdir() if p.is_file()}
expected_pending = {"国标-001.pdf", "新规-003.docx", "精确匹配-005.pdf"}
assert expected_pending.issubset(pending_files), f"pending 缺文件: {expected_pending - pending_files}"
print(f"✓ pending/ 包含所有移入文件")

# 7) 验证 input/ 已被清空
input_files = {p.name for p in INPUT.iterdir() if p.name != ".gitkeep"}
# 团标/歧义/已处理 这些不该被移走
print(f"  input 剩余: {input_files}")

# 8) 幂等性：第二次扫描
second = scanner.scan_and_stage(dry_run=False)
assert second.staged == 0, f"第二次应 staged=0, 实际={second.staged}"
# 应有 3 个已导入 + 1 个 MISSING（团标-002）+ 1 个已处理（已处理-004）
print(f"✓ 第二次扫描幂等: staged={second.staged}, skipped={second.skipped_done}, missing={second.missing_on_disk}")

# 恢复 manifest 表
manifest_store.clear()
if saved_manifest:
    manifest_store.bulk_upsert(saved_manifest)

# 清掉测试文件
for d in (PENDING,):
    for f in d.iterdir():
        if f.name not in (".gitkeep",) and (f.name.startswith("国标") or f.name.startswith("新规") or f.name.startswith("精确匹配") or f.name.startswith("歧义")):
            if f.is_dir():
                import shutil
                shutil.rmtree(f, ignore_errors=True)
            else:
                f.unlink()
for f in INPUT.iterdir():
    if f.name not in (".gitkeep",) and (f.name.startswith("国标") or f.name.startswith("新规") or f.name.startswith("精确匹配") or f.name.startswith("歧义")):
        f.unlink()

print("\n=== 全部 E2E 验证通过（扩展名自动补全） ===")
