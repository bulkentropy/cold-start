# MG Program Dashboard — L0 funnels + L1 leading metrics

Self-refreshing local dashboard for the Minimum Guarantee program.

## Run

```
py server.py          # from this folder
```

Open http://localhost:8090. The page re-polls every 30 minutes; the server
caches all queries for 30 minutes. **Refresh Now** button (or `GET /refresh`)
forces a full re-query on demand. `GET /data` returns the raw JSON.

## What it shows

Two tabs. **L0 · Enrolment** replicates github.com/kushagraagarwal-11/mbg-tv-wall exactly:
- **Migration strip** — Cohort 2 → Cohort 1 graduations: launch size (100,
  frozen), graduated (audit completed, MG-campaign-scoped), % drained, remaining.
- **Two funnels** — distinct frozen-cohort CSPs per stage: App Opened (CleverTap
  "App Launched" since go-live via `partner_cspid_map.json`) → Banner Viewed
  ("InApp_Shown", per-flow campaign id) → screen beacons (Viewed hero →
  Education deal → Education guarantee → Quiz intro → Quiz started) → Enrolled.
  Flow-2 adds Opted-in → Audit started → Audit completed → Enrolled (strict).

**L1 · Leading Metrics** — definitions are **sacrosanct to Metabase Q11528**
(B2I funnel with attributed drops): anchor = booking confirmed
(`fct_booking_window`, test-LCO excluded), booking → connection (days=14
window) → **current** TAS task. A booking counts iff that task belongs to an
enrolled CSP (= "CSP received booking"). "CSP accepted / slot proposed" =
current depth ≥ 3; "slot confirmed by customer" = depth ≥ 4 (Q11528's ladder).
Speed = task created → slot accepted (ALLOCATION_ACCEPTED, timing only — TAS
stores no acceptance timestamp). Day-on-day since 24 Jun, complete IST days
only, pre (24–30 Jun avg, dashed) vs post (1 Jul+) deltas, with a toggle:
- **By booking day** (cohort): outcomes attach to the booking's confirm day.
- **By event day**: each event on its own day (% can exceed 100 on catch-up days).
A NSM strip (installs/day by enrolled CSPs, `INSTALLATION_COMPLETED_AT` IST
day, today + last 7 complete days) sits above the tabs on every view.

## Data sources

| Source | Used for |
|---|---|
| Supabase audit-tool (`gonqnxpdtvjydppbrnie`): `mg_optins`, `campaign_partners` | opt-ins, audit status, graduations, enrolled set |
| Supabase mbg-portal (`oobaxfbsmqhdaligebmg`): `mbg_audit_baseline`, `mbg_screen_log` | frozen Cohort-2, funnel stages |
| Metabase → Snowflake (db 113): `TAS_INSTALL_EXECUTION_CANDIDATES`, `CONNECTION_EVENT_HISTORY` | L1 (`sql/l1_daily.sql`, partner list injected per refresh) |

Secrets come from `C:\credentials\.env`: `METABASE_API_KEY` and
`PROD_SUPABASE_SERVICE_ROLE_KEY` (audit-tool). The mbg-portal is read with its
public publishable (anon) key; set `SUPABASE_PORTAL_SERVICE_KEY` to override.

## Known gaps / notes

1. Screen logging began ~11:57 IST on 1 Jul (~3 h after go-live), so beacon
   screen counts slightly undercount vs opt-ins (Enrolled can exceed them) —
   same caveat as the TV wall's tracker.
2. Local-only for now; to deploy, push to a repo and run on Railway like the
   TV dashboard (set the env vars on the service).
3. `data/frozen_cohort.json` + `data/partner_cspid_map.json` are copies from
   the mbg-tv-wall repo (cloned at `..\mbg-tv-wall`) — if Kushagra re-freezes
   the cohort, re-copy them.

`_mb.py` (ad-hoc Metabase runner), `_render_l1.py`, `sql/l1_daily.sql` and
`_q_*.sql` are validation scratch tools, not used by the server.
