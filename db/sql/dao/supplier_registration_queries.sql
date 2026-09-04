-- Supplier registration DAO: the second approval queue an official works.
--
-- The same shape as worker_queries.sql, deliberately. A supplier's staff
-- account used to be gated by one shared string typed into a form -- the same
-- string for every installer, never rotated, not tied to an invitation -- so
-- anyone holding it could attach themselves to any firm on the list. That is
-- now a decision an official takes: they see the applicant's name, National
-- ID and the organisation they claim, and check it against records the
-- application cannot forge.
--
-- Scope is the official's own district, in SQL rather than in a filter, and
-- it comes from `supplier_profile.service_district` -- the district the
-- applicant registered for, not every district their firm covers. A firm
-- working four districts is four separate decisions by four officials, which
-- is the point: the official who can verify a person is the one in the region
-- that person says they work.
--
-- A registration outside the caller's district returns zero rows here, so the
-- handler answers 404 rather than 403 -- the same reasoning as the worker
-- queue: 403 would confirm the account exists to an official with no business
-- learning who is registered next door.


-- name: pending_supplier_registrations
-- The queue: staff accounts awaiting a decision, newest first.
--
-- Everything an official needs to decide is on the row -- full name, National
-- ID, the organisation claimed and its licence number. There is nothing to
-- open, because there is nothing else to see: the decision is a comparison
-- against records held outside this system.
--
-- $1 NULL means every district (admin).
SELECT sp.account_id,
       a.full_name,
       a.email::text            AS email,
       a.national_id,
       a.phone,
       sp.job_title,
       sp.supplier_id,
       sc.code                  AS supplier_code,
       sc.name                  AS supplier_name,
       sc.license_no,
       sp.service_district,
       sp.approval_status::text AS approval_status,
       sp.rejection_reason,
       sp.approved_at,
       a.created_at             AS registered_at
FROM supplier_profile sp
JOIN account a ON a.account_id = sp.account_id
JOIN supplier_company sc ON sc.supplier_id = sp.supplier_id
WHERE sp.approval_status = 'pending'
  AND ($1::text IS NULL OR sp.service_district = $1)
ORDER BY a.created_at DESC, a.full_name;


-- name: supplier_approval_row
-- One registration, in the same shape as the queue, scoped the same way.
--
-- Used for the read-back after a decision AND as the existence check before
-- one, so a registration in another district is 404 and not 403.
SELECT sp.account_id,
       a.full_name,
       a.email::text            AS email,
       a.national_id,
       a.phone,
       sp.job_title,
       sp.supplier_id,
       sc.code                  AS supplier_code,
       sc.name                  AS supplier_name,
       sc.license_no,
       sp.service_district,
       sp.approval_status::text AS approval_status,
       sp.rejection_reason,
       sp.approved_at,
       a.created_at             AS registered_at
FROM supplier_profile sp
JOIN account a ON a.account_id = sp.account_id
JOIN supplier_company sc ON sc.supplier_id = sp.supplier_id
WHERE sp.account_id = $1
  AND ($2::text IS NULL OR sp.service_district = $2);


-- name: decide_supplier_registration
-- Approve or reject, once. Three guards, all in the statement:
--
--   * `approval_status = 'pending'` -- two officials working the same queue is
--     the normal case, and the second must get a 409 rather than silently
--     replacing the first decision and the name attached to it.
--   * the district predicate, repeated here rather than trusted from the
--     SELECT that listed the row.
--   * `approved_at` is stamped for BOTH outcomes, because
--     supplier_approval_timestamps is
--     `(approval_status = 'pending') = (approved_at IS NULL)` -- the column
--     records when the decision was made, not whether it was favourable.
UPDATE supplier_profile sp
SET approval_status        = $2::approval_status,
    approved_by_account_id = $3,
    approved_at            = now(),
    rejection_reason       = CASE WHEN $2 = 'rejected' THEN $4 ELSE NULL END
WHERE sp.account_id = $1
  AND sp.approval_status = 'pending'
  AND ($5::text IS NULL OR sp.service_district = $5)
RETURNING sp.account_id;
