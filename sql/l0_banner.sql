-- In-app HOME-BANNER opens (L0 · reach). Sibling of l0_clicks.sql (the PUSH card).
--
-- ⚠ READ THIS BEFORE USING THE NUMBER: this counts ALL home banners, not just the
-- MBP/MBG status banner. It is a SUPERSET. Do not label it "MBG banner clicks".
--
-- Why it cannot be narrowed (verified against the warehouse, 15 Jul):
--   • The MBP banner is a NATIVE app component, not a CleverTap campaign. It emits
--     the custom event `banner_opened`. (The CleverTap InApp campaigns listed in §7
--     of mbg_click_funnel_LOGIC.md are NOT this banner: 5 of those 7 ids emit zero
--     'Notification Clicked' at all, and they predate the banner's 9-Jul go-live.
--     They are the in-app comms/education popups.)
--   • `banner_opened` carries a `banner` property, but it is a PER-CSP UUID: all 619
--     distinct UUIDs seen since 10 Jul map to exactly ONE CSP each. There is no
--     shared banner/template id in the event, so nothing in CleverTap identifies an
--     open as the MBP banner. That mapping lives in the banner backend (Ashish/i2e1).
--   • `banner_opened` also predates MBP (~115 CSPs/day on 5 Jul, before go-live), so
--     the series has a non-MBP floor. The MBP go-live (9 Jul) is marked on the chart:
--     the step up from ~90 to ~187 CSPs/day across 9→10 Jul IS the MBP banner
--     landing, and reconciles with Ashish's reported 232 CSPs / 600 clicks.
--
-- THE FIX, when someone wants an MBP-only number: get the banner-UUID → banner-type
-- mapping from the banner backend, or (better, durable) have a `banner_type` property
-- added to the `banner_opened` event. Then filter here and drop this caveat.
--
-- There is NO impression event for the native banner, so unlike a CleverTap campaign
-- this surface cannot yield a CTR — only opens and unique openers.
--
-- {PARTNER_IN_LIST} {START_DATE} {LAST3_START} {LAST3_END} substituted at run time.
WITH universe AS (
    SELECT DISTINCT CSP_ID AS csp_id
    FROM PROD_DB.CSP_GATEWAY_SERVICE_CSP_GATEWAY_SERVICE.CSP_ACCOUNT
    WHERE _fivetran_active = TRUE
      AND CSP_ID NOT IN ('a0a0b1','a0a6w1')
      AND PARTNER_ID::TEXT IN ({PARTNER_IN_LIST})
),
ev AS (   -- one row per banner open by an enrolled CSP. TIMESTAMP is UTC: +330min
    -- BEFORE TO_DATE or opens land on the wrong IST day near midnight.
    SELECT p.CSPID AS csp_id,
           TO_DATE(DATEADD(minute, 330, e.TIMESTAMP)) AS day_ist
    FROM PROD_DB.CLEVERTAP_CSP_API.EVENTS_DATA e
    JOIN PROD_DB.CLEVERTAP_CSP_API.PROFILE_DATA p
      ON e.CLEVERTAP_ID = p.CLEVERTAP_ID
    WHERE e.EVENT_NAME = 'banner_opened'
      AND TO_DATE(DATEADD(minute, 330, e.TIMESTAMP)) >= '{START_DATE}'
      AND p.CSPID IN (SELECT csp_id FROM universe)
)
SELECT 'daily' AS mode, day_ist::STRING AS k,
       COUNT(DISTINCT csp_id) AS openers, COUNT(*) AS opens, NULL AS targeted
FROM ev GROUP BY 2
UNION ALL
SELECT 'total', NULL, COUNT(DISTINCT csp_id), COUNT(*), (SELECT COUNT(*) FROM universe)
FROM ev
UNION ALL
-- HIGHLIGHT: unique CSPs who opened a banner in the last 3 COMPLETE IST days. Own
-- COUNT(DISTINCT) over the window — summing the daily bars double-counts anyone who
-- opened on more than one of the three days.
SELECT 'last3', NULL, COUNT(DISTINCT csp_id), COUNT(*), (SELECT COUNT(*) FROM universe)
FROM ev WHERE day_ist BETWEEN '{LAST3_START}'::DATE AND '{LAST3_END}'::DATE
UNION ALL
-- opens-per-CSP histogram (1..9, 10+)
SELECT 'hist', LEAST(t, 10)::STRING, COUNT(*), NULL, NULL
FROM (SELECT csp_id, COUNT(*) AS t FROM ev GROUP BY 1)
GROUP BY 2
ORDER BY 1, 2;
