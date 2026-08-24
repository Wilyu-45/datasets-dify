"""检查 chunks 目录中的文档状态，找出问题 2/3/4"""
import sys, json
from pathlib import Path

sys.stdout.reconfigure(encoding='utf-8')

CHUNKS_DIR = Path(r"d:\programmtools\tools\ragsystem\data\chunks")
OUTPUT_DIR = Path(r"d:\programmtools\tools\ragsystem\data\output")

for root_dir in [CHUNKS_DIR, OUTPUT_DIR]:
    if not root_dir.exists():
        continue
    print(f"\n=== {root_dir.name}/ ===")
    for d in sorted(root_dir.iterdir()):
        if not d.is_dir():
            continue
        meta_file = d / "chunk_metadata.json"
        if not meta_file.exists():
            print(f"  {d.name}: NO METADATA")
            continue
        meta = json.loads(meta_file.read_text(encoding='utf-8'))
        chunk_count = meta.get('chunk_count', 0)
        chunks = meta.get('chunks', [])
        
        # 检查图片
        images_dir = d / "images"
        image_files = list(images_dir.rglob("*")) if images_dir.exists() else []
        
        # 收集所有图片引用
        all_image_refs = set()
        for c in chunks:
            refs = c.get('image_refs', [])
            for r in refs:
                all_image_refs.add(r)
        
        # 检查引用但缺失的图片
        missing_refs = []
        for ref in all_image_refs:
            ref_path = d / ref.replace('/', '\\')
            if not ref_path.exists():
                missing_refs.append(ref)
        
        # 检查每个 chunk 的内容长度
        chunk_details = []
        for c in chunks:
            fn = c.get('file_name', '')
            fp = d / fn
            content = fp.read_text(encoding='utf-8') if fp.exists() else ''
            chunk_details.append({
                'id': c.get('chunk_id', ''),
                'file': fn,
                'chars': len(content),
                'title_path': c.get('title_path', '')[:60],
                'image_refs': len(c.get('image_refs', [])),
                'first_100': content[:100].replace('\n', ' '),
            })
        
        print(f"  {d.name}: {chunk_count} chunks, {len(image_files)} images, {len(all_image_refs)} refs, {len(missing_refs)} missing")
        if missing_refs:
            print(f"    MISSING REFS: {missing_refs}")
        for cd in chunk_details[:5]:
            print(f"    {cd['id']}: {cd['chars']} chars, {cd['image_refs']} imgs, title={cd['title_path']}")
            print(f"      first: {cd['first_100'][:80]}...")
        if len(chunk_details) > 5:
            print(f"    ... and {len(chunk_details) - 5} more chunks")
