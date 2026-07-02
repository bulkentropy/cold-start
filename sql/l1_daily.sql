-- L1 leading metrics, day-on-day (IST), for the enrolled MG CSP set.
-- {PARTNER_IN_LIST} is replaced at runtime with the live enrolled partner_id list.
-- Cohort-by-creation-day: each task is attributed to the IST day the FPN was
-- created; slot/confirmation are the eventual outcomes of that day's tasks.
WITH mg_csp AS (
    SELECT DISTINCT CSP_ID
    FROM PROD_DB.CSP_GATEWAY_SERVICE_CSP_GATEWAY_SERVICE.CSP_ACCOUNT
    WHERE _fivetran_active = TRUE
      AND PARTNER_ID IN ({PARTNER_IN_LIST})
),
hist AS (
    SELECT EXECUTION_CANDIDATE_ID, CONNECTION_ID, CREATED_AT, VALID_FROM,
           PROPOSED_SLOT_DATE, CONFIRMED_SLOT_AT
    FROM PROD_DB.DBT_CSP.TAS_INSTALL_EXECUTION_CANDIDATES
    WHERE CSP_ID IN (SELECT CSP_ID FROM mg_csp)
      AND CREATED_AT >= DATEADD(minute, -330, '{START_DATE} 00:00:00'::TIMESTAMP_NTZ)
),
tasks AS (
    SELECT EXECUTION_CANDIDATE_ID,
           MIN(CONNECTION_ID)                                          AS connection_id,
           MIN(CREATED_AT)                                             AS created_at,
           MIN(IFF(PROPOSED_SLOT_DATE IS NOT NULL, VALID_FROM, NULL))  AS slot_proposed_at,
           MIN(CONFIRMED_SLOT_AT)                                      AS confirmed_at
    FROM hist
    GROUP BY 1
),
-- Accept == slot proposal (validated identical); the event carries the true
-- acceptance timestamp, unlike the SCD snapshot which quantises to sync time.
accepts AS (
    SELECT t.EXECUTION_CANDIDATE_ID,
           MIN(e.EVENT_TIMESTAMP) AS accepted_at
    FROM tasks t
    JOIN PROD_DB.CSP_CONNECTION_LIFECYCLE_SERVICE_CSP_CONNECTION_LIFECYCLE_SERVICE.CONNECTION_EVENT_HISTORY e
      ON e.CONNECTION_ID = t.connection_id
     AND e.EVENT_TYPE = 'ALLOCATION_ACCEPTED'
     AND e._FIVETRAN_DELETED = FALSE
     AND e.EVENT_TIMESTAMP >= t.created_at
    GROUP BY 1
)
SELECT TO_DATE(DATEADD(minute, 330, created_at))                          AS day_ist,
       COUNT(*)                                                          AS tasks_sent,
       COUNT(DISTINCT connection_id)                                     AS bookings_sent,
       COUNT(slot_proposed_at)                                           AS slot_selected,
       ROUND(100.0 * COUNT(slot_proposed_at) / NULLIF(COUNT(*),0), 1)    AS slot_pct,
       COUNT(confirmed_at)                                               AS cust_confirmed,
       ROUND(100.0 * COUNT(confirmed_at)
             / NULLIF(COUNT(slot_proposed_at),0), 1)                     AS confirm_pct,
       ROUND(MEDIAN(DATEDIFF('minute', created_at, accepted_at)) / 60.0, 1) AS med_hrs_to_accept,
       ROUND(PERCENTILE_CONT(0.9) WITHIN GROUP (
             ORDER BY DATEDIFF('minute', created_at, accepted_at)) / 60.0, 1) AS p90_hrs_to_accept
FROM tasks LEFT JOIN accepts USING (EXECUTION_CANDIDATE_ID)
GROUP BY 1
ORDER BY 1;
