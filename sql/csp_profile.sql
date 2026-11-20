-- Live account status and any open exit for ONE CSP (the one picked on /tasklist).
-- {CSP_ID} is validated against the Mumbai CSP list before substitution.
select a.status,
       (select max(e.exit_state)
          from prod_db.csp_exit_service_csp_exit_service.exit_records e
         where e._fivetran_active and e.csp_id = '{CSP_ID}'
           and e.exit_completed_at is null and e.aborted_at is null) as exit_state
from prod_db.csp_gateway_service_csp_gateway_service.csp_account a
where a._fivetran_active and a.csp_id = '{CSP_ID}'
