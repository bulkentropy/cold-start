"""MG Program — L0 enrolment funnels + L1 leading-metrics dashboard.

Single-file, stdlib-only web server (same pattern as the MBG TV dashboard):
  GET /         -> dashboard HTML (index.html)
  GET /data     -> current numbers as JSON (served from a 30-min cache)
  GET /refresh  -> force a full re-query now, then return fresh JSON

L0 replicates github.com/kushagraagarwal-11/mbg-tv-wall exactly:
frozen cohort (469 flow-1 + 100 flow-2), CleverTap App Launched / InApp_Shown
via the partner<->identity map, per-screen beacons from mbg_screen_log
intersected with the cohort, opt-ins from mg_optins, audit stages from
campaign_partners scoped to the MG campaign.

L1 is anchored on BOOKING CONFIRM (fct_booking_window, test-LCO excluded): a
booking counts iff its connection's CURRENT TAS task belongs to an enrolled
CSP (opted-in + audit done). Both attribution modes are computed: cohort
(everything on the booking day) and event (each event on its own day).
Only complete IST calendar days are kept.

Secrets from C:\credentials\.env: METABASE_API_KEY,
PROD_SUPABASE_SERVICE_ROLE_KEY (audit db). Portal read uses its public
publishable key unless SUPABASE_PORTAL_SERVICE_KEY is set.
"""
import base64
import hashlib
import hmac
import json
import os
import re
import sys
import threading
import time
import traceback
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

try:
    from dotenv import load_dotenv
    load_dotenv(r"C:\credentials\.env")
except ImportError:
    pass  # on Railway the vars come from the service config

# ----------------------------------------------------------------------------
# CONFIG
# ----------------------------------------------------------------------------
PORT = int(os.environ.get("PORT", 8090))
CACHE_TTL_S = 30 * 60
IST = timezone(timedelta(hours=5, minutes=30))

METABASE_URL = os.environ.get("METABASE_URL", "https://metabase.wiom.in")
METABASE_DB = 113

SUPABASE_AUDIT_URL = os.environ.get("SUPABASE_AUDIT_URL", "https://gonqnxpdtvjydppbrnie.supabase.co")
SUPABASE_PORTAL_URL = os.environ.get("SUPABASE_PORTAL_URL", "https://oobaxfbsmqhdaligebmg.supabase.co")

MG_CAMPAIGN_ID = "108a08d1-749a-4236-a0e9-fd4f1d3c6a27"   # 04-Jun-2026-1080-AllAudit-Kushagra
GOLIVE = "2026-07-01 09:00"                                # IST, as in the TV wall
BANNER_F1 = ["1782846718"]
BANNER_F2 = ["1782823566"]

L1_START = "2026-06-24"        # pre-period start (last 7 days of June)
PRE_END = "2026-06-30"         # inclusive
POST_START = "2026-07-01"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Frozen launch cohort + partner<->CleverTap identity map (from mbg-tv-wall)
FC = json.load(open(os.path.join(BASE_DIR, "data", "frozen_cohort.json"), encoding="utf-8"))
MAP = json.load(open(os.path.join(BASE_DIR, "data", "partner_cspid_map.json"), encoding="utf-8"))
F1, F2 = set(FC["flow1"]), set(FC["flow2"])
ID2P = {i: p for p, ids in MAP.items() for i in ids}
ALLIDS = {i for p in F1 | F2 for i in MAP.get(p, [])}

# PUBLIC publishable (anon) key for the mbg-portal project — not a secret, it
# ships inside client apps. RLS permits the reads this dashboard needs. A
# service key in the env (SUPABASE_PORTAL_SERVICE_KEY) overrides it.
PORTAL_PUBLISHABLE_KEY = ("eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Im9v"
                          "YmF4ZmJzbXFoZGFsaWdlYm1nIiwicm9sZSI6ImFub24iLCJpYXQiOjE3ODI4MTE0MTksImV4"
                          "cCI6MjA5ODM4NzQxOX0.nnurfDvGJ_mNYT_L5aEbvikg5SmZwSMFEPrF7CpRMK4")

