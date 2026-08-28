-- Work order DAO. See db/sql/dao/site_queries.sql for the loader convention.


-- name: list_work_orders
-- Assignments are aggregated as json because they carry no NUMERIC -- names,
-- roles and statuses survive a JSON round trip unchanged.
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
       -- Which application this visit fulfils, what the technician fitted, and
       -- what the household said about it (migration b7d3f5a92c14). Projected
       -- on both aggregates because the worker's queue and the official's read
       -- the same shape and must not disagree about what a visit recorded.
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
GROUP BY w.order_id, s.label, s.district
ORDER BY w.created_at DESC;


-- name: get_work_order
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
       -- Which application this visit fulfils, what the technician fitted, and
       -- what the household said about it (migration b7d3f5a92c14). Projected
       -- on both aggregates because the worker's queue and the official's read
       -- the same shape and must not disagree about what a visit recorded.
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
WHERE w.order_id = $1
GROUP BY w.order_id, s.label, s.district;


-- name: update_work_order_status
-- The lifecycle timestamps are maintained here rather than left to the caller,
-- so a dispatcher clicking a status button cannot leave an order 'completed'
-- with a NULL completed_at. Both are set once and never rewritten -- moving
-- back to an earlier status does not erase the fact that work started.
--
-- $3 is the serial of the meter actually fitted, recorded by the technician at
-- the property (migration b7d3f5a92c14). COALESCE, not assignment: walking an
-- order back through 'completed' a second time with the field blank must not
-- erase the number, because the official's registration step reads it and
-- nobody else ever saw the hardware.
--
-- $4/$5 carry the completion note and the failure reason. Same treatment, same
-- reason -- a status corrected by a dispatcher should not blank what the
-- worker wrote.
UPDATE work_order
SET status = $2::work_order_status,
    installed_serial_no = COALESCE(nullif(btrim($3), ''), installed_serial_no),
    completion_notes = COALESCE(nullif(btrim($4), ''), completion_notes),
    failure_reason = COALESCE(nullif(btrim($5), ''), failure_reason),
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


-- name: set_order_verdict
-- The household's verdict on a visit.
--
-- Guarded on the order being completed and on the caller owning the site, both
-- in the WHERE clause rather than checked first: there is then no window
-- between the check and the write, and an order on somebody else's site is
-- indistinguishable from one that does not exist.
--
-- Only one verdict is ever recorded. `order_one_verdict` forbids holding both,
-- and re-confirming is idempotent rather than an error -- a second click on a
-- slow connection is not a thing to punish.
UPDATE work_order wo
SET consumer_confirmed_at = CASE WHEN $3 THEN now() ELSE NULL END,
    consumer_disputed_at  = CASE WHEN $3 THEN NULL ELSE now() END,
    consumer_note = COALESCE(nullif(btrim($4), ''), wo.consumer_note)
FROM site s
WHERE s.site_id = wo.site_id
  AND wo.order_id = $1
  AND s.account_id = $2
  AND wo.status = 'completed'
RETURNING wo.order_id, wo.site_id, wo.meter_application_id, wo.agreement_id;


-- name: work_order_origin
-- What a visit exists to fulfil, and who cares about the outcome.
--
-- One statement rather than three lookups: the failure path has to reach the
-- district's officials AND the household, and both are one join away from the
-- order through whichever origin it carries.
SELECT wo.order_id,
       wo.status,
       wo.order_type,
       wo.meter_application_id,
       wo.agreement_id,
       wo.issue_id,
       wo.site_id,
       wo.installed_serial_no,
       wo.failure_reason,
       wo.consumer_confirmed_at,
       s.account_id AS owner_account_id,
       s.district,
       s.label AS site_label,
       wo.created_by_account_id
FROM work_order wo
JOIN site s ON s.site_id = wo.site_id
WHERE wo.order_id = $1;


-- name: create_work_order
-- Raise an order, optionally against an issue.
--
-- site_id and device_id are NOT taken from the caller when an issue is given:
-- they are copied from the issue itself, in SQL, so a dispatcher cannot file an
-- order against issue X on site Y. That pairing is the whole audit trail behind
-- "this visit happened because of that complaint", and a client that could
-- choose both halves independently could break it by accident.
--
-- $2 is the issue (nullable), $3 the site used only when there is no issue.
-- COALESCE order matters: the issue wins whenever one is named.
INSERT INTO work_order (
    issue_id, site_id, device_id, created_by_account_id,
    order_type, status, priority, scheduled_for
)
SELECT i.issue_id,
       COALESCE(i.site_id, $3),
       COALESCE(i.device_id, $4),
       $1,
       $5::work_order_type,
       'draft',
       COALESCE($6, i.priority, 3),
       $7
