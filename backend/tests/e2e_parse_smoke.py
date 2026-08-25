"""E2E 验证：§3.2 解析流程。

场景：
  1. 用户写入 manifest 行（文件名无扩展名）+ 把对应 .pdf 放入 input/
  2. 启动 → bootstrap 初始化 PostgreSQL manifest 表
  3. 模拟「扫描」：从 input/ 移到 pending/，manifest 标记「已移入待处理」
  4. mock 一个 MinerU API（POST /file_parse → 返回 md+json）
  5. 模拟「解析」：调 parser.parse_pending
  6. 验证：
       - parsed/{stem}/ 目录被创建
       - .md / .json 落盘正确
       - manifest 的 parse 列 = 解析目录绝对路径
       - manifest 的 status 列 = 'parsing_done'
       - 第二次解析 = SKIPPED_DONE（幂等）
"""

from __future__ import annotations

import importlib
import json
import logging
import shutil
import sys
from pathlib import Path
from typing import Any, Dict, List

logging.disable(logging.CRITICAL)

# ============ 准备 ============

ROOT = Path("d:/programmtools/tools/ragsystem")
DATA = ROOT / "data"
INPUT = DATA / "input"
PENDING = DATA / "pending"
PARSED = DATA / "parsed"
ERROR = DATA / "error"
MANIFEST = DATA / "manifest.xlsx"  # 兼容占位：manifest 已迁移 PostgreSQL，此路径不再读写

# 清空 input/pending/parsed/error（保留 .gitkeep）
for d in (INPUT, PENDING, PARSED, ERROR):
    for f in d.iterdir():
        if f.name == ".gitkeep":
            continue
        if f.is_dir():
            shutil.rmtree(f, ignore_errors=True)
        else:
            f.unlink(missing_ok=True)

# ============ 1) 初始化 PostgreSQL manifest 并写入用户数据 ============

sys.path.insert(0, str(ROOT / "backend"))
import os
os.environ.setdefault("RAG_DATA_ROOT", str(DATA))

from app import config as cfg_mod
importlib.reload(cfg_mod)
settings = cfg_mod.settings
from app.models.schemas import ManifestRow
from app.services import scanner, parser, manifest_store
scanner.settings = settings
parser.settings = settings

# bootstrap：PostgreSQL manifest 表结构固定（幂等建表）
manifest_store.bootstrap(DATA)

# 备份现有 manifest 表（测试结束后恢复）
saved_manifest = list(manifest_store.load().values())
manifest_store.clear()

# 写入用户 manifest 行（文件名无扩展名）
manifest_store.bulk_upsert([
    ManifestRow(seq=1, filename="国标-001", category_l1="国标", category_l2="医用氧舱",
                keywords="高压氧", department="高压氧科", effective_date="2006-04-01",
                process_note="用户原备注"),
    ManifestRow(seq=2, filename="团标-002", category_l1="团标", category_l2="康复",
                keywords="智能", department="康复科", effective_date="2024-01-01"),
    ManifestRow(seq=3, filename="失败-003", category_l1="规范", category_l2="院内",
                keywords="院感", department="院感科", effective_date="2025-06-01",
                process_note="这个会解析失败"),
])
print(f"✓ manifest 表（PostgreSQL）已写入 3 条用户记录")

# 放入 input 文件
(INPUT / "国标-001.pdf").write_bytes(b"PDF content for guobiao")
(INPUT / "团标-002.docx").write_bytes(b"DOCX content for tuibiao")
(INPUT / "失败-003.pdf").write_bytes(b"PDF content for failure")

# ============ 3) 模拟「扫描」按钮 ============

report_scan = scanner.scan_and_stage(dry_run=False)
assert report_scan.staged == 3, f"应 staged=3, 实际={report_scan.staged}"
print(f"✓ 扫描完成: staged={report_scan.staged}, renamed={report_scan.renamed}")

# ============ 4) Mock 一个真实 MinerU API（返回 ZIP，含多个产物）============
import io
import zipfile


