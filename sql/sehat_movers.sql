-- Sehat MG observation layer · per-CSP movement drill-down.
-- Same rolling-window logic as sehat_quality.sql, but instead of the daily cohort
-- median it returns ONE row per CSP: their rolling metric on the first vs the last
-- observed day of the window, and the delta (points). The server buckets these into
-- improved / dropped / flat and sums the quantum of movement.
--
-- first_pct / last_pct use MIN_BY/MAX_BY over the day, so "first" is the earliest day
-- that CSP has a computable rolling value and "last" is the most recent — a CSP with
-- sparse early telemetry is measured over a shorter span (noted in the UI).
--
-- {TABLE} {GOOD} {TOTAL} {DATECOL} {WINDOW} {CSP_IN} {GROUP_CASE} {OBS_START} {OBS_END} {NDAYS} at run time.
WITH cohort_day AS (
    SELECT CSP_ID,
           TO_DATE({DATECOL}) AS obs_date,
           {GOOD}  AS good,
           {TOTAL} AS total
    FROM PROD_DB.CSP_QUALITY_SERVICE_CSP_QUALITY_SERVICE.{TABLE}
    WHERE _FIVETRAN_ACTIVE
      AND CSP_ID IN ({CSP_IN})
      AND TO_DATE({DATECOL}) >  DATEADD(day, -{WINDOW}, '{OBS_START}'::date)
      AND TO_DATE({DATECOL}) <= '{OBS_END}'::date
),
spine AS (
    SELECT DATEADD(day, SEQ4(), '{OBS_START}'::date) AS d
    FROM TABLE(GENERATOR(ROWCOUNT => {NDAYS}))
),
per_csp AS (
    SELECT s.d,
           t.CSP_ID,
           {GROUP_CASE} AS grp,
           SUM(t.good) / NULLIF(SUM(t.total), 0) * 100 AS pct
    FROM spine s
    JOIN cohort_day t
      ON t.obs_date >  DATEADD(day, -{WINDOW}, s.d)
     AND t.obs_date <= s.d
    GROUP BY s.d, t.CSP_ID
)
SELECT CSP_ID                              AS csp_id,
       ANY_VALUE(grp)                      AS grp,
       ROUND(MIN_BY(pct, d), 1)            AS first_pct,
       ROUND(MAX_BY(pct, d), 1)            AS last_pct,
       ROUND(MAX_BY(pct, d) - MIN_BY(pct, d), 1) AS delta,
       COUNT(*)                            AS days_observed
FROM per_csp
WHERE pct IS NOT NULL
GROUP BY CSP_ID
ORDER BY delta DESC;
