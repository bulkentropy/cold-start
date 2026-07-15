-- MBG push-click funnel (L0 · reach). Per mbg_click_funnel_LOGIC.md.
--
-- WHAT A "CLICK" IS HERE: a click on CleverTap campaign 1780477786, whose
-- Campaign type = Push (Android channel wiom_task_alerts). This is the MBG
-- PUSH-NOTIFICATION click, NOT the in-app home-banner tap. The in-app card is a
-- different set of InApp campaigns, one PER SCREEN (secured / almost / keep-going
-- / no-leads), each of which mints a new id when recreated — see §7 of the logic
-- doc. Do not relabel this "in-app" without switching the id inventory.
--
-- Grain of the atomic CTE = one CSP x one IST day -> clicks; everything else
-- aggregates it. Pre-aggregated here on purpose: the raw CSP x day grain is
-- ~464 CSPs x N days and would breach Metabase's ~2000-row native cap.
--
-- CAMPAIGN IDS ARE AN INVENTORY, NOT A CONSTANT: if the push is stopped and
-- recreated CleverTap mints a new id and this silently reads ~zero. Add the new
-- id to CLICK_CAMPAIGN_IDS in server.py and both are SUMmed.
--
-- {PARTNER_IN_LIST} {CAMPAIGN_IDS} {START_DATE} {LAST3_START} {LAST3_END} substituted at run time.
WITH universe AS (   -- targeted = the enrolled MBG cohort, minus 2 test CSPs. Dynamic: grows with enrolment.
    SELECT DISTINCT CSP_ID AS csp_id
    FROM PROD_DB.CSP_GATEWAY_SERVICE_CSP_GATEWAY_SERVICE.CSP_ACCOUNT
    WHERE _fivetran_active = TRUE
      AND CSP_ID NOT IN ('a0a0b1','a0a6w1')
      AND PARTNER_ID::TEXT IN ({PARTNER_IN_LIST})
),
clk AS (   -- the one atomic source. EVENTS_DATA has no CSP id: the PROFILE_DATA
    -- join on CLEVERTAP_ID is the only way to attribute a click to a CSP.
    -- TIMESTAMP is UTC; +330min BEFORE TO_DATE or clicks land on the wrong day near midnight.
    SELECT p.CSPID AS csp_id,
           TO_DATE(DATEADD(minute, 330, e.TIMESTAMP)) AS click_day_ist,
           COUNT(*) AS clicks
    FROM PROD_DB.CLEVERTAP_CSP_API.EVENTS_DATA e
    JOIN PROD_DB.CLEVERTAP_CSP_API.PROFILE_DATA p
      ON e.CLEVERTAP_ID = p.CLEVERTAP_ID
    WHERE e.EVENT_NAME = 'Notification Clicked'
      AND SPLIT_PART(TRY_PARSE_JSON(e.PROPERTIES):wzrk_id::string, '_', 1) IN ({CAMPAIGN_IDS})
      AND TO_DATE(DATEADD(minute, 330, e.TIMESTAMP)) >= '{START_DATE}'
      AND p.CSPID IN (SELECT csp_id FROM universe)
    GROUP BY 1, 2
)
-- day = IST click day: unique clickers + total clicks (the daily bars)
SELECT 'daily' AS mode, click_day_ist::STRING AS k,
       COUNT(DISTINCT csp_id) AS clickers, SUM(clicks) AS clicks, NULL AS targeted
FROM clk GROUP BY 2
UNION ALL
-- whole-window funnel: targeted / clickers / total clicks. never_clicked and
-- avg_per_clicker are derived server-side from these.
SELECT 'total', NULL, COUNT(DISTINCT csp_id), SUM(clicks), (SELECT COUNT(*) FROM universe)
FROM clk
UNION ALL
-- HIGHLIGHT: unique CSPs who clicked in the last 3 COMPLETE IST days. Must be its
-- own COUNT(DISTINCT) over the window — summing the daily clickers would
-- double-count any CSP that clicked on more than one of the three days.
SELECT 'last3', NULL, COUNT(DISTINCT csp_id), SUM(clicks), (SELECT COUNT(*) FROM universe)
FROM clk WHERE click_day_ist BETWEEN '{LAST3_START}'::DATE AND '{LAST3_END}'::DATE
UNION ALL
-- clicks-per-CSP histogram, bucketed 1..9 then 10+
SELECT 'hist', LEAST(t, 10)::STRING, COUNT(*), NULL, NULL
FROM (SELECT csp_id, SUM(clicks) AS t FROM clk GROUP BY 1)
GROUP BY 2
ORDER BY 1, 2;