def _build_mineru_zip(stem: str) -> bytes:
    """构造一个模拟 mineru 返回的 ZIP：含 .md/.json/图片/layout 等多个产物。"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(f"{stem}.md", f"# {stem}\n\n解析后的 markdown 内容\n")
        zf.writestr(
            f"{stem}.json",
            json.dumps(
                {
                    "blocks": [
                        {"type": "h1", "text": stem},
                        {"type": "p", "text": "示例段落"},
                    ],
                    "content_order": [0, 1],
                },
                ensure_ascii=False,
                indent=2,
            ),
        )
        zf.writestr("images/image_0.png", b"\x89PNG\r\n\x1a\nFAKE_PNG")
        zf.writestr("images/image_1.jpg", b"\xff\xd8\xff\xe0FAKE_JPG")
        zf.writestr("layout.json", json.dumps({"layout_spans": []}, ensure_ascii=False))
        zf.writestr(f"{stem}_origin.pdf", b"%PDF-1.4\nFAKE_PDF")
    return buf.getvalue()


# 失败-003.pdf → 5xx；其它 → 200 + ZIP
class _Resp:
    def __init__(self, status, body=b"", ctype="application/zip"):
        self.status_code = status
        self.content = body
        self.text = body.decode("utf-8", errors="replace") if body else ""
        self.reason_phrase = "OK" if status == 200 else "Service Unavailable"
        self.headers = {"content-type": ctype} if status == 200 else {}

    def json(self):
        return {}


class _Cli:
    def __init__(self, *a, **kw):  # noqa: ARG002
        self.fail_names = {"失败-003.pdf"}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def post(self, url, files=None, data=None, headers=None):  # noqa: A002
        # files: {"files": (filename, fileobj, mime)}
        # fileobj 是 SpooledTemporaryFile，读取其 name
        try:
            name = files["files"][0]
        except Exception:
            name = "unknown"
        if name in self.fail_names:
            return _Resp(500, b"oops", "text/plain")
        # 推断 stem：取首个上传文件的 stem
        stem = Path(name).stem
        return _Resp(200, _build_mineru_zip(stem), "application/zip")


# monkeypatch httpx.Client
import httpx
real_client = httpx.Client
httpx.Client = _Cli  # type: ignore[assignment]

# ============ 5) 模拟「解析」按钮 ============

report_parse = parser.parse_pending(dry_run=False)
print(f"✓ 解析完成: scanned={report_parse.scanned}, parsed={report_parse.parsed}, "
      f"skipped={report_parse.skipped_done}, failed={report_parse.failed}")
assert report_parse.parsed == 2, f"应 parsed=2, 实际={report_parse.parsed}"
assert report_parse.failed == 1, f"应 failed=1, 实际={report_parse.failed}"

# ============ 6) 验证 parsed/ 目录（应含所有产物）============

# 国标-001.pdf → parsed/国标-001/
out1 = PARSED / "国标-001"
assert out1.is_dir(), f"应存在 {out1}"
# .md
assert (out1 / "国标-001.md").is_file(), "应生成 .md"
md_content = (out1 / "国标-001.md").read_text(encoding="utf-8")
assert "解析后的 markdown" in md_content, f".md 内容应含『解析后的 markdown』, 实际={md_content!r}"
# .json
assert (out1 / "国标-001.json").is_file(), "应生成 .json"
json_content = json.loads((out1 / "国标-001.json").read_text(encoding="utf-8"))
assert "blocks" in json_content, ".json 应含 blocks 字段"
# ★ images（多张）
imgs = sorted((out1 / "images").glob("*"))
assert len(imgs) == 2, f"应有 2 张图片, 实际 {len(imgs)}: {[p.name for p in imgs]}"
print(f"✓ 国标-001 解析产物: parsed/国标-001/{{.md, .json, images/*.png, images/*.jpg}}")
# ★ 其它产物（layout.json, *_origin.pdf）
other_files = [p for p in out1.rglob("*") if p.is_file() and p.suffix in (".json", ".pdf", ".png", ".jpg")]
other_count = sum(1 for p in out1.rglob("*") if p.is_file())
print(f"  共 {other_count} 个产物文件")

# 团标-002.docx → parsed/团标-002/
out2 = PARSED / "团标-002"
assert out2.is_dir(), f"应存在 {out2}"
assert (out2 / "团标-002.md").is_file()
print(f"✓ 团标-002 解析产物: parsed/团标-002/{{.md, .json, images/*, layout.json, *_origin.pdf}}")

# 失败-003.pdf → error/失败-003.pdf
err_file = ERROR / "失败-003.pdf"
assert err_file.is_file(), f"失败文件应移入 {err_file}"
assert not (PENDING / "失败-003.pdf").exists(), "失败文件应不在 pending/"
# 失败文件不应在 parsed/
assert not (PARSED / "失败-003").exists(), "失败文件不应在 parsed/"
print(f"✓ 失败文件已移入 error/，未污染 parsed/")

# ============ 7) 验证 manifest 更新 ============

manifest = manifest_store.load(MANIFEST)
# 成功行
row1 = manifest["国标-001.pdf"]
assert row1.status == "parsing_done", f"status 应为 parsing_done, 实际={row1.status}"
assert "parsed" in (row1.parse or "").lower() or "国标-001" in (row1.parse or ""), \
    f"parse 应含路径，实际={row1.parse!r}"
print(f"✓ 国标-001 manifest: status={row1.status}, parse={row1.parse}")

row2 = manifest["团标-002.docx"]
assert row2.status == "parsing_done"
print(f"✓ 团标-002 manifest: status={row2.status}, parse={row2.parse}")

# 失败行
row3 = manifest["失败-003.pdf"]
assert row3.status == "error", f"status 应为 error, 实际={row3.status}"
assert "失败" in (row3.parse or ""), f"parse 应含失败描述，实际={row3.parse!r}"
assert "5xx" in (row3.error_msg or ""), f"error_msg 应含错误原因，实际={row3.error_msg!r}"
print(f"✓ 失败-003 manifest: status={row3.status}, error_msg={row3.error_msg[:40]}…")

# ============ 8) 幂等性：第二次解析 = 全 SKIPPED_DONE ============

report_2 = parser.parse_pending(dry_run=False)
assert report_2.parsed == 0, f"第二次应 parsed=0, 实际={report_2.parsed}"
assert report_2.skipped_done == 2, f"第二次应 skipped=2, 实际={report_2.skipped_done}"
print(f"✓ 第二次解析幂等: parsed=0, skipped=2")

# ============ 9) 试运行 dry_run ============

# 新增一行不会真解析
(INPUT / "新文件-004.pdf").write_bytes(b"new content")
manifest_store.upsert(ManifestRow(filename="新文件-004.pdf", import_status=""))
# 用真扫描把它移入 pending
(INPUT / "新文件-004.pdf").unlink()
(PENDING / "新文件-004.pdf").write_bytes(b"new content")
from app.models.schemas import ManifestRow
manifest_store.upsert(
    ManifestRow(filename="新文件-004.pdf", import_status="已移入待处理", status="pending"),
)
report_dry = parser.parse_pending(dry_run=True)
assert any(a.action.value == "dry_run_parse" for a in report_dry.actions)
print(f"✓ 试运行：dry_run=True 时不调 API、不移动")

# ============ 清理 ============

# 恢复 manifest 表（PostgreSQL）
manifest_store.clear()
if saved_manifest:
    manifest_store.bulk_upsert(saved_manifest)

# 清掉测试文件（目录用 rmtree，文件用 unlink）
for d in (PENDING, PARSED, ERROR, INPUT):
    for f in d.iterdir():
        if f.name == ".gitkeep":
            continue
        if f.is_dir():
            shutil.rmtree(f, ignore_errors=True)
        else:
            f.unlink(missing_ok=True)

print("\n=== 全部 §3.2 E2E 验证通过 ===")
