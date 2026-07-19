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

# In-app HOME-BANNER opens (custom native event `banner_opened`) — the L0 card is a
# reproduction of CleverTap board 1783687017 ("Wiom CSP → MBG Board"), per
# mbg_click_funnel_LOGIC.md. See sql/l0_banner.sql for the full reasoning.
#
# ALL USERS, NO COHORT FILTER: the board's cards carry no CSP segment, and the card
# matches that so portal and board agree. It is therefore not a reach % — there is no
# denominator. (The push-click funnel that used to sit beside this was removed 16 Jul:
# the logic doc covers the banner only. Push reach is no longer computed anywhere.)
#
# SUPERSET: `banner_opened` covers every home banner, not just MBP — its `banner`
# property is a per-CSP uuid, so nothing in CleverTap marks an open as MBP.
BANNER_START = "2026-07-10"          # the board's window: "After Jul 09, 2026"
MBG_BOARD_URL = "https://eu1.dashboard.clevertap.com/44Z-644-777Z/dashboards/custom/1783687017"

# Sehat MG enrollment funnel (sign-up feed) — the second initiative's opt-in tracker,
# a warehouse-computed recreation of vikaswiom.github.io/wiom-csp-guarantee-campaign.
# See sql/sehat_funnel.sql. Awaiting-data until the CleverTap events sync (launch 16 Jul).
SEHAT_START = "2026-07-16"           # campaign go-live
# The six funnel events, in order (key, label, CleverTap event name).
SEHAT_STEPS = [
    ("reached",        "Reached",           "Sehat_View_education"),
    ("learn_more",     "Learn more",        "Sehat_Learn_More"),
    ("view_plan",      "Viewed plan",       "Sehat_View_plan"),
    ("start_quiz",     "Started quiz",      "Sehat_Start_Quiz"),
    ("quiz_complete",  "Completed quiz",    "Sehat_Quiz_Complete"),
    ("enrolled",       "Enrolled (opt-in)", "Sehat_OptIn"),
]
EVENT_TO_STEP = {ev: key for key, _, ev in SEHAT_STEPS}
# Two cohorts. `eligible` is the addressable count from the provision (the 99-CSP
# launch cohort: 61 Track A + 37 Track B; the 1 unclassified CSP has no track).
# creative = the source repo's live in-app flow, embedded as a phone-frame iframe.
SEHAT_COHORTS = [
    {"key": "sehat_optical", "label": "Optical Power", "suffix": "low",
     "desc": "Track A · Ilaaj — CSPs whose Optical Power is low", "color": "#D9008D",
     "eligible": 61,
     "creative": "https://vikaswiom.github.io/wiom-csp-guarantee-campaign/index.html"},
    {"key": "sehat_sla", "label": "Service SLA", "suffix": "poor",
     "desc": "Track B · Fit rakhna — CSPs whose Service SLA is poor", "color": "#2563EB",
     "eligible": 37,
     "creative": "https://vikaswiom.github.io/wiom-csp-guarantee-campaign/sla.html"},
]

# Sehat MG observation layer — day-on-day quality trend per cohort (card 11616 logic,
# rolled per day). Cohort membership + opt-in status come from the "Sehat MG" tab of
# the offer sheet, read LIVE via gviz so the opted-vs-not split lights up the moment
# the "Opted in" column is populated; falls back to data/sehat_cohort.json.
SEHAT_SHEET_ID = "1XHqjybQYKyfCpgdraPiL32GBm2R2wf-CguxlHalmu8M"
SEHAT_SHEET_GID = "1116493306"
SEHAT_SHEET_URL = (f"https://docs.google.com/spreadsheets/d/{SEHAT_SHEET_ID}"
                   f"/edit?gid={SEHAT_SHEET_GID}#gid={SEHAT_SHEET_GID}")
SEHAT_OBS_START = "2026-07-01"        # a pre-launch baseline, then across the cycle
SEHAT_GATE = 80                        # the payout gate on both tracks (≥80%)
# metric spec per cohort: which quality table + columns + rolling window feed the trend
SEHAT_QUALITY = {
    "sehat_optical": {"table": "TELEMETRY_ROLLUP_RECORDS", "good": "OPTICAL_NUMERATOR",
                      "total": "OPTICAL_DENOMINATOR", "datecol": "SIGNAL_DATE", "window": 15,
                      "metric": "Optical Power", "unit": "% in-range pings · 15-day rolling"},
    "sehat_sla":     {"table": "COMPLAINT_RESOLUTION_LEDGER", "good": "IFF(RESOLVED_WITHIN_TAT,1,0)",
                      "total": "1", "datecol": "OPENED_AT", "window": 60,
                      "metric": "Service SLA", "unit": "% resolved in 4h TAT · 60-day rolling"},
}
# Board snapshot from the logic doc, for the card's parity note. STATIC — it is a
# 15-Jul reading, labelled as such on the card; it does not track the live board.
BOARD_SNAPSHOT = {"date": "2026-07-15", "opens": 2394, "users": 398}
PRE_END = "2026-06-30"         # inclusive
POST_START = "2026-07-01"
# Belief-cohort before/after cut: matured pre-window vs the live post-window.
COHORT_BEFORE = ("2026-06-01", "2026-06-15")
# CSP-status (moved/ignition/demand) before vs after: two equal 7-day windows.
IGN_BEFORE = ("2026-06-24", "2026-06-30")   # last 7 days of June
IGN_AFTER = ("2026-07-01", "2026-07-07")    # first week of July
# week-on-week ignition windows: (start, end, label, tasks-key, installs-key in l1_status)
IGN_WEEKS = [("2026-06-24", "2026-06-30", "24–30 Jun", "tb", "ib"),
             ("2026-07-01", "2026-07-07", "1–7 Jul", "ta", "ia"),
             ("2026-07-08", "2026-07-14", "8–14 Jul", "tc", "ic")]

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Frozen launch cohort + partner<->CleverTap identity map (from mbg-tv-wall)
FC = json.load(open(os.path.join(BASE_DIR, "data", "frozen_cohort.json"), encoding="utf-8"))
MAP = json.load(open(os.path.join(BASE_DIR, "data", "partner_cspid_map.json"), encoding="utf-8"))
F1, F2 = set(FC["flow1"]), set(FC["flow2"])
ID2P = {i: p for p, ids in MAP.items() for i in ids}
ALLIDS = {i for p in F1 | F2 for i in MAP.get(p, [])}

# Engagement campaign (CSP stage movement + banner engagement, 3-10 Jul) — a
# fixed one-off dataset exported from mbg_stage_movement.xlsx; served as-is.
try:
    ENGAGEMENT = json.load(open(os.path.join(BASE_DIR, "data", "mbg_engagement.json"), encoding="utf-8"))
except Exception:
    ENGAGEMENT = None

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
        "engagement": ENGAGEMENT,
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


