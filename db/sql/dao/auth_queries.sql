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
-- The account, plus whichever organisation profile its role hangs off. Both
-- joins are one-row-or-none, so this stays a single row however many of them
-- are null.
--
-- The worker context is deliberately NOT joined here -- see
-- worker_registration_state. It is wanted for one role only, and folding it
-- in would put five permanently-null columns on every household's sign-in.
SELECT a.account_id,
       a.email::text AS email,
       a.full_name,
       a.phone,
       a.national_id,
       a.role::text AS role,
       a.status::text AS status,
       a.created_at,
       sp.supplier_id,
       sc.name    AS supplier_name,
       gp.district AS government_district
FROM account a
LEFT JOIN supplier_profile sp ON sp.account_id = a.account_id
LEFT JOIN supplier_company sc ON sc.supplier_id = sp.supplier_id
LEFT JOIN government_profile gp ON gp.account_id = a.account_id
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
-- $5 is the National ID. Nullable in the column because the seeded demo
-- accounts predate the requirement and have none; every registration route
-- that reaches this statement demands one, so new rows always carry it. The
-- column is UNIQUE, so a second registration under the same NID arrives as a
-- UniqueViolation the handler turns into a 409.
INSERT INTO account (email, password_hash, full_name, phone, national_id,
                     role, status)
VALUES ($1::citext, $2, $3, $4, $5, $6::account_role, 'active')
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
-- Organisations: the lists the registration forms choose from, and the rows
-- registration writes. Added with migration e7c4b19a2d83.
-- ---------------------------------------------------------------------------

-- name: list_districts
-- The districts a site, worker or official may be filed under.
--
-- is_selectable is false for values that only exist because they were typed
-- into a free-text column before it had a key (see the migration). They stay
-- joinable so the government rollup keeps reporting them honestly, but they
-- are never offered as a choice.
SELECT name, latitude, longitude
FROM district
WHERE is_selectable
ORDER BY name;


-- name: district_centroid
-- One district, case-insensitively. Returns nothing for an unknown or
-- non-selectable name, which the caller answers 422 to.
SELECT name, latitude, longitude
FROM district
WHERE lower(name) = lower(btrim($1))
  AND is_selectable;


-- name: list_distribution_companies
-- Utilities, optionally narrowed to those serving one district. $1 nullable:
-- a NULL makes the filter a no-op rather than needing a second statement.
SELECT dc.company_id,
       dc.code,
       dc.name,
       dc.contact_email::text AS contact_email,
       dc.contact_phone,
       array_remove(array_agg(a.district ORDER BY a.district), NULL) AS districts
FROM distribution_company dc
LEFT JOIN distribution_company_area a ON a.company_id = dc.company_id
WHERE dc.status = 'active'
  AND ($1::text IS NULL OR EXISTS (
        SELECT 1 FROM distribution_company_area x
        WHERE x.company_id = dc.company_id AND x.district = $1
      ))
GROUP BY dc.company_id, dc.code, dc.name, dc.contact_email, dc.contact_phone
ORDER BY dc.name;


-- name: list_supplier_companies
-- Installers, optionally narrowed to those serving one district, with their
-- rating so far.
--
-- The average is computed here rather than materialized on supplier_company:
-- a stored aggregate is one more thing that can drift from the rows it
-- summarises, and there are never enough ratings for the scan to matter.
-- rating_avg is NULL, not 0, for a supplier nobody has rated -- "unrated" and
-- "rated badly" must not look the same in a dropdown that sorts by it
-- (supplier requirement 4).
SELECT sc.supplier_id,
       sc.code,
       sc.name,
       sc.license_no,
       sc.contact_email::text AS contact_email,
       sc.contact_phone,
       array_remove(array_agg(DISTINCT a.district), NULL) AS districts,
       r.rating_avg,
       COALESCE(r.rating_count, 0)::int AS rating_count
FROM supplier_company sc
LEFT JOIN supplier_service_area a ON a.supplier_id = sc.supplier_id
LEFT JOIN LATERAL (
    SELECT round(avg(sr.stars), 2) AS rating_avg,
           count(*)                AS rating_count
    FROM service_rating sr
    WHERE sr.supplier_id = sc.supplier_id
) r ON TRUE
WHERE sc.status = 'active'
  AND ($1::text IS NULL OR EXISTS (
        SELECT 1 FROM supplier_service_area x
        WHERE x.supplier_id = sc.supplier_id AND x.district = $1
      ))
GROUP BY sc.supplier_id, sc.code, sc.name, sc.license_no, sc.contact_email,
         sc.contact_phone, r.rating_avg, r.rating_count
ORDER BY sc.name;


-- name: supplier_company_by_code
SELECT supplier_id, code, name
FROM supplier_company
WHERE lower(code) = lower(btrim($1)) AND status = 'active';


-- name: distribution_company_serves
-- Whether this utility actually serves this district. Registration checks it
-- so a government worker cannot be filed under a company with no presence in
-- the region whose officials would have to approve them.
SELECT EXISTS (
    SELECT 1 FROM distribution_company_area
    WHERE company_id = $1 AND district = $2
);


-- name: government_code_for_claim
-- A pre-issued official code, and whether anyone has taken it.
--
-- Replaces the single shared secret every government user used to type. The
-- code carries the district its holder governs, so an official's scope comes
-- from what was issued to them rather than from what they type about
-- themselves at registration.
SELECT code,
       district,
       issued_to,
       (claimed_by_account_id IS NOT NULL) AS is_claimed
FROM government_official_code
WHERE lower(code) = lower(btrim($1));


-- name: claim_government_code
-- Guarded on the code still being unclaimed, so two people registering the
-- same code at once cannot both succeed -- the loser updates zero rows and
-- its transaction rolls back, taking the account with it.
UPDATE government_official_code
SET claimed_by_account_id = $2,
    claimed_at            = now()
WHERE code = $1
  AND claimed_by_account_id IS NULL
RETURNING code, district;


-- name: create_government_profile
INSERT INTO government_profile (account_id, district, official_code)
VALUES ($1, $2, $3);


-- name: create_supplier_profile
INSERT INTO supplier_profile (account_id, supplier_id, job_title)
VALUES ($1, $2, $3);


-- name: create_worker_profile
-- Self-registration, as opposed to claim_account below, which takes over a
-- profile the seed already created.
--
-- approval_status is the caller's to decide and is not defaulted here: a
-- private worker is usable immediately, a government one waits for an
-- official in their own district (worker requirement 2). Putting that choice
-- at the call site rather than in a DEFAULT keeps both branches visible.
INSERT INTO worker_profile (
    account_id, employee_code, service_district, worker_kind,
    distribution_company_id, approval_status, approved_at, hired_on
)
VALUES ($1, $2, $3, $4::worker_kind, $5, $6::approval_status,
        CASE WHEN $6::approval_status = 'pending' THEN NULL ELSE now() END,
        CURRENT_DATE);


-- name: worker_registration_state
-- What sign-in needs to know about a worker beyond the account row: which
-- kind they are, and whether their registration has been approved.
--
-- Worker requirement 3 -- "the system will automatically check the database
-- behind the scenes to determine if the user is a government or private
-- worker" -- is this statement. It is a lookup at sign-in, not something the
-- worker asserts.
SELECT w.worker_kind::text     AS worker_kind,
       w.approval_status::text AS approval_status,
       w.service_district,
       w.rejection_reason,
       w.distribution_company_id,
       dc.code AS distribution_company_code,
       dc.name AS distribution_company_name
FROM worker_profile w
LEFT JOIN distribution_company dc
       ON dc.company_id = w.distribution_company_id
WHERE w.account_id = $1;
