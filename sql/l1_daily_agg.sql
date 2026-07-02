-- L1 daily aggregates, enrolled (enr=1) vs ALL non-enrolled CSPs (enr=0).
-- Definitions SACROSANCT to Metabase Q11528: anchor = booking confirmed
-- (fct_booking_window, test-LCO excluded) -> connection (days=14 window)
-- -> CURRENT TAS candidate; stages = current-position depth
-- (>=3 accepted/slot proposed, >=4 slot confirmed by customer, 6 installed).
-- ALLOCATION_ACCEPTED is used for accept-speed timing only.
-- Row shape (mode): 'cohort'        day=booking day, full stage counts + speed
--                   'event_accept'  day=accept IST day, n=accepts + speed
--                   'event_confirm' day=confirm IST day, n=confirms
--                   'csps'          day=NULL, n=distinct receiving CSPs (for scaling)
-- {PARTNER_IN_LIST} and {START_DATE} are substituted at run time.
WITH mg_csp AS (
    SELECT DISTINCT CSP_ID
    FROM PROD_DB.CSP_GATEWAY_SERVICE_CSP_GATEWAY_SERVICE.CSP_ACCOUNT
    WHERE _fivetran_active = TRUE
      AND PARTNER_ID IN ({PARTNER_IN_LIST})
),
bookings AS (
    SELECT MOBILE AS mobile, TO_DATE(BOOKING_CONFIRM_DATE) AS booking_date,
           BOOKING_CONFIRM_TIME AS bt, NEXT_BOOKING_CONFIRM_TIME AS nb
    FROM PROD_DB.DBT.fct_booking_window
    WHERE BOOKING_CONFIRM_DATE >= '{START_DATE}'
),
acc AS (
    SELECT b.*, dr.ACCOUNT_ID::STRING AS account_id, dr.LCO_ACCOUNT_ID AS lco
    FROM bookings b
    LEFT JOIN PROD_DB.DYNAMODB_read.BOOKING dr
      ON dr.MOBILE = b.mobile AND dr._FIVETRAN_DELETED = FALSE
    QUALIFY ROW_NUMBER() OVER (PARTITION BY b.mobile, b.booking_date
                               ORDER BY dr.ADDED_TIME DESC NULLS LAST) = 1
),
acc_clean AS (
    SELECT * FROM acc
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
    QUALIFY ROW_NUMBER() OVER (PARTITION BY a.mobile, a.booking_date ORDER BY ceh.EVENT_TIMESTAMP) = 1
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
    SELECT cn.booking_date, cn.CONNECTION_ID, tl.CSP_ID,
           IFF(tl.CSP_ID IN (SELECT CSP_ID FROM mg_csp), 1, 0) AS enr,
           tl.CREATED_AT AS task_created_at, tl.csa AS confirmed_at,
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
),
accepts AS (
    SELECT j.CONNECTION_ID, MIN(e.EVENT_TIMESTAMP) AS accepted_at
    FROM joined j
    JOIN PROD_DB.CSP_CONNECTION_LIFECYCLE_SERVICE_CSP_CONNECTION_LIFECYCLE_SERVICE.CONNECTION_EVENT_HISTORY e
      ON e.CONNECTION_ID = j.CONNECTION_ID
     AND e.EVENT_TYPE = 'ALLOCATION_ACCEPTED' AND e._FIVETRAN_DELETED = FALSE
     AND e.EVENT_TIMESTAMP >= j.task_created_at
    GROUP BY 1
),
full_j AS (
    SELECT j.*, a.accepted_at,
           DATEDIFF('minute', j.task_created_at, a.accepted_at) AS mins_to_accept
    FROM joined j LEFT JOIN accepts a USING (CONNECTION_ID)
)
SELECT 'cohort' AS mode, booking_date::STRING AS day_ist, enr,
       COUNT(*) AS bookings,
       SUM(IFF(depth >= 3, 1, 0)) AS accepted,
       SUM(IFF(depth >= 4, 1, 0)) AS confirmed,
       SUM(IFF(depth >= 6, 1, 0)) AS installed,
       NULL AS n,
       ROUND(MEDIAN(mins_to_accept) / 60, 1) AS med_hrs,
       ROUND(PERCENTILE_CONT(0.9) WITHIN GROUP (ORDER BY mins_to_accept) / 60, 1) AS p90_hrs
FROM full_j GROUP BY 2, 3
UNION ALL
SELECT 'event_accept', TO_DATE(DATEADD(minute, 330, accepted_at))::STRING, enr,
       NULL, NULL, NULL, NULL, COUNT(*),
       ROUND(MEDIAN(mins_to_accept) / 60, 1),
       ROUND(PERCENTILE_CONT(0.9) WITHIN GROUP (ORDER BY mins_to_accept) / 60, 1)
FROM full_j WHERE accepted_at IS NOT NULL GROUP BY 2, 3
UNION ALL
SELECT 'event_confirm', TO_DATE(DATEADD(minute, 330, confirmed_at))::STRING, enr,
       NULL, NULL, NULL, NULL, COUNT(*), NULL, NULL
FROM full_j WHERE confirmed_at IS NOT NULL GROUP BY 2, 3
UNION ALL
SELECT 'csps', NULL, enr, NULL, NULL, NULL, NULL, COUNT(DISTINCT CSP_ID), NULL, NULL
FROM full_j GROUP BY 3
UNION ALL
-- all qualified bookings created that day (Q11528 stage 1), whether or not
-- they ever reached a connection or CSP task
SELECT 'total', booking_date::STRING, NULL, COUNT(*), NULL, NULL, NULL, NULL, NULL, NULL
FROM acc_clean GROUP BY 2
ORDER BY 1, 2;
