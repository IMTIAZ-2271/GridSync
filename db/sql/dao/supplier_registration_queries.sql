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
-- ID and the organisation the applicant typed. There is nothing to open,
-- because there is nothing else to see: the decision is a comparison against
-- records held outside this system.
--
-- `suggested_supplier_id` is a convenience and never an answer: an exact
-- case-insensitive match on the typed string, so the ordinary case (somebody
-- typing the name of a firm already on the books) is one click rather than a
-- scan. It is deliberately exact -- a fuzzy match that quietly proposed the
-- wrong firm would be worse than proposing nothing, because the official is
-- the only check there is. NULL means "no obvious match", not "new firm".
--
-- $1 NULL means every district (admin).
SELECT sp.account_id,
       a.full_name,
       a.email::text            AS email,
       a.national_id,
       a.phone,
       sp.job_title,
       sp.claimed_organisation,
       sp.service_district,
       sp.approval_status::text AS approval_status,
       sp.rejection_reason,
       sp.approved_at,
       sp.supplier_id,
       sc.name                  AS supplier_name,
       match.supplier_id        AS suggested_supplier_id,
       match.name               AS suggested_supplier_name,
       match.code               AS suggested_supplier_code,
       a.created_at             AS registered_at
FROM supplier_profile sp
JOIN account a ON a.account_id = sp.account_id
LEFT JOIN supplier_company sc ON sc.supplier_id = sp.supplier_id
LEFT JOIN LATERAL (
    SELECT c.supplier_id, c.name, c.code
    FROM supplier_company c
    WHERE c.status = 'active'
      AND lower(c.name) = lower(sp.claimed_organisation)
    ORDER BY c.code
    LIMIT 1
) AS match ON TRUE
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
       sp.claimed_organisation,
       sp.service_district,
       sp.approval_status::text AS approval_status,
       sp.rejection_reason,
       sp.approved_at,
       sp.supplier_id,
       sc.name                  AS supplier_name,
       match.supplier_id        AS suggested_supplier_id,
       match.name               AS suggested_supplier_name,
       match.code               AS suggested_supplier_code,
       a.created_at             AS registered_at
FROM supplier_profile sp
JOIN account a ON a.account_id = sp.account_id
LEFT JOIN supplier_company sc ON sc.supplier_id = sp.supplier_id
LEFT JOIN LATERAL (
    SELECT c.supplier_id, c.name, c.code
    FROM supplier_company c
    WHERE c.status = 'active'
      AND lower(c.name) = lower(sp.claimed_organisation)
    ORDER BY c.code
    LIMIT 1
) AS match ON TRUE
WHERE sp.account_id = $1
  AND ($2::text IS NULL OR sp.service_district = $2);


-- name: decide_supplier_registration
-- Approve or reject, once, and link the firm in the same statement.
--
-- $6 is the supplier_company the official resolved the claim to -- an existing
-- firm they picked, or one they just created. It is written here rather than
-- in a second UPDATE so that "approved" and "belongs to a firm" become true
-- together: `supplier_approved_has_firm` would refuse them apart, which is the
-- constraint doing its job rather than an ordering the code has to remember.
-- COALESCE keeps a rejection's NULL, since nobody ever linked it.
--
-- Three more guards, all in the statement:
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
    rejection_reason       = CASE WHEN $2 = 'rejected' THEN $4 ELSE NULL END,
    supplier_id            = COALESCE($6::uuid, sp.supplier_id)
WHERE sp.account_id = $1
  AND sp.approval_status = 'pending'
  AND ($5::text IS NULL OR sp.service_district = $5)
RETURNING sp.account_id;


-- name: supplier_company_for_linking
-- One firm, by id, if it is a firm an official may link somebody to.
--
-- The status check is the point: a suspended or closed installer must not gain
-- new staff. Returns nothing otherwise, which the handler answers 422 to --
-- the official picked from a list, so a miss here is a stale page rather than
-- a probe.
SELECT supplier_id, code, name
FROM supplier_company
WHERE supplier_id = $1 AND status = 'active';


-- name: create_supplier_company
-- The firm an official creates from a claim nothing on the books matched.
--
-- `code` is generated by the caller rather than typed: nobody enters a code
-- anywhere any more, it only has to be unique, and asking an official to
-- invent one would be asking them to guess at a convention. license_no is
-- optional and UNIQUE, so two firms cannot be registered against one licence.
INSERT INTO supplier_company (code, name, license_no)
VALUES ($1, btrim($2), $3)
RETURNING supplier_id, code, name;


-- name: add_supplier_service_area
-- Record that this firm works this district.
--
-- Written at approval, because that is the moment somebody with authority
-- asserts it: an official approving staff for a firm in their district has
-- just said the firm operates there. Without this a newly created installer
-- would be invisible to every household -- requirement 7's supplier list is
-- filtered by district -- and an existing firm would be missing a district it
-- demonstrably works.
--
-- ON CONFLICT DO NOTHING: the second approval in the same district is the
-- normal case, not an error.
INSERT INTO supplier_service_area (supplier_id, district)
VALUES ($1, $2)
ON CONFLICT (supplier_id, district) DO NOTHING;
