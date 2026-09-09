-- Day-on-day MG-gate cohort movement, month-to-date. One row per day
-- (MONTH_START .. TODAY) with the count of enrolled CSPs in each gate state AS OF
-- that day (cumulative). Mirrors the AUGUST 2026 l1_status gate rule:
--   denominator = leads that reached TECH ASSIGNED (EXECUTOR_ID set)
--   numerator   = of those, installed (any install)
--   grain       = one row per (CONNECTION_ID, CSP_ID); re-allotments collapse
--   month       = SEP 2026: the lead's real terminal events, not UPDATED_AT (BUG-571)
--                 numerator   -> INSTALLATION_COMPLETED_AT
--                 denominator -> GREATEST(INSTALLATION_COMPLETED_AT,
--                                         FAILURE_REPORTED_AT, DISMISSED_AT)
-- States: above / below / not_dispatched (customer confirmed, no technician ever
-- assigned — outside the metric under the new rule) / no_leads.
-- Each measure now enters the running counts on ITS OWN event day, so the reconstruction
-- is a real timeline rather than the old approximation where a pair appeared on whatever
-- day its row was last written. A dispatched lead with no terminal event yet is in no
-- day's denominator until it terminates.
-- {PARTNER_IN_LIST} {MONTH_START} {TODAY} {ENROLLED_N} substituted at run time.
WITH mg AS (
    SELECT CSP_ID, PARTNER_ID
    FROM PROD_DB.CSP_GATEWAY_SERVICE_CSP_GATEWAY_SERVICE.CSP_ACCOUNT
    WHERE _fivetran_active = TRUE AND PARTNER_ID IN ({PARTNER_IN_LIST})
    QUALIFY ROW_NUMBER() OVER (PARTITION BY CSP_ID ORDER BY 1) = 1
),
pairs AS (
    SELECT mg.PARTNER_ID AS partner_id, c.CONNECTION_ID,
      MAX(IFF(c.INSTALLATION_COMPLETED_AT IS NOT NULL, 1, 0))          AS has_installed,
      MAX(IFF(c.EXECUTOR_ID IS NOT NULL, 1, 0))                        AS tech_assigned,
      MAX(IFF(c.CONFIRMED_SLOT_AT IS NOT NULL
              OR c.CURRENT_STATE = 'AWAITING_TECHNICIAN_ASSIGNMENT', 1, 0)) AS was_confirmed,
      TO_DATE(DATEADD(minute, 330, MAX(c.INSTALLATION_COMPLETED_AT)))  AS install_d,
      TO_DATE(DATEADD(minute, 330, MIN(c.CREATED_AT)))                 AS created_d,
      {TERMINAL_D}                                                     AS terminal_d
    FROM PROD_DB.CSP_TAS_SERVICE_CSP_TAS_SERVICE.INSTALL_EXECUTION_CANDIDATES c
    JOIN mg ON mg.CSP_ID = c.CSP_ID
    WHERE c._FIVETRAN_ACTIVE
    GROUP BY 1, 2
),
-- Terminated this month, OR still running at all. In-flight work is not bounded by the
-- month: a job dispatched in August and still open today is in flight today, and the
-- snapshot counts it that way — so the daily series must too, or the last row of the
-- chart will not equal the live tiles above it.
elig AS (SELECT * FROM pairs
         WHERE terminal_d >= '{MONTH_START}'::DATE
            OR terminal_d IS NULL),
days AS (
    SELECT day FROM (
        SELECT DATEADD(day, SEQ4(), '{MONTH_START}'::DATE) AS day
        FROM TABLE(GENERATOR(ROWCOUNT => 40))
    ) WHERE day <= '{TODAY}'::DATE
),
-- Each measure accrues on its own event date: a lead joins the denominator the day it
-- terminates, and the numerator the day it was installed.
per AS (
    SELECT d.day, e.partner_id,
      COUNT_IF(e.tech_assigned = 1 AND e.terminal_d <= d.day)                     AS recv,
      COUNT_IF(e.tech_assigned = 1 AND e.has_installed = 1
               AND e.install_d >= '{MONTH_START}'::DATE AND e.install_d <= d.day) AS inst,
      -- dispatched and unfinished as of that day: raised by then, terminal event
      -- either absent or still in the future
      COUNT_IF(e.tech_assigned = 1 AND e.created_d <= d.day
               AND (e.terminal_d IS NULL OR e.terminal_d > d.day))                AS pend,
      COUNT_IF(e.tech_assigned = 0 AND e.was_confirmed = 1
               AND e.terminal_d <= d.day)                                         AS nodisp
    FROM days d JOIN elig e
      ON e.created_d <= d.day OR e.terminal_d <= d.day
    GROUP BY 1, 2
)
SELECT d.day::STRING AS day,
  COUNT_IF(p.recv > 0 AND p.inst >= 0.6 * p.recv) AS above,
  COUNT_IF(p.recv > 0 AND p.inst <  0.6 * p.recv) AS below,
  COUNT_IF(p.recv = 0 AND p.pend > 0)             AS in_flight,
  COUNT_IF(p.recv = 0 AND p.pend = 0 AND p.nodisp > 0) AS not_dispatched,
  {ENROLLED_N} - COUNT_IF(p.recv > 0 OR p.pend > 0 OR p.nodisp > 0) AS no_leads
FROM days d LEFT JOIN per p ON p.day = d.day
GROUP BY 1 ORDER BY 1;