KEY_ALIASES = {
    "SUPABASE_AUDIT_SERVICE_KEY": ["SUPABASE_AUDIT_SERVICE_KEY", "PROD_SUPABASE_SERVICE_ROLE_KEY"],
    "SUPABASE_PORTAL_SERVICE_KEY": ["SUPABASE_PORTAL_SERVICE_KEY"],
    "METABASE_API_KEY": ["METABASE_API_KEY"],
}
DEFAULTS = {"SUPABASE_PORTAL_SERVICE_KEY": PORTAL_PUBLISHABLE_KEY}


def _require(name):
    for cand in KEY_ALIASES.get(name, [name]):
        v = os.environ.get(cand)
        if v:
            return v
    if name in DEFAULTS:
        return DEFAULTS[name]
    raise RuntimeError(f"{name} missing — add it to C:\\credentials\\.env")


# ----------------------------------------------------------------------------
# Fetchers
# ----------------------------------------------------------------------------
def _http_json(req):
    with urllib.request.urlopen(req, timeout=300) as resp:
        return json.loads(resp.read())


def supabase_rows(base_url, key_env, path):
    key = _require(key_env)
    rows, page, start = [], 1000, 0
    while True:
        req = urllib.request.Request(
            f"{base_url}/rest/v1/{path}",
            headers={"apikey": key, "Authorization": f"Bearer {key}",
                     "Range": f"{start}-{start + page - 1}"})
        chunk = _http_json(req)
        rows.extend(chunk)
        if len(chunk) < page:
            return rows
        start += page


def metabase_sql(sql):
    key = _require("METABASE_API_KEY")
    body = json.dumps({"database": METABASE_DB, "type": "native",
                       "native": {"query": sql}}).encode()
    req = urllib.request.Request(
        f"{METABASE_URL}/api/dataset", data=body,
        headers={"Content-Type": "application/json", "x-api-key": key})
    out = _http_json(req)
    if out.get("error") or (out.get("status") and out["status"] != "completed"):
        raise RuntimeError(f"metabase: {str(out.get('error') or out.get('status'))[:300]}")
    data = out["data"]
    cols = [c["name"].lower() for c in data["cols"]]
    return [dict(zip(cols, r)) for r in data["rows"]]


def _inlist(xs):
    return "','".join(sorted(xs))


def _to_partners(rows):
    return {ID2P.get(r["identity"]) for r in rows if ID2P.get(r["identity"])}


# ----------------------------------------------------------------------------
# L0 — exact mbg-tv-wall logic
# ----------------------------------------------------------------------------
def clevertap_app_opened():
    rows = metabase_sql(f"""select distinct p.IDENTITY from PROD_DB.CLEVERTAP_CSP_API.EVENTS_DATA e
      join PROD_DB.CLEVERTAP_CSP_API.PROFILE_DATA p on e.CLEVERTAP_ID=p.CLEVERTAP_ID
      where e.EVENT_NAME='App Launched' and e.TIMESTAMP>='{GOLIVE}' and p.IDENTITY in ('{_inlist(ALLIDS)}')""")
    return _to_partners(rows)


def clevertap_banner(camps):
    like = " or ".join(f"PARSE_JSON(e.PROPERTIES):campaign_id::string like '{c}%'" for c in camps)
    rows = metabase_sql(f"""select distinct p.IDENTITY from PROD_DB.CLEVERTAP_CSP_API.EVENTS_DATA e
      join PROD_DB.CLEVERTAP_CSP_API.PROFILE_DATA p on e.CLEVERTAP_ID=p.CLEVERTAP_ID
      where e.EVENT_NAME='InApp_Shown' and e.TIMESTAMP>='{GOLIVE}' and ({like}) and p.IDENTITY in ('{_inlist(ALLIDS)}')""")
    return _to_partners(rows)


