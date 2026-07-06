-- =====================================================================================
-- B2I FUNNEL v-next with ATTRIBUTED DROPS (current-position). Rebuilt from 6 validations (2 Jul):
--  * Anchor = PROD_DB.DBT.fct_booking_window (canonical; matches skills-MCP total_bookings).
--  * Test-LCO bookings excluded (TEST_LCO_ACCOUNT_ID).
--  * Downstream all measured from the TAS TASK (execution candidate), not DAS -> Accept == Slot.
--  * Stage 2 'Task Created (CSP FPN)' = TAS candidate exists (= DAS ASSIGNED, validated 1:1).
--  * Accept & Slot-Given MERGED into 'CSP Accepted / Slot Proposed' (proven identical).
--  * INSTALLATION_REPORTED_FAILED split: no confirmed slot => 'CSP Backed Out / Cust Refused
--    (pre-install)'; with confirmed slot => 'Installation Failed'.
-- Output = stacked-bar funnel: each stage bar = REACHED (bar1=total, descends), split into
--   '0 Advanced to next stage' + dispositions that leak there. Viz: Bar, X=funnel_stage, Y=bookings,
--   Series=segment, Stacked. Params: {{days}} {{start_date}} {{end_date}} {{city}} {{flow}} {{csp_id}}.
-- =====================================================================================
-- WITH bookings AS (   -- canonical booking window (matches skills-MCP), self-defines next-booking window
--     SELECT MOBILE AS mobile, TO_DATE(BOOKING_CONFIRM_DATE) AS booking_date,
--           BOOKING_CONFIRM_TIME AS bt, NEXT_BOOKING_CONFIRM_TIME AS nb
--     FROM PROD_DB.DBT.fct_booking_window
--     WHERE BOOKING_CONFIRM_DATE >= DATEADD(day,-62, CAST(DATEADD(minute,330,CURRENT_TIMESTAMP()) AS DATE))
-- ),
-- acc AS (
--     SELECT b.*, dr.ACCOUNT_ID::STRING AS account_id, dr.GROUP_NAME AS flow, dr.LCO_ACCOUNT_ID AS lco
--     FROM bookings b
--     LEFT JOIN PROD_DB.DYNAMODB_read.BOOKING dr ON dr.MOBILE=b.mobile AND dr._FIVETRAN_DELETED=FALSE
--     QUALIFY ROW_NUMBER() OVER (PARTITION BY b.mobile,b.booking_date ORDER BY dr.ADDED_TIME DESC NULLS LAST)=1
-- ),
-- acc_clean AS (   -- drop test-LCO bookings
--     SELECT * FROM acc
--     WHERE lco IS NULL OR lco NOT IN (SELECT LCO_ACCOUNT_ID FROM PROD_DB.PUBLIC.TEST_LCO_ACCOUNT_ID WHERE LCO_ACCOUNT_ID IS NOT NULL)
-- ),
-- city AS (
--     SELECT mobile, city_bucket FROM (
--         SELECT mobile, PARSE_JSON(data):city::STRING raw
--         FROM PROD_DB.PUBLIC.booking_logs
--         WHERE event_name='google_location' AND added_time >= DATEADD(day,-64, CURRENT_TIMESTAMP())
--         QUALIFY ROW_NUMBER() OVER (PARTITION BY mobile ORDER BY added_time DESC)=1
--     ) x, LATERAL (SELECT CASE
--         WHEN raw ILIKE '%delhi%' OR raw ILIKE '%noida%' OR raw ILIKE '%ghaziabad%' OR raw ILIKE '%faridabad%'
--           OR raw ILIKE '%gurugram%' OR raw ILIKE '%gurgaon%'
--           OR raw IN ('Bahadurgarh','Sonipat','Loni Dehat','Muradnagar','Modinagar','Hapur','Dasna','Morta',
--                      'Meerut','Muzaffarnagar','Baraut','Mahiuddin Pur Kanawni','Chipyana Khurd Urf Tigri') THEN 'Delhi'
--         WHEN raw ILIKE '%mumbai%' OR raw ILIKE '%thane%' OR raw ILIKE '%bhayandar%' OR raw ILIKE '%vasai%'
--           OR raw ILIKE '%virar%' OR raw ILIKE '%kalyan%' OR raw ILIKE '%panvel%' OR raw ILIKE '%bhiwandi%'
--           OR raw ILIKE '%dombivli%' OR raw ILIKE '%ulhasnagar%' THEN 'Mumbai'
--         ELSE 'Bharat' END AS city_bucket) v
-- ),
-- conn AS (
--     SELECT a.mobile, a.booking_date, c.CONNECTION_ID
--     FROM acc_clean a
--     JOIN PROD_DB.CSP_CONNECTION_LIFECYCLE_SERVICE_CSP_CONNECTION_LIFECYCLE_SERVICE.CONNECTION_EVENT_HISTORY ceh
--       ON ceh.EVENT_TYPE='CONNECTION_REQUEST' AND ceh._FIVETRAN_DELETED=FALSE
--      AND ceh.EVENT_TIMESTAMP BETWEEN DATEADD(hour,-2, DATEADD(minute,-330,a.bt))
--                                 AND DATEADD(hour,24*{{days}}, DATEADD(minute,-330,a.bt))
--      AND (a.nb IS NULL OR DATEADD(minute,330,ceh.EVENT_TIMESTAMP) < a.nb)
--     JOIN PROD_DB.CSP_CONNECTION_LIFECYCLE_SERVICE_CSP_CONNECTION_LIFECYCLE_SERVICE.CONNECTIONS c
--       ON c.CONNECTION_ID=ceh.CONNECTION_ID AND c.CUSTOMER_ID::STRING=a.account_id AND c._fivetran_active=TRUE
--     QUALIFY ROW_NUMBER() OVER (PARTITION BY a.mobile,a.booking_date ORDER BY ceh.EVENT_TIMESTAMP)=1
-- ),
-- tl AS (   -- latest/active TAS candidate (the task) per connection
--     SELECT CONNECTION_ID, CURRENT_STATE cs, REASON_CODE rc, FAILURE_REASON fr, FAILURE_SUBREASON_CODE fsc,
--           CSP_ID AS csp_id, PROPOSED_SLOT_DATE psd, CONFIRMED_SLOT_AT csa, EXECUTOR_ID exid,
--           MAX(IFF(OTP_VERIFIED=TRUE OR INSTALLATION_COMPLETED_AT IS NOT NULL OR COMPLETED_STEP>=7,1,0))
--               OVER (PARTITION BY CONNECTION_ID) AS inst_any
--     FROM PROD_DB.DBT_CSP.TAS_INSTALL_EXECUTION_CANDIDATES WHERE ETL_CURRENT=TRUE
--     QUALIFY ROW_NUMBER() OVER (PARTITION BY CONNECTION_ID ORDER BY UPDATED_AT DESC)=1
-- ),
-- csp AS (SELECT CSP_ID, PARTNER_ID FROM PROD_DB.CSP_GATEWAY_SERVICE_CSP_GATEWAY_SERVICE.CSP_ACCOUNT WHERE _fivetran_active=TRUE
--         QUALIFY ROW_NUMBER() OVER (PARTITION BY CSP_ID ORDER BY 1)=1),
-- base AS (
--     SELECT a.booking_date, a.flow, COALESCE(ct.city_bucket,'Delhi') AS city, tl.csp_id, csp.PARTNER_ID AS partner_id,
--           CASE
--              WHEN cn.CONNECTION_ID IS NULL THEN 'No CSP connection (old-app / not requested)'
--              WHEN tl.inst_any=1 THEN 'Installed'
--              WHEN tl.cs='DECLINED' THEN 'CSP Denied'
--              WHEN tl.cs='CANCELLED_BY_UPSTREAM' AND tl.fr='TIMEOUT_P74' AND tl.fsc='CSP_NO_SHOW' AND tl.rc='ALLOCATION_ACCEPTED' THEN 'CSP No-Show (post-accept)'
--              WHEN tl.cs='CANCELLED_BY_UPSTREAM' AND tl.fr='TIMEOUT_P74' AND tl.fsc='CSP_NO_SHOW' THEN 'Abandoned (post-slot proposal) with TIMEOUT_P74'
--              WHEN tl.cs='CANCELLED_BY_UPSTREAM' AND tl.rc='TIMEOUT_P41' THEN 'CSP No-Response / Timeout P41'
--              WHEN tl.cs='CANCELLED_BY_CUSTOMER' THEN 'Cancelled by Customer'
--              WHEN tl.cs='INSTALLATION_REPORTED_FAILED' AND tl.csa IS NULL THEN 'INSTALLATION_REPORTED_FAILED before slot confirmation'
--              WHEN tl.cs='INSTALLATION_REPORTED_FAILED' THEN 'INSTALLATION_REPORTED_FAILED post slot confirmation'
--              WHEN tl.cs='CANCELLED_BY_UPSTREAM' THEN 'Cancelled Upstream (other)'
--              WHEN tl.CONNECTION_ID IS NULL THEN 'No CSP candidate'
--              ELSE 'In Progress / Other'
--           END AS disposition,
--           CASE   -- current-position depth, measured from the TAS task (1=booking..6=installed)
--              WHEN tl.inst_any=1 THEN 6
--              WHEN tl.exid IS NOT NULL OR tl.cs IN ('TECHNICIAN_ASSIGNED','ARRIVED_AT_SITE','INSTALLATION_IN_PROGRESS_POST_FEE','AWAITING_CUSTOMER_OTP','FEE_COLLECTION_PENDING') THEN 5
--              WHEN tl.csa IS NOT NULL OR tl.cs='AWAITING_TECHNICIAN_ASSIGNMENT' THEN 4
--              WHEN tl.psd IS NOT NULL OR tl.cs='AWAITING_CUSTOMER_SLOT_CONFIRMATION' THEN 3
--              WHEN tl.CONNECTION_ID IS NOT NULL THEN 2
--              ELSE 1
--           END AS cur_depth
--     FROM acc_clean a
--     LEFT JOIN city ct ON ct.mobile=a.mobile
--     LEFT JOIN conn cn ON cn.mobile=a.mobile AND cn.booking_date=a.booking_date
--     LEFT JOIN tl       USING (CONNECTION_ID)
--     LEFT JOIN csp   ON csp.CSP_ID=tl.csp_id
-- ),
-- fbase AS (
--     SELECT * FROM base WHERE 1=1
--       [[ AND booking_date >= {{start_date}} ]]
--       [[ AND booking_date <= {{end_date}} ]]
--       [[ AND city = {{city}} ]]
--       [[ AND flow = {{flow}} ]]
--       [[ AND csp_id = {{csp_id}} ]]
-- ),
-- stages AS (
--     SELECT ord,label FROM (VALUES
--       (1,'1 Qualified Booking'),(2,'2 Task Created (CSP FPN)'),(3,'3 CSP Accepted / Slot Proposed'),
--       (4,'4 Slot Confirmed by Customer'),(5,'5 Technician Assigned'),(6,'6 Installed')
--     ) v(ord,label)
-- )
-- SELECT s.ord AS stage_ord, s.label AS funnel_stage, '0 Advanced to next stage' AS segment, COUNT(*) AS bookings
-- FROM stages s JOIN fbase f ON f.cur_depth > s.ord WHERE s.ord < 6
-- GROUP BY 1,2,3
-- UNION ALL
-- SELECT s.ord, s.label, f.disposition, COUNT(*)
-- FROM stages s JOIN fbase f ON f.cur_depth = s.ord
-- GROUP BY 1,2,3
-- ORDER BY stage_ord, segment;


