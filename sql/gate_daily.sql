-- Day-on-day MG-gate cohort movement, month-to-date. One row per day
-- (MONTH_START .. TODAY) with the count of enrolled CSPs in each gate state AS OF
-- that day (cumulative). Mirrors the AUGUST 2026 l1_status gate rule:
--   denominator = leads that reached TECH ASSIGNED (EXECUTOR_ID set)
--   numerator   = of those, installed (any install)
--   grain       = one row per (CONNECTION_ID, CSP_ID); re-allotments collapse
--   month       = TO_DATE(IST(MAX(UPDATED_AT))) >= MONTH_START
-- States: above / below / not_dispatched (customer confirmed, no technician ever
-- assigned — outside the metric under the new rule) / no_leads.
-- ⚠ Reconstruction is approximate in the same way the previous version was: a pair
-- enters the running counts on its FINAL update day, because MAX(UPDATED_AT) is the
-- only date the rule exposes. The last row therefore equals the live snapshot exactly,
-- but earlier days understate activity that has since been overwritten.
-- {PARTNER_IN_LIST} {MONTH_START} {TODAY} {ENROLLED_N} substituted at run time.
WITH mg AS (
    SELECT CSP_ID, PARTNER_ID
    FROM PROD_DB.CSP_GATEWAY_SERVICE_CSP_GATEWAY_SERVICE.CSP_ACCOUNT
    WHERE _fivetran_active = TRUE AND PARTNER_ID IN ({PARTNER_IN_LIST})
    QUALIFY ROW_NUMBER() OVER (PARTITION BY CSP_ID ORDER BY 1) = 1
),
pairs AS (
    SELECT mg.PARTNER_ID AS partner_id, c.CONNECTION_ID,
      MAX(IFF(c.OTP_VERIFIED = TRUE OR c.INSTALLATION_COMPLETED_AT IS NOT NULL
              OR c.COMPLETED_STEP >= 7, 1, 0))                         AS has_installed,
      MAX(IFF(c.EXECUTOR_ID IS NOT NULL, 1, 0))                        AS tech_assigned,
      MAX(IFF(c.CONFIRMED_SLOT_AT IS NOT NULL
              OR c.CURRENT_STATE = 'AWAITING_TECHNICIAN_ASSIGNMENT', 1, 0)) AS was_confirmed,
      TO_DATE(DATEADD(minute, 330, MAX(c.UPDATED_AT)))                 AS last_date
    FROM PROD_DB.CSP_TAS_SERVICE_CSP_TAS_SERVICE.INSTALL_EXECUTION_CANDIDATES c
    JOIN mg ON mg.CSP_ID = c.CSP_ID
    WHERE c._FIVETRAN_ACTIVE
    GROUP BY 1, 2
),
elig AS (SELECT * FROM pairs WHERE last_date >= '{MONTH_START}'::DATE),
days AS (
    SELECT day FROM (
        SELECT DATEADD(day, SEQ4(), '{MONTH_START}'::DATE) AS day
        FROM TABLE(GENERATOR(ROWCOUNT => 40))
    ) WHERE day <= '{TODAY}'::DATE
),
per AS (
    SELECT d.day, e.partner_id,
      COUNT_IF(e.tech_assigned = 1)                            AS recv,
      COUNT_IF(e.has_installed = 1)                            AS inst,
      COUNT_IF(e.tech_assigned = 0 AND e.was_confirmed = 1)    AS nodisp
    FROM days d JOIN elig e ON e.last_date <= d.day
    GROUP BY 1, 2
)
SELECT d.day::STRING AS day,
  COUNT_IF(p.recv > 0 AND p.inst >= 0.6 * p.recv) AS above,
  COUNT_IF(p.recv > 0 AND p.inst <  0.6 * p.recv) AS below,
  COUNT_IF(p.recv = 0 AND p.nodisp > 0)           AS not_dispatched,
  {ENROLLED_N} - COUNT_IF(p.recv > 0 OR p.nodisp > 0) AS no_leads
FROM days d LEFT JOIN per p ON p.day = d.day
GROUP BY 1 ORDER BY 1;
