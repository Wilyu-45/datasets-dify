"""Check manifest (PostgreSQL) files against input/."""
from pathlib import Path

from app.services import manifest_store

INPUT = Path(r"d:\programmtools\tools\ragsystem\data\input")
manifest = manifest_store.load()

print("Checking manifest files against input/:")
exist = 0
missing = 0
for filename in manifest:
    fname = str(filename or "").strip()
    if not fname:
        continue
    found = (INPUT / fname).exists()
    if found:
        exist += 1
    else:
        missing += 1
        print(f"  MISSING: {fname}")

print(f"\nExist: {exist}, Missing: {missing}")
input_files = [f for f in INPUT.iterdir() if f.name != ".gitkeep"]
print(f"Total in input/: {len(input_files)}")
