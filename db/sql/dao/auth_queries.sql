-- Authentication and registration statements.
--
-- Loaded by services/api/queries.py alongside every other file under
-- db/sql/dao/, same `-- name:` convention. Consumed only by
-- services/api/routes_auth.py -- the role-scoped reads used elsewhere live in
-- db/sql/dao/scoping_queries.sql.
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
