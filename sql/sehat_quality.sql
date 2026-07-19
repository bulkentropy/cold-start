-- Sehat MG observation layer — day-on-day quality trend per cohort.
-- Reproduces Metabase card 11616's metric logic (Priority-CSP quality), but as a
-- ROLLING per-day series instead of a single point-in-time value, so we can watch
-- each cohort's number move across the Sehat cycle. Matches what the CSP sees
-- in-app ('सेवा स्थिति'): a rolling window, HIGH = GOOD.
--
--   Optical Power (Track A) = SUM(OPTICAL_NUMERATOR)/SUM(OPTICAL_DENOMINATOR) over
--     the last 15 telemetry days ending each day D. Numerator = in-range/OK pings.
--   Service SLA  (Track B) = COUNT_IF(RESOLVED_WITHIN_TAT)/COUNT(*) complaints over
--     the 60-day window ending each day D (4-hour TAT).
--
-- Unified as SUM(good)/NULLIF(SUM(total),0) per (CSP, day), then MEDIAN across the
-- cohort per day. {GROUP_CASE} tags each CSP for the opted-vs-not split; until the
-- opt-in list is wired it is the constant 'all', giving one whole-cohort line.
--
-- {TABLE} {GOOD} {TOTAL} {DATECOL} {WINDOW} {CSP_IN} {GROUP_CASE} {OBS_START} {OBS_END} {NDAYS} at run time.
WITH cohort_day AS (   -- one row per (CSP, day, good, total) inside the observation reach
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
spine AS (   -- every IST day in the observation window
    SELECT DATEADD(day, SEQ4(), '{OBS_START}'::date) AS d
    FROM TABLE(GENERATOR(ROWCOUNT => {NDAYS}))
),
per_csp AS (   -- rolling {WINDOW}-day ratio per CSP per day = the number that CSP sees
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
SELECT d::STRING       AS day,
       grp,
       MEDIAN(pct)     AS median_pct,
       AVG(pct)        AS mean_pct,
       COUNT(*)        AS n_csps
FROM per_csp
WHERE pct IS NOT NULL
GROUP BY d, grp
ORDER BY d, grp;