# Funnel screen rows (repo labels, mbg_tracker.py). Per user 2 Jul: education/
# quiz detail rows removed; flow-1 also drops Viewed (hero), flow-2 keeps it.
F1_SCREENS = []
F2_SCREENS = [("0", "Viewed (hero)")]


def compute_l0():
    optins = supabase_rows(
        SUPABASE_AUDIT_URL, "SUPABASE_AUDIT_SERVICE_KEY",
        "mg_optins?select=partner_id&program=eq.MG&first_opted_at=not.is.null")
    opt = {o["partner_id"] for o in optins}

    cps = supabase_rows(
        SUPABASE_AUDIT_URL, "SUPABASE_AUDIT_SERVICE_KEY",
        f"campaign_partners?select=partner_id,audit_status,scan_complete_at&campaign_id=eq.{MG_CAMPAIGN_ID}")
    audit_done = {c["partner_id"] for c in cps if c["scan_complete_at"]} & F2
    audit_started = {c["partner_id"] for c in cps if c["audit_status"] != "not_started"} & F2

    screens = supabase_rows(
        SUPABASE_PORTAL_URL, "SUPABASE_PORTAL_SERVICE_KEY", "mbg_screen_log?select=flow,screen,pid")
    reached = {}
    for s in screens:
        reached.setdefault(str(s["flow"]), {}).setdefault(str(s["screen"]), set()).add(s["pid"])

    def R(fl, sc, coh):
        return len(reached.get(fl, {}).get(sc, set()) & coh)

    # CleverTap stages fail independently (null -> front-end shows a dash)
    app_p, ban1_p, ban2_p = None, None, None
    errors = []
    for name, fn in (("app", clevertap_app_opened),
                     ("ban1", lambda: clevertap_banner(BANNER_F1)),
                     ("ban2", lambda: clevertap_banner(BANNER_F2))):
        try:
            v = fn()
            if name == "app":
                app_p = v
            elif name == "ban1":
                ban1_p = v
            else:
                ban2_p = v
        except Exception as e:
            errors.append(f"L0 CleverTap {name}: {type(e).__name__}")

    f1_enr = len(opt & F1)
    f2_opt = len(opt & F2)
    f2_enr = len(opt & F2 & audit_done)

    def funnel(coh, scr_list, fl, app, ban, tail):
        rows = [{"stage": "Cohort", "csps": len(coh)},
                {"stage": "App Opened", "csps": len(app & coh) if app is not None else None},
                {"stage": "Banner Viewed", "csps": len(ban & coh) if ban is not None else None}]
        rows += [{"stage": lbl, "csps": R(fl, sc, coh)} for sc, lbl in scr_list]
        rows += tail
        return rows

    return {
        "migration": {
            "start": len(F2),
            "graduated": len(audit_done),
            "drained_pct": round(100 * len(audit_done) / len(F2), 1) if F2 else 0,
            "remaining": len(F2) - len(audit_done),
        },
        "funnel1": funnel(F1, F1_SCREENS, "1", app_p, ban1_p,
                          [{"stage": "Enrolled (opted-in)", "csps": f1_enr}]),
        "funnel2": funnel(F2, F2_SCREENS, "2", app_p, ban2_p,
                          [{"stage": "Opted-in", "csps": f2_opt},
                           {"stage": "Audit started", "csps": len(audit_started)},
                           {"stage": "Audit completed", "csps": len(audit_done)},
                           {"stage": "Enrolled (opt-in + audit)", "csps": f2_enr}]),
        "totals": {"joined": f1_enr + f2_opt, "enrolled_strict": f1_enr + f2_enr},
        # L1 base: strictly-enrolled CSPs (flow-1 audit was done at launch)
        "enrolled_partner_ids": sorted((opt & F1) | (opt & F2 & audit_done)),
        "l0_errors": errors,
    }


