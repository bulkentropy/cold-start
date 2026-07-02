"""Render sql/l1_daily.sql with the snapshot partner list for validation runs."""
import json, sys

ids = json.load(open(r"data\enrolled_snapshot.json", encoding="utf-8"))["partner_ids"]
sql = open(r"sql\l1_daily.sql", encoding="utf-8").read()
sql = sql.replace("{PARTNER_IN_LIST}", ",".join(f"'{i}'" for i in ids))
sql = sql.replace("{START_DATE}", sys.argv[1] if len(sys.argv) > 1 else "2026-06-24")
open(r"_q_l1_rendered.sql", "w", encoding="utf-8").write(sql)
print("rendered", len(ids), "partners")