def _leadtime_stats(enrolled_ids):
    # Install lead time = INSTALLATION_COMPLETED_AT - CONFIRMED_SLOT_AT per task,
    # for MG-cohort installs COMPLETED in the last 15 days (dedup to the
    # completing row per connection; drop negatives). Percentiles in hours.
    inlist = ",".join(f"'{p}'" for p in enrolled_ids)
    sql = f"""
    WITH acct AS (
      SELECT CSP_ID, PARTNER_ID::STRING AS pid
      FROM PROD_DB.CSP_GATEWAY_SERVICE_CSP_GATEWAY_SERVICE.CSP_ACCOUNT
      WHERE _fivetran_active = TRUE
      QUALIFY ROW_NUMBER() OVER (PARTITION BY CSP_ID ORDER BY 1) = 1
    ),
    t AS (
      SELECT c.CONNECTION_ID,
        DATEDIFF('minute', c.CONFIRMED_SLOT_AT, c.INSTALLATION_COMPLETED_AT) AS dmin
      FROM PROD_DB.DBT_CSP.TAS_INSTALL_EXECUTION_CANDIDATES c
      JOIN acct a ON a.CSP_ID = c.CSP_ID AND a.pid IN ({inlist})
      WHERE c.ETL_CURRENT = TRUE
        AND c.CONFIRMED_SLOT_AT IS NOT NULL
        AND c.INSTALLATION_COMPLETED_AT IS NOT NULL
        AND c.INSTALLATION_COMPLETED_AT >= DATEADD(day, -15, DATEADD(minute, -330, CURRENT_TIMESTAMP()))
      QUALIFY ROW_NUMBER() OVER (PARTITION BY c.CONNECTION_ID ORDER BY c.INSTALLATION_COMPLETED_AT DESC) = 1
    ),
    pos AS (SELECT dmin FROM t WHERE dmin >= 0)
    SELECT COUNT(*) AS n,
      ROUND(AVG(dmin)/60.0, 1) AS mean_h,
      ROUND(PERCENTILE_CONT(0.50) WITHIN GROUP (ORDER BY dmin)/60.0, 1) AS p50_h,
      ROUND(PERCENTILE_CONT(0.90) WITHIN GROUP (ORDER BY dmin)/60.0, 1) AS p90_h,
      ROUND(PERCENTILE_CONT(0.99) WITHIN GROUP (ORDER BY dmin)/60.0, 1) AS p99_h,
      COUNT_IF(dmin <= 2880) AS within_2d,
      COUNT_IF(dmin <= 4320) AS within_3d
    FROM pos"""
    rows = metabase_sql(sql)
    return rows[0] if rows and rows[0].get("n") else None


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
    coh_ever = bucket("cohort_ever")
    coh_task = bucket("cohort_task")   # CSP view (task level, re-farm counted)
    ccoh = bucket("confirm_cohort")
    ccoh_ever = bucket("confirm_cohort_ever")
    csps = {r["enr"]: r["n"] for r in raw if r["mode"] == "csps"}
    total = {str(r["day_ist"])[:10]: r["bookings"] for r in raw
             if r["mode"] == "total" and r["day_ist"]}

    def pct(a, b):
        return round(100 * a / b, 1) if b else None

    def _agg_cohort(src):
        out = []
        for d in days:
            e = src.get((d, 1), {})
            s = src.get((d, 0), {})
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

    def agg_cohort():        return _agg_cohort(coh)        # current-state (live pipeline)
    def agg_cohort_ever():   return _agg_cohort(coh_ever)   # ever-reached (progression)
    # CSP view: identical row shape (task counts land in 'bookings'), so the same
    # aggregator applies — only the grain underneath differs.
    def agg_cohort_task():   return _agg_cohort(coh_task)

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

    def _agg_confirm(src):   # install ratio by customer-slot-confirmed day
        out = []
        for d in days:
            e, s = src.get((d, 1), {}), src.get((d, 0), {})
            cnf, ins = e.get("confirmed", 0), e.get("installed", 0)
            scnf, sins = s.get("confirmed", 0), s.get("installed", 0)
            out.append({"day_ist": d, "cust_confirmed": cnf, "installs": ins,
                        "install_ratio": pct(ins, cnf),
                        "sh_cust_confirmed": scnf, "sh_installs": sins,
                        "sh_install_ratio": pct(sins, scnf)})
        return out

    def agg_confirm():        return _agg_confirm(ccoh)        # current-state
    def agg_confirm_ever():   return _agg_confirm(ccoh_ever)   # ever-reached (Wiom view)

    def block(rows):
        def avg(k):
            vals = [r[k] for r in rows if r.get(k) is not None]
            return round(sum(vals) / len(vals), 1) if vals else None
        return {k: avg(k) for k in L1_KEYS}

    # Install ratio uses a UNIFORM 4-day (96h) maturity cap: an install counts
    # only if it completed within 96h of the slot-confirm (see l1_daily_agg.sql),
    # so every confirm-day is measured on an identical window. A confirm-day is
    # only fully observable once its whole cohort has had 96h, i.e. it is >=4 days
    # old, so we hold out confirm-days newer than that from the post average. The
    # daily series ends yesterday (today-1), so the last fully-aged day = today-4;
    # the still-maturing tail stays on the chart, greyed.
    mature_cutoff = (datetime.now(IST).date() - timedelta(days=4)).isoformat()

    modes = {}
    for mode, rows in (("cohort", agg_cohort()), ("cohort_ever", agg_cohort_ever()),
                       ("cohort_task", agg_cohort_task()),
                       ("event", agg_event()),
                       ("confirm_cohort", agg_confirm()),
                       ("confirm_cohort_ever", agg_confirm_ever())):
        pre = [r for r in rows if L1_START <= r["day_ist"] <= PRE_END]
        post = [r for r in rows if r["day_ist"] >= POST_START]
        if mode in ("confirm_cohort", "confirm_cohort_ever"):
            post = [r for r in post if r["day_ist"] <= mature_cutoff]
        modes[mode] = {"daily": rows, "pre_avg": block(pre), "post_avg": block(post)}
    modes["confirm_cohort"]["mature_cutoff"] = mature_cutoff
    modes["confirm_cohort_ever"]["mature_cutoff"] = mature_cutoff

    try:
        leadtime = _leadtime_stats(enrolled_ids)
    except Exception:
        traceback.print_exc()
        leadtime = None

    # task-level: customer-slot-confirmed TASKS (each CSP-task, re-farm counted)
    # vs installs among them, by slot-confirmed day. This is the per-CSP-task
    # basis MG is paid on — confirm_cohort dedups to the customer's latest CSP;
    # here EVERY task counts, so re-farming carries its weight.
    task_confirm = None
    try:
        inlist = ",".join(f"'{p}'" for p in enrolled_ids)
        tsql = f"""
        WITH mg AS (SELECT CSP_ID FROM PROD_DB.CSP_GATEWAY_SERVICE_CSP_GATEWAY_SERVICE.CSP_ACCOUNT
          WHERE _fivetran_active=TRUE AND PARTNER_ID IN ({inlist})
          QUALIFY ROW_NUMBER() OVER (PARTITION BY CSP_ID ORDER BY 1)=1)
        SELECT TO_DATE(DATEADD(minute,330,c.CONFIRMED_SLOT_AT))::STRING day,
          COUNT(*) confirmed_tasks,
          SUM(IFF(c.INSTALLATION_COMPLETED_AT IS NOT NULL
                  AND c.INSTALLATION_COMPLETED_AT <= DATEADD(hour,96,c.CONFIRMED_SLOT_AT),1,0)) installs
        FROM PROD_DB.DBT_CSP.TAS_INSTALL_EXECUTION_CANDIDATES c
        JOIN mg ON mg.CSP_ID = c.CSP_ID
        WHERE c.ETL_CURRENT=TRUE AND c.CONFIRMED_SLOT_AT IS NOT NULL
          AND c.CONFIRMED_SLOT_AT >= DATEADD(minute,-330,'{L1_START} 00:00:00'::TIMESTAMP_NTZ)
        GROUP BY 1"""
        tmap = {str(r["day"])[:10]: r for r in metabase_sql(tsql)}
        tdaily = []
        for d in days:
            r = tmap.get(d, {})
            ct = r.get("confirmed_tasks") or 0
            ins = r.get("installs") or 0
            tdaily.append({"day_ist": d, "confirmed_tasks": ct, "installs": ins, "install_ratio": pct(ins, ct)})
        task_confirm = {"daily": tdaily, "mature_cutoff": mature_cutoff}
    except Exception:
        traceback.print_exc()

    # per-flow daily breakdown (enrolled only) for the L1/L2 flow filter — the
    # frontend sums the selected flows and recomputes rates client-side.
    flows = None
    try:
        fsql = open(os.path.join(BASE_DIR, "sql", "l1_flows.sql"), encoding="utf-8").read()
        fsql = fsql.replace("{PARTNER_IN_LIST}", ",".join(f"'{p}'" for p in enrolled_ids))
        fsql = fsql.replace("{START_DATE}", L1_START)
        frows = [{"mode": r["mode"], "flow": str(r["flow"]), "day": str(r["day_ist"])[:10],
                  "bookings": r.get("bookings") or 0, "accepted": r.get("accepted") or 0,
                  "confirmed": r.get("confirmed") or 0, "installed": r.get("installed") or 0,
                  "accepted_ever": r.get("accepted_ever") or 0, "confirmed_ever": r.get("confirmed_ever") or 0,
                  "installed_ever": r.get("installed_ever") or 0, "n": r.get("n") or 0,
                  "sh_bookings": r.get("sh_bookings") or 0, "sh_accepted": r.get("sh_accepted") or 0,
                  "sh_confirmed": r.get("sh_confirmed") or 0, "sh_accepted_ever": r.get("sh_accepted_ever") or 0,
                  "sh_confirmed_ever": r.get("sh_confirmed_ever") or 0, "sh_n": r.get("sh_n") or 0}
                 for r in metabase_sql(fsql) if r.get("day_ist")]
        flows = {"list": sorted({r["flow"] for r in frows}), "rows": frows}
    except Exception:
        traceback.print_exc()

    return {"modes": modes, "enrolled_n": len(enrolled_ids), "leadtime": leadtime,
            "task_confirm": task_confirm, "flows": flows,
            "csps_receiving": {"enrolled": csps.get(1), "non_enrolled": csps.get(0)},
            "complete_through": yday}


