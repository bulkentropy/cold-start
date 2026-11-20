-- The CSP's own app scorecard: the Quality OS daily snapshot (rolling 60-day lookback),
-- last 30 days, for ONE CSP. These are the numbers the CSP app shows — read them, never
-- recompute them. {CSP_ID} is validated against the Mumbai CSP list before substitution.
select to_char(snapshot_date, 'YYYY-MM-DD') as day,
       to_char(convert_timezone('Asia/Kolkata', snapshot_taken_at), 'YYYY-MM-DD HH24:MI') as taken_ist,
       to_char(lookback_start, 'YYYY-MM-DD') as lookback_start,
       to_char(lookback_end, 'YYYY-MM-DD') as lookback_end,
       composite_state, tier_band, t1_band, t2_band,
       active_connection_count, ont_active_connection_count, non_ont_active_connection_count,
       avg_rating, avg_rating_count, avg_rating_state,
       install_compliance_rate, install_compliance_numerator, install_compliance_denominator, install_compliance_state,
       resolution_timeliness_rate, resolution_timeliness_numerator, resolution_timeliness_denominator, resolution_timeliness_state,
       long_open_ticket_count, long_open_closed_in_time_count, long_open_breach_count, long_open_state,
       t1_oor_rate, t2_speed_ok_rate
from prod_db.csp_quality_service_csp_quality_service.daily_metric_snapshots
where csp_id = '{CSP_ID}' and _fivetran_active
  and snapshot_date >= dateadd(day, -30, current_date())
qualify row_number() over (partition by snapshot_date order by snapshot_taken_at desc) = 1
order by snapshot_date
