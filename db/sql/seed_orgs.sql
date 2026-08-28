-- GridSync organisation seed: distribution companies, solar suppliers, and
-- the pre-issued government official codes.
--
-- Separate from seed_demo.sql on purpose. seed_demo.sql deletes and rebuilds
-- its own rows, which rule 1's forbid_mutation() trigger blocks once bills
-- exist; this file never deletes anything, so it is safe to run against a
-- database that has already been billed. Everything is an idempotent upsert
-- keyed on a stable code.
--
-- It also ATTACHES the organisations to whatever seed data is present:
-- which utility handles each connection's meter, and which installer fitted
-- each array. Those UPDATEs touch no money and are re-runnable.
--
-- Run after seed_demo.sql:
--   psql -d gridsync -f db/sql/seed_orgs.sql

BEGIN;

-- ---------------------------------------------------------------------------
-- Distribution companies. Dhaka is split between two real utilities: DESCO
-- north of the centre, DPDC through the middle and south. Badda sits on the
-- boundary and is listed by both, which is why the consumer's dropdown in
-- requirement 6 is a dropdown and not a lookup -- sometimes there genuinely
-- is a choice to make.
-- ---------------------------------------------------------------------------
INSERT INTO distribution_company (code, name, contact_email, contact_phone)
VALUES
  ('DESCO', 'Dhaka Electric Supply Company Limited',
   'support@desco.example', '+8809612345678'),
  ('DPDC',  'Dhaka Power Distribution Company Limited',
   'support@dpdc.example',  '+8809612345679')
ON CONFLICT (code) DO UPDATE
  SET name = EXCLUDED.name,
      contact_email = EXCLUDED.contact_email,
      contact_phone = EXCLUDED.contact_phone;

INSERT INTO distribution_company_area (company_id, district)
SELECT dc.company_id, a.district
FROM distribution_company dc
JOIN (VALUES
        ('DESCO', 'Gulshan'),
        ('DESCO', 'Banani'),
        ('DESCO', 'Uttara'),
        ('DESCO', 'Mirpur'),
        ('DESCO', 'Bashundhara'),
        ('DESCO', 'Badda'),
        ('DPDC',  'Dhanmondi'),
        ('DPDC',  'Mohammadpur'),
        ('DPDC',  'Badda')
     ) AS a(code, district) ON a.code = dc.code
ON CONFLICT (company_id, district) DO NOTHING;

-- ---------------------------------------------------------------------------
-- Solar suppliers. These are the private installers a consumer applies to
-- (requirement 7) and rates (requirement 10) -- distinct from the utilities
-- above, which are regulated, own the billing meter, and are never rated.
-- ---------------------------------------------------------------------------
INSERT INTO supplier_company (code, name, license_no, contact_email, contact_phone)
VALUES
  ('SOLARIS', 'Supplier 2',               'SREDA-2019-0114',
   'contact@supplier2.example', '+8801711220001'),
  ('RAHIMA',  'Supplier 3',               'SREDA-2021-0298',
   'contact@supplier3.example', '+8801711220002'),
  ('PADMA',   'Supplier 4',               'SREDA-2020-0071',
   'contact@supplier4.example', '+8801711220003'),
  ('NOOR',    'Supplier 1',               'SREDA-2022-0446',
   'contact@supplier1.example', '+8801711220004')
ON CONFLICT (code) DO UPDATE
  SET name = EXCLUDED.name,
      license_no = EXCLUDED.license_no,
      contact_email = EXCLUDED.contact_email,
      contact_phone = EXCLUDED.contact_phone;