def compute_banner():
    """MBG Board reproduction — in-app home-banner opens (see sql/l0_banner.sql).

    Mirrors CleverTap board 1783687017, whose three cards all read one event,
    `banner_opened`, with NO CSP segment. Takes no cohort: this is deliberately an
    all-users number so the portal and the board agree. There is no denominator, so
    no reach % — do not reintroduce one against the enrolled count.

    SUPERSET, NOT MBP-ONLY: `banner_opened` covers every home banner, and its
    `banner` property is a per-CSP uuid, so MBP cannot be isolated from CleverTap
    alone. Labelled as such on the card. No impression event exists for the native
    banner, so there is no CTR here.

    Also returns last3/prev3 — unique CSPs engaging over the last 3 COMPLETE IST
    days vs the 3 before, the leading-indicator card. Complete days only, so these
    exclude today while the board tiles include it; that divergence is intentional
    (a trend needs like-for-like windows, board parity needs today).
    """
    today = datetime.now(IST).date()
    last3_end = today - timedelta(days=1)           # yesterday = newest complete day
    last3_start = today - timedelta(days=3)
    prev3_end = last3_start - timedelta(days=1)
    prev3_start = last3_start - timedelta(days=3)
    # The prev3 window can only be trusted if it sits fully inside the query window —
    # otherwise the ev CTE silently clips it and the delta reads as a crash rather
    # than as missing data. Suppress the comparison instead of lying about it.
    prev3_valid = prev3_start.isoformat() >= BANNER_START

    sql = open(os.path.join(BASE_DIR, "sql", "l0_banner.sql"), encoding="utf-8").read()
    sql = (sql.replace("{START_DATE}", BANNER_START)
              .replace("{LAST3_START}", last3_start.isoformat())
              .replace("{LAST3_END}", last3_end.isoformat())
              .replace("{PREV3_START}", prev3_start.isoformat())
              .replace("{PREV3_END}", prev3_end.isoformat()))
    raw = metabase_sql(sql)

    daily = sorted(({"day_ist": str(r["k"])[:10], "opens": r["opens"] or 0,
                     "uniq_csps": r["uniq_csps"] or 0, "uniq_users": r["uniq_users"] or 0}
                    for r in raw if r["mode"] == "daily" and r["k"]),
                   key=lambda r: r["day_ist"])
    tot = next((r for r in raw if r["mode"] == "total"), {})
    l3 = next((r for r in raw if r["mode"] == "last3"), {})
    p3 = next((r for r in raw if r["mode"] == "prev3"), {})
    # histogram is per USER (identity), not per CSP — matches the board's card.
    hist = sorted(({"bucket": int(r["k"]), "users": r["uniq_users"] or 0}
                   for r in raw if r["mode"] == "hist" and r["k"]),
                  key=lambda r: r["bucket"])

    opens = tot.get("opens") or 0
    uniq_csps = tot.get("uniq_csps") or 0
    uniq_users = tot.get("uniq_users") or 0

    l3_csps = l3.get("uniq_csps") or 0
    p3_csps = p3.get("uniq_csps") or 0
    l3_opens = l3.get("opens") or 0
    return {
        "opens": opens, "uniq_csps": uniq_csps, "uniq_users": uniq_users,
        "avg_per_user": round(opens / uniq_users, 1) if uniq_users else None,
        "daily": daily, "hist": hist,
        "last3": {
            "csps": l3_csps, "opens": l3_opens,
            "users": l3.get("uniq_users") or 0,
            "avg_per_csp": round(l3_opens / l3_csps, 1) if l3_csps else None,
            "start": last3_start.isoformat(), "end": last3_end.isoformat(),
            # prev3 is None (not 0) when the comparison window falls outside the
            # query window — the card hides the delta rather than showing a fake drop.
            "prev_csps": p3_csps if prev3_valid else None,
            "delta": (l3_csps - p3_csps) if prev3_valid else None,
            "prev_start": prev3_start.isoformat() if prev3_valid else None,
            "prev_end": prev3_end.isoformat() if prev3_valid else None,
        },
        "window_start": BANNER_START, "today_ist": today.isoformat(),
        "board_url": MBG_BOARD_URL, "board": BOARD_SNAPSHOT,
    }


def compute_sehat_funnel():
    """Sehat MG enrollment funnel — the sign-up feed for the second initiative.

    Warehouse recreation of the CSP-Guarantee campaign dashboard: the six opt-in
    events per cohort (offer_id), counted as unique CSPs, plus a daily
    reached/enrolled trend. `live` is False until any event appears, so the tab
    renders 'awaiting sign-up feed' rather than fake zeros — matching the provision.
    Takes no cohort argument: eligibility is the fixed 99-CSP launch cohort, split
    61/37 by track in SEHAT_COHORTS.
    """
    today = datetime.now(IST).date()
    sql = open(os.path.join(BASE_DIR, "sql", "sehat_funnel.sql"), encoding="utf-8").read()
    sql = sql.replace("{START_DATE}", SEHAT_START)
    raw = metabase_sql(sql)

    # index step counts and daily rows by cohort
    steps = {c["key"]: {} for c in SEHAT_COHORTS}       # offer -> {step_key: distinct csps}
    daily = {c["key"]: {} for c in SEHAT_COHORTS}       # offer -> {day: {reached, enrolled}}
    for r in raw:
        offer = r.get("offer")
        if offer not in steps:
            continue
        if r["mode"] == "step":
            step_key = EVENT_TO_STEP.get(r["k"])
            if step_key:
                steps[offer][step_key] = r["a"] or 0
        elif r["mode"] == "daily" and r["k"]:
            daily[offer][str(r["k"])[:10]] = {"reached": r["a"] or 0, "enrolled": r["b"] or 0}

    campaigns = []
    for c in SEHAT_COHORTS:
        funnel = {key: steps[c["key"]].get(key, 0) for key, _, _ in SEHAT_STEPS}
        day_rows = [{"date": d, **v} for d, v in sorted(daily[c["key"]].items())]
        campaigns.append({**{k: c[k] for k in
                             ("key", "label", "suffix", "desc", "color", "eligible", "creative")},
                          "funnel": funnel, "daily": day_rows})

    total_events = sum(sum(cc["funnel"].values()) for cc in campaigns)
    return {
        "live": total_events > 0,
        "steps": [[k, lbl, ev] for k, lbl, ev in SEHAT_STEPS],
        "campaigns": campaigns,
        "eligible_total": sum(c["eligible"] for c in SEHAT_COHORTS),
        "window_start": SEHAT_START, "today_ist": today.isoformat(),
        "source_url": "https://vikaswiom.github.io/wiom-csp-guarantee-campaign/dashboard.html",
    }


def _fetch_sehat_cohort():
    """Cohort + opt-in status from the offer sheet's 'Sehat MG' tab (gid), live via
    gviz CSV. Any non-blank 'Opted in' cell = opted. Falls back to the committed
    snapshot (data/sehat_cohort.json) with no opt-in info if the sheet is unreachable.
    Returns {optical:[ids], sla:[ids], opted_optical:set, opted_sla:set, source}.
    """
    import csv, io
    url = (f"https://docs.google.com/spreadsheets/d/{SEHAT_SHEET_ID}"
           f"/gviz/tq?tqx=out:csv&gid={SEHAT_SHEET_GID}")
    out = {"optical": [], "sla": [], "opted_optical": set(), "opted_sla": set(),
           "source": "sheet"}
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "cold-start-dashboard"})
        with urllib.request.urlopen(req, timeout=20) as r:
            text = r.read().decode("utf-8", "replace")
        if "<html" in text[:200].lower():
            raise RuntimeError("gviz returned HTML (sheet not accessible unauth)")
        for row in csv.DictReader(io.StringIO(text)):
            cid = (row.get("CSP ID") or "").strip()
            iv = (row.get("Sehat Intervention") or "").lower()
            track = "optical" if "optical" in iv else ("sla" if "sla" in iv else None)
            if not cid or not track:
                continue
            out[track].append(cid)
            if (row.get("Opted in") or "").strip():
                out["opted_" + track].add(cid)
        if out["optical"] or out["sla"]:
            return out
        raise RuntimeError("sheet parsed empty")
    except Exception as e:
        snap = json.load(open(os.path.join(BASE_DIR, "data", "sehat_cohort.json"), encoding="utf-8"))
        return {"optical": snap.get("optical", []), "sla": snap.get("sla", []),
                "opted_optical": set(), "opted_sla": set(),
                "source": f"snapshot ({type(e).__name__})"}


