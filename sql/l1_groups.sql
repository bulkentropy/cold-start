-- Booking-anchored L1 funnel split into 3 CSP groups: enrolled (MG),
-- eligible-not-enrolled (offered MG at launch, did not enrol), and non-eligible
-- (rest of network). Same Q11528 journey as l1_cohort.sql, but AGGREGATED in
-- Snowflake so the network-wide non-eligible set stays under the row cap.
-- {ENROLLED_IN_LIST}, {ELIGIBLE_IN_LIST}, {START_DATE} substituted at run time.
WITH enr_csp AS (
    SELECT DISTINCT CSP_ID FROM PROD_DB.CSP_GATEWAY_SERVICE_CSP_GATEWAY_SERVICE.CSP_ACCOUNT
    WHERE _fivetran_active = TRUE AND PARTNER_ID IN ({ENROLLED_IN_LIST})
),
elig_csp AS (
    SELECT DISTINCT CSP_ID FROM PROD_DB.CSP_GATEWAY_SERVICE_CSP_GATEWAY_SERVICE.CSP_ACCOUNT
    WHERE _fivetran_active = TRUE AND PARTNER_ID IN ({ELIGIBLE_IN_LIST})
),
bookings AS (
    SELECT MOBILE AS mobile, TO_DATE(BOOKING_CONFIRM_DATE) AS booking_date,
           BOOKING_CONFIRM_TIME AS bt, NEXT_BOOKING_CONFIRM_TIME AS nb
    FROM PROD_DB.DBT.fct_booking_window
    WHERE BOOKING_CONFIRM_DATE >= '{START_DATE}'
),
acc AS (
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
hw AS (   -- high-water per connection: deepest rung ever reached (ever-reached basis)
    SELECT CONNECTION_ID,
           MAX(IFF(OTP_VERIFIED = TRUE OR INSTALLATION_COMPLETED_AT IS NOT NULL OR COMPLETED_STEP >= 7, 1, 0)) AS hw_inst,
           MAX(IFF(EXECUTOR_ID IS NOT NULL OR CURRENT_STATE IN ('TECHNICIAN_ASSIGNED','ARRIVED_AT_SITE',
               'INSTALLATION_IN_PROGRESS_POST_FEE','AWAITING_CUSTOMER_OTP','FEE_COLLECTION_PENDING'), 1, 0)) AS hw_tech,
           MAX(IFF(CONFIRMED_SLOT_AT IS NOT NULL OR CURRENT_STATE = 'AWAITING_TECHNICIAN_ASSIGNMENT', 1, 0)) AS hw_conf,
           MAX(IFF(PROPOSED_SLOT_DATE IS NOT NULL OR CURRENT_STATE = 'AWAITING_CUSTOMER_SLOT_CONFIRMATION', 1, 0)) AS hw_prop
    FROM PROD_DB.DBT_CSP.TAS_INSTALL_EXECUTION_CANDIDATES
    WHERE ETL_CURRENT = TRUE
    GROUP BY CONNECTION_ID
),
joined AS (
    SELECT cn.booking_date, cn.CONNECTION_ID, tl.CSP_ID, tl.CREATED_AT AS task_created_at,
           CASE WHEN tl.CSP_ID IN (SELECT CSP_ID FROM enr_csp)  THEN 'enrolled'
                WHEN tl.CSP_ID IN (SELECT CSP_ID FROM elig_csp) THEN 'eligible_ne'
                ELSE 'non_eligible' END AS grp,
           CASE
             WHEN tl.inst_any = 1 THEN 6
             WHEN tl.exid IS NOT NULL OR tl.cs IN ('TECHNICIAN_ASSIGNED','ARRIVED_AT_SITE',
                  'INSTALLATION_IN_PROGRESS_POST_FEE','AWAITING_CUSTOMER_OTP','FEE_COLLECTION_PENDING') THEN 5
             WHEN tl.csa IS NOT NULL OR tl.cs = 'AWAITING_TECHNICIAN_ASSIGNMENT' THEN 4
             WHEN tl.psd IS NOT NULL OR tl.cs = 'AWAITING_CUSTOMER_SLOT_CONFIRMATION' THEN 3
             ELSE 2
           END AS depth,
           CASE
             WHEN hw.hw_inst = 1 THEN 6
             WHEN hw.hw_tech = 1 THEN 5
             WHEN hw.hw_conf = 1 THEN 4
             WHEN hw.hw_prop = 1 THEN 3
             ELSE 2
           END AS depth_ever
    FROM conn cn
    JOIN tl ON tl.CONNECTION_ID = cn.CONNECTION_ID
    LEFT JOIN hw ON hw.CONNECTION_ID = cn.CONNECTION_ID
),
accepts AS (
    SELECT j.CONNECTION_ID, MIN(e.EVENT_TIMESTAMP) AS accepted_at
    FROM joined j
    JOIN PROD_DB.CSP_CONNECTION_LIFECYCLE_SERVICE_CSP_CONNECTION_LIFECYCLE_SERVICE.CONNECTION_EVENT_HISTORY e
      ON e.CONNECTION_ID = j.CONNECTION_ID AND e.EVENT_TYPE = 'ALLOCATION_ACCEPTED'
     AND e._FIVETRAN_DELETED = FALSE AND e.EVENT_TIMESTAMP >= j.task_created_at
    GROUP BY 1
),
full_j AS (
    SELECT j.*, DATEDIFF('minute', j.task_created_at, a.accepted_at) AS mins_to_accept
    FROM joined j LEFT JOIN accepts a USING (CONNECTION_ID)
)
SELECT 'funnel' AS mode, grp, booking_date::STRING AS day_ist, NULL AS win,
       COUNT(*) AS bookings, SUM(IFF(depth >= 3, 1, 0)) AS accepted,
       SUM(IFF(depth >= 4, 1, 0)) AS confirmed, SUM(IFF(depth >= 6, 1, 0)) AS installed,
       SUM(IFF(depth_ever >= 3, 1, 0)) AS accepted_ever,
       SUM(IFF(depth_ever >= 4, 1, 0)) AS confirmed_ever,
       SUM(IFF(depth_ever >= 6, 1, 0)) AS installed_ever,
       NULL AS csps, NULL AS med_hrs
FROM full_j GROUP BY 2, 3
UNION ALL
SELECT 'meta', grp, NULL,
       CASE WHEN booking_date BETWEEN '2026-06-24' AND '2026-06-30' THEN 'before'
            WHEN booking_date >= '2026-07-01' THEN 'after' END,
       NULL, NULL, NULL, NULL, NULL, NULL, NULL,
       COUNT(DISTINCT CSP_ID), ROUND(MEDIAN(mins_to_accept) / 60.0, 1)
FROM full_j
WHERE (booking_date BETWEEN '2026-06-24' AND '2026-06-30') OR booking_date >= '2026-07-01'
GROUP BY 2, 4;