-- Service areas. Requirement 7 asks for "suppliers in the consumer's nearby
-- region", so coverage overlaps deliberately -- a household should have more
-- than one installer to choose between.
INSERT INTO supplier_service_area (supplier_id, district)
SELECT sc.supplier_id, a.district
FROM supplier_company sc
JOIN (VALUES
        ('SOLARIS', 'Gulshan'),  ('SOLARIS', 'Banani'),
        ('SOLARIS', 'Badda'),    ('SOLARIS', 'Bashundhara'),
        ('RAHIMA',  'Dhanmondi'),('RAHIMA',  'Mohammadpur'),
        ('RAHIMA',  'Mirpur'),
        ('PADMA',   'Uttara'),   ('PADMA',   'Mirpur'),
        ('PADMA',   'Bashundhara'),
        ('NOOR',    'Gulshan'),  ('NOOR',    'Dhanmondi'),
        ('NOOR',    'Uttara'),   ('NOOR',    'Badda'),
        ('NOOR',    'Banani'),   ('NOOR',    'Mohammadpur')
     ) AS a(code, district) ON a.code = sc.code
ON CONFLICT (supplier_id, district) DO NOTHING;

-- ---------------------------------------------------------------------------
-- Government official codes: one per district, pre-issued, claimable once.
--
-- This is what replaces the single shared registration code. A code carries
-- the district its holder governs, so requirements 2-4 (monitor my region,
-- approve workers in my region) get their scope from the code rather than
-- from something the registering user types about themselves.
--
-- claimed_by_account_id is left alone on conflict -- re-running this file
-- must never un-claim a code someone has already registered against.
-- ---------------------------------------------------------------------------
INSERT INTO government_official_code (code, district, issued_to)
SELECT format('GOV-%s-01', upper(replace(d.name, ' ', ''))),
       d.name,
       format('Area Officer, %s', d.name)
FROM district d
WHERE d.is_selectable
ON CONFLICT (code) DO NOTHING;

