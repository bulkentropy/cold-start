-- Mumbai restart: every confirmed booking inside the MMR bounding box since
-- {START_DATE}, with where it stands today and which CSP holds it. One row per
-- booking journey. Feeds the calling queue on the Mumbai initiative pane.
--
-- Booking anchor = fct_booking_window (confirmed bookings), phantom-guarded the
-- same way as sql/l1_bookings.sql. Location, customer name and address come from
-- the DynamoDB booking row active at that confirm time; "Mumbai" is the LAT/LNG
-- box 18.85-19.55 N, 72.75-73.25 E (the pack's MMR box) because ZONE_CITY is
-- null on every 2026 row. Stage comes from the CURRENT TAS candidate on the
-- connection (latest UPDATED_AT), install from ANY candidate, and the last
-- state transition supplies the failure reason for terminal rows. Source is
-- resolved in server.py from three layers: the Branch ad touch (campaign /
-- adset / click-vs-view), the dbt attribution journey (Online / CC / BDO), and
-- the first-touch lead source — FINAL_SOURCE alone misses ~30% of ad-driven
-- bookings (Branch click present, dbt says Organic).
WITH b AS (
    SELECT MOBILE, BOOKING_CONFIRM_TIME AS bt, BOOKING_CONFIRM_DATE AS bd,
           CONNECTION_ID, CSP_ID AS fct_csp_id, FINAL_SOURCE, BOOKING_ATTRIBUTION,
           SOURCE_ACC_LEAD_CREATED,
           IS_INSTALLED, INSTALL_TIME, SLOT_CONFIRMED_TIME, ASSIGNED_TIME,
           IS_SERVICEABLE, IS_MATCHED, MAX_PARTNERS_MATCHED
    FROM PROD_DB.DBT.fct_booking_window
    WHERE BOOKING_CONFIRM_DATE >= '{START_DATE}'
      QUALIFY (CONNECTION_ID IS NOT NULL
               AND ROW_NUMBER() OVER (PARTITION BY CONNECTION_ID
                        ORDER BY DBT_LOADED_AT, BOOKING_CONFIRM_TIME) = 1)
           OR (CONNECTION_ID IS NULL
               AND COALESCE(DATEDIFF('second',
                     LAG(BOOKING_CONFIRM_TIME) OVER (PARTITION BY MOBILE
                                                     ORDER BY BOOKING_CONFIRM_TIME),
                     BOOKING_CONFIRM_TIME), 999999) > 900)
),
d AS (
    SELECT b.*, dr.ID AS booking_id, dr.NAME AS cust_name, dr.LAT, dr.LNG,
           dr.BOOKING_STATE, dr.CANCEL_REASON, dr.CANCELLATION_BOOKING_DATE AS cancelled_at,
           dr.LCO_ACCOUNT_ID AS lco, dr.PREF_INST_DATE,
           dr.ADDRESS:city::STRING AS city, dr.ADDRESS:locality::STRING AS locality,
           dr.ADDRESS:pincode::STRING AS pincode,
           TRIM(COALESCE(dr.ADDRESS:home::STRING, '') || ' ' || COALESCE(dr.ADDRESS:address::STRING, '')) AS address,
           dr.GROUP_NAME AS flow
    FROM b
    JOIN PROD_DB.DYNAMODB_read.BOOKING dr
      ON dr.MOBILE = b.MOBILE AND dr._FIVETRAN_DELETED = FALSE
    QUALIFY ROW_NUMBER() OVER (PARTITION BY b.MOBILE, b.bt ORDER BY dr.ADDED_TIME DESC NULLS LAST) = 1
),
m AS (
    SELECT * FROM d
    WHERE LAT BETWEEN 18.85 AND 19.55 AND LNG BETWEEN 72.75 AND 73.25
      AND (lco IS NULL OR lco NOT IN
           (SELECT LCO_ACCOUNT_ID FROM PROD_DB.PUBLIC.TEST_LCO_ACCOUNT_ID WHERE LCO_ACCOUNT_ID IS NOT NULL))
),
tl AS (   -- current candidate per connection + install-from-any + re-farm count
    SELECT CONNECTION_ID, CSP_ID, EXECUTION_CANDIDATE_ID, CREATED_AT, UPDATED_AT,
           CURRENT_STATE, PROPOSED_SLOT_DATE, CONFIRMED_SLOT_AT, EXECUTOR_ID,
           INSTALLATION_COMPLETED_AT,
           MAX(IFF(OTP_VERIFIED = TRUE OR INSTALLATION_COMPLETED_AT IS NOT NULL OR COMPLETED_STEP >= 7, 1, 0))
               OVER (PARTITION BY CONNECTION_ID) AS inst_any,
           MAX(INSTALLATION_COMPLETED_AT) OVER (PARTITION BY CONNECTION_ID) AS inst_at_any,
           COUNT(*) OVER (PARTITION BY CONNECTION_ID) AS n_candidates
    FROM PROD_DB.DBT_CSP.TAS_INSTALL_EXECUTION_CANDIDATES
    WHERE ETL_CURRENT = TRUE
      AND CONNECTION_ID IN (SELECT CONNECTION_ID FROM m WHERE CONNECTION_ID IS NOT NULL)
    QUALIFY ROW_NUMBER() OVER (PARTITION BY CONNECTION_ID ORDER BY UPDATED_AT DESC) = 1
),
tr AS (   -- last transition on the current candidate (reason for terminal states)
    SELECT t.EXECUTION_CANDIDATE_ID, t.TO_STATE, t.REASON_CODE, t.OCCURRED_AT
    FROM PROD_DB.CSP_TAS_SERVICE_CSP_TAS_SERVICE.INSTALL_STATE_TRANSITION_LOG t
    WHERE t._FIVETRAN_DELETED = FALSE
      AND t.EXECUTION_CANDIDATE_ID IN (SELECT EXECUTION_CANDIDATE_ID FROM tl)
    QUALIFY ROW_NUMBER() OVER (PARTITION BY t.EXECUTION_CANDIDATE_ID ORDER BY t.OCCURRED_AT DESC) = 1
),
att AS (   -- dbt attribution journey (last touch at booking confirm): Online / CC / BDO / Organic
    SELECT m.MOBILE, m.bt, a.SOURCE AS att_source, a.ATTRIBUTION AS att_channel,
           a.LAST_CLICK AS att_last_click, a.LAST_CALL AS att_last_call,
           a.BDO_LAST_INTERACTION AS att_last_bdo
    FROM m
    JOIN PROD_DB.DBT.ATTRIBUTION_ACC_BOOKING_CONF1 a ON a.MOBILE = m.MOBILE
    QUALIFY ROW_NUMBER() OVER (PARTITION BY m.MOBILE, m.bt
                               ORDER BY ABS(DATEDIFF('second', a.LEAD_BKCNF_OR_CREATE, m.bt))) = 1
),
br AS (    -- Branch: last attributed ad/link touch in the 7 days before the booking confirm.
           -- USER_DATA_DEVELOPER_IDENTITY is the customer mobile once logged in; ISO
           -- timestamps carry +05:30 so they are converted to IST-naive like bt.
    SELECT m.MOBILE, m.bt,
           e.LAST_ATTRIBUTED_TOUCH_TYPE AS touch_type,
           CONVERT_TIMEZONE('Asia/Kolkata', TRY_TO_TIMESTAMP_TZ(NULLIF(e.LAST_ATTRIBUTED_TOUCH_TIMESTAMP_ISO, '')))::TIMESTAMP_NTZ AS touch_at,
           e.LAST_ATTRIBUTED_TOUCH_DATA_TILDE_ADVERTISING_PARTNER_NAME AS touch_partner,
           e.LAST_ATTRIBUTED_TOUCH_DATA_TILDE_FEATURE AS touch_feature,
           e.LAST_ATTRIBUTED_TOUCH_DATA_TILDE_CAMPAIGN AS touch_campaign,
           e.LAST_ATTRIBUTED_TOUCH_DATA_TILDE_AD_SET_NAME AS touch_adset,
           e.LAST_ATTRIBUTED_TOUCH_DATA_TILDE_AD_NAME AS touch_ad
    FROM m
    JOIN PROD_DB.BRANCH_EO_CUSTOM_EVENTS.BRANCH_EO_CUSTOM_EVENTS e
      ON e.USER_DATA_DEVELOPER_IDENTITY = m.MOBILE
     AND e.EVENT_TIMESTAMP >= DATEADD(day, -40, '{START_DATE}')
     AND NULLIF(e.LAST_ATTRIBUTED_TOUCH_TIMESTAMP_ISO, '') IS NOT NULL
     AND (e.LAST_ATTRIBUTED_TOUCH_DATA_TILDE_ADVERTISING_PARTNER_NAME IS NOT NULL
          OR e.LAST_ATTRIBUTED_TOUCH_DATA_TILDE_FEATURE IS NOT NULL)
    WHERE CONVERT_TIMEZONE('Asia/Kolkata', TRY_TO_TIMESTAMP_TZ(NULLIF(e.LAST_ATTRIBUTED_TOUCH_TIMESTAMP_ISO, '')))::TIMESTAMP_NTZ
              BETWEEN DATEADD(day, -7, m.bt) AND DATEADD(minute, 5, m.bt)
    QUALIFY ROW_NUMBER() OVER (PARTITION BY m.MOBILE, m.bt
                               ORDER BY touch_at DESC, e.EVENT_TIMESTAMP DESC) = 1
),
csp AS (
    SELECT CSP_ID, PARTNER_ID, NAME, POC_NAME, MOBILE_NUMBER, LOGICAL_GROUP, STATUS
    FROM PROD_DB.CSP_GATEWAY_SERVICE_CSP_GATEWAY_SERVICE.CSP_ACCOUNT
    WHERE _FIVETRAN_ACTIVE = TRUE
    QUALIFY ROW_NUMBER() OVER (PARTITION BY CSP_ID ORDER BY UPDATED_AT DESC NULLS LAST) = 1
)
SELECT m.MOBILE                                             AS mobile,
       m.cust_name, m.booking_id::STRING                    AS booking_id,
       m.bt::STRING                                         AS booked_at,
       m.bd::STRING                                         AS booked_on,
       m.LAT AS lat, m.LNG AS lng, m.city, m.locality, m.pincode, m.address, m.flow,
       m.FINAL_SOURCE AS source, m.BOOKING_ATTRIBUTION AS attribution,
       m.SOURCE_ACC_LEAD_CREATED AS lead_source,
       att.att_source, att.att_channel,
       att.att_last_click::STRING AS att_last_click, att.att_last_call::STRING AS att_last_call,
       att.att_last_bdo::STRING AS att_last_bdo,
       br.touch_type, br.touch_at::STRING AS touch_at, br.touch_partner, br.touch_feature,
       br.touch_campaign, br.touch_adset, br.touch_ad,
       m.BOOKING_STATE AS booking_state, m.CANCEL_REASON AS cancel_reason,
       m.cancelled_at::STRING                               AS cancelled_at,
       m.PREF_INST_DATE::STRING                             AS pref_install,
       m.CONNECTION_ID::STRING                              AS connection_id,
       m.IS_SERVICEABLE AS is_serviceable, m.IS_MATCHED AS is_matched,
       m.MAX_PARTNERS_MATCHED AS partners_matched,
       m.SLOT_CONFIRMED_TIME::STRING AS fct_slot_confirmed,
       m.ASSIGNED_TIME::STRING       AS fct_assigned,
       m.INSTALL_TIME::STRING        AS fct_install,
       m.IS_INSTALLED                AS fct_installed,
       COALESCE(tl.CSP_ID, m.fct_csp_id)                    AS csp_id,
       tl.CURRENT_STATE              AS tas_state,
       tl.CREATED_AT::STRING         AS task_created,
       tl.UPDATED_AT::STRING         AS task_updated,
       tl.PROPOSED_SLOT_DATE::STRING AS slot_proposed,
       tl.CONFIRMED_SLOT_AT::STRING  AS slot_confirmed,
       IFF(tl.EXECUTOR_ID IS NOT NULL, 1, 0) AS tech_assigned,
       tl.inst_any, tl.inst_at_any::STRING AS installed_at, tl.n_candidates,
       tr.TO_STATE AS last_state, tr.REASON_CODE AS last_reason,
       tr.OCCURRED_AT::STRING AS last_moved,
       c.NAME AS csp_name, c.POC_NAME AS csp_poc, c.MOBILE_NUMBER AS csp_mobile,
       c.LOGICAL_GROUP AS csp_group, c.STATUS AS csp_status, c.PARTNER_ID::STRING AS partner_id
FROM m
LEFT JOIN tl  ON tl.CONNECTION_ID = m.CONNECTION_ID
LEFT JOIN tr  ON tr.EXECUTION_CANDIDATE_ID = tl.EXECUTION_CANDIDATE_ID
LEFT JOIN att ON att.MOBILE = m.MOBILE AND att.bt = m.bt
LEFT JOIN br  ON br.MOBILE = m.MOBILE AND br.bt = m.bt
LEFT JOIN csp c ON c.CSP_ID = COALESCE(tl.CSP_ID, m.fct_csp_id)
ORDER BY m.bt DESC
