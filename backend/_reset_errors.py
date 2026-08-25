"""重置错误文件的 parse 列，让它们可以被重新解析（PostgreSQL manifest 表）。"""
import sys

sys.stdout.reconfigure(encoding="utf-8")

from app.services import manifest_store

manifest = manifest_store.load()

# 重置 error 状态的文件
reset_count = 0
for filename, row in manifest.items():
    if (row.status or "").strip() == "error":
        row.parse = ""
        row.status = "pending"
        # 也清空 chunks 和 dify 列
        row.chunks = ""
        row.dify_doc_id = ""
        row.dify_status = ""
        row.update_time = manifest_store.now_iso()
        manifest_store.upsert(row)
        reset_count += 1
        print(f"  重置: {filename}")

print(f"\n共重置 {reset_count} 个文件")
