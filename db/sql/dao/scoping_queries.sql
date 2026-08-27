-- Role-scoped reads.
--
-- Government and supplier use the unscoped statements in site_queries.sql /
-- issue_queries.sql / work_order_queries.sql. These are the narrowed versions
-- for the two roles that may not see the whole fleet -- a consumer only their
-- own sites, a worker only sites an assignment ties them to. Scoping lives in
-- the WHERE clause rather than in a filter applied after the fact, so a row
-- the caller may not see is never fetched at all.
--
-- Consumed by services/api/auth.py (visible_site_or_404) and by
-- routes_sites.py / routes_issues.py / routes_work_orders.py wherever a
-- handler narrows a fleet-wide statement to "what this caller may see".


-- name: sites_for_account
-- Every site this account owns. Plural deliberately: the schema is
-- ACCOUNT ||--o{ SITE, and a household that owns two service points is a
-- perfectly ordinary case even though registration claims one at a time.
SELECT s.site_id,
       s.label,
       s.district,
       a.full_name AS account_name,
       EXISTS (
           SELECT 1
           FROM solar_array sa
           WHERE sa.site_id = s.site_id
             AND sa.status <> 'decommissioned'
       ) AS has_solar
FROM site s
JOIN account a ON a.account_id = s.account_id
WHERE s.account_id = $1
ORDER BY s.label;


-- name: account_owns_site
SELECT 1 FROM site WHERE site_id = $1 AND account_id = $2;


-- name: worker_covers_site
-- True when this worker has a LIVE assignment on the given site. This is what
-- lets a worker read a site's issues: they are dispatched to it, so they need
-- the reported fault, but only for as long as an assignment ties them to it.
--
-- 'declined', 'expired' and 'released' do not tie them to anything -- each one
-- means the work never happened and somebody else is being found. 'completed'
-- does: a worker keeps the job they actually did, and the site it was on.
SELECT 1
FROM work_order_assignment wa
JOIN work_order w ON w.order_id = wa.order_id
WHERE wa.account_id = $2
  AND w.site_id = $1
  AND wa.status IN ('offered', 'accepted', 'completed')
LIMIT 1;


-- name: issues_for_account
-- A consumer's own issues: everything reported against a site they own.
-- Keyed on site rather than on reported_by_account_id, so a fault the previous
-- owner reported still reaches the person now living with it.
SELECT i.issue_id,
       i.site_id,
       s.label AS site_label,
       i.device_id,
       i.bill_id,
       i.category,
       i.severity,
       i.status,
       i.title,
       i.description,
       i.priority,
       i.reported_at,
       i.acknowledged_at,
       i.resolved_at,
       i.reported_by_account_id,
       a.full_name AS reported_by_name,
       -- Consumer requirement 6: who the complaint is against. Both nullable
       -- and both optional -- a data gap is nobody's fault until someone looks,
       -- and forcing a name onto every report would just produce wrong ones.
       i.distribution_company_id,
       dc.name AS distribution_company_name,
       i.supplier_id,
       sc.name AS supplier_name
FROM issue i
JOIN site s ON s.site_id = i.site_id
JOIN account a ON a.account_id = i.reported_by_account_id
LEFT JOIN distribution_company dc ON dc.company_id = i.distribution_company_id
LEFT JOIN supplier_company sc ON sc.supplier_id = i.supplier_id
WHERE s.account_id = $1
ORDER BY i.reported_at DESC;


-- name: issues_for_worker
-- Issues on sites this worker is dispatched to.
SELECT i.issue_id,
       i.site_id,
       s.label AS site_label,
       i.device_id,
       i.bill_id,
       i.category,
       i.severity,
       i.status,
       i.title,
       i.description,
       i.priority,
       i.reported_at,
       i.acknowledged_at,
       i.resolved_at,
       i.reported_by_account_id,
       a.full_name AS reported_by_name,
       -- Consumer requirement 6: who the complaint is against. Both nullable
       -- and both optional -- a data gap is nobody's fault until someone looks,
       -- and forcing a name onto every report would just produce wrong ones.
       i.distribution_company_id,
       dc.name AS distribution_company_name,
       i.supplier_id,
       sc.name AS supplier_name
FROM issue i
JOIN site s ON s.site_id = i.site_id
JOIN account a ON a.account_id = i.reported_by_account_id
LEFT JOIN distribution_company dc ON dc.company_id = i.distribution_company_id
LEFT JOIN supplier_company sc ON sc.supplier_id = i.supplier_id
WHERE EXISTS (
    SELECT 1
    FROM work_order_assignment wa
    JOIN work_order w ON w.order_id = wa.order_id
    WHERE wa.account_id = $1
      AND w.site_id = i.site_id
      -- Live assignments only, exactly as worker_covers_site -- a worker who
      -- declined the visit does not keep reading the household's complaints.
      AND wa.status IN ('offered', 'accepted', 'completed')
)
ORDER BY i.reported_at DESC;


-- name: work_orders_for_worker
-- Only orders this worker is actually on. The assignment aggregate still lists
-- every assignee, including their crewmates -- who else is on the job is part
-- of the job, and a two-person meter swap that showed only yourself would be
-- misleading.
SELECT w.order_id,
       w.site_id,
       s.label AS site_label,
       s.district,
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
       -- Same six as the other two order aggregates (migration b7d3f5a92c14).
       -- The worker's queue is where the serial is *recorded*, so it is also
       -- where it has to be readable back.
       w.meter_application_id,
       w.agreement_id,
       w.installed_serial_no,
       w.consumer_confirmed_at,
       w.consumer_disputed_at,
       w.consumer_note,
       w.created_at,
       COALESCE(
           json_agg(
               json_build_object(
                   'account_id',  wa.account_id,
                   'worker_name', a.full_name,
                   'job_role',    wa.job_role,
                   'status',      wa.status,
                   'assigned_at', wa.assigned_at,
                   -- The two clocks services/jobs sweeps. Carried on the
                   -- assignment so a worker can see how long they have to
                   -- answer, and a dispatcher can see how long an offer has
                   -- been sitting, without either of them having to guess at
                   -- the durations.
                   'offer_expires_at',  wa.offer_expires_at,
                   'start_deadline_at', wa.start_deadline_at
               )
               ORDER BY wa.job_role, a.full_name
           ) FILTER (WHERE wa.account_id IS NOT NULL),
           '[]'::json
       ) AS assignments
FROM work_order w
JOIN site s ON s.site_id = w.site_id
LEFT JOIN work_order_assignment wa ON wa.order_id = w.order_id
LEFT JOIN account a ON a.account_id = wa.account_id
WHERE EXISTS (
    SELECT 1 FROM work_order_assignment mine
    WHERE mine.order_id = w.order_id
      AND mine.account_id = $1
      -- Live assignments only. An order whose offer this worker declined (or
      -- let lapse) is not theirs, and leaving it here did more than clutter
      -- the queue: the row renders whatever buttons its status offers, which
      -- is how a worker who had said no was shown "Start work" on a job that
      -- was still sitting at 'dispatched'.
      AND mine.status IN ('offered', 'accepted', 'completed')
)
GROUP BY w.order_id, s.label, s.district
ORDER BY w.created_at DESC;


-- name: worker_assigned_to_order
-- The guard behind PATCH /api/work-orders/{id}/status. Live assignments only,
-- for the same reason as the queue above and a stronger one: hiding a button
-- is not a permission. While this matched a declined row, a worker who had
-- turned the job down could still walk it into 'in_progress' by URL.
SELECT 1
FROM work_order_assignment
WHERE order_id = $1
  AND account_id = $2
  AND status IN ('offered', 'accepted', 'completed');
