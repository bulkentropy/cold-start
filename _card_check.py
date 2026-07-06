"""Fetch Metabase card 11528 definition + execute it with the user's params."""
import json
import os
import sys
import urllib.request

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from dotenv import load_dotenv
load_dotenv(r"C:\credentials\.env")
KEY = os.environ["METABASE_API_KEY"]
H = {"Content-Type": "application/json", "x-api-key": KEY}

card = json.loads(urllib.request.urlopen(
    urllib.request.Request("https://metabase.wiom.in/api/card/11528", headers=H), timeout=120).read())
sql = card["dataset_query"]["native"]["query"]
print("=== card name:", card["name"], "| updated_at:", card.get("updated_at"))
print("=== template tags:", list(card["dataset_query"]["native"].get("template-tags", {})))
open(r"_card_11528_live.sql", "w", encoding="utf-8").write(sql)
print("=== live SQL saved to _card_11528_live.sql,", len(sql), "chars")

params = [
    {"type": "number/=", "target": ["variable", ["template-tag", "days"]], "value": "14"},
    {"type": "date/single", "target": ["variable", ["template-tag", "start_date"]], "value": "2026-07-05"},
    {"type": "date/single", "target": ["variable", ["template-tag", "end_date"]], "value": "2026-07-05"},
]
body = json.dumps({"parameters": params}).encode()
out = json.loads(urllib.request.urlopen(
    urllib.request.Request("https://metabase.wiom.in/api/card/11528/query", data=body, headers=H),
    timeout=300).read())
if out.get("error"):
    print("QUERY ERROR:", str(out["error"])[:500])
    sys.exit(1)
data = out["data"]
cols = [c["name"] for c in data["cols"]]
print("=== rows (", cols, "):")
for r in data["rows"]:
    print("   ", r)
