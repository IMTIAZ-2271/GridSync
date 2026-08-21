-- Authentication and registration statements.
--
-- Loaded by services/api/queries.py alongside api_queries.sql, same
-- `-- name:` convention.
--
-- The claiming model, once, since several statements below assume it:
--
--   A seeded account carries '$argon2id$seed$notarealhash' in password_hash.
--   That is not a valid argon2 encoding, so it can never verify -- the account
--   exists, owns real data, and cannot be logged into. Registration converts
--   one of those into a usable account, and which row gets converted differs
--   by role because the data hangs off different keys:
--
--   * A customer's data is keyed on site_id -- readings, bills, credit ledger
--     and issues all follow the site. So a customer registers as a genuinely
--     new account and the site is transferred to them. Old bills keep the
--     previous account_id, which is rule 2 working as designed: a bill names
--     whoever owed the money at the time, not whoever owns the site now.
--
--   * A worker's data is keyed on account_id -- work_order_assignment and
--     worker_skill both reference worker_profile(account_id), which IS the
--     account's primary key. A new account would strand every assignment, and
--     rewriting them to point at the new row would destroy the assignment
--     history that table exists to keep. So a worker claims the existing
--     account in place: same row, same id, new credentials.


-- name: account_by_email
-- Login lookup. Returns the hash even for an unclaimed account so the caller
-- runs a verify either way -- returning early on "no such user" leaks which
-- emails exist through response timing.
SELECT account_id,
       email::text AS email,
       full_name,
       role::text AS role,
       status::text AS status,
       password_hash
FROM account
WHERE email = $1::citext;


-- name: account_profile
SELECT a.account_id,
       a.email::text AS email,
       a.full_name,
       a.phone,
       a.role::text AS role,
       a.status::text AS status,
       a.created_at
FROM account a
WHERE a.account_id = $1;


-- name: site_by_meter_serial
-- Resolve a customer's meter serial to the site it bills, and report whether
-- that site is still claimable.
--
-- Restricted to the billing meter (rule 7): a generation-only meter or a check
-- meter is not the device a household is given the serial of, and letting one
-- of those claim a site would be a second route to the same site.
SELECT d.device_id,
       d.serial_no,
       s.site_id,
       s.label AS site_label,
       s.district,
       s.account_id AS current_account_id,
       (owner.password_hash = $2) AS is_unclaimed
FROM device d
JOIN meter_spec ms ON ms.device_id = d.device_id
JOIN site s ON s.site_id = d.site_id
JOIN account owner ON owner.account_id = s.account_id
WHERE upper(d.serial_no) = upper($1)
  AND ms.billing_role = 'billing'
  AND d.removed_at IS NULL;


-- name: create_account
INSERT INTO account (email, password_hash, full_name, phone, role, status)
VALUES ($1::citext, $2, $3, $4, $5::account_role, 'active')
RETURNING account_id;


-- name: transfer_site
-- Hand a site to its new owner. Guarded on the site still belonging to the
-- account the caller checked, so two people registering the same serial at
-- once cannot both succeed -- the loser updates zero rows.
UPDATE site
SET account_id = $2
WHERE site_id = $1
  AND account_id = $3
RETURNING site_id;


-- name: worker_profile_by_employee_code
SELECT wp.account_id,
       wp.employee_code,
       wp.service_district,
       wp.availability::text AS availability,
       a.full_name,
       a.email::text AS email,
       (a.password_hash = $2) AS is_unclaimed
FROM worker_profile wp
JOIN account a ON a.account_id = wp.account_id
WHERE upper(wp.employee_code) = upper($1)
  AND wp.left_on IS NULL;


-- name: claim_account
-- Convert a seeded account into a real one, in place.
--
-- Guarded on the placeholder still being present, so a second registration
-- against the same employee code updates nothing rather than silently taking
-- the account over from whoever registered first.
UPDATE account
SET email = $2::citext,
    password_hash = $3,
    full_name = $4,
    phone = COALESCE($5, phone),
    updated_at = now()
WHERE account_id = $1
  AND password_hash = $6
RETURNING account_id;


-- ---------------------------------------------------------------------------
-- Role-scoped reads
--
-- Government and supplier use the unscoped statements in api_queries.sql.
-- These are the narrowed versions for the two roles that may not see the whole
-- fleet. Scoping lives in the WHERE clause rather than in a filter applied
-- after the fact, so a row the caller may not see is never fetched at all.
-- ---------------------------------------------------------------------------

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
-- True when this worker has any assignment on the given site. This is what
-- lets a worker read a site's issues: they are dispatched to it, so they need
-- the reported fault, but only for as long as an assignment ties them to it.
SELECT 1
FROM work_order_assignment wa
JOIN work_order w ON w.order_id = wa.order_id
WHERE wa.account_id = $2
  AND w.site_id = $1
LIMIT 1;


-- name: issues_for_account
-- A customer's own issues: everything reported against a site they own.
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
       a.full_name AS reported_by_name
FROM issue i
JOIN site s ON s.site_id = i.site_id
JOIN account a ON a.account_id = i.reported_by_account_id
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
       a.full_name AS reported_by_name
FROM issue i
JOIN site s ON s.site_id = i.site_id
JOIN account a ON a.account_id = i.reported_by_account_id
WHERE EXISTS (
    SELECT 1
    FROM work_order_assignment wa
    JOIN work_order w ON w.order_id = wa.order_id
    WHERE wa.account_id = $1
      AND w.site_id = i.site_id
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
WHERE EXISTS (
    SELECT 1 FROM work_order_assignment mine
    WHERE mine.order_id = w.order_id
      AND mine.account_id = $1
)
GROUP BY w.order_id, s.label
ORDER BY w.created_at DESC;


-- name: worker_assigned_to_order
SELECT 1
FROM work_order_assignment
WHERE order_id = $1
  AND account_id = $2;