# ----------------------------------------------------------------------------
# L1 — booking-confirm anchored, dual attribution modes
# ----------------------------------------------------------------------------
L1_KEYS = ("bookings", "slot_selected", "slot_pct", "cust_confirmed",
           "confirm_pct", "med_hrs_to_accept", "p90_hrs_to_accept",
           "installs", "install_ratio",
           # non-enrolled shadow series (compare overlay)
           "sh_bookings", "sh_slot_selected", "sh_cust_confirmed",
           "sh_slot_pct", "sh_confirm_pct", "sh_med_hrs_to_accept",
           "total_bookings")


def _daterange(start, end):
    d = datetime.fromisoformat(start).date()
    e = datetime.fromisoformat(end).date()
    out = []
    while d <= e:
        out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def compute_l1(enrolled_ids):
    if not enrolled_ids:
        raise RuntimeError("no enrolled partners — L1 skipped")
    sql = open(os.path.join(BASE_DIR, "sql", "l1_daily_agg.sql"), encoding="utf-8").read()
    sql = sql.replace("{PARTNER_IN_LIST}", ",".join(f"'{p}'" for p in enrolled_ids))
    sql = sql.replace("{START_DATE}", L1_START)
    raw = metabase_sql(sql)

    yday = (datetime.now(IST).date() - timedelta(days=1)).isoformat()
    days = _daterange(L1_START, yday)

    def bucket(mode):
        return {(str(r["day_ist"])[:10], r["enr"]): r
                for r in raw if r["mode"] == mode and r["day_ist"]}

    coh, eacc, econf = bucket("cohort"), bucket("event_accept"), bucket("event_confirm")
    csps = {r["enr"]: r["n"] for r in raw if r["mode"] == "csps"}
    total = {str(r["day_ist"])[:10]: r["bookings"] for r in raw
             if r["mode"] == "total" and r["day_ist"]}

    def pct(a, b):
        return round(100 * a / b, 1) if b else None

    def agg_cohort():
        out = []
        for d in days:
            e = coh.get((d, 1), {})
            s = coh.get((d, 0), {})
            bks, acc, cnf, ins = (e.get("bookings", 0), e.get("accepted", 0),
                                  e.get("confirmed", 0), e.get("installed", 0))
            out.append({"day_ist": d, "bookings": bks, "slot_selected": acc,
                        "slot_pct": pct(acc, bks), "cust_confirmed": cnf,
                        "confirm_pct": pct(cnf, acc), "installs": ins,
                        "install_ratio": pct(ins, cnf),
                        "med_hrs_to_accept": e.get("med_hrs"), "p90_hrs_to_accept": e.get("p90_hrs"),
                        "sh_bookings": s.get("bookings"),
                        "sh_slot_pct": pct(s.get("accepted", 0), s.get("bookings", 0)),
                        "sh_confirm_pct": pct(s.get("confirmed", 0), s.get("accepted", 0)),
                        "sh_med_hrs_to_accept": s.get("med_hrs"),
                        "total_bookings": total.get(d)})
        return out

    def agg_event():
        out = []
        for d in days:
            e, s = coh.get((d, 1), {}), coh.get((d, 0), {})
            ea, sa = eacc.get((d, 1), {}), eacc.get((d, 0), {})
            ec, scf = econf.get((d, 1), {}), econf.get((d, 0), {})
            bks, acc, cnf = e.get("bookings", 0), ea.get("n", 0), ec.get("n", 0)
            out.append({"day_ist": d, "bookings": bks, "slot_selected": acc,
                        "slot_pct": pct(acc, bks), "cust_confirmed": cnf,
                        "confirm_pct": pct(cnf, acc),
                        "med_hrs_to_accept": ea.get("med_hrs"), "p90_hrs_to_accept": ea.get("p90_hrs"),
                        "sh_bookings": s.get("bookings"),
                        "sh_slot_selected": sa.get("n"),
                        "sh_cust_confirmed": scf.get("n"),
                        "sh_med_hrs_to_accept": sa.get("med_hrs"),
                        "total_bookings": total.get(d)})
        return out

    def block(rows):
        def avg(k):
            vals = [r[k] for r in rows if r.get(k) is not None]
            return round(sum(vals) / len(vals), 1) if vals else None
        return {k: avg(k) for k in L1_KEYS}

    modes = {}
    for mode, rows in (("cohort", agg_cohort()), ("event", agg_event())):
        pre = [r for r in rows if L1_START <= r["day_ist"] <= PRE_END]
        post = [r for r in rows if r["day_ist"] >= POST_START]
        modes[mode] = {"daily": rows, "pre_avg": block(pre), "post_avg": block(post)}

    return {"modes": modes, "enrolled_n": len(enrolled_ids),
            "csps_receiving": {"enrolled": csps.get(1), "non_enrolled": csps.get(0)},
            "complete_through": yday}


