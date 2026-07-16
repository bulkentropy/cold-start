-- MBG Board reproduction (L0 · reach) — in-app HOME-BANNER opens.
-- Per mbg_click_funnel_LOGIC.md: this mirrors CleverTap dashboard 1783687017
-- ("Wiom CSP → MBG Board"), whose three cards all read ONE event: `banner_opened`.
--
-- ⚠ ALL USERS, NOT THE ENROLLED COHORT. The board's cards carry NO CSP segment
-- (Event = banner_opened · Segment = All users · After Jul 09). This query matches
-- that on purpose, so the portal and the board tell the same story. It is therefore
-- NOT a cohort reach number and has no denominator — do not divide it by 466.
--
-- ⚠ SUPERSET: this counts ALL home banners, not just the MBP/MBG status banner.
-- Do not label it "MBG banner clicks". Why it cannot be narrowed (verified against
-- the warehouse, 15 Jul):
--   • The MBP banner is a NATIVE app component, not a CleverTap campaign. It emits
--     the custom event `banner_opened`, which carries no campaign / wzrk_id.
--   • `banner_opened` carries a `banner` property, but it is a PER-CSP UUID: every
--     distinct UUID maps to exactly ONE CSP. There is no shared banner/template id,
--     so nothing in CleverTap identifies an open as the MBP banner. That mapping
--     lives in the banner backend (Ashish/i2e1).
-- THE FIX, when someone wants an MBP-only number: get the banner-UUID → banner-type
-- mapping from the banner backend, or (better, durable) have a `banner_type` property
-- added to the `banner_opened` event. Then filter here and drop this caveat.
--
-- There is NO impression event for the native banner, so unlike a CleverTap campaign
-- this surface cannot yield a CTR — only opens and unique openers.
--
-- THREE WAYS TO COUNT "UNIQUE" — the card shows two of them; the board shows a third
-- it alone can compute:
--   uniq_csps  = COUNT(DISTINCT CSPID)       ≈ 360 — a CSP once (owner+admin merged)
--   uniq_users = COUNT(DISTINCT CLEVERTAP_ID) ≈ 468 — each identity separate
--   board card = CleverTap device-attributed  ≈ 398 — Mobile+Tablet; NOT reproducible
--                from the warehouse (CleverTap-internal Device Stats). It sits between
--                the two above, which is the tell that it is a third definition.
--
-- LEFT JOIN (not inner) on PROFILE_DATA is deliberate: `opens` must be the true
-- COUNT(*) of the event, so the daily bars sum exactly to the total. CSPID is NULL
-- for any unmatched identity and COUNT(DISTINCT) skips NULLs on its own.
--
-- Timezone: EVENTS_DATA.TIMESTAMP is UTC → +330min BEFORE TO_DATE, or opens land on
-- the wrong IST day near midnight. The board's date boundary uses the CleverTap
-- ACCOUNT timezone, not IST, so it starts a few hours later — this is most of the
-- ~3% gap vs the board and is expected, not a bug.
--
-- Pre-aggregated on purpose: the raw identity x day grain would breach Metabase's
-- ~2000-row native cap.
--
-- last3 / prev3 are the LEADING-INDICATOR card: unique CSPs engaging with the banner
-- over the last 3 COMPLETE IST days, and the 3 before that for direction. Each is its
-- OWN COUNT(DISTINCT) over its window — summing the daily bars would double-count any
-- CSP active on more than one day. Today is excluded from both: it is partial up to
-- the last Fivetran sync and would understate against a full day. (The tiles on the
-- board card DO include today, deliberately — that is board parity. These don't,
-- because a trend needs like-for-like windows. The two disagree on purpose.)
--
-- {START_DATE} {LAST3_START} {LAST3_END} {PREV3_START} {PREV3_END} substituted at run time.
WITH ev AS (   -- one row per banner open, by anyone (no cohort filter — see above)
    SELECT e.CLEVERTAP_ID AS ct_id,
           p.CSPID        AS csp_id,
           TO_DATE(DATEADD(minute, 330, e.TIMESTAMP)) AS day_ist
    FROM PROD_DB.CLEVERTAP_CSP_API.EVENTS_DATA e
    LEFT JOIN PROD_DB.CLEVERTAP_CSP_API.PROFILE_DATA p
      ON e.CLEVERTAP_ID = p.CLEVERTAP_ID
    WHERE e.EVENT_NAME = 'banner_opened'
      AND TO_DATE(DATEADD(minute, 330, e.TIMESTAMP)) >= '{START_DATE}'
)
-- day-on-day: opens + both unique definitions, one row per IST day
SELECT 'daily' AS mode, day_ist::STRING AS k,
       COUNT(*)                AS opens,
       COUNT(DISTINCT csp_id)  AS uniq_csps,
       COUNT(DISTINCT ct_id)   AS uniq_users
FROM ev GROUP BY 2
UNION ALL
-- whole-window totals = the board's "Total clikes on banner" + "Unique CSP ids"
SELECT 'total', NULL, COUNT(*), COUNT(DISTINCT csp_id), COUNT(DISTINCT ct_id)
FROM ev
UNION ALL
-- LEADING INDICATOR: unique CSPs engaging in the last 3 COMPLETE IST days...
SELECT 'last3', NULL, COUNT(*), COUNT(DISTINCT csp_id), COUNT(DISTINCT ct_id)
FROM ev WHERE day_ist BETWEEN '{LAST3_START}'::DATE AND '{LAST3_END}'::DATE
UNION ALL
-- ...and the 3 days before that, so the card can show direction rather than a level.
SELECT 'prev3', NULL, COUNT(*), COUNT(DISTINCT csp_id), COUNT(DISTINCT ct_id)
FROM ev WHERE day_ist BETWEEN '{PREV3_START}'::DATE AND '{PREV3_END}'::DATE
UNION ALL
-- "MBG- Banner metrics" histogram: opens PER USER (identity), bucketed 1..9 then 10+.
-- Per-user, not per-CSP, to match the board card.
SELECT 'hist', LEAST(n, 10)::STRING, NULL, NULL, COUNT(*)
FROM (SELECT ct_id, COUNT(*) AS n FROM ev GROUP BY 1)
GROUP BY 2
ORDER BY 1, 2;
