-- Worker DAO: the approval queue an official works, and the decision itself.
--
-- Government requirement 3. `worker_profile.approval_status` has been written
-- at registration since migration e7c4b19a2d83 -- a private worker lands
-- 'approved', a government worker lands 'pending' -- and until now nothing
-- could move it. That mattered more after the jobs runner landed:
-- `offerable_worker` (assignment_queries.sql) refuses to offer a job to a
-- pending profile, so a government worker who registered could never be
-- dispatched to anything.
--
-- Scope is the official's own district, enforced in SQL rather than by a filter
-- in the handler. An official governs one district (government_profile.district
-- carries the one their single-use code was issued for), and a decision on
-- somebody else's district must not merely be hidden -- it must be impossible.


-- name: official_district
-- Which district this account governs, or NULL.
--
-- NULL is meaningful and is what lets 'admin' through the same statements:
-- an admin has no government_profile, so the scope predicates below fall back
-- to "every district" rather than to "none". A government account always has
-- one -- registration creates the profile in the same transaction as the
-- account -- so a NULL here for role 'government' would be a broken row, not a
-- permission.
SELECT gp.district
FROM government_profile gp
WHERE gp.account_id = $1;


-- name: pending_workers
-- The queue: registrations awaiting a decision, oldest first.
--
-- Oldest-first, not newest: a queue sorted by recency buries whatever nobody
-- picked up, and someone waiting three weeks for a decision they cannot work
-- without is exactly who should be at the top. Same reasoning as the worker
-- issue triage.
--
-- $1 NULL means every district (admin). Listing every pending worker to a
-- district official would show them names they cannot act on, which reads as a
-- broken button rather than as a scope.
SELECT wp.account_id,
       a.full_name,
       a.email,
       a.national_id,
       wp.employee_code,
       wp.service_district,
       wp.worker_kind::text     AS worker_kind,
       wp.availability::text    AS availability,
       wp.max_daily_jobs,
       wp.hired_on,
       wp.approval_status::text AS approval_status,
       wp.rejection_reason,
       wp.approved_at,
       dc.company_id            AS distribution_company_id,
       dc.name                  AS distribution_company_name,
       a.created_at             AS registered_at
FROM worker_profile wp
JOIN account a ON a.account_id = wp.account_id
LEFT JOIN distribution_company dc
       ON dc.company_id = wp.distribution_company_id
WHERE wp.approval_status = 'pending'
  AND ($1::text IS NULL OR wp.service_district = $1)
ORDER BY a.created_at, a.full_name;


-- name: worker_approval_row
-- One worker, in the same shape as the queue, scoped the same way.
--
-- Used for the read-back after a decision AND as the existence check before
-- one: a worker outside the caller's district returns zero rows here, so the
-- handler answers 404 rather than 403. 403 would confirm the account exists,
-- and an official has no business learning who is registered in a district
-- they do not govern.
SELECT wp.account_id,
       a.full_name,
       a.email,
       a.national_id,
       wp.employee_code,
       wp.service_district,
       wp.worker_kind::text     AS worker_kind,
       wp.availability::text    AS availability,
       wp.max_daily_jobs,
       wp.hired_on,
       wp.approval_status::text AS approval_status,
       wp.rejection_reason,
       wp.approved_at,
       dc.company_id            AS distribution_company_id,
       dc.name                  AS distribution_company_name,
       a.created_at             AS registered_at
FROM worker_profile wp
JOIN account a ON a.account_id = wp.account_id
LEFT JOIN distribution_company dc
       ON dc.company_id = wp.distribution_company_id
WHERE wp.account_id = $1
  AND ($2::text IS NULL OR wp.service_district = $2);


-- name: decide_worker_approval
-- Approve or reject, once.
--
-- Three guards, all of them in the statement rather than in Python:
--
--   * `approval_status = 'pending'` -- a decision is made once. A second
--     official deciding the same registration a moment later updates zero rows
--     and gets a 409, instead of silently overwriting the first decision and
--     the name attached to it.
--   * the district predicate, repeated here and not merely trusted from the
--     SELECT that listed the row.
--   * `approved_at` is set for BOTH outcomes, because worker_approval_timestamps
--     is `(approval_status = 'pending') = (approved_at IS NULL)` -- the column
--     records when the decision was made, not when it was favourable.
--
-- rejection_reason is written only on a rejection: an approval carrying a
-- leftover reason string would be a lie the UI would eventually render.
UPDATE worker_profile wp
SET approval_status       = $2::approval_status,
    approved_by_account_id = $3,
    approved_at           = now(),
    rejection_reason      = CASE WHEN $2 = 'rejected' THEN $4 ELSE NULL END
WHERE wp.account_id = $1
  AND wp.approval_status = 'pending'
  AND ($5::text IS NULL OR wp.service_district = $5)
RETURNING wp.account_id;