def compute_nsm(enrolled_ids):
    """Installs/day by enrolled CSPs: today (partial) + last 7 complete days."""
    if not enrolled_ids:
        raise RuntimeError("no enrolled partners — NSM skipped")
    sql = open(os.path.join(BASE_DIR, "sql", "nsm_installs.sql"), encoding="utf-8").read()
    sql = sql.replace("{PARTNER_IN_LIST}", ",".join(f"'{p}'" for p in enrolled_ids))
    by_day = {str(r["day_ist"])[:10]: r for r in metabase_sql(sql)}
    today = datetime.now(IST).date()

    def row(d):
        r = by_day.get(d, {})
        return {"day_ist": d, "installs": r.get("installs", 0),
                "total_installs": r.get("total_installs", 0)}

    trend = [row((today - timedelta(days=i)).isoformat()) for i in range(7, 0, -1)]
    t = row(today.isoformat())
    return {"today": t["installs"], "today_total": t["total_installs"],
            "today_date": today.isoformat(), "trend": trend}


# ----------------------------------------------------------------------------
# Auth — whole app is gated when COLDSTART_PASSWORD is set (Railway); the gate
# is off locally when the env var is absent.
# ----------------------------------------------------------------------------
GATE_PASSWORD = os.environ.get("COLDSTART_PASSWORD")
SESSION_DAYS = 30
EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@wiom\.in$", re.IGNORECASE)


def _sign(msg):
    return hmac.new(GATE_PASSWORD.encode(), msg.encode(), hashlib.sha256).hexdigest()[:32]


def make_token(email):
    exp = str(int(time.time()) + SESSION_DAYS * 86400)
    e64 = base64.urlsafe_b64encode(email.encode()).decode().rstrip("=")
    return f"{exp}.{e64}.{_sign(exp + '.' + e64)}"


def token_email(tok):
    """Return the signed-in email, or None if the token is invalid/expired."""
    try:
        exp, e64, sig = tok.split(".")
        if int(exp) < time.time() or not hmac.compare_digest(sig, _sign(exp + "." + e64)):
            return None
        return base64.urlsafe_b64decode(e64 + "=" * (-len(e64) % 4)).decode()
    except Exception:
        return None


