-- Pending tasks on one CSP's app list, as of the latest sync (~minutes behind the app).
-- One row per open task across the four CSP-app queues. "Pending" = the app's own
-- IS_CSP_ACTIONABLE flag on the current (_FIVETRAN_ACTIVE) row of each queue, so the
-- list matches what the CSP sees. {CSP_ID} is validated against the Mumbai CSP list
-- before substitution. Every read is scoped to that one CSP - never pull bulk data here.
with install as (
    select 'install' as kind, 'Install' as type_label,
           current_state as state, execution_candidate_id as task_id,
           connection_id as ref_id, customer_name, device_id,
           coalesce(service_address, locality) as place,
           created_at, deadline_at, updated_at,
           executor_name as assignee,
           iff(confirmed_slot_date is not null,
               'Slot ' || to_char(confirmed_slot_date, 'DD Mon') || ' ' || coalesce(left(confirmed_slot_time_range_start, 5), ''),
               iff(proposed_slot_date is not null, 'Proposed ' || to_char(proposed_slot_date, 'DD Mon'), null)) as detail,
           _fivetran_synced as synced
    from prod_db.csp_tas_service_csp_tas_service.install_execution_candidates
    where _fivetran_active and is_csp_actionable and csp_id = '{CSP_ID}'
),
service as (
    select 'service', iff(task_family = 'SHIFTING', 'Service · Shifting', 'Service'),
           state, execution_candidate_id,
           coalesce(ticket_id, complaint_id), customer_name, coalesce(device_id, diagnosis_device_id),
           coalesce(customer_address, locality),
           created_at, deadline_at, updated_at,
           assigned_technician_id,
           nullif(array_to_string(array_construct_compact(primary_class, secondary_subtype), ' · '), ''),
           _fivetran_synced
    from prod_db.csp_tas_service_csp_tas_service.restore_execution_candidates
    where _fivetran_active and is_csp_actionable and csp_id = '{CSP_ID}'
),
pickup as (
    select 'pickup', 'Pickup', state, execution_candidate_id,
           last_connection_id, customer_name, device_id,
           coalesce(customer_address, locality),
           created_at, deadline_at, updated_at,
           assigned_member_name,
           nullif(array_to_string(array_construct_compact(recovery_method, 'Customer ' || lower(customer_status)), ' · '), ''),
           _fivetran_synced
    from prod_db.csp_tas_service_csp_tas_service.nbrec_execution_candidates
    where _fivetran_active and is_csp_actionable and csp_id = '{CSP_ID}'
),
claim as (
    select 'claim', 'Claim · Recharge', state, execution_candidate_id,
           coalesce(connection_short_id, connection_id), customer_name, device_id,
           locality,
           created_at, coalesce(deadline_at, obligation_window_end), updated_at,
           null,
           'Commission ' || lower(coalesce(commission_status, 'unclaimed')),
           _fivetran_synced
    from prod_db.csp_tas_service_csp_tas_service.recharge_execution_candidates
    where _fivetran_active and is_csp_actionable and state = 'ACTION_REQUIRED'
      and csp_id = '{CSP_ID}'
),
t as (
    select * from install union all select * from service
    union all select * from pickup union all select * from claim
)
select kind, type_label, state, task_id, ref_id, customer_name, device_id, place, assignee, detail,
       to_char(convert_timezone('Asia/Kolkata', created_at), 'YYYY-MM-DD HH24:MI') as created_ist,
       to_char(convert_timezone('Asia/Kolkata', deadline_at), 'YYYY-MM-DD HH24:MI') as deadline_ist,
       to_char(convert_timezone('Asia/Kolkata', updated_at), 'YYYY-MM-DD HH24:MI') as updated_ist,
       to_char(convert_timezone('Asia/Kolkata', synced), 'YYYY-MM-DD HH24:MI') as synced_ist
from t
order by created_at
