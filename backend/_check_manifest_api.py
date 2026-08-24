"""Check manifest API response."""
import httpx
r = httpx.get("http://localhost:8000/api/manifest?limit=3", timeout=10)
data = r.json()
rows = data.get("rows", [])
print(f"Total: {data.get('total')}")
for row in rows[:2]:
    print(f"\nRow: {row}")
