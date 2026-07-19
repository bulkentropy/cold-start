-- Sehat MG enrollment funnel (sign-up feed) — recreation of the campaign-tracking
-- dashboard at vikaswiom.github.io/wiom-csp-guarantee-campaign, computed live from
-- the warehouse instead of a data.json / CleverTap-API pull.
--
-- The campaign fires six CleverTap events as a CSP walks the opt-in flow, each
-- carrying an `offer_id` property that splits the two cohorts:
--   sehat_optical  = Track A · Ilaaj      (Optical Power low — heal the plant)
--   sehat_sla      = Track B · Fit rakhna (Service SLA poor — Samasya ka Samadhan)
-- Counted as UNIQUE CSP IDs (PROFILE_DATA.CSPID), not identities: one CSP has
-- several staff logins (OWNER / MANAGER / tech), exactly as the source notes.
--
-- AWAITING-DATA BY DESIGN: as of 19 Jul these events are not yet in EVENTS_DATA
-- (launch was 16 Jul; the source itself is still on its sample fallback). This
-- query returns zero rows until the feed lands, and the tab renders an
-- "awaiting sign-up feed" state rather than fake zeros — matching the provision.
-- The moment Fivetran syncs the events, every number here goes live automatically.
-- If the real event names or the offer_id property key differ from the source's
-- stated shape, this reads empty (same as now) until corrected here — no bad data.
--
-- offer_id lives in the event PROPERTIES JSON. TIMESTAMP is UTC → +330min BEFORE
-- TO_DATE or events land on the wrong IST day near midnight.
--
-- {START_DATE} substituted at run time.
WITH ev AS (
    SELECT p.CSPID                                                AS csp_id,
           LOWER(TRIM(TRY_PARSE_JSON(e.PROPERTIES):offer_id::string)) AS offer,
           e.EVENT_NAME                                          AS ename,
           TO_DATE(DATEADD(minute, 330, e.TIMESTAMP))            AS day_ist
    FROM PROD_DB.CLEVERTAP_CSP_API.EVENTS_DATA e
    JOIN PROD_DB.CLEVERTAP_CSP_API.PROFILE_DATA p
      ON e.CLEVERTAP_ID = p.CLEVERTAP_ID
    WHERE e.EVENT_NAME IN ('Sehat_View_education','Sehat_Learn_More','Sehat_View_plan',
                           'Sehat_Start_Quiz','Sehat_Quiz_Complete','Sehat_OptIn')
      AND TO_DATE(DATEADD(minute, 330, e.TIMESTAMP)) >= '{START_DATE}'
)
-- per funnel step: distinct CSPs who reached that event, per cohort
SELECT 'step' AS mode, offer, ename AS k,
       COUNT(DISTINCT csp_id) AS a, NULL AS b
FROM ev WHERE offer IN ('sehat_optical','sehat_sla')
GROUP BY offer, ename
UNION ALL
-- per IST day: reached (View_education) + enrolled (OptIn), per cohort — the trend
SELECT 'daily', offer, day_ist::STRING,
       COUNT(DISTINCT CASE WHEN ename = 'Sehat_View_education' THEN csp_id END),
       COUNT(DISTINCT CASE WHEN ename = 'Sehat_OptIn'          THEN csp_id END)
FROM ev WHERE offer IN ('sehat_optical','sehat_sla')
GROUP BY offer, day_ist
ORDER BY 1, 2, 3;