def compute_sehat_quality():
    """Sehat MG observation layer — day-on-day quality trend per cohort.

    Optical cohort → rolling 15-day Optical Power; SLA cohort → rolling 60-day
    on-time %. Card 11616's metric logic, computed per day (see sql/sehat_quality.sql).
    Splits opted-vs-not when the sheet's opt-in column is populated; until then each
    cohort renders a single whole-cohort line. HIGH = GOOD on both; gate = 80%.
    """
    coh = _fetch_sehat_cohort()
    today = datetime.now(IST).date()
    obs_end = today - timedelta(days=1)                 # complete IST days only
    obs_start = datetime.strptime(SEHAT_OBS_START, "%Y-%m-%d").date()
    ndays = (obs_end - obs_start).days + 1
    tmpl = open(os.path.join(BASE_DIR, "sql", "sehat_quality.sql"), encoding="utf-8").read()

    cohorts = []
    for cdef in SEHAT_COHORTS:
        track = "optical" if cdef["key"] == "sehat_optical" else "sla"
        ids = coh[track]
        opted = coh["opted_" + track] & set(ids)
        q = SEHAT_QUALITY[cdef["key"]]
        # split only if BOTH groups are non-empty — otherwise one whole-cohort line
        split = 0 < len(opted) < len(ids)
        if split:
            opted_in = ",".join(f"'{c}'" for c in sorted(opted))
            group_case = f"CASE WHEN t.CSP_ID IN ({opted_in}) THEN 'opted' ELSE 'notopted' END"
        else:
            group_case = "'all'"
        series = {}
        if ids and ndays > 0:
            sql = (tmpl.replace("{TABLE}", q["table"]).replace("{GOOD}", q["good"])
                       .replace("{TOTAL}", q["total"]).replace("{DATECOL}", q["datecol"])
                       .replace("{WINDOW}", str(q["window"]))
                       .replace("{CSP_IN}", ",".join(f"'{c}'" for c in ids))
                       .replace("{GROUP_CASE}", group_case)
                       .replace("{OBS_START}", obs_start.isoformat())
                       .replace("{OBS_END}", obs_end.isoformat())
                       .replace("{NDAYS}", str(ndays)))
            for r in metabase_sql(sql):
                grp = r["grp"]
                series.setdefault(grp, []).append({
                    "date": str(r["day"])[:10],
                    "median": round(r["median_pct"], 1) if r["median_pct"] is not None else None,
                    "mean": round(r["mean_pct"], 1) if r["mean_pct"] is not None else None,
                    "n": r["n_csps"] or 0})
            for g in series:
                series[g].sort(key=lambda x: x["date"])
        cohorts.append({
            "key": cdef["key"], "label": cdef["label"], "color": cdef["color"],
            "track": "A · Ilaaj" if track == "optical" else "B · Fit rakhna",
            "metric": q["metric"], "unit": q["unit"], "window": q["window"],
            "gate": SEHAT_GATE, "n": len(ids), "n_opted": len(opted),
            "split": split, "series": series,
        })

    return {
        "cohorts": cohorts, "obs_start": obs_start.isoformat(), "obs_end": obs_end.isoformat(),
        "today_ist": today.isoformat(), "gate": SEHAT_GATE,
        "cohort_source": coh["source"], "sheet_url": SEHAT_SHEET_URL,
        "split_live": any(c["split"] for c in cohorts),
    }


def compute_feedback(enrolled_ids):
    """Enrolled-CSP belief-check answers (flow-3 beacon on the mbg portal).

    Answers: excited / questions / dontknow (unaware) / dontcare (indifferent).
    f3a_/f3b_ prefixes are an A/B position test — merged. Latest answer per CSP.
    """
    rows = supabase_rows(
        SUPABASE_PORTAL_URL, "SUPABASE_PORTAL_SERVICE_KEY",
        "mbg_screen_log?select=pid,screen,ts&flow=eq.3"
        "&or=(screen.like.f3a_*,screen.like.f3b_*)&order=ts.asc")
    enrolled = set(enrolled_ids)
    latest = {}
    for r in rows:
        pid, ans = r["pid"], str(r["screen"])[4:]
        # flow-3 pids are person-level CleverTap identities (zero-padded, e.g.
        # '009278' for map identity '9278'); resolve to partner
        partner = (pid if pid in enrolled
                   else ID2P.get(pid) or ID2P.get(str(pid).lstrip("0")))
        if partner in enrolled and ans in ("excited", "questions", "dontknow", "dontcare"):
            latest[partner] = ans   # ts ascending — last write wins
    counts = {k: 0 for k in ("excited", "questions", "dontknow", "dontcare")}
    for ans in latest.values():
        counts[ans] += 1
    names = FC.get("names", {})
    call_list = sorted(
        ({"partner_id": p, "name": names.get(p, ""), "answer": a}
         for p, a in latest.items() if a != "excited"),
        key=lambda r: ({"dontcare": 0, "dontknow": 1, "questions": 2}[r["answer"]], r["name"]))
    return {"counts": counts, "answered": len(latest), "enrolled_n": len(enrolled),
            "call_list": call_list, "latest": latest}


# ----------------------------------------------------------------------------
# Belief-cohort segmentation of L1 metrics — how each belief group performs.
# ----------------------------------------------------------------------------
COHORT_ORDER = [("excited", "Excited"), ("questions", "Has questions"),
                ("dontknow", "Unaware"), ("dontcare", "Indifferent"),
                ("no_response", "No response")]
SMALL_COHORT = 30   # flag cohorts below this many CSPs as noisy