LOGIN_HTML = """<!DOCTYPE html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Cold-start Project</title>
<style>body{font-family:-apple-system,'Segoe UI',Roboto,sans-serif;background:#F1EDF7;display:flex;
align-items:center;justify-content:center;height:100vh;margin:0}
.card{background:#FAF9FC;border-radius:16px;box-shadow:0 4px 24px rgba(22,16,33,.07);padding:36px;width:340px}
.label{font-size:11px;font-weight:700;letter-spacing:1.5px;text-transform:uppercase;color:#665E75}
h1{font-size:1.4rem;font-weight:800;color:#161021;margin:4px 0 18px}
input{width:100%;box-sizing:border-box;border:1px solid #D7D3E0;border-radius:8px;padding:12px;font-size:15px;margin-bottom:14px}
button{width:100%;background:#D9008D;color:#fff;border:none;border-radius:40px;padding:12px;font-size:15px;font-weight:600;cursor:pointer}
.err{color:#E01E00;font-size:13px;margin-bottom:10px}</style></head><body>
<form class="card" method="POST" action="/login">
<div class="label">WIOM · Cold-start Project</div><h1>Team access</h1>
{ERR}<input type="email" name="email" placeholder="you@wiom.in" required
  pattern="[A-Za-z0-9._%+-]+@wiom\\.in" title="Use your @wiom.in email" autofocus>
<input type="password" name="password" placeholder="Access password" required>
<button>Enter</button></form></body></html>"""


# ----------------------------------------------------------------------------
# Content CRUD (Supabase, service key, server-side only)
# ----------------------------------------------------------------------------
CS_KINDS = {
    "tasks": ("cs_tasks", "created_at.desc",
              ("title", "owner", "status", "due_date", "blocked_by", "notes")),
    "actions": ("cs_actions", "created_at.desc",
                ("title", "kind", "initiative", "owner", "due_date", "status",
                 "blocker", "notes", "options", "resolution")),
    "decisions": ("cs_decisions", "decided_at.desc",
                  ("title", "context", "decision", "reasoning", "owner", "decided_at", "status", "tags")),
    "documents": ("cs_documents", "shared_at.desc.nullslast",
                  ("title", "url", "doc_type", "summary", "source", "channel", "shared_by", "shared_at", "body_md")),
    "changelog": ("cs_changelog", "changed_at.desc",
                  ("title", "description", "category", "impact", "owner", "changed_at")),
    "content": ("cs_content", "order_index.asc",
                ("slug", "title", "section", "body_md", "order_index")),
}


def supabase_write(method, path, body=None):
    key = _require("SUPABASE_AUDIT_SERVICE_KEY")
    req = urllib.request.Request(
        f"{SUPABASE_AUDIT_URL}/rest/v1/{path}",
        data=json.dumps(body).encode() if body is not None else None, method=method,
        headers={"apikey": key, "Authorization": f"Bearer {key}",
                 "Content-Type": "application/json", "Prefer": "return=representation"})
    return _http_json(req)


def cs_list(kind):
    table, order, _ = CS_KINDS[kind]
    return supabase_rows(SUPABASE_AUDIT_URL, "SUPABASE_AUDIT_SERVICE_KEY",
                         f"{table}?select=*&order={order}")


def _audit(kind, entity_id, title, change, editor):
    """Silent append-only activity trail; a log failure never blocks the edit."""
    try:
        supabase_write("POST", "cs_activity_log",
                       {"entity_kind": kind, "entity_id": entity_id,
                        "entity_title": (title or "")[:200], "change": change[:2000],
                        "changed_by": editor or None})
    except Exception:
        traceback.print_exc()


def cs_activity():
    return supabase_rows(SUPABASE_AUDIT_URL, "SUPABASE_AUDIT_SERVICE_KEY",
                         "cs_activity_log?select=*&order=changed_at.desc&limit=50")


def cs_create(kind, data):
    table, _, fields = CS_KINDS[kind]
    editor = data.pop("_editor", None)
    clean = {k: v for k, v in data.items() if k in fields and v not in ("", None)}
    out = supabase_write("POST", table, clean)
    row = out[0] if isinstance(out, list) and out else {}
    _audit(kind, row.get("id"), clean.get("title") or clean.get("slug"),
           "created: " + "; ".join(f"{k}={v}" for k, v in clean.items() if k != "body_md"),
           editor)
    return out