-- ---------------------------------------------------------------------------
-- Coverage: three districts, and the staff who can actually serve them.
--
-- An official governs exactly ONE district (their code carries it), so three
-- officials can cover three districts and no more. That single fact decides
-- the shape of the whole estate: every consumer site lives in Badda,
-- Dhanmondi or Uttara, and the other five canonical districts are marked
-- NOT selectable so nothing new can be filed where nobody is on duty.
--
-- This is the mechanism migration e7c4b19a2d83 already built for the legacy
-- free-text districts, used for its other purpose: a non-selectable district
-- stays joinable, so the regulator's rollup keeps reporting any history filed
-- under it, but no registration, onboarding wizard or meter application can
-- choose one. Re-select a district here the day it gets an official.
--
-- The three were not picked arbitrarily. Badda is served by BOTH utilities
-- (which is the only reason requirement 6's dropdown is a choice at all),
-- Dhanmondi is DPDC-only and Uttara is DESCO-only, so the estate exercises
-- every branch. All four installers keep work: Supplier 1 (NOOR) covers all
-- three, Supplier 2 Badda, Supplier 3 Dhanmondi, Supplier 4 Uttara.
-- ---------------------------------------------------------------------------
UPDATE district
SET is_selectable = (name IN ('Badda', 'Dhanmondi', 'Uttara'))
WHERE name IN (
    'Badda', 'Banani', 'Bashundhara', 'Dhanmondi',
    'Gulshan', 'Mirpur', 'Mohammadpur', 'Uttara'
);

-- The roster. One table drives the accounts, the profiles and the checks, so
-- a worker's district and their employer's service area cannot drift apart.
CREATE TEMP TABLE seed_staff (
    email          text PRIMARY KEY,
    role           account_role NOT NULL,
    full_name      text NOT NULL,
    national_id    text NOT NULL,
    district       text,          -- worker + government
    worker_kind    worker_kind,   -- worker only
    utility_code   text,          -- government workers only
    employee_code  text,          -- worker only
    supplier_code  text,          -- supplier only
    official_code  text           -- government only
) ON COMMIT DROP;

-- Ten technicians. Four in Badda (it holds both utilities, so it is the one
-- district where a government worker can belong to either), three each in
-- Dhanmondi and Uttara. Every government worker's employer actually serves
-- their district -- the same rule POST /api/auth/register/worker enforces
-- with a 422, applied here rather than assumed.
INSERT INTO seed_staff (email, role, full_name, national_id, district,
                        worker_kind, utility_code, employee_code)
VALUES
  ('worker1@demo.com',  'worker', 'Worker 1',  '2000000001', 'Badda',     'government', 'DESCO', 'SEED-EMP-001'),
  ('worker2@demo.com',  'worker', 'Worker 2',  '2000000002', 'Badda',     'private',    NULL,    'SEED-EMP-002'),
  ('worker3@demo.com',  'worker', 'Worker 3',  '2000000003', 'Badda',     'government', 'DPDC',  'SEED-EMP-003'),
  ('worker4@demo.com',  'worker', 'Worker 4',  '2000000004', 'Badda',     'private',    NULL,    'SEED-EMP-004'),
  ('worker5@demo.com',  'worker', 'Worker 5',  '2000000005', 'Dhanmondi', 'government', 'DPDC',  'SEED-EMP-005'),
  ('worker6@demo.com',  'worker', 'Worker 6',  '2000000006', 'Dhanmondi', 'private',    NULL,    'SEED-EMP-006'),
  ('worker7@demo.com',  'worker', 'Worker 7',  '2000000007', 'Dhanmondi', 'private',    NULL,    'SEED-EMP-007'),
  ('worker8@demo.com',  'worker', 'Worker 8',  '2000000008', 'Uttara',    'government', 'DESCO', 'SEED-EMP-008'),
  ('worker9@demo.com',  'worker', 'Worker 9',  '2000000009', 'Uttara',    'private',    NULL,    'SEED-EMP-009'),
  ('worker10@demo.com', 'worker', 'Worker 10', '2000000010', 'Uttara',    'private',    NULL,    'SEED-EMP-010');

-- One official per covered district. The code carries the district, so these
-- three claims are what make the three scopes real.
INSERT INTO seed_staff (email, role, full_name, national_id, district, official_code)
VALUES
  ('gov1@demo.com', 'government', 'Gov 1', '3000000001', 'Badda',     'GOV-BADDA-01'),
  ('gov2@demo.com', 'government', 'Gov 2', '3000000002', 'Dhanmondi', 'GOV-DHANMONDI-01'),
  ('gov3@demo.com', 'government', 'Gov 3', '3000000003', 'Uttara',    'GOV-UTTARA-01');

-- Five installer staff across four firms. Supplier 1 (NOOR) gets two logins on
-- purpose:
-- staff attach to a company rather than being one (docs/decisions.md), and
-- that is only demonstrable if some firm has more than one person.
INSERT INTO seed_staff (email, role, full_name, national_id, supplier_code)
VALUES
  ('supplier1@demo.com', 'supplier', 'Supplier 1', '4000000001', 'NOOR'),
  ('supplier2@demo.com', 'supplier', 'Supplier 2', '4000000002', 'SOLARIS'),
  ('supplier3@demo.com', 'supplier', 'Supplier 3', '4000000003', 'RAHIMA'),
  ('supplier4@demo.com', 'supplier', 'Supplier 4', '4000000004', 'PADMA'),
  ('supplier5@demo.com', 'supplier', 'Supplier 5', '4000000005', 'NOOR');

-- A government worker's employer must serve the district they work in, and an
-- official's code must have been issued for the district they govern. Both are
-- assertions about this file's own data, so they are checked here rather than
-- left to be discovered as a 409 during a demo.
DO $$
DECLARE bad text;
BEGIN
    SELECT string_agg(s.email, ', ') INTO bad
    FROM seed_staff s
    WHERE s.worker_kind = 'government'
      AND NOT EXISTS (
          SELECT 1 FROM distribution_company dc
          JOIN distribution_company_area a ON a.company_id = dc.company_id
          WHERE dc.code = s.utility_code AND a.district = s.district
      );
    IF bad IS NOT NULL THEN
        RAISE EXCEPTION 'government worker employed by a utility that does not serve their district: %', bad;
    END IF;

    SELECT string_agg(s.email, ', ') INTO bad
    FROM seed_staff s
    JOIN government_official_code c ON c.code = s.official_code
    WHERE c.district <> s.district;
    IF bad IS NOT NULL THEN
        RAISE EXCEPTION 'official code issued for a different district: %', bad;
    END IF;
END $$;

-- Accounts. Created with the placeholder hash seed_demo.sql uses; the password
-- for every one of them is set by scripts/seed_auth.py afterwards. An account
-- that already exists keeps its password and gets its name and NID corrected.
INSERT INTO account (email, password_hash, full_name, national_id, role, status)
SELECT s.email, '$argon2id$seed$notarealhash', s.full_name, s.national_id,
       s.role, 'active'
FROM seed_staff s
ON CONFLICT (email) DO UPDATE
SET full_name   = EXCLUDED.full_name,
    national_id = EXCLUDED.national_id,
    role        = EXCLUDED.role,
    status      = 'active',
    updated_at  = now();

-- Worker profiles. An existing technician keeps their employee code and their
-- history; what moves is where they work and who they work for.
INSERT INTO worker_profile (account_id, employee_code, service_district,
                            max_daily_jobs, availability, hired_on,
                            worker_kind, distribution_company_id,
                            approval_status, approved_at)
SELECT a.account_id, s.employee_code, s.district, 5, 'available',
       (CURRENT_DATE - INTERVAL '3 years')::date,
       s.worker_kind,
       (SELECT company_id FROM distribution_company WHERE code = s.utility_code),
       'approved', now()
FROM seed_staff s
JOIN account a ON a.email = s.email
WHERE s.role = 'worker'
ON CONFLICT (account_id) DO UPDATE
SET service_district        = EXCLUDED.service_district,
    worker_kind             = EXCLUDED.worker_kind,
    distribution_company_id = EXCLUDED.distribution_company_id,
    approval_status         = 'approved',
    approved_at             = coalesce(worker_profile.approved_at, now()),
    rejection_reason        = NULL,
    availability            = 'available',
    left_on                 = NULL;

-- Every technician can do the three job types the fulfilment flow dispatches.
INSERT INTO worker_skill (account_id, skill_type, proficiency, certified_on)
SELECT a.account_id, sk.skill_type::worker_skill_type, 'expert',
       (CURRENT_DATE - INTERVAL '2 years')::date
FROM seed_staff s
JOIN account a ON a.email = s.email
CROSS JOIN (VALUES ('meter_install'), ('meter_swap'), ('inspection')) AS sk(skill_type)
WHERE s.role = 'worker'
ON CONFLICT DO NOTHING;

-- Official codes: release any claim that no longer matches the roster, then
-- claim the right one. gov2 held GOV-GULSHAN-01 before this file gave it
-- Dhanmondi; releasing first is what lets the district move without the
-- one-claim-per-account unique getting in the way.
UPDATE government_official_code c
SET claimed_by_account_id = NULL, claimed_at = NULL
FROM account a
WHERE c.claimed_by_account_id = a.account_id
  AND NOT EXISTS (
      SELECT 1 FROM seed_staff s
      WHERE s.email = a.email::text AND s.official_code = c.code
  );

UPDATE government_official_code c
SET claimed_by_account_id = a.account_id, claimed_at = now()
FROM seed_staff s
JOIN account a ON a.email = s.email
WHERE c.code = s.official_code
  AND c.claimed_by_account_id IS DISTINCT FROM a.account_id;

INSERT INTO government_profile (account_id, district, official_code)
SELECT a.account_id, s.district, s.official_code
FROM seed_staff s
JOIN account a ON a.email = s.email
WHERE s.role = 'government'
ON CONFLICT (account_id) DO UPDATE
SET district      = EXCLUDED.district,
    official_code = EXCLUDED.official_code;

-- Installer staff, attached to the firm the roster names rather than to
-- whichever one sorts first.
INSERT INTO supplier_profile (account_id, supplier_id, job_title)
SELECT a.account_id,
       (SELECT supplier_id FROM supplier_company WHERE code = s.supplier_code),
       'Dispatcher'
FROM seed_staff s
JOIN account a ON a.email = s.email
WHERE s.role = 'supplier'
ON CONFLICT (account_id) DO UPDATE
SET supplier_id = EXCLUDED.supplier_id;

-- ---------------------------------------------------------------------------
-- Attach the organisations to the demo estate.
--
-- Utility by district: whichever company serves the site's district, and the
-- lowest code when two do (Badda), so the assignment is deterministic rather
-- than dependent on scan order.
-- ---------------------------------------------------------------------------
UPDATE billing_point bp
SET distribution_company_id = pick.company_id
FROM site s
CROSS JOIN LATERAL (
    SELECT dc.company_id
    FROM distribution_company_area a
    JOIN distribution_company dc ON dc.company_id = a.company_id
    WHERE a.district = s.district
    ORDER BY dc.code
    LIMIT 1
) AS pick
WHERE bp.site_id = s.site_id
  AND bp.distribution_company_id IS NULL;

-- Installer by district, same tie-break. Arrays that already name an
-- installer are left alone.
UPDATE solar_array sa
SET installed_by_supplier_id = pick.supplier_id
FROM site s
CROSS JOIN LATERAL (
    SELECT sc.supplier_id
    FROM supplier_service_area a
    JOIN supplier_company sc ON sc.supplier_id = a.supplier_id
    WHERE a.district = s.district
    ORDER BY sc.code
    LIMIT 1
) AS pick
WHERE sa.site_id = s.site_id
  AND sa.installed_by_supplier_id IS NULL;

-- ---------------------------------------------------------------------------
-- Give every 'government' account a government_profile.
--
-- The demo accounts predate migration e7c4b19a2d83: `gov1@demo.com` was created
-- when 'government' was a bare account_role with nothing behind it, so it has
-- no profile and therefore governs no district. That was invisible while the
-- regulator only read fleet-wide aggregates, and stopped being invisible the
-- moment an endpoint scoped itself to the official's own district -- the worker
-- approval queue answers 403 to an official with no district, which is correct
-- and which made the demo account unusable on that page.
--
-- Claims the lowest unclaimed code, exactly as POST /api/auth/register/government
-- does, so the profile a demo account ends up with is the same shape a real
-- registration produces. Ordered by code so re-running picks the same district.
--
-- Idempotent: accounts that already have a profile are skipped by the NOT
-- EXISTS, and the code is marked claimed in the same statement's wake so a
-- second account cannot take it.
-- ---------------------------------------------------------------------------
WITH needy AS (
    SELECT a.account_id,
           row_number() OVER (ORDER BY a.created_at, a.email) AS rn
    FROM account a
    WHERE a.role = 'government'
      AND NOT EXISTS (
          SELECT 1 FROM government_profile gp WHERE gp.account_id = a.account_id
      )
),
free AS (
    SELECT c.code, c.district,
           row_number() OVER (ORDER BY c.code) AS rn
    FROM government_official_code c
    WHERE c.claimed_by_account_id IS NULL
),
paired AS (
    SELECT n.account_id, f.code, f.district
    FROM needy n JOIN free f ON f.rn = n.rn
),
claimed AS (
    UPDATE government_official_code c
    SET claimed_by_account_id = p.account_id,
        claimed_at = now()
    FROM paired p
    WHERE c.code = p.code
    RETURNING c.code
)
INSERT INTO government_profile (account_id, district, official_code)
SELECT p.account_id, p.district, p.code
FROM paired p
WHERE EXISTS (SELECT 1 FROM claimed WHERE claimed.code = p.code);

-- ---------------------------------------------------------------------------
-- Give every 'supplier' account a supplier_profile.
--
-- Exactly the same gap as the government one above, and found the same way:
-- `supplier1@demo.com` was created when 'supplier' was a bare account_role with
-- no company behind it. It stayed invisible while the supplier only read
-- fleet-wide lists, and surfaced the moment a household tried to RATE the firm
-- that sent a technician -- `service_rating.supplier_id` is derived from the
-- dispatcher's profile, so an account without one dispatches visits that can
-- never be rated.
--
-- Attaches the lowest-coded firm, deterministically, so re-running picks the
-- same one. Staff attach to a company rather than being one (docs/decisions.md),
-- so this is a membership row, not a new organisation.
-- ---------------------------------------------------------------------------
INSERT INTO supplier_profile (account_id, supplier_id, job_title)
SELECT a.account_id,
       (SELECT supplier_id FROM supplier_company ORDER BY code LIMIT 1),
       'Dispatcher'
FROM account a
WHERE a.role = 'supplier'
  AND NOT EXISTS (
      SELECT 1 FROM supplier_profile sp WHERE sp.account_id = a.account_id
  );

-- ---------------------------------------------------------------------------
-- Every meter that exists was issued to somebody.
--
-- Migration c9e2f4a71b83 made `meter_asset` the record of hardware issued to
-- an account, and backfilled the rows that existed when it ran. A database
-- built from scratch runs the seeds AFTER that migration, so the seeded
-- meters would otherwise have no asset behind them -- the demo household
-- would open /consumer/meters and be told it owns none of the meters it is
-- plainly billed for.
--
-- Same projection as the migration's backfill, and idempotent on the device:
-- re-running adds nothing.
-- ---------------------------------------------------------------------------
INSERT INTO meter_asset (
    account_id, serial_no, manufacturer, model,
    issued_by_company_id, issued_at, device_id
)
SELECT s.account_id,
       d.serial_no,
       d.manufacturer,
       d.model,
       bp.distribution_company_id,
       d.installed_at,
       d.device_id
FROM device d
JOIN site s ON s.site_id = d.site_id
LEFT JOIN meter_spec ms ON ms.device_id = d.device_id
LEFT JOIN billing_point bp ON bp.point_id = ms.billing_point_id
WHERE d.device_type = 'meter'
  AND NOT EXISTS (
      SELECT 1 FROM meter_asset ma WHERE ma.device_id = d.device_id
  );

-- ---------------------------------------------------------------------------
-- Two spare meters for the demo household.
--
-- Not decoration: since c9e2f4a71b83 a consumer adds a meter by choosing one
-- the utility already issued them, so an account with none can only ever apply
-- and wait. That IS the intended flow -- and it is also demoable from both
-- ends only if somebody starts with stock in hand. Serials are prefixed the
-- way an unissued serial is minted at approval, so nothing here pretends to be
-- a manufacturer's number.
--
-- Keyed on the serial rather than counted, so re-running is a no-op instead of
-- handing out two more every time.
-- ---------------------------------------------------------------------------
INSERT INTO meter_asset (
    account_id, serial_no, manufacturer, model, issued_by_company_id
)
SELECT a.account_id,
       spare.serial_no,
       'Hexing',
       'HXE310-BD',
       (SELECT dc.company_id
        FROM distribution_company dc
        ORDER BY dc.code
        LIMIT 1)
FROM account a
CROSS JOIN (VALUES ('GSM-DEMO0001'), ('GSM-DEMO0002')) AS spare(serial_no)
WHERE a.email = 'consumer1@demo.com'
  AND NOT EXISTS (
      SELECT 1 FROM meter_asset ma WHERE ma.serial_no = spare.serial_no
  );

COMMIT;
