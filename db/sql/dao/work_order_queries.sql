-- Work order DAO. See db/sql/dao/site_queries.sql for the loader convention.


-- name: list_work_orders
-- Assignments are aggregated as json because they carry no NUMERIC -- names,
-- roles and statuses survive a JSON round trip unchanged.
SELECT w.order_id,
       w.site_id,
       s.label AS site_label,
       w.issue_id,
       w.device_id,
       w.order_type,
       w.status,
       w.priority,
       w.scheduled_for,
       w.started_at,
       w.completed_at,
       w.completion_notes,
       w.failure_reason,
       w.created_at,
       COALESCE(
           json_agg(
               json_build_object(
                   'account_id',  wa.account_id,
                   'worker_name', a.full_name,
                   'job_role',    wa.job_role,
                   'status',      wa.status,
                   'assigned_at', wa.assigned_at
               )
               ORDER BY wa.job_role, a.full_name
           ) FILTER (WHERE wa.account_id IS NOT NULL),
           '[]'::json
       ) AS assignments
FROM work_order w
JOIN site s ON s.site_id = w.site_id
LEFT JOIN work_order_assignment wa ON wa.order_id = w.order_id
LEFT JOIN account a ON a.account_id = wa.account_id
GROUP BY w.order_id, s.label
ORDER BY w.created_at DESC;


-- name: get_work_order
SELECT w.order_id,
       w.site_id,
       s.label AS site_label,
       w.issue_id,
       w.device_id,
       w.order_type,
       w.status,
       w.priority,
       w.scheduled_for,
       w.started_at,
       w.completed_at,
       w.completion_notes,
       w.failure_reason,
       w.created_at,
       COALESCE(
           json_agg(
               json_build_object(
                   'account_id',  wa.account_id,
                   'worker_name', a.full_name,
                   'job_role',    wa.job_role,
                   'status',      wa.status,
                   'assigned_at', wa.assigned_at
               )
               ORDER BY wa.job_role, a.full_name
           ) FILTER (WHERE wa.account_id IS NOT NULL),
           '[]'::json
       ) AS assignments
FROM work_order w
JOIN site s ON s.site_id = w.site_id
LEFT JOIN work_order_assignment wa ON wa.order_id = w.order_id
LEFT JOIN account a ON a.account_id = wa.account_id
WHERE w.order_id = $1
GROUP BY w.order_id, s.label;


-- name: update_work_order_status
-- The lifecycle timestamps are maintained here rather than left to the caller,
-- so a dispatcher clicking a status button cannot leave an order 'completed'
-- with a NULL completed_at. Both are set once and never rewritten -- moving
-- back to an earlier status does not erase the fact that work started.
UPDATE work_order
SET status = $2::work_order_status,
    started_at = CASE
        WHEN started_at IS NULL
         AND $2::work_order_status IN ('in_progress', 'completed', 'failed')
        THEN now()
        ELSE started_at
    END,
    completed_at = CASE
        WHEN completed_at IS NULL
         AND $2::work_order_status IN ('completed', 'failed')
        THEN now()
        ELSE completed_at
    END
WHERE order_id = $1
RETURNING order_id;