def cs_update(kind, row_id, data):
    table, _, fields = CS_KINDS[kind]
    editor = data.pop("_editor", None)
    clean = {k: v for k, v in data.items() if k in fields}
    old_rows = supabase_rows(SUPABASE_AUDIT_URL, "SUPABASE_AUDIT_SERVICE_KEY",
                             f"{table}?select=*&id=eq.{urllib.parse.quote(row_id)}")
    old = old_rows[0] if old_rows else {}
    diffs = [f"{k}: {old.get(k) if old.get(k) not in (None, '') else '—'} → {v if v not in (None, '') else '—'}"
             for k, v in clean.items() if (old.get(k) or "") != (v or "")]
    if kind != "changelog":
        clean["updated_at"] = datetime.now(timezone.utc).isoformat()
    out = supabase_write("PATCH", f"{table}?id=eq.{urllib.parse.quote(row_id)}", clean)
    if diffs:
        _audit(kind, row_id, old.get("title") or old.get("slug"), "; ".join(diffs), editor)
    return out


def cs_delete(kind, row_id, editor=None):
    table, _, _ = CS_KINDS[kind]
    out = supabase_write("DELETE", f"{table}?id=eq.{urllib.parse.quote(row_id)}")
    row = out[0] if isinstance(out, list) and out else {}
    _audit(kind, row_id, row.get("title") or row.get("slug"), "deleted", editor)
    return out


# ----------------------------------------------------------------------------
# Cache with last-good merge
# ----------------------------------------------------------------------------
_cache = {"payload": None, "at": 0.0}
_lock = threading.Lock()


def refresh(force=False):
    with _lock:
        if (not force and _cache["payload"]
                and time.time() - _cache["at"] < CACHE_TTL_S):
            return _cache["payload"]

        prev = _cache["payload"] or {}
        payload = {"meta": {"asof": datetime.now(IST).isoformat(timespec="seconds"),
                            "golive": GOLIVE, "pre": [L1_START, PRE_END],
                            "post_start": POST_START, "cache_ttl_min": CACHE_TTL_S // 60,
                            "errors": []}}
        try:
            payload["l0"] = compute_l0()
            payload["meta"]["errors"].extend(payload["l0"].pop("l0_errors", []))
        except Exception as e:
            traceback.print_exc()
            payload["meta"]["errors"].append(f"L0: {type(e).__name__}: {e}")
            payload["l0"] = prev.get("l0")

        enrolled = (payload["l0"] or {}).get("enrolled_partner_ids") or []
        try:
            payload["nsm"] = compute_nsm(enrolled)
        except Exception as e:
            traceback.print_exc()
            payload["meta"]["errors"].append(f"NSM: {type(e).__name__}: {e}")
            payload["nsm"] = prev.get("nsm")

        try:
            payload["l1"] = compute_l1(enrolled)
        except Exception as e:
            traceback.print_exc()
            payload["meta"]["errors"].append(f"L1: {type(e).__name__}: {e}")
            payload["l1"] = prev.get("l1")

        if payload.get("l0") is not None:
            payload["l0"].pop("enrolled_partner_ids", None)
        _cache["payload"] = payload
        _cache["at"] = time.time()
        return payload


