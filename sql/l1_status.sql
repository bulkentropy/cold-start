-- CSP status by TASK ACTIVITY. Two independent blocks, one row per enrolled CSP:
--
-- 1) Ignition 7-day windows (tb/ib = 24-30 Jun, ta/ia = 1-7 Jul): tasks CREATED
--    in the window + how many installed. Feeds the moved/ignition/demand card.
--    (A CSP working July tasks off a June booking counts as active in July.)
--
-- 2) Install-rate GATE, calendar-month-to-date. REVISED FOR AUGUST 2026 to match the
--    payout engine:
--      recv_m (DENOMINATOR) = leads that reached TECH ASSIGNED (EXECUTOR_ID set).
--      inst_m (NUMERATOR)   = of those, installed (any install; on-time is NOT tested).
--    Grain is one row per (CONNECTION_ID, CSP_ID) — a booking re-allotted to the SAME
--    CSP counts once. Month attribution is TO_DATE(IST(MAX(UPDATED_AT))) per pair.
--
--    Changes from the pre-August rule, all deliberate and all matching the payout side:
--      * denominator was "customer-confirmed lead that reached a final state"; it is now
--        the narrower "technician was assigned". Aug MTD this cuts the cohort denominator
--        ~58% and lifts the rate ~29 pts with an unchanged numerator.
--      * still-open jobs are no longer held out — a dispatched job not yet finished now
--        counts against the rate, so a mid-month reading is pessimistic and firms up.
--      * true system/upstream cancels are no longer excluded (immaterial: 67 of 4,279).
--      * nodisp_m below is NOT part of the rate. It exists because a confirmed lead that
--        never got a technician now leaves the metric entirely instead of counting as a
--        miss, and that failure must stay visible somewhere.
--    ⚠ MAX(UPDATED_AT) is a row-write timestamp, not a business event: a July install
--    whose row is touched in August moves into August, and a past month's number is not
--    reproducible later. Kept deliberately so the dashboard matches what actually pays.
--    {PARTNER_IN_LIST}/{MONTH_START} subst.
WITH mg AS (
    SELECT CSP_ID, PARTNER_ID
    FROM PROD_DB.CSP_GATEWAY_SERVICE_CSP_GATEWAY_SERVICE.CSP_ACCOUNT
    WHERE _fivetran_active = TRUE AND PARTNER_ID IN ({PARTNER_IN_LIST})
    QUALIFY ROW_NUMBER() OVER (PARTITION BY CSP_ID ORDER BY 1) = 1
),
tt AS (
    SELECT mg.PARTNER_ID AS partner_id,
           c.CREATED_AT AS created_at,
           c.CONFIRMED_SLOT_AT AS confirmed_at,
           (c.INSTALLATION_COMPLETED_AT IS NOT NULL OR c.OTP_VERIFIED = TRUE OR c.COMPLETED_STEP >= 7) AS is_installed,
           (c.CURRENT_STATE IN ('TECHNICIAN_ASSIGNED','AWAITING_TECHNICIAN_ASSIGNMENT','ARRIVED_AT_SITE',
                'INSTALLATION_IN_PROGRESS_POST_FEE','AWAITING_CUSTOMER_OTP','FEE_COLLECTION_PENDING',
                'AWAITING_CUSTOMER_SLOT_CONFIRMATION')
            AND NOT (c.INSTALLATION_COMPLETED_AT IS NOT NULL OR c.OTP_VERIFIED = TRUE OR c.COMPLETED_STEP >= 7)) AS is_open,
           -- true system/upstream cancel (not CSP's fault). CSP-fault upstream
           -- (CSP_NO_SHOW / timeout) keeps counting, per the MG doc.
           (c.CURRENT_STATE = 'CANCELLED_BY_UPSTREAM'
            AND COALESCE(c.FAILURE_SUBREASON_CODE,'') <> 'CSP_NO_SHOW'
            AND COALESCE(c.REASON_CODE,'') NOT ILIKE '%P41%'
            AND COALESCE(c.REASON_CODE,'') NOT ILIKE '%P74%') AS is_system,
           COALESCE(c.INSTALLATION_COMPLETED_AT, c.UPDATED_AT) AS final_state_at
    FROM PROD_DB.DBT_CSP.TAS_INSTALL_EXECUTION_CANDIDATES c
    JOIN mg ON mg.CSP_ID = c.CSP_ID
    WHERE c.ETL_CURRENT = TRUE
),
-- Gate grain (Aug 2026 rule): one row per (CONNECTION_ID, CSP_ID), so a booking
-- re-allotted to the SAME CSP counts once. Deliberately a different grain from `tt`
-- above, which stays task-level for the ignition windows.
pairs AS (
    SELECT mg.PARTNER_ID AS partner_id, c.CONNECTION_ID,
      MAX(IFF(c.OTP_VERIFIED = TRUE OR c.INSTALLATION_COMPLETED_AT IS NOT NULL
              OR c.COMPLETED_STEP >= 7, 1, 0))                         AS has_installed,
      MAX(IFF(c.EXECUTOR_ID IS NOT NULL, 1, 0))                        AS tech_assigned,
      MAX(IFF(c.CONFIRMED_SLOT_AT IS NOT NULL
              OR c.CURRENT_STATE = 'AWAITING_TECHNICIAN_ASSIGNMENT', 1, 0)) AS was_confirmed,
      MAX(IFF(c.CURRENT_STATE IN ('TECHNICIAN_ASSIGNED','ARRIVED_AT_SITE',
              'INSTALLATION_IN_PROGRESS_PRE_FEE','INSTALLATION_IN_PROGRESS_POST_FEE',
              'AWAITING_CUSTOMER_OTP','FEE_COLLECTION_PENDING'), 1, 0)) AS still_open,
      IFF(TO_DATE(DATEADD(minute, 330, MAX(c.UPDATED_AT))) >= '{MONTH_START}'::DATE, 1, 0) AS in_month
    FROM PROD_DB.CSP_TAS_SERVICE_CSP_TAS_SERVICE.INSTALL_EXECUTION_CANDIDATES c
    JOIN mg ON mg.CSP_ID = c.CSP_ID
    WHERE c._FIVETRAN_ACTIVE
    GROUP BY 1, 2
),
gate AS (
    SELECT partner_id,
      SUM(IFF(tech_assigned = 1 AND in_month = 1, 1, 0))                             AS recv_m,
      SUM(IFF(has_installed = 1 AND in_month = 1, 1, 0))                             AS inst_m,
      SUM(IFF(tech_assigned = 1 AND has_installed = 0 AND still_open = 1
              AND in_month = 1, 1, 0))                                               AS pend_m,
      SUM(IFF(tech_assigned = 0 AND was_confirmed = 1 AND in_month = 1, 1, 0))        AS nodisp_m
    FROM pairs GROUP BY 1
),
ign AS (
SELECT partner_id,
  COUNT_IF(created_at >= DATEADD(minute,-330,'2026-06-24 00:00:00'::TIMESTAMP_NTZ)
       AND created_at <  DATEADD(minute,-330,'2026-07-01 00:00:00'::TIMESTAMP_NTZ)) AS tb,
  COUNT_IF(created_at >= DATEADD(minute,-330,'2026-06-24 00:00:00'::TIMESTAMP_NTZ)
       AND created_at <  DATEADD(minute,-330,'2026-07-01 00:00:00'::TIMESTAMP_NTZ) AND is_installed) AS ib,
  COUNT_IF(created_at >= DATEADD(minute,-330,'2026-07-01 00:00:00'::TIMESTAMP_NTZ)
       AND created_at <  DATEADD(minute,-330,'2026-07-08 00:00:00'::TIMESTAMP_NTZ)) AS ta,
  COUNT_IF(created_at >= DATEADD(minute,-330,'2026-07-01 00:00:00'::TIMESTAMP_NTZ)
       AND created_at <  DATEADD(minute,-330,'2026-07-08 00:00:00'::TIMESTAMP_NTZ) AND is_installed) AS ia,
  COUNT_IF(created_at >= DATEADD(minute,-330,'2026-07-08 00:00:00'::TIMESTAMP_NTZ)
       AND created_at <  DATEADD(minute,-330,'2026-07-15 00:00:00'::TIMESTAMP_NTZ)) AS tc,
  COUNT_IF(created_at >= DATEADD(minute,-330,'2026-07-08 00:00:00'::TIMESTAMP_NTZ)
       AND created_at <  DATEADD(minute,-330,'2026-07-15 00:00:00'::TIMESTAMP_NTZ) AND is_installed) AS ic,
  COUNT_IF(created_at >= DATEADD(minute,-330,'2026-07-15 00:00:00'::TIMESTAMP_NTZ)
       AND created_at <  DATEADD(minute,-330,'2026-07-22 00:00:00'::TIMESTAMP_NTZ)) AS t4,
  COUNT_IF(created_at >= DATEADD(minute,-330,'2026-07-15 00:00:00'::TIMESTAMP_NTZ)
       AND created_at <  DATEADD(minute,-330,'2026-07-22 00:00:00'::TIMESTAMP_NTZ) AND is_installed) AS i4,
  COUNT_IF(created_at >= DATEADD(minute,-330,'2026-07-22 00:00:00'::TIMESTAMP_NTZ)
       AND created_at <  DATEADD(minute,-330,'2026-07-29 00:00:00'::TIMESTAMP_NTZ)) AS t5,
  COUNT_IF(created_at >= DATEADD(minute,-330,'2026-07-22 00:00:00'::TIMESTAMP_NTZ)
       AND created_at <  DATEADD(minute,-330,'2026-07-29 00:00:00'::TIMESTAMP_NTZ) AND is_installed) AS i5,
  COUNT_IF(created_at >= DATEADD(minute,-330,'2026-07-29 00:00:00'::TIMESTAMP_NTZ)
       AND created_at <  DATEADD(minute,-330,'2026-08-05 00:00:00'::TIMESTAMP_NTZ)) AS t6,
  COUNT_IF(created_at >= DATEADD(minute,-330,'2026-07-29 00:00:00'::TIMESTAMP_NTZ)
       AND created_at <  DATEADD(minute,-330,'2026-08-05 00:00:00'::TIMESTAMP_NTZ) AND is_installed) AS i6,
  COUNT_IF(created_at >= DATEADD(minute,-330,'2026-08-05 00:00:00'::TIMESTAMP_NTZ)
       AND created_at <  DATEADD(minute,-330,'2026-08-12 00:00:00'::TIMESTAMP_NTZ)) AS t7,
  COUNT_IF(created_at >= DATEADD(minute,-330,'2026-08-05 00:00:00'::TIMESTAMP_NTZ)
       AND created_at <  DATEADD(minute,-330,'2026-08-12 00:00:00'::TIMESTAMP_NTZ) AND is_installed) AS i7,
  -- running week: upper bound is NOW, so this window is short until 19 Aug and its
  -- task counts are mechanically lower than a full 7-day week. Flagged partial in the UI.
  COUNT_IF(created_at >= DATEADD(minute,-330,'2026-08-12 00:00:00'::TIMESTAMP_NTZ)
       AND created_at <  DATEADD(minute,-330,'2026-08-19 00:00:00'::TIMESTAMP_NTZ)) AS t8,
  COUNT_IF(created_at >= DATEADD(minute,-330,'2026-08-12 00:00:00'::TIMESTAMP_NTZ)
       AND created_at <  DATEADD(minute,-330,'2026-08-19 00:00:00'::TIMESTAMP_NTZ) AND is_installed) AS i8,
  -- Before/after matrix windows: BEFORE = whole of June, AFTER = 1 July to date (now).
  COUNT_IF(created_at >= DATEADD(minute,-330,'2026-06-01 00:00:00'::TIMESTAMP_NTZ)
       AND created_at <  DATEADD(minute,-330,'2026-07-01 00:00:00'::TIMESTAMP_NTZ)) AS jb,
  COUNT_IF(created_at >= DATEADD(minute,-330,'2026-06-01 00:00:00'::TIMESTAMP_NTZ)
       AND created_at <  DATEADD(minute,-330,'2026-07-01 00:00:00'::TIMESTAMP_NTZ) AND is_installed) AS jbi,
  COUNT_IF(created_at >= DATEADD(minute,-330,'2026-07-01 00:00:00'::TIMESTAMP_NTZ)
       AND created_at <  CURRENT_TIMESTAMP()) AS jd,
  COUNT_IF(created_at >= DATEADD(minute,-330,'2026-07-01 00:00:00'::TIMESTAMP_NTZ)
       AND created_at <  CURRENT_TIMESTAMP() AND is_installed) AS jdi,
  COUNT(*) AS _tasks
FROM tt GROUP BY 1
)
SELECT COALESCE(i.partner_id, g.partner_id) AS partner_id,
       i.tb, i.ib, i.ta, i.ia, i.tc, i.ic, i.t4, i.i4, i.t5, i.i5,
       i.t6, i.i6, i.t7, i.i7, i.t8, i.i8, i.jb, i.jbi, i.jd, i.jdi,
       COALESCE(g.recv_m, 0)   AS recv_m,
       COALESCE(g.inst_m, 0)   AS inst_m,
       COALESCE(g.pend_m, 0)   AS pend_m,
       COALESCE(g.nodisp_m, 0) AS nodisp_m
FROM ign i FULL OUTER JOIN gate g ON g.partner_id = i.partner_id;