def compute_cohort(enrolled_ids, latest):
    """L1 funnel per belief cohort, over the post period, per-CSP-normalised.

    Cohort = the CSP's latest belief answer (response supersedes no-response,
    already enforced by `latest`); enrolled CSPs with no answer = 'no_response'.
    """
    if not enrolled_ids:
        raise RuntimeError("no enrolled partners — cohort view skipped")
    # partner -> cohort key
    cohort_of = {p: latest.get(p, "no_response") for p in enrolled_ids}

    yday = (datetime.now(IST).date() - timedelta(days=1)).isoformat()
    # Install-ratio maturity: bookings from the last 3 days haven't had time to
    # install, so the HEADLINE install ratio holds them out (same rule as L2).
    mature_cutoff = (datetime.now(IST).date() - timedelta(days=4)).isoformat()
    sql = open(os.path.join(BASE_DIR, "sql", "l1_cohort.sql"), encoding="utf-8").read()
    sql = (sql.replace("{PARTNER_IN_LIST}", ",".join(f"'{p}'" for p in enrolled_ids))
              .replace("{START_DATE}", "2026-06-24")
              .replace("{YEST}", yday)
              .replace("{MATURE_CUTOFF}", mature_cutoff))
    raw = metabase_sql(sql)   # AGGREGATED per (partner, flow) — no 2000-row truncation

    after_win = (POST_START, yday)
    after_days = len(_daterange(*after_win))
    BEFORE_WINDOWS = [("a", ("2026-06-24", "2026-06-30"), "24–30 Jun · post-migration (clean baseline)")]
    before_days = {wid: len(_daterange(*win)) for wid, win, _ in BEFORE_WINDOWS}

    # cohort-size denominators (every enrolled CSP has a cohort, even with 0 bookings)
    cohort_size = {k: 0 for k, _ in COHORT_ORDER}
    for p in enrolled_ids:
        cohort_size[cohort_of[p]] += 1

    def fresh():
        return {k: {"bookings": 0, "accepted": 0, "confirmed": 0, "installed": 0,
                    "bookings_mat": 0, "accepted_mat": 0, "confirmed_mat": 0, "installed_mat": 0,
                    "accepted_ever": 0, "confirmed_ever": 0, "installed_ever": 0,
                    "accepted_ever_mat": 0, "confirmed_ever_mat": 0, "installed_ever_mat": 0,
                    "csps": set(), "amin_sum": 0.0, "amin_n": 0} for k, _ in COHORT_ORDER}

    befores = {wid: fresh() for wid, _, _ in BEFORE_WINDOWS}
    after = fresh()
    bwid = BEFORE_WINDOWS[0][0]
    flow_rows = []   # per (cohort, flow, partner) aggregates for the client-side flow filter
    for r in raw:
        pid = str(r["partner_id"])
        ck = cohort_of.get(pid)
        if ck is None:
            continue
        g = lambda k: r.get(k) or 0
        b, a = befores[bwid][ck], after[ck]
        # BEFORE (24-30 Jun) is fully matured, so matured == full
        for src, dst in (("b_bk", "bookings"), ("b_ac", "accepted"), ("b_cf", "confirmed"), ("b_in", "installed"),
                         ("b_ace", "accepted_ever"), ("b_cfe", "confirmed_ever"), ("b_ine", "installed_ever")):
            b[dst] += g(src); b[dst + "_mat"] += g(src)
        if g("b_bk"): b["csps"].add(pid)
        b["amin_sum"] += g("b_amin"); b["amin_n"] += g("b_an")
        # AFTER (1 Jul..yest): full = a_*, matured subset = m_*
        for src, dst in (("a_bk", "bookings"), ("a_ac", "accepted"), ("a_cf", "confirmed"), ("a_in", "installed"),
                         ("a_ace", "accepted_ever"), ("a_cfe", "confirmed_ever"), ("a_ine", "installed_ever")):
            a[dst] += g(src)
        for src, dst in (("m_bk", "bookings_mat"), ("m_ac", "accepted_mat"), ("m_cf", "confirmed_mat"), ("m_in", "installed_mat"),
                         ("m_ace", "accepted_ever_mat"), ("m_cfe", "confirmed_ever_mat"), ("m_ine", "installed_ever_mat")):
            a[dst] += g(src)
        if g("a_bk"): a["csps"].add(pid)
        a["amin_sum"] += g("a_amin"); a["amin_n"] += g("a_an")
        flow_rows.append({"cohort": ck, "flow": str(r["flow"]), "pid": pid, **{k: g(k) for k in
            ("b_bk", "b_ac", "b_cf", "b_in", "b_ace", "b_cfe", "b_ine",
             "a_bk", "a_ac", "a_cf", "a_in", "a_ace", "a_cfe", "a_ine",
             "m_bk", "m_ac", "m_cf", "m_in", "m_ace", "m_cfe", "m_ine")}})

    def pct(a, b):
        return round(100 * a / b, 1) if b else None

    def block(bucket, keys, size, days):
        aks = [bucket[k] for k in keys]
        bk = sum(a["bookings"] for a in aks)
        acc = sum(a["accepted"] for a in aks)
        cnf = sum(a["confirmed"] for a in aks)
        ins = sum(a["installed"] for a in aks)
        bk_m = sum(a["bookings_mat"] for a in aks)
        acc_m = sum(a["accepted_mat"] for a in aks)
        cnf_m = sum(a["confirmed_mat"] for a in aks)
        ins_m = sum(a["installed_mat"] for a in aks)
        acce = sum(a["accepted_ever"] for a in aks)
        cnfe = sum(a["confirmed_ever"] for a in aks)
        inse = sum(a["installed_ever"] for a in aks)
        acce_m = sum(a["accepted_ever_mat"] for a in aks)
        cnfe_m = sum(a["confirmed_ever_mat"] for a in aks)
        inse_m = sum(a["installed_ever_mat"] for a in aks)
        recv = len(set().union(*[a["csps"] for a in aks])) if aks else 0
        asum = sum(a["amin_sum"] for a in aks)
        an = sum(a["amin_n"] for a in aks)
        med = round(asum / an / 60.0, 1) if an else None   # mean hrs to accept (additive across flows)
        return {"csps_receiving": recv, "bookings": bk,
                "bk_per_csp_day": round(bk / size / days, 2) if (size and days) else None,
                # each rate: matured (headline) + full-window total (faint), per basis
                "accept_pct": pct(acc_m, bk_m), "accept_pct_total": pct(acc, bk),
                "confirm_pct": pct(cnf_m, acc_m), "confirm_pct_total": pct(cnf, acc),
                "install_ratio": pct(ins_m, cnf_m), "install_ratio_total": pct(ins, cnf),
                "accept_pct_ever": pct(acce_m, bk_m), "accept_pct_ever_total": pct(acce, bk),
                "confirm_pct_ever": pct(cnfe_m, acce_m), "confirm_pct_ever_total": pct(cnfe, acce),
                "install_ratio_ever": pct(inse_m, cnfe_m), "install_ratio_ever_total": pct(inse, cnfe),
                "med_hrs_to_accept": med}

    def row_for(keys, label, size):
        return {"label": label, "keys": list(keys), "csps": size, "small": size < SMALL_COHORT,
                "befores": {wid: block(befores[wid], keys, size, before_days[wid])
                            for wid, _, _ in BEFORE_WINDOWS},
                "after": block(after, keys, size, after_days)}

    rows = [row_for([k], lbl, cohort_size[k]) for k, lbl in COHORT_ORDER]
    rows.append(row_for([k for k, _ in COHORT_ORDER], "All enrolled", len(enrolled_ids)))
    cohort = {"after_window": list(after_win), "after_days": after_days,
              "before_options": [{"id": wid, "window": list(win), "days": before_days[wid], "label": lbl}
                                 for wid, win, lbl in BEFORE_WINDOWS],
              "rows": rows,
              # per (cohort, flow, partner) aggregates + windows/days for the client-side flow filter
              "flow_rows": flow_rows, "before_days": before_days[bwid], "mature_cutoff": mature_cutoff}

    # ----- comparison rows: eligible-not-enrolled + non-eligible CSPs ----------
    # eligible = frozen launch cohort (offered MG); enrolled is its opted-in
    # subset. Aggregated in Snowflake (l1_groups.sql) so the network-wide
    # non-eligible set stays under the row cap. CSPs shown = distinct receiving.
    try:
        eligible_ids = sorted(F1 | F2)
        gsql = open(os.path.join(BASE_DIR, "sql", "l1_groups.sql"), encoding="utf-8").read()
        gsql = gsql.replace("{ENROLLED_IN_LIST}", ",".join(f"'{p}'" for p in enrolled_ids))
        gsql = gsql.replace("{ELIGIBLE_IN_LIST}", ",".join(f"'{p}'" for p in eligible_ids))
        gsql = gsql.replace("{START_DATE}", "2026-06-24")
        graw = metabase_sql(gsql)
        funnel = {}
        meta = {}
        for r in graw:
            if r["mode"] == "funnel":
                funnel.setdefault(r["grp"], []).append(r)
            elif r["mode"] == "meta" and r.get("win"):
                meta[(r["grp"], r["win"])] = r
        b_wid, b_win, _ = BEFORE_WINDOWS[0]
        b_days = before_days[b_wid]

        def grp_block(grp, win_name, win_range, days, fixed_size=None):
            rs = [x for x in funnel.get(grp, []) if win_range[0] <= x["day_ist"] <= win_range[1]]
            def s(k, mat=False):
                return sum((x[k] or 0) for x in rs if (not mat or x["day_ist"] <= mature_cutoff))
            bk, acc, cnf, ins = s("bookings"), s("accepted"), s("confirmed"), s("installed")
            bk_m, acc_m, cnf_m, ins_m = s("bookings", 1), s("accepted", 1), s("confirmed", 1), s("installed", 1)
            acce, cnfe, inse = s("accepted_ever"), s("confirmed_ever"), s("installed_ever")
            acce_m, cnfe_m, inse_m = s("accepted_ever", 1), s("confirmed_ever", 1), s("installed_ever", 1)
            m = meta.get((grp, win_name), {})
            recv = m.get("csps") or 0
            size = fixed_size if fixed_size is not None else recv   # per-CSP-day denominator
            return {"csps_receiving": recv, "bookings": bk,
                    "bk_per_csp_day": round(bk / size / days, 2) if (size and days) else None,
                    "accept_pct": pct(acc_m, bk_m), "accept_pct_total": pct(acc, bk),
                    "confirm_pct": pct(cnf_m, acc_m), "confirm_pct_total": pct(cnf, acc),
                    "install_ratio": pct(ins_m, cnf_m), "install_ratio_total": pct(ins, cnf),
                    "accept_pct_ever": pct(acce_m, bk_m), "accept_pct_ever_total": pct(acce, bk),
                    "confirm_pct_ever": pct(cnfe_m, acce_m), "confirm_pct_ever_total": pct(cnfe, acce),
                    "install_ratio_ever": pct(inse_m, cnfe_m), "install_ratio_ever_total": pct(inse, cnfe),
                    "med_hrs_to_accept": m.get("med_hrs")}

        # eligible-not-enrolled: fixed universe (offered MG per the sheet, not
        # enrolled). non-eligible: no bounded universe -> distinct receiving CSPs.
        elig_ne_n = len(set(eligible_ids) - set(enrolled_ids))
        extra = [
            {"label": "Non-enrolled · eligible", "csps": elig_ne_n, "small": False, "group": "eligible_ne",
             "befores": {b_wid: grp_block("eligible_ne", "before", b_win, b_days, elig_ne_n)},
             "after": grp_block("eligible_ne", "after", after_win, after_days, elig_ne_n)},
        ]
        ne_after = (meta.get(("non_eligible", "after")) or {}).get("csps") or 0
        extra.append(
            {"label": "Non-eligible", "csps": ne_after, "small": False, "group": "non_eligible",
             "befores": {b_wid: grp_block("non_eligible", "before", b_win, b_days)},
             "after": grp_block("non_eligible", "after", after_win, after_days)})
        cohort["extra_rows"] = extra
        # per (grp, flow, day) funnel counts for the client-side flow filter on the
        # comparison rows (csps stay all-flows; only rates/bookings filter).
        cohort["extra_flow_rows"] = [
            {"grp": r["grp"], "flow": str(r["flow"]), "day": str(r["day_ist"])[:10],
             "bookings": r.get("bookings") or 0, "accepted": r.get("accepted") or 0,
             "confirmed": r.get("confirmed") or 0, "installed": r.get("installed") or 0,
             "accepted_ever": r.get("accepted_ever") or 0, "confirmed_ever": r.get("confirmed_ever") or 0,
             "installed_ever": r.get("installed_ever") or 0}
            for r in graw if r["mode"] == "funnel" and r.get("day_ist")]
    except Exception:
        traceback.print_exc()
        cohort["extra_rows"] = []
        cohort["extra_flow_rows"] = []

    # ----- CSP-status split: TASK-ACTIVITY based (matches the team cross-tab) --
    month_start = datetime.now(IST).date().replace(day=1).isoformat()
    ssql = open(os.path.join(BASE_DIR, "sql", "l1_status.sql"), encoding="utf-8").read()
    ssql = ssql.replace("{PARTNER_IN_LIST}", ",".join(f"'{p}'" for p in enrolled_ids))
    ssql = ssql.replace("{MONTH_START}", month_start)
    sraw = {str(r["partner_id"]): r for r in metabase_sql(ssql)}

    def classify(p, tk, ik):
        r = sraw.get(p)
        if r and (r[ik] or 0) > 0:
            return "moved"           # >= 1 install (from tasks created in the window)
        if r and (r[tk] or 0) > 0:
            return "ignition"        # tasks created, none installed
        return "demand"              # 0 tasks — nothing reached the CSP

    out_b = {p: classify(p, "tb", "ib") for p in enrolled_ids}
    out_a = {p: classify(p, "ta", "ia") for p in enrolled_ids}

    def split(out, keys):
        s = {"moved": 0, "ignition": 0, "demand": 0}
        for p in enrolled_ids:
            if cohort_of[p] in keys:
                s[out[p]] += 1
        return s

    by_belief = []
    for k, lbl in COHORT_ORDER:
        by_belief.append({"cohort": k, "label": lbl, "csps": cohort_size[k],
                          "small": cohort_size[k] < SMALL_COHORT,
                          "before": split(out_b, {k}), "after": split(out_a, {k})})
    allk = {k for k, _ in COHORT_ORDER}

    # installation-behaviour transition: state per window is moved / ignition
    # (available demand, no install) / demand (demand deficit). before -> after.
    STATES = ("moved", "ignition", "demand")
    matrix = {s: {s2: 0 for s2 in STATES} for s in STATES}
    for p in enrolled_ids:
        matrix[out_b[p]][out_a[p]] += 1
    transition = {
        "matrix": matrix,
        "newly_installing": {"from_available": matrix["ignition"]["moved"],
                             "from_deficit": matrix["demand"]["moved"],
                             "total": matrix["ignition"]["moved"] + matrix["demand"]["moved"]},
        "stopped_installing": {"to_available": matrix["moved"]["ignition"],
                               "to_deficit": matrix["moved"]["demand"],
                               "total": matrix["moved"]["ignition"] + matrix["moved"]["demand"]},
        "stayed_installing": matrix["moved"]["moved"]}

    # maturity of a window (tasks created in it; ~14-day install runway)
    today = datetime.now(IST).date()
    runway = 14

    def _week_mat(w0, w1):
        ws = datetime.fromisoformat(w0).date()
        we = datetime.fromisoformat(w1).date()
        fracs, d = [], ws
        while d <= we:
            fracs.append(min(1.0, max(0.0, (today - d).days / runway)))
            d += timedelta(days=1)
        return {"pct": round(100 * sum(fracs) / len(fracs)),
                "fully_on": (we + timedelta(days=runway)).isoformat()}

    # week-on-week: classify every enrolled CSP into moved/ignition/demand per week
    weeks = []
    for w0, w1, lbl, tk, ik in IGN_WEEKS:
        out_w = {p: classify(p, tk, ik) for p in enrolled_ids}
        weeks.append({"window": [w0, w1], "label": lbl,
                      "split": split(out_w, allk), "maturity": _week_mat(w0, w1)["pct"]})
    maturity = _week_mat(*IGN_WEEKS[-1][:2])   # maturity band references the latest week

    ignition = {"before_window": list(IGN_BEFORE), "after_window": list(IGN_AFTER), "days": 7,
                "enrolled": len(enrolled_ids), "weeks": weeks,
                "totals_before": split(out_b, allk), "totals_after": split(out_a, allk),
                "by_belief": by_belief, "transition": transition, "maturity": maturity}

    # ----- install-rate gate (TASK level), calendar-month-to-date -------------
    # Aligned to the MG payout logic (MG_calculation_logic.md): denominator =
    # customer-confirmed leads that reached a FINAL state this month (open ones
    # sit in `pending`, not the denominator; true system cancels excluded). Rate
    # = inst / recv. A CSP with recv=0 but pending>0 has leads not yet matured
    # (not "below"); recv=0 and pending=0 = no confirmed leads at all.
    GATE = 0.60
    g = {"above": 0, "below": 0, "pending_only": 0, "no_leads": 0,
         "below_zero_install": 0, "one_more": 0, "pending_tasks": 0}
    for p in enrolled_ids:
        r = sraw.get(p)
        recv = (r.get("recv_m") or 0) if r else 0
        inst = (r.get("inst_m") or 0) if r else 0
        pend = (r.get("pend_m") or 0) if r else 0
        g["pending_tasks"] += pend
        if recv == 0:
            g["pending_only" if pend > 0 else "no_leads"] += 1
        elif inst / recv >= GATE:
            g["above"] += 1
        else:
            g["below"] += 1
            if inst == 0:
                g["below_zero_install"] += 1
            if (inst + 1) / (recv + 1) >= GATE:   # installing one more lead crosses
                g["one_more"] += 1
    gate = {"gate_pct": int(GATE * 100), "month": today.strftime("%B %Y"),
            "window": [month_start, today.isoformat()], "enrolled": len(enrolled_ids), **g}

    # day-on-day cohort movement (month-to-date): count of enrolled CSPs in each
    # gate state as of each day, reconstructed cumulatively (last day == snapshot).
    try:
        dsql = open(os.path.join(BASE_DIR, "sql", "gate_daily.sql"), encoding="utf-8").read()
        dsql = (dsql.replace("{PARTNER_IN_LIST}", ",".join(f"'{p}'" for p in enrolled_ids))
                    .replace("{MONTH_START}", month_start)
                    .replace("{TODAY}", today.isoformat())
                    .replace("{ENROLLED_N}", str(len(enrolled_ids))))
        gate["daily"] = [{"day": str(r["day"])[:10], "above": r.get("above") or 0,
                          "below": r.get("below") or 0, "pending": r.get("pending") or 0,
                          "no_leads": r.get("no_leads") or 0} for r in metabase_sql(dsql)]
    except Exception:
        traceback.print_exc()
        gate["daily"] = []

    cohort["_ignition"] = ignition
    cohort["_gate"] = gate
    return cohort