# ----------------------------------------------------------------------------
# HTTP
# ----------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype, extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _email(self):
        """Signed-in email; None when unauthenticated; '' when gate is off (local dev)."""
        if not GATE_PASSWORD:
            return ""
        cookie = SimpleCookie(self.headers.get("Cookie", ""))
        tok = cookie.get("cs_auth")
        return token_email(tok.value) if tok else None

    def _authed(self):
        return self._email() is not None

    def _json_body(self):
        n = int(self.headers.get("Content-Length", 0) or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def do_GET(self):
        path = self.path.split("?")[0]
        try:
            if path == "/login":
                return self._send(200, LOGIN_HTML.replace("{ERR}", "").encode(), "text/html; charset=utf-8")
            if not self._authed():
                if path.startswith("/api") or path in ("/data", "/refresh"):
                    return self._send(401, b'{"error":"auth required"}', "application/json")
                return self._send(200, LOGIN_HTML.replace("{ERR}", "").encode(), "text/html; charset=utf-8")
            if path == "/":
                html = open(os.path.join(BASE_DIR, "index.html"), "rb").read()
                self._send(200, html, "text/html; charset=utf-8")
            elif path == "/data":
                self._send(200, json.dumps(refresh()).encode(), "application/json")
            elif path == "/refresh":
                self._send(200, json.dumps(refresh(force=True)).encode(), "application/json")
            elif path == "/api/me":
                self._send(200, json.dumps({"email": self._email() or None}).encode(), "application/json")
            elif path == "/api/activity":
                self._send(200, json.dumps(cs_activity()).encode(), "application/json")
            elif path.startswith("/api/"):
                kind = path.split("/")[2]
                if kind not in CS_KINDS:
                    return self._send(404, b'{"error":"unknown kind"}', "application/json")
                self._send(200, json.dumps(cs_list(kind)).encode(), "application/json")
            else:
                self._send(404, b"not found", "text/plain")
        except Exception as e:
            traceback.print_exc()
            self._send(500, json.dumps({"error": str(e)}).encode(), "application/json")

    def do_POST(self):
        path = self.path.split("?")[0]
        try:
            if path == "/login":
                n = int(self.headers.get("Content-Length", 0) or 0)
                form = urllib.parse.parse_qs(self.rfile.read(n).decode())
                email = (form.get("email") or [""])[0].strip().lower()
                pw = (form.get("password") or [""])[0]
                if not EMAIL_RE.match(email):
                    err = '<div class="err">Use your @wiom.in email.</div>'
                elif GATE_PASSWORD and hmac.compare_digest(pw, GATE_PASSWORD):
                    return self._send(302, b"", "text/plain",
                                      {"Location": "/",
                                       "Set-Cookie": f"cs_auth={make_token(email)}; Path=/; Max-Age={SESSION_DAYS*86400}; HttpOnly; SameSite=Lax"})
                else:
                    err = '<div class="err">Wrong password.</div>'
                return self._send(200, LOGIN_HTML.replace("{ERR}", err).encode(), "text/html; charset=utf-8")
            if not self._authed():
                return self._send(401, b'{"error":"auth required"}', "application/json")
            parts = path.split("/")
            if len(parts) >= 3 and parts[1] == "api" and parts[2] in CS_KINDS:
                kind = parts[2]
                body = self._json_body()
                if self._email():          # signed-in email is the authoritative editor
                    body["_editor"] = self._email()
                if len(parts) == 3:
                    out = cs_create(kind, body)
                else:
                    out = cs_update(kind, parts[3], body)
                return self._send(200, json.dumps(out).encode(), "application/json")
            self._send(404, b"not found", "text/plain")
        except Exception as e:
            traceback.print_exc()
            self._send(500, json.dumps({"error": str(e)}).encode(), "application/json")

    def do_DELETE(self):
        path = self.path.split("?")[0]
        try:
            if not self._authed():
                return self._send(401, b'{"error":"auth required"}', "application/json")
            parts = path.split("/")
            if len(parts) == 4 and parts[1] == "api" and parts[2] in CS_KINDS:
                out = cs_delete(parts[2], parts[3], self._email() or None)
                return self._send(200, json.dumps(out).encode(), "application/json")
            self._send(404, b"not found", "text/plain")
        except Exception as e:
            traceback.print_exc()
            self._send(500, json.dumps({"error": str(e)}).encode(), "application/json")

    def log_message(self, fmt, *args):
        print(f"[{datetime.now(IST):%H:%M:%S}] {fmt % args}")


if __name__ == "__main__":
    print(f"MG dashboard on http://localhost:{PORT}  (refresh every {CACHE_TTL_S // 60} min; GET /refresh to force)")
    threading.Thread(target=refresh, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
