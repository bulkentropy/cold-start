"""Read-only Metabase query runner. Usage: python _mb.py <sql_file>
Reads METABASE_API_KEY from C:\\credentials\\.env (never printed)."""
import os, sys, json, urllib.request
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from dotenv import load_dotenv

load_dotenv(r"C:\credentials\.env")
KEY = os.environ.get("METABASE_API_KEY")
if not KEY:
    sys.exit("ERROR: METABASE_API_KEY missing in C:\\credentials\\.env")

sql = open(sys.argv[1], encoding="utf-8").read()
body = json.dumps({"database": 113, "type": "native",
                   "native": {"query": sql}}).encode("utf-8")
req = urllib.request.Request(
    "https://metabase.wiom.in/api/dataset", data=body,
    headers={"Content-Type": "application/json", "x-api-key": KEY})
try:
    resp = urllib.request.urlopen(req, timeout=300)
    out = json.loads(resp.read())
except urllib.error.HTTPError as e:
    print("HTTP", e.code)
    print(e.read().decode("utf-8", "replace")[:2000])
    sys.exit(1)

data = out.get("data", {})
status = out.get("status")
if out.get("error") or (status and status != "completed"):
    print("QUERY ERROR / status:", status)
    print(json.dumps(out.get("error") or out, default=str)[:2000])
    sys.exit(1)

cols = [c["name"] for c in data.get("cols", [])]
rows = data.get("rows", [])
print(" | ".join(cols))
print("-" * 80)
for r in rows:
    print(" | ".join("" if v is None else str(v) for v in r))
print(f"\n[{len(rows)} rows]")