def compute_nsm(enrolled_ids):
    """Installs/day by enrolled CSPs: today (partial) + last 15 complete days."""
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

    trend = [row((today - timedelta(days=i)).isoformat()) for i in range(15, 0, -1)]
    t = row(today.isoformat())

    # month-to-date (calendar month, IST) — computed independently of the 17-day
    # trend window so it stays correct late in the month.
    inlist = ",".join(f"'{p}'" for p in enrolled_ids)
    msql = f"""
    WITH mg_csp AS (SELECT DISTINCT CSP_ID
        FROM PROD_DB.CSP_GATEWAY_SERVICE_CSP_GATEWAY_SERVICE.CSP_ACCOUNT
        WHERE _fivetran_active = TRUE AND PARTNER_ID IN ({inlist}))
    SELECT COUNT(DISTINCT IFF(CSP_ID IN (SELECT CSP_ID FROM mg_csp), CONNECTION_ID, NULL)) AS mg_installs,
           COUNT(DISTINCT CONNECTION_ID) AS total_installs
    FROM PROD_DB.DBT_CSP.TAS_INSTALL_EXECUTION_CANDIDATES
    WHERE ETL_CURRENT = TRUE AND INSTALLATION_COMPLETED_AT IS NOT NULL
      AND TO_DATE(DATEADD(minute, 330, INSTALLATION_COMPLETED_AT))
          >= DATE_TRUNC('month', TO_DATE(DATEADD(minute, 330, CURRENT_TIMESTAMP())))"""
    mrow = (metabase_sql(msql) or [{}])[0]

    return {"today": t["installs"], "today_total": t["total_installs"],
            "today_date": today.isoformat(), "trend": trend,
            "month_label": today.strftime("%b"),
            "month_mg": mrow.get("mg_installs", 0),
            "month_total": mrow.get("total_installs", 0)}


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
                  ("title", "url", "doc_type", "summary", "summary_source", "source",
                   "channel", "shared_by", "shared_at", "body_md", "file_path")),
    "changelog": ("cs_changelog", "changed_at.desc",
                  ("title", "description", "category", "impact", "owner", "changed_at")),
    "content": ("cs_content", "order_index.asc",
                ("slug", "title", "section", "body_md", "order_index")),
    "call_logs": ("cs_call_logs", "created_at.desc",
                  ("partner_id", "partner_name", "reason", "called_by", "called_at",
                   "outcome", "learning", "belief_after")),
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


