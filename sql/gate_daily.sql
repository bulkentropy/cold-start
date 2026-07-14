-- Day-on-day MG-gate cohort movement, month-to-date. One row per day
-- (MONTH_START .. TODAY) with the count of enrolled CSPs in each gate state AS OF
-- that day (cumulative): above / below / pending (confirmed, none matured yet) /
-- no_leads. Mirrors l1_status gate logic (MG_calculation_logic.md), reconstructed
-- per day so the last row equals the live snapshot.
-- {PARTNER_IN_LIST} {MONTH_START} {TODAY} {ENROLLED_N} substituted at run time.
WITH mg AS (
    SELECT CSP_ID, PARTNER_ID
    FROM PROD_DB.CSP_GATEWAY_SERVICE_CSP_GATEWAY_SERVICE.CSP_ACCOUNT
    WHERE _fivetran_active = TRUE AND PARTNER_ID IN ({PARTNER_IN_LIST})
    QUALIFY ROW_NUMBER() OVER (PARTITION BY CSP_ID ORDER BY 1) = 1
),
tt AS (
    SELECT mg.PARTNER_ID AS partner_id,
      c.CONFIRMED_SLOT_AT AS confirmed_at,
      (c.INSTALLATION_COMPLETED_AT IS NOT NULL OR c.OTP_VERIFIED = TRUE OR c.COMPLETED_STEP >= 7) AS is_installed,
      (c.CURRENT_STATE IN ('TECHNICIAN_ASSIGNED','AWAITING_TECHNICIAN_ASSIGNMENT','ARRIVED_AT_SITE',
           'INSTALLATION_IN_PROGRESS_POST_FEE','AWAITING_CUSTOMER_OTP','FEE_COLLECTION_PENDING',
           'AWAITING_CUSTOMER_SLOT_CONFIRMATION')
       AND NOT (c.INSTALLATION_COMPLETED_AT IS NOT NULL OR c.OTP_VERIFIED = TRUE OR c.COMPLETED_STEP >= 7)) AS is_open,
      (c.CURRENT_STATE = 'CANCELLED_BY_UPSTREAM'
       AND COALESCE(c.FAILURE_SUBREASON_CODE,'') <> 'CSP_NO_SHOW'
       AND COALESCE(c.REASON_CODE,'') NOT ILIKE '%P41%'
       AND COALESCE(c.REASON_CODE,'') NOT ILIKE '%P74%') AS is_system,
      COALESCE(c.INSTALLATION_COMPLETED_AT, c.UPDATED_AT) AS final_state_at
    FROM PROD_DB.DBT_CSP.TAS_INSTALL_EXECUTION_CANDIDATES c
    JOIN mg ON mg.CSP_ID = c.CSP_ID
    WHERE c.ETL_CURRENT = TRUE AND c.CONFIRMED_SLOT_AT IS NOT NULL
),
leads AS (   -- confirmed universe for this month: matured-in-month OR still open (exclude system + June-matured)
    SELECT partner_id,
      GREATEST(TO_DATE(DATEADD(minute,330,confirmed_at)), '{MONTH_START}'::DATE) AS conf_day,
      IFF(is_open, NULL, TO_DATE(DATEADD(minute,330,final_state_at))) AS fs_day,
      is_installed
    FROM tt
    WHERE NOT is_system
      AND (is_open OR TO_DATE(DATEADD(minute,330,final_state_at)) >= '{MONTH_START}'::DATE)
),
days AS (
    SELECT day FROM (
        SELECT DATEADD(day, SEQ4(), '{MONTH_START}'::DATE) AS day
        FROM TABLE(GENERATOR(ROWCOUNT => 40))
    ) WHERE day <= '{TODAY}'::DATE
),
per AS (   -- per (day, CSP): matured (recv) and installed (inst) so far
    SELECT d.day, l.partner_id,
      COUNT_IF(l.fs_day IS NOT NULL AND l.fs_day <= d.day) AS recv,
      COUNT_IF(l.is_installed AND l.fs_day <= d.day) AS inst
    FROM days d JOIN leads l ON l.conf_day <= d.day
    GROUP BY 1, 2
)
SELECT d.day::STRING AS day,
  COUNT_IF(p.recv > 0 AND p.inst >= 0.6 * p.recv) AS above,
  COUNT_IF(p.recv > 0 AND p.inst <  0.6 * p.recv) AS below,
  COUNT_IF(p.recv = 0) AS pending,
  {ENROLLED_N} - COUNT(p.partner_id) AS no_leads
FROM days d LEFT JOIN per p ON p.day = d.day
GROUP BY 1 ORDER BY 1;
