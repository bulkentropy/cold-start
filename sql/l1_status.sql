-- CSP status by TASK ACTIVITY (matches the team's tasks x installs cross-tab),
-- NOT booking-confirm anchored: per enrolled partner, tasks created and installs
-- completed in two fixed 7-day windows. A CSP working July tasks off a June
-- booking counts as active in July here (unlike the booking-anchored L1 cut).
-- Before = 24-30 Jun, After = 1-7 Jul (IST). {PARTNER_IN_LIST} substituted.
WITH mg AS (
    SELECT CSP_ID, PARTNER_ID
    FROM PROD_DB.CSP_GATEWAY_SERVICE_CSP_GATEWAY_SERVICE.CSP_ACCOUNT
    WHERE _fivetran_active = TRUE AND PARTNER_ID IN ({PARTNER_IN_LIST})
    QUALIFY ROW_NUMBER() OVER (PARTITION BY CSP_ID ORDER BY 1) = 1
)
SELECT mg.PARTNER_ID AS partner_id,
  SUM(IFF(c.CREATED_AT >= DATEADD(minute,-330,'2026-06-24 00:00:00'::TIMESTAMP_NTZ)
          AND c.CREATED_AT < DATEADD(minute,-330,'2026-07-01 00:00:00'::TIMESTAMP_NTZ),1,0)) AS tb,
  SUM(IFF(c.CREATED_AT >= DATEADD(minute,-330,'2026-06-24 00:00:00'::TIMESTAMP_NTZ)
          AND c.CREATED_AT < DATEADD(minute,-330,'2026-07-01 00:00:00'::TIMESTAMP_NTZ)
          AND c.INSTALLATION_COMPLETED_AT IS NOT NULL,1,0)) AS ib,
  SUM(IFF(c.CREATED_AT >= DATEADD(minute,-330,'2026-07-01 00:00:00'::TIMESTAMP_NTZ)
          AND c.CREATED_AT < DATEADD(minute,-330,'2026-07-08 00:00:00'::TIMESTAMP_NTZ),1,0)) AS ta,
  SUM(IFF(c.CREATED_AT >= DATEADD(minute,-330,'2026-07-01 00:00:00'::TIMESTAMP_NTZ)
          AND c.CREATED_AT < DATEADD(minute,-330,'2026-07-08 00:00:00'::TIMESTAMP_NTZ)
          AND c.INSTALLATION_COMPLETED_AT IS NOT NULL,1,0)) AS ia
FROM PROD_DB.DBT_CSP.TAS_INSTALL_EXECUTION_CANDIDATES c
JOIN mg ON mg.CSP_ID = c.CSP_ID
WHERE c.ETL_CURRENT = TRUE
GROUP BY 1;