# ----------------------------------------------------------------------------
# Smart synopsis — when a document is added without a summary, fetch the URL
# (best effort) and have Claude write the 2-3 sentence card.
# ----------------------------------------------------------------------------
def _fetch_page_text(url, cap=8000):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (coldstart-hub)"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            if "text" not in (resp.headers.get("Content-Type") or "text"):
                return None
            html = resp.read(400_000).decode("utf-8", "replace")
        text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.S | re.I)
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
        if len(text) < 200 or "accounts.google.com" in text[:2000].lower():
            return None            # empty page or a login wall — nothing useful
        return text[:cap]
    except Exception:
        return None


SYNOPSIS_INSTRUCTIONS = (
    "You write synopsis cards for WIOM's Cold-start project knowledge "
    "repository (the Minimum Guarantee program for CSP partners). "
    "Given a document's metadata and content, write 2-3 plain sentences: "
    "what the document contains and who on the team should read it for what. "
    "No preamble, no markdown, no quotes around the output.")


def _synopsis_context(doc):
    page = _fetch_page_text(doc.get("url")) if doc.get("url") else None
    context = (f"Title: {doc.get('title')}\nType: {doc.get('doc_type')}\n"
               f"Shared by: {doc.get('shared_by') or 'unknown'}"
               f"{' in ' + doc['channel'] if doc.get('channel') else ''}\n")
    if page:
        context += f"\nExtracted page content (may be partial):\n{page}"
    else:
        context += ("\nThe document content could not be fetched (private link). "
                    "Write the card from the title and metadata only, and end with "
                    "'(contents unverified)'.")
    return context


def _synopsis_via_cli(context):
    """Primary path: the claude CLI runs on the user's Claude subscription."""
    import shutil
    import subprocess
    exe = shutil.which("claude")
    if not exe:
        return None
    r = subprocess.run([exe, "-p", "--model", "claude-opus-4-8"],
                       input=f"{SYNOPSIS_INSTRUCTIONS}\n\n{context}",
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=180)
    out = (r.stdout or "").strip()
    return out if r.returncode == 0 and out else None


def _synopsis_via_api(context):
    """Backup path: the Anthropic API (ANTHROPIC_API_KEY)."""
    import anthropic
    client = anthropic.Anthropic()
    resp = client.messages.create(
        model="claude-opus-4-8", max_tokens=300,
        system=SYNOPSIS_INSTRUCTIONS,
        messages=[{"role": "user", "content": context}],
    )
    return next((b.text for b in resp.content if b.type == "text"), "").strip() or None


def synopsize_document(doc, file_text=None):
    """Return (summary, source): subscription CLI first, API as backup."""
    try:
        if file_text:
            context = (f"Title: {doc.get('title')}\nType: {doc.get('doc_type')}\n"
                       f"Uploaded by: {doc.get('shared_by') or 'unknown'}\n"
                       f"\nFile content (may be partial):\n{file_text[:8000]}")
        else:
            context = _synopsis_context(doc)
    except Exception:
        traceback.print_exc()
        return None, None
    for fn in (_synopsis_via_cli, _synopsis_via_api):
        try:
            text = fn(context)
            if text:
                return text, "auto"
        except Exception:
            traceback.print_exc()
    return None, None


def _synopsis_pdf_api(title, pdf_b64):
    """PDF synopsis via the API's document input (backup-path only)."""
    import anthropic
    client = anthropic.Anthropic()
    resp = client.messages.create(
        model="claude-opus-4-8", max_tokens=300,
        system=SYNOPSIS_INSTRUCTIONS,
        messages=[{"role": "user", "content": [
            {"type": "document",
             "source": {"type": "base64", "media_type": "application/pdf", "data": pdf_b64}},
            {"type": "text", "text": f"Write the synopsis card for this document, titled: {title}"},
        ]}],
    )
    return next((b.text for b in resp.content if b.type == "text"), "").strip() or None


# ----------------------------------------------------------------------------
# File uploads — Supabase Storage (private bucket); downloads stream through
# this server so files stay behind the portal's login.
# ----------------------------------------------------------------------------
UPLOAD_BUCKET = "cs-docs"
MAX_UPLOAD = 15 * 1024 * 1024
DOC_TYPES = {"pdf": "pdf", "doc": "doc", "docx": "doc", "xls": "sheet", "xlsx": "sheet",
             "csv": "sheet", "ppt": "deck", "pptx": "deck", "md": "doc", "txt": "doc"}


def storage_upload(path, data, content_type):
    key = _require("SUPABASE_AUDIT_SERVICE_KEY")
    req = urllib.request.Request(
        f"{SUPABASE_AUDIT_URL}/storage/v1/object/{UPLOAD_BUCKET}/{urllib.parse.quote(path)}",
        data=data, method="POST",
        headers={"Authorization": f"Bearer {key}", "Content-Type": content_type,
                 "x-upsert": "false"})
    return _http_json(req)


def storage_download(path):
    key = _require("SUPABASE_AUDIT_SERVICE_KEY")
    req = urllib.request.Request(
        f"{SUPABASE_AUDIT_URL}/storage/v1/object/{UPLOAD_BUCKET}/{urllib.parse.quote(path)}",
        headers={"Authorization": f"Bearer {key}"})
    resp = urllib.request.urlopen(req, timeout=120)
    return resp.read(), resp.headers.get("Content-Type", "application/octet-stream")


def handle_upload(body, editor):
    import base64 as b64
    import uuid
    filename = os.path.basename(body.get("filename") or "file")
    data = b64.b64decode(body.get("data_b64") or "")
    if not data:
        raise ValueError("empty file")
    if len(data) > MAX_UPLOAD:
        raise ValueError("file exceeds 15 MB limit")
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    path = f"{uuid.uuid4().hex[:12]}-{re.sub(r'[^A-Za-z0-9._-]', '_', filename)}"
    ctype = {"pdf": "application/pdf", "csv": "text/csv", "txt": "text/plain",
             "md": "text/markdown"}.get(ext, "application/octet-stream")
    storage_upload(path, data, ctype)

    doc = {"title": body.get("title") or filename,
           "doc_type": DOC_TYPES.get(ext, "other"), "source": "upload",
           "shared_by": body.get("shared_by") or editor or None,
           "shared_at": datetime.now(IST).date().isoformat(),
           "url": f"/files/{path}", "file_path": path}
    summary = (body.get("summary") or "").strip()
    if summary:
        doc["summary"], doc["summary_source"] = summary, "manual"
    else:
        text = None
        if ext in ("txt", "md", "csv"):
            text = data.decode("utf-8", "replace")
        s, src = synopsize_document(doc, file_text=text) if (text or ext != "pdf") else (None, None)
        if not s and ext == "pdf":
            try:
                s, src = _synopsis_pdf_api(doc["title"], b64.b64encode(data).decode()), "auto"
            except Exception:
                traceback.print_exc()
                s = None
        if s:
            doc["summary"], doc["summary_source"] = s, src
    return cs_create("documents", {**doc, "_editor": editor})


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
    if kind == "documents" and not clean.get("summary"):
        summary, src = synopsize_document(clean)
        if summary:
            clean["summary"], clean["summary_source"] = summary, src
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


