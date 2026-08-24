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
  ('SOLARIS', 'Solaris Bangladesh Ltd',   'SREDA-2019-0114',
   'hello@solaris.example',   '+8801711220001'),
  ('RAHIMA',  'Rahima Renewables',        'SREDA-2021-0298',
   'info@rahima.example',     '+8801711220002'),
  ('PADMA',   'Padma Solar Engineering',  'SREDA-2020-0071',
   'contact@padma.example',   '+8801711220003'),
  ('NOOR',    'Noor Energy Systems',      'SREDA-2022-0446',
   'sales@noorenergy.example', '+8801711220004')
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
-- Make one of the two demo workers a DESCO employee, so both branches of
-- worker requirement 1 are represented. The other stays private.
--
-- Ordered by employee_code so re-running picks the same worker every time.
-- ---------------------------------------------------------------------------
UPDATE worker_profile w
SET worker_kind = 'government',
    distribution_company_id = (
        SELECT company_id FROM distribution_company WHERE code = 'DESCO'
    )
WHERE w.account_id = (
    SELECT account_id FROM worker_profile ORDER BY employee_code LIMIT 1
);

-- ---------------------------------------------------------------------------
-- Give every 'government' account a government_profile.
--
-- The demo accounts predate migration e7c4b19a2d83: `gov@demo.com` was created
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

COMMIT;
