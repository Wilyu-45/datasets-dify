"""检查 hh_monograph 文档的切分和图片情况"""
import sys, json
from pathlib import Path

sys.stdout.reconfigure(encoding='utf-8')

# 检查 chunks 和 output 目录
for root_name in ['chunks', 'output']:
    root = Path(r"d:\programmtools\tools\ragsystem\data") / root_name
    target = root / "hh_monograph"
    if target.exists():
        print(f"\n=== {root_name}/hh_monograph ===")
        meta_file = target / "chunk_metadata.json"
        if meta_file.exists():
            meta = json.loads(meta_file.read_text(encoding='utf-8'))
            print(f"chunk_count: {meta.get('chunk_count')}")
            chunks = meta.get('chunks', [])
            for c in chunks:
                fn = c.get('file_name', '')
                fp = target / fn
                content = fp.read_text(encoding='utf-8') if fp.exists() else ''
                refs = c.get('image_refs', [])
                print(f"\n  {c.get('chunk_id')}: {fn}")
                print(f"    chars: {len(content)}, image_refs: {refs}")
                print(f"    title_path: {c.get('title_path', '')[:80]}")
                print(f"    first 200 chars: {content[:200]}")
        
        # 检查 images 目录
        images_dir = target / "images"
        if images_dir.exists():
            imgs = list(images_dir.iterdir())
            print(f"\n  images/ 目录有 {len(imgs)} 个文件:")
            for img in imgs[:10]:
                print(f"    {img.name} ({img.stat().st_size} bytes)")
        else:
            print(f"\n  images/ 目录不存在!")

# 也检查 parsed 目录
parsed_dir = Path(r"d:\programmtools\tools\ragsystem\data\parsed\hh_monograph")
if parsed_dir.exists():
    print(f"\n=== parsed/hh_monograph ===")
    for f in sorted(parsed_dir.rglob("*")):
        if f.is_file():
            print(f"  {f.relative_to(parsed_dir)} ({f.stat().st_size} bytes)")