FAIL_STATES = "('DECLINED','CANCELLED_BY_UPSTREAM','CANCELLED_BY_CUSTOMER'," \
              "'INSTALLATION_REPORTED_FAILED','INSTALLATION_EXPIRED')"


def _fail_owner(reason):
    """Map a terminal-failure reason -> (owner, bucket, plain-English why)."""
    k = (reason or "").upper()
    if "SUPERSEDED" in k or "REOPEN" in k: return ("exclude", "", "")
    if "NO_SHOW" in k: return ("csp", "CSP no-show", "Technician assigned but did not show up.")
    if "TIMEOUT" in k or "RETRY_EXHAUST" in k: return ("csp", "Timeout / inaction", "CSP did not accept/act within the SLA window; the booking timed out.")
    if "ROUTING" in k: return ("wiom", "Routing failure", "Platform failed to route the booking to a CSP.")
    if "DEVICE" in k: return ("wiom", "Device unavailable", "Required device was unavailable (device ordering held).")
    if "SCHEDUL" in k: return ("wiom", "Scheduling", "Installation could not be scheduled.")
    if "SERVICE NOT AVAILABLE" in k or "COVERAGE" in k: return ("wiom", "No coverage", "Declared not serviceable at the address — booking routed where it cannot be served (serviceability / planning gap).")
    if "NETWORK SETUP" in k: return ("wiom", "Network setup not possible", "Network setup not possible at the location.")
    if "BACKHAUL" in k: return ("wiom", "Backhaul not ready", "Backhaul / network not ready.")
    if "PRICE" in k or "PLAN PRI" in k: return ("customer", "Price", "Customer did not agree with the plan price.")
    if "NOT INTERESTED" in k: return ("customer", "Not interested", "Customer reported not interested.")
    if "CANCEL" in k: return ("customer", "Cancelled", "Customer cancelled the booking.")
    return ("unclassified", str(reason or "?"), "")


def compute_failures(days_n=15):
    """Daily failed installs by owner (CSP/Wiom/Customer), absolute-terminal per
    Connection ID (last transition is a terminal failure, never installed), plus
    the full Wiom-attributed booking list. Re-farm aware."""
    today = datetime.now(IST).date()
    start = (today - timedelta(days=days_n)).isoformat()
    yday = (today - timedelta(days=1)).isoformat()
    cte = f"""
    WITH cc AS (SELECT DISTINCT EXECUTION_CANDIDATE_ID, CONNECTION_ID FROM PROD_DB.DBT_CSP.TAS_INSTALL_EXECUTION_CANDIDATES),
    inst AS (SELECT DISTINCT CONNECTION_ID FROM PROD_DB.DBT_CSP.TAS_INSTALL_EXECUTION_CANDIDATES WHERE INSTALLATION_COMPLETED_AT IS NOT NULL),
    tr AS (SELECT cc.CONNECTION_ID, t.TO_STATE, t.REASON_CODE, t.OCCURRED_AT
           FROM PROD_DB.CSP_TAS_SERVICE_CSP_TAS_SERVICE.INSTALL_STATE_TRANSITION_LOG t
           JOIN cc ON cc.EXECUTION_CANDIDATE_ID = t.EXECUTION_CANDIDATE_ID
           WHERE t._FIVETRAN_DELETED = FALSE AND t.OCCURRED_AT >= DATEADD(day, -35, CURRENT_TIMESTAMP())),
    final AS (SELECT CONNECTION_ID, TO_STATE, REASON_CODE, OCCURRED_AT FROM tr
              QUALIFY ROW_NUMBER() OVER (PARTITION BY CONNECTION_ID ORDER BY OCCURRED_AT DESC) = 1),
    failed AS (SELECT CONVERT_TIMEZONE('Asia/Kolkata', f.OCCURRED_AT)::DATE::STRING AS day,
                      f.CONNECTION_ID::STRING AS conn, f.REASON_CODE AS reason
               FROM final f
               WHERE f.CONNECTION_ID NOT IN (SELECT CONNECTION_ID FROM inst)
                 AND f.TO_STATE IN {FAIL_STATES}
                 AND CONVERT_TIMEZONE('Asia/Kolkata', f.OCCURRED_AT)::DATE >= '{start}')"""
    agg = metabase_sql(cte + " SELECT day, reason, COUNT(DISTINCT conn) n FROM failed GROUP BY 1,2")

    days = _daterange(start, yday)
    daily = {d: {"day": d, "csp": 0, "wiom": 0, "customer": 0} for d in days}
    for r in agg:
        owner = _fail_owner(r["reason"])[0]
        d = str(r["day"])[:10]
        if d in daily and owner in ("csp", "wiom", "customer"):
            daily[d][owner] += r["n"] or 0
    for v in daily.values():
        v["total"] = v["csp"] + v["wiom"] + v["customer"]

    lst = metabase_sql(cte + f""" SELECT day, conn, reason FROM failed
        WHERE day = '{yday}' AND (reason ILIKE '%service not available%' OR reason ILIKE '%network setup%'
           OR reason ILIKE '%device%' OR reason ILIKE '%schedul%' OR reason ILIKE '%coverage%'
           OR reason ILIKE '%backhaul%' OR reason ILIKE '%routing%')
        ORDER BY conn""")
    wiom = []
    for r in lst:
        owner, bucket, why = _fail_owner(r["reason"])
        if owner != "wiom":
            continue
        wiom.append({"day": str(r["day"])[:10], "conn": r["conn"], "bucket": bucket,
                     "reason": r["reason"], "why": why})
    return {"daily": [daily[d] for d in days], "wiom_list": wiom, "through": yday}


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
            # No cohort argument by design — the board this reproduces has no CSP
            # segment, so this reads all users.
            payload["banner"] = compute_banner()
        except Exception as e:
            traceback.print_exc()
            payload["meta"]["errors"].append(f"banner: {type(e).__name__}: {e}")
            payload["banner"] = prev.get("banner")

        try:
            # Sehat MG sign-up feed (second initiative). Awaiting-data until the
            # campaign's CleverTap events sync; renders regardless.
            payload["sehat"] = compute_sehat_funnel()
        except Exception as e:
            traceback.print_exc()
            payload["meta"]["errors"].append(f"sehat: {type(e).__name__}: {e}")
            payload["sehat"] = prev.get("sehat")

        try:
            # Observation layer — day-on-day OP/SLA trend per Sehat cohort.
            payload["sehat_quality"] = compute_sehat_quality()
        except Exception as e:
            traceback.print_exc()
            payload["meta"]["errors"].append(f"sehat_quality: {type(e).__name__}: {e}")
            payload["sehat_quality"] = prev.get("sehat_quality")

        try:
            payload["feedback"] = compute_feedback(enrolled)
            latest = payload["feedback"].pop("latest", {})
        except Exception as e:
            traceback.print_exc()
            payload["meta"]["errors"].append(f"feedback: {type(e).__name__}: {e}")
            payload["feedback"] = prev.get("feedback")
            latest = {}

        try:
            # Always compute — the moved/ignition/demand + install-behaviour
            # analysis is task-activity based and needs no belief data. Only the
            # by-belief split degrades to 'no_response' when the belief-check
            # source (mbg_screen_log) is empty; don't blank the whole card for it.
            coh = compute_cohort(enrolled, latest or {})
            payload["ignition"] = coh.pop("_ignition", None)
            payload["gate"] = coh.pop("_gate", None)
            payload["cohort"] = coh
        except Exception as e:
            traceback.print_exc()
            payload["meta"]["errors"].append(f"cohort: {type(e).__name__}: {e}")
            payload["cohort"] = prev.get("cohort")
            payload["ignition"] = prev.get("ignition")
            payload["gate"] = prev.get("gate")

        try:
            payload["l1"] = compute_l1(enrolled)
        except Exception as e:
            traceback.print_exc()
            payload["meta"]["errors"].append(f"L1: {type(e).__name__}: {e}")
            payload["l1"] = prev.get("l1")

        try:
            payload["failures"] = compute_failures()
        except Exception as e:
            traceback.print_exc()
            payload["meta"]["errors"].append(f"failures: {type(e).__name__}: {e}")
            payload["failures"] = prev.get("failures")

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
            elif path.startswith("/files/"):
                data, ctype = storage_download(path[len("/files/"):])
                self._send(200, data, ctype)
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
            if path == "/api/upload":
                out = handle_upload(self._json_body(), self._email() or None)
                return self._send(200, json.dumps(out).encode(), "application/json")
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
