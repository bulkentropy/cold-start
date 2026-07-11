-- CSP status by TASK ACTIVITY. Two independent blocks, one row per enrolled CSP:
--
-- 1) Ignition 7-day windows (tb/ib = 24-30 Jun, ta/ia = 1-7 Jul): tasks CREATED
--    in the window + how many installed. Feeds the moved/ignition/demand card.
--    (A CSP working July tasks off a June booking counts as active in July.)
--
-- 2) Install-rate GATE, calendar-month-to-date, aligned to the MG payout logic
--    (MG_calculation_logic.md / mbg_stage3_poller). Per CSP-task:
--      recv_m (denominator) = customer-confirmed lead that reached a FINAL state
--        this month, EXCLUDING still-open (-> pending) and true system/upstream
--        cancels; CSP-fault upstream (CSP_NO_SHOW etc.) IS counted.
--      inst_m (numerator)   = of those, installed this month.
--      pend_m               = customer-confirmed leads still OPEN (live pipeline;
--                             not in the denominator, don't hurt the rate yet).
--    Anchored on FINAL-STATE date (not confirm date), so a June-confirmed lead
--    that resolved in July counts this month. Task level: a reassigned task
--    counts for each CSP that received it. {PARTNER_IN_LIST}/{MONTH_START} subst.
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
)
SELECT partner_id,
  COUNT_IF(created_at >= DATEADD(minute,-330,'2026-06-24 00:00:00'::TIMESTAMP_NTZ)
       AND created_at <  DATEADD(minute,-330,'2026-07-01 00:00:00'::TIMESTAMP_NTZ)) AS tb,
  COUNT_IF(created_at >= DATEADD(minute,-330,'2026-06-24 00:00:00'::TIMESTAMP_NTZ)
       AND created_at <  DATEADD(minute,-330,'2026-07-01 00:00:00'::TIMESTAMP_NTZ) AND is_installed) AS ib,
  COUNT_IF(created_at >= DATEADD(minute,-330,'2026-07-01 00:00:00'::TIMESTAMP_NTZ)
       AND created_at <  DATEADD(minute,-330,'2026-07-08 00:00:00'::TIMESTAMP_NTZ)) AS ta,
  COUNT_IF(created_at >= DATEADD(minute,-330,'2026-07-01 00:00:00'::TIMESTAMP_NTZ)
       AND created_at <  DATEADD(minute,-330,'2026-07-08 00:00:00'::TIMESTAMP_NTZ) AND is_installed) AS ia,
  -- MG-doc gate (final-state anchored, month-to-date)
  COUNT_IF(confirmed_at IS NOT NULL AND NOT is_open AND NOT is_system
       AND final_state_at >= DATEADD(minute,-330,'{MONTH_START} 00:00:00'::TIMESTAMP_NTZ)) AS recv_m,
  COUNT_IF(confirmed_at IS NOT NULL AND is_installed
       AND final_state_at >= DATEADD(minute,-330,'{MONTH_START} 00:00:00'::TIMESTAMP_NTZ)) AS inst_m,
  COUNT_IF(confirmed_at IS NOT NULL AND is_open) AS pend_m
FROM tt GROUP BY 1;