WITH bookings AS (   -- canonical booking window (matches skills-MCP), self-defines next-booking window
    SELECT MOBILE AS mobile, TO_DATE(BOOKING_CONFIRM_DATE) AS booking_date,
           BOOKING_CONFIRM_TIME AS bt, NEXT_BOOKING_CONFIRM_TIME AS nb
    FROM PROD_DB.DBT.fct_booking_window
    WHERE BOOKING_CONFIRM_DATE >= DATEADD(day,-62, CAST(DATEADD(minute,330,CURRENT_TIMESTAMP()) AS DATE))
),
acc AS (
    SELECT b.*, dr.ACCOUNT_ID::STRING AS account_id, dr.GROUP_NAME AS flow, dr.LCO_ACCOUNT_ID AS lco
    FROM bookings b
    LEFT JOIN PROD_DB.DYNAMODB_read.BOOKING dr ON dr.MOBILE=b.mobile AND dr._FIVETRAN_DELETED=FALSE
    QUALIFY ROW_NUMBER() OVER (PARTITION BY b.mobile,b.booking_date ORDER BY dr.ADDED_TIME DESC NULLS LAST)=1
),
acc_clean AS (   -- drop test-LCO bookings (KEPT)
    SELECT * FROM acc
    WHERE lco IS NULL OR lco NOT IN (SELECT LCO_ACCOUNT_ID FROM PROD_DB.PUBLIC.TEST_LCO_ACCOUNT_ID WHERE LCO_ACCOUNT_ID IS NOT NULL)
),
mobile_accounts AS (   -- <<< FIX: full account set per mobile from RAW booking (retains all re-book accounts, like Q1's twg)
    SELECT DISTINCT MOBILE AS mobile, ACCOUNT_ID::STRING AS account_id
    FROM PROD_DB.DYNAMODB.BOOKING
    WHERE ACCOUNT_ID IS NOT NULL AND MOBILE > '5999999999'
),
city AS (
    SELECT mobile, city_bucket FROM (
        SELECT mobile, PARSE_JSON(data):city::STRING raw
        FROM PROD_DB.PUBLIC.booking_logs
        WHERE event_name='google_location' AND added_time >= DATEADD(day,-64, CURRENT_TIMESTAMP())
        QUALIFY ROW_NUMBER() OVER (PARTITION BY mobile ORDER BY added_time DESC)=1
    ) x, LATERAL (SELECT CASE
        WHEN raw ILIKE '%delhi%' OR raw ILIKE '%noida%' OR raw ILIKE '%ghaziabad%' OR raw ILIKE '%faridabad%'
          OR raw ILIKE '%gurugram%' OR raw ILIKE '%gurgaon%'
          OR raw IN ('Bahadurgarh','Sonipat','Loni Dehat','Muradnagar','Modinagar','Hapur','Dasna','Morta',
                     'Meerut','Muzaffarnagar','Baraut','Mahiuddin Pur Kanawni','Chipyana Khurd Urf Tigri') THEN 'Delhi'
        WHEN raw ILIKE '%mumbai%' OR raw ILIKE '%thane%' OR raw ILIKE '%bhayandar%' OR raw ILIKE '%vasai%'
          OR raw ILIKE '%virar%' OR raw ILIKE '%kalyan%' OR raw ILIKE '%panvel%' OR raw ILIKE '%bhiwandi%'
          OR raw ILIKE '%dombivli%' OR raw ILIKE '%ulhasnagar%' THEN 'Mumbai'
        ELSE 'Bharat' END AS city_bucket) v
),
conn AS (
    SELECT a.mobile, a.booking_date, c.CONNECTION_ID
    FROM acc_clean a
    JOIN mobile_accounts ma ON ma.mobile = a.mobile                              -- <<< FIX (new join)
    JOIN PROD_DB.CSP_CONNECTION_LIFECYCLE_SERVICE_CSP_CONNECTION_LIFECYCLE_SERVICE.CONNECTION_EVENT_HISTORY ceh
      ON ceh.EVENT_TYPE='CONNECTION_REQUEST' AND ceh._FIVETRAN_DELETED=FALSE
     AND ceh.EVENT_TIMESTAMP BETWEEN DATEADD(hour,-2, DATEADD(minute,-330,a.bt))
                                AND DATEADD(hour,24*{{days}}, DATEADD(minute,-330,a.bt))
     AND (a.nb IS NULL OR DATEADD(minute,330,ceh.EVENT_TIMESTAMP) < a.nb)
    JOIN PROD_DB.CSP_CONNECTION_LIFECYCLE_SERVICE_CSP_CONNECTION_LIFECYCLE_SERVICE.CONNECTIONS c
      ON c.CONNECTION_ID=ceh.CONNECTION_ID
     AND c.CUSTOMER_ID::STRING = ma.account_id                                   -- <<< FIX (was a.account_id)
     AND c._fivetran_active=TRUE
    QUALIFY ROW_NUMBER() OVER (PARTITION BY a.mobile,a.booking_date ORDER BY ceh.EVENT_TIMESTAMP)=1
),
tl AS (   -- latest/active TAS candidate (the task) per connection
    SELECT CONNECTION_ID, CURRENT_STATE cs, REASON_CODE rc, FAILURE_REASON fr, FAILURE_SUBREASON_CODE fsc,
           CSP_ID AS csp_id, PROPOSED_SLOT_DATE psd, CONFIRMED_SLOT_AT csa, EXECUTOR_ID exid,
           MAX(IFF(OTP_VERIFIED=TRUE OR INSTALLATION_COMPLETED_AT IS NOT NULL OR COMPLETED_STEP>=7,1,0))
               OVER (PARTITION BY CONNECTION_ID) AS inst_any
    FROM PROD_DB.DBT_CSP.TAS_INSTALL_EXECUTION_CANDIDATES WHERE ETL_CURRENT=TRUE
    QUALIFY ROW_NUMBER() OVER (PARTITION BY CONNECTION_ID ORDER BY UPDATED_AT DESC)=1
),
csp AS (SELECT CSP_ID, PARTNER_ID FROM PROD_DB.CSP_GATEWAY_SERVICE_CSP_GATEWAY_SERVICE.CSP_ACCOUNT WHERE _fivetran_active=TRUE
        QUALIFY ROW_NUMBER() OVER (PARTITION BY CSP_ID ORDER BY 1)=1),
base AS (
    SELECT a.booking_date, a.flow, COALESCE(ct.city_bucket,'Delhi') AS city, tl.csp_id, csp.PARTNER_ID AS partner_id,
           CASE
             WHEN cn.CONNECTION_ID IS NULL THEN 'No CSP connection (old-app / not requested)'
             WHEN tl.inst_any=1 THEN 'Installed'
             WHEN tl.cs='DECLINED' THEN 'CSP Denied'
             WHEN tl.cs='CANCELLED_BY_UPSTREAM' AND tl.fr='TIMEOUT_P74' AND tl.fsc='CSP_NO_SHOW' AND tl.rc='ALLOCATION_ACCEPTED' THEN 'CSP No-Show (post-accept)'
             WHEN tl.cs='CANCELLED_BY_UPSTREAM' AND tl.fr='TIMEOUT_P74' AND tl.fsc='CSP_NO_SHOW' THEN 'Abandoned (post-slot proposal) with TIMEOUT_P74'
             WHEN tl.cs='CANCELLED_BY_UPSTREAM' AND tl.rc='TIMEOUT_P41' THEN 'CSP No-Response / Timeout P41'
             WHEN tl.cs='CANCELLED_BY_CUSTOMER' THEN 'Cancelled by Customer'
             WHEN tl.cs='INSTALLATION_REPORTED_FAILED' AND tl.csa IS NULL THEN 'INSTALLATION_REPORTED_FAILED before slot confirmation'
             WHEN tl.cs='INSTALLATION_REPORTED_FAILED' THEN 'INSTALLATION_REPORTED_FAILED post slot confirmation'
             WHEN tl.cs='CANCELLED_BY_UPSTREAM' THEN 'Cancelled Upstream (other)'
             WHEN tl.CONNECTION_ID IS NULL THEN 'No CSP candidate'
             ELSE 'In Progress / Other'
           END AS disposition,
           CASE   -- current-position depth, measured from the TAS task (1=booking..6=installed)
             WHEN tl.inst_any=1 THEN 6
             WHEN tl.exid IS NOT NULL OR tl.cs IN ('TECHNICIAN_ASSIGNED','ARRIVED_AT_SITE','INSTALLATION_IN_PROGRESS_POST_FEE','AWAITING_CUSTOMER_OTP','FEE_COLLECTION_PENDING') THEN 5
             WHEN tl.csa IS NOT NULL OR tl.cs='AWAITING_TECHNICIAN_ASSIGNMENT' THEN 4
             WHEN tl.psd IS NOT NULL OR tl.cs='AWAITING_CUSTOMER_SLOT_CONFIRMATION' THEN 3
             WHEN tl.CONNECTION_ID IS NOT NULL THEN 2
             ELSE 1
           END AS cur_depth
    FROM acc_clean a
    LEFT JOIN city ct ON ct.mobile=a.mobile
    LEFT JOIN conn cn ON cn.mobile=a.mobile AND cn.booking_date=a.booking_date
    LEFT JOIN tl       ON tl.CONNECTION_ID=cn.CONNECTION_ID
    LEFT JOIN csp   ON csp.CSP_ID=tl.csp_id
),
fbase AS (
    SELECT * FROM base WHERE 1=1
      [[ AND booking_date >= {{start_date}} ]]
      [[ AND booking_date <= {{end_date}} ]]
      [[ AND city = {{city}} ]]
      [[ AND flow = {{flow}} ]]
      [[ AND csp_id = {{csp_id}} ]]
),
stages AS (
    SELECT ord,label FROM (VALUES
      (1,'1 Qualified Booking'),(2,'2 Task Created (CSP FPN)'),(3,'3 CSP Accepted / Slot Proposed'),
      (4,'4 Slot Confirmed by Customer'),(5,'5 Technician Assigned'),(6,'6 Installed')
    ) v(ord,label)
)
SELECT s.ord AS stage_ord, s.label AS funnel_stage, '0 Advanced to next stage' AS segment, COUNT(*) AS bookings
FROM stages s JOIN fbase f ON f.cur_depth > s.ord WHERE s.ord < 6
GROUP BY 1,2,3
UNION ALL
SELECT s.ord, s.label, f.disposition, COUNT(*)
FROM stages s JOIN fbase f ON f.cur_depth = s.ord
GROUP BY 1,2,3
ORDER BY stage_ord, segment;