-- L1 per-booking rows tagged with the CURRENT task's CSP partner_id, so the
-- server can bucket bookings by the CSP's belief cohort. Same Q11528 chain and
-- depth ladder as l1_bookings.sql; the only addition is partner_id on output.
-- {PARTNER_IN_LIST} and {START_DATE} are substituted at run time.
WITH mg_csp AS (
    SELECT CSP_ID, PARTNER_ID
    FROM PROD_DB.CSP_GATEWAY_SERVICE_CSP_GATEWAY_SERVICE.CSP_ACCOUNT
    WHERE _fivetran_active = TRUE
      AND PARTNER_ID IN ({PARTNER_IN_LIST})
    QUALIFY ROW_NUMBER() OVER (PARTITION BY CSP_ID ORDER BY 1) = 1
),
bookings AS (
    SELECT MOBILE AS mobile, TO_DATE(BOOKING_CONFIRM_DATE) AS booking_date,
           BOOKING_CONFIRM_TIME AS bt, NEXT_BOOKING_CONFIRM_TIME AS nb
    FROM PROD_DB.DBT.fct_booking_window
    WHERE BOOKING_CONFIRM_DATE >= '{START_DATE}'
),
acc AS (   -- Q11528 fix (7 Jul): journey-specific account_id — the audit row
    -- active at THIS booking's confirm time; booking key = (mobile, confirm-time)
    SELECT b.mobile, b.booking_date, b.bt, b.nb,
           ad.ACCOUNT_ID::STRING AS account_id, ad.LCO_ACCOUNT_ID AS lco
    FROM bookings b
    LEFT JOIN PROD_DB.DYNAMODB.BOOKING ad
      ON ad.MOBILE::STRING = b.mobile AND ad.ACCOUNT_ID IS NOT NULL
     AND DATEADD(minute, 330, ad.modified_time) <= b.bt
     AND (b.nb IS NULL OR DATEADD(minute, 330, ad.modified_time) < b.nb)
    QUALIFY ROW_NUMBER() OVER (PARTITION BY b.mobile, b.bt
                               ORDER BY ad.modified_time DESC NULLS LAST) = 1
),
acc_clean AS (
    SELECT mobile, booking_date, bt, nb, account_id FROM acc
    WHERE lco IS NULL OR lco NOT IN
        (SELECT LCO_ACCOUNT_ID FROM PROD_DB.PUBLIC.TEST_LCO_ACCOUNT_ID WHERE LCO_ACCOUNT_ID IS NOT NULL)
),
conn AS (
    SELECT a.mobile, a.booking_date, ceh.CONNECTION_ID
    FROM acc_clean a
    JOIN PROD_DB.CSP_CONNECTION_LIFECYCLE_SERVICE_CSP_CONNECTION_LIFECYCLE_SERVICE.CONNECTION_EVENT_HISTORY ceh
      ON ceh.EVENT_TYPE = 'CONNECTION_REQUEST' AND ceh._FIVETRAN_DELETED = FALSE
     AND ceh.EVENT_TIMESTAMP BETWEEN DATEADD(hour, -2, DATEADD(minute, -330, a.bt))
                                 AND DATEADD(hour, 24 * 14, DATEADD(minute, -330, a.bt))
     AND (a.nb IS NULL OR DATEADD(minute, 330, ceh.EVENT_TIMESTAMP) < a.nb)
    JOIN PROD_DB.CSP_CONNECTION_LIFECYCLE_SERVICE_CSP_CONNECTION_LIFECYCLE_SERVICE.CONNECTIONS c
      ON c.CONNECTION_ID = ceh.CONNECTION_ID AND c.CUSTOMER_ID::STRING = a.account_id
     AND c._fivetran_active = TRUE
    QUALIFY ROW_NUMBER() OVER (PARTITION BY a.mobile, a.bt ORDER BY ceh.EVENT_TIMESTAMP) = 1
),
tl AS (
    SELECT CONNECTION_ID, CSP_ID, CREATED_AT,
           CURRENT_STATE cs, PROPOSED_SLOT_DATE psd, CONFIRMED_SLOT_AT csa, EXECUTOR_ID exid,
           MAX(IFF(OTP_VERIFIED = TRUE OR INSTALLATION_COMPLETED_AT IS NOT NULL OR COMPLETED_STEP >= 7, 1, 0))
               OVER (PARTITION BY CONNECTION_ID) AS inst_any
    FROM PROD_DB.DBT_CSP.TAS_INSTALL_EXECUTION_CANDIDATES
    WHERE ETL_CURRENT = TRUE
    QUALIFY ROW_NUMBER() OVER (PARTITION BY CONNECTION_ID ORDER BY UPDATED_AT DESC) = 1
),
joined AS (
    SELECT cn.booking_date, cn.CONNECTION_ID, mc.PARTNER_ID AS partner_id,
           tl.CREATED_AT AS task_created_at,
           CASE
             WHEN tl.inst_any = 1 THEN 6
             WHEN tl.exid IS NOT NULL OR tl.cs IN ('TECHNICIAN_ASSIGNED','ARRIVED_AT_SITE',
                  'INSTALLATION_IN_PROGRESS_POST_FEE','AWAITING_CUSTOMER_OTP','FEE_COLLECTION_PENDING') THEN 5
             WHEN tl.csa IS NOT NULL OR tl.cs = 'AWAITING_TECHNICIAN_ASSIGNMENT' THEN 4
             WHEN tl.psd IS NOT NULL OR tl.cs = 'AWAITING_CUSTOMER_SLOT_CONFIRMATION' THEN 3
             ELSE 2
           END AS depth
    FROM conn cn
    JOIN tl ON tl.CONNECTION_ID = cn.CONNECTION_ID
    JOIN mg_csp mc ON mc.CSP_ID = tl.CSP_ID
),
accepts AS (
    SELECT j.CONNECTION_ID, MIN(e.EVENT_TIMESTAMP) AS accepted_at
    FROM joined j
    JOIN PROD_DB.CSP_CONNECTION_LIFECYCLE_SERVICE_CSP_CONNECTION_LIFECYCLE_SERVICE.CONNECTION_EVENT_HISTORY e
      ON e.CONNECTION_ID = j.CONNECTION_ID
     AND e.EVENT_TYPE = 'ALLOCATION_ACCEPTED' AND e._FIVETRAN_DELETED = FALSE
     AND e.EVENT_TIMESTAMP >= j.task_created_at
    GROUP BY 1
)
SELECT j.partner_id                                       AS partner_id,
       j.booking_date::STRING                             AS booking_date,
       j.depth                                            AS depth,
       DATE_PART(EPOCH_SECOND, j.task_created_at)         AS task_epoch,
       DATE_PART(EPOCH_SECOND, a.accepted_at)             AS accept_epoch
FROM joined j
LEFT JOIN accepts a USING (CONNECTION_ID);