-- One row always, with i.* NULL when no issue was named. A FULL JOIN reads
-- more naturally here but PostgreSQL refuses one whose condition involves a
-- parameter rather than two columns ("only supported with merge-joinable or
-- hash-joinable join conditions"); a LEFT JOIN from a single-row source has no
-- such restriction and the same meaning.
FROM (SELECT 1) AS one
LEFT JOIN issue i ON i.issue_id = $2
RETURNING order_id;


-- name: assignable_workers
-- Who a dispatcher may offer a job to, and what they need to choose between.
--
-- Approved, still employed, not marked unavailable. That is the same gate
-- `offerable_worker` applies at the moment of offering (assignment_queries.sql)
-- -- listed here as well so the dispatcher never sees a name the next call
-- would refuse. The two must agree; if they drift, this one is the cosmetic
-- copy and that one is the enforcement.
--
-- `open_jobs` is the load figure that makes the list a decision rather than a
-- dropdown: an available worker already holding four live assignments is not
-- the one to offer a fifth. Counted from live assignment states, not from
-- work_order.status, because an offer nobody has answered is still capacity
-- spoken for.
--
-- `rating_avg` is supplier requirement 4's sort key. `service_rating` has
-- nothing writing it yet, so this is NULL for everyone today -- and NULL sorts
-- last below rather than pretending to be zero, because "not yet rated" and
-- "rated badly" must not look the same.
--
-- $1 optionally narrows to one district; $2 optionally to workers whose load is
-- under their own max_daily_jobs.
SELECT wp.account_id,
       a.full_name,
       wp.employee_code,
       wp.service_district,
       wp.worker_kind::text  AS worker_kind,
       wp.availability::text AS availability,
       wp.max_daily_jobs,
       dc.name AS distribution_company_name,
       (
           SELECT count(*)::int
           FROM work_order_assignment wa
           WHERE wa.account_id = wp.account_id
             AND wa.status IN ('offered', 'accepted')
       ) AS open_jobs,
       (
           SELECT round(avg(sr.stars), 2)
           FROM service_rating sr
           WHERE sr.worker_account_id = wp.account_id
             AND sr.subject = 'worker'
       ) AS rating_avg,
       (
           SELECT count(*)::int
           FROM service_rating sr
           WHERE sr.worker_account_id = wp.account_id
             AND sr.subject = 'worker'
       ) AS rating_count
FROM worker_profile wp
JOIN account a ON a.account_id = wp.account_id
LEFT JOIN distribution_company dc
       ON dc.company_id = wp.distribution_company_id
WHERE wp.approval_status = 'approved'
  AND wp.left_on IS NULL
  -- worker_availability is (available, busy, off_duty, on_leave). 'busy'
  -- stays on the list: it means loaded, not unreachable, and open_jobs
  -- below is the honest measure of that. Only genuinely off-shift states
  -- are excluded.
  AND wp.availability NOT IN ('off_duty', 'on_leave')
  AND ($1::text IS NULL OR wp.service_district = $1)
  AND (
      $2::boolean IS NOT TRUE
      OR (
          SELECT count(*)
          FROM work_order_assignment wa
          WHERE wa.account_id = wp.account_id
            AND wa.status IN ('offered', 'accepted')
      ) < wp.max_daily_jobs
  )
-- Best-rated first, then least loaded, then by name so the order is stable
-- between refreshes. NULLS LAST keeps the unrated below the rated rather than
-- at the top, which is what an unqualified DESC would do.
ORDER BY rating_avg DESC NULLS LAST, open_jobs, a.full_name;


-- name: dispatchable_issues
-- Unresolved issues with no live work order against them.
--
-- This is the dispatcher's actual inbox: a complaint nobody has raised a visit
-- for. An issue whose order was cancelled or failed comes BACK here, because
-- the fault is still real and somebody has to go again -- which is why the
-- NOT EXISTS filters on the order's status rather than on its mere existence.
SELECT i.issue_id,
       i.site_id,
       s.label   AS site_label,
       s.district,
       i.device_id,
       d.serial_no      AS device_serial,
       i.category::text AS category,
       i.severity::text AS severity,
       i.status::text   AS status,
       i.title,
       i.description,
       i.priority,
       i.reported_at,
       acct.full_name   AS reported_by_name
FROM issue i
JOIN site s     ON s.site_id = i.site_id
JOIN account acct ON acct.account_id = i.reported_by_account_id
LEFT JOIN device d ON d.device_id = i.device_id
WHERE i.status NOT IN ('resolved', 'closed', 'duplicate')
  AND NOT EXISTS (
      SELECT 1 FROM work_order w
      WHERE w.issue_id = i.issue_id
        AND w.status NOT IN ('cancelled', 'failed')
        -- A completed visit the household says did NOT fix it stops counting as
        -- coverage. Without this the complaint would sit disputed and invisible:
        -- back at 'in_progress' in the triage queue, but never back in the queue
        -- of things needing a technician, because an order already existed.
        AND NOT (
            w.status = 'completed'
            AND i.consumer_disputed_at IS NOT NULL
            AND i.consumer_disputed_at > w.completed_at
        )
  )
-- Severity band first -- a critical fault outranks a cosmetic one whenever
-- it arrived -- then newest first within the band.
ORDER BY i.severity DESC, i.reported_at DESC;
