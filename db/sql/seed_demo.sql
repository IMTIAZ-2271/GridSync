-- GridSync demo seed. Pure SQL, no simulator.
--
-- 8 accounts / 8 Dhaka sites, each with a bidirectional billing meter.
-- Sites 1-5 also get an inverter, a solar array and an ACTIVE net metering
-- agreement. Sites 6-8 get PENDING agreements, for the approval queue.
-- 90 days of 30-minute readings, one TOU plan, plus workers, issues and
-- work orders.
--
-- Accounts are numbered rather than named: consumer1..consumer8@demo.com and
-- worker1..worker2@demo.com, each with a role-prefixed National ID. Sign-in
-- passwords are not set here -- db/sql/seed_auth.sql does that, for all ten.
--
-- Reproducible: setseed() is called before any random().
-- Safe to re-run: drops its own rows first, by the 'Seed ' / 'SEED-' prefixes
-- and by the ten account addresses it writes.

BEGIN;

SELECT setseed(0.42);

-- ---------------------------------------------------------------------------
-- Clean out a previous run. Order follows the FKs; most of it cascades from
-- account and site, but bills/periods are RESTRICT so they go explicitly.
-- ---------------------------------------------------------------------------
DELETE FROM work_order_assignment wa USING work_order w
  WHERE wa.order_id = w.order_id
    AND w.site_id IN (SELECT site_id FROM site WHERE label LIKE 'Seed %');
DELETE FROM work_order WHERE site_id IN
  (SELECT site_id FROM site WHERE label LIKE 'Seed %');
DELETE FROM issue_comment ic USING issue i
  WHERE ic.issue_id = i.issue_id
    AND i.site_id IN (SELECT site_id FROM site WHERE label LIKE 'Seed %');
DELETE FROM issue WHERE site_id IN
  (SELECT site_id FROM site WHERE label LIKE 'Seed %');
DELETE FROM credit_ledger WHERE site_id IN
  (SELECT site_id FROM site WHERE label LIKE 'Seed %');
DELETE FROM payment p USING bill b
  WHERE p.bill_id = b.bill_id
    AND b.site_id IN (SELECT site_id FROM site WHERE label LIKE 'Seed %');
DELETE FROM bill WHERE site_id IN
  (SELECT site_id FROM site WHERE label LIKE 'Seed %');
DELETE FROM billing_period WHERE site_id IN
  (SELECT site_id FROM site WHERE label LIKE 'Seed %');
DELETE FROM site WHERE label LIKE 'Seed %';
-- Exactly the ten addresses this file writes, not a prefix match: the demo
-- estate numbers every account consumerN / workerN, and the ones past the
-- seed's own range were registered through the app and are not its to drop.
DELETE FROM account WHERE email IN (
    SELECT format('consumer%s@demo.com', n) FROM generate_series(1, 8) AS n
    UNION ALL
    SELECT format('worker%s@demo.com', n) FROM generate_series(1, 2) AS n
);
DELETE FROM tariff_plan WHERE code = 'SEED-TOU-RES';

-- ---------------------------------------------------------------------------
-- Partitions covering the seed window (idempotent).
-- ---------------------------------------------------------------------------
SELECT create_reading_partition(d::date)
FROM generate_series(
       date_trunc('month', CURRENT_DATE - INTERVAL '95 days'),
       date_trunc('month', CURRENT_DATE + INTERVAL '35 days'),
       INTERVAL '1 month'
     ) AS d;

-- ---------------------------------------------------------------------------
-- Tariff: one plan, peak 17:00-23:00, off-peak everywhere else.
-- Off-peak is two rows per day type because a range cannot wrap midnight.
-- ---------------------------------------------------------------------------
INSERT INTO tariff_plan (
    code, name, customer_class, currency,
    fixed_monthly_charge, demand_charge_per_kw, tax_rate, effective_from
)
VALUES ('SEED-TOU-RES', 'Residential Time-of-Use', 'residential', 'BDT',
        120.0000, NULL, 0.0500, DATE '2026-01-01');

INSERT INTO tariff_rate (plan_id, period_name, day_type,
                         start_time, end_time, import_rate, export_credit_rate)
SELECT p.plan_id, w.period_name::tou_period, d.day_type::rate_day_type,
       w.start_time::time, w.end_time::time, w.import_rate, w.export_credit_rate
FROM tariff_plan p
CROSS JOIN (VALUES ('weekday'), ('weekend'), ('holiday')) AS d(day_type)
CROSS JOIN (VALUES
        ('off_peak', '00:00', '17:00',  7.250000, 5.500000),
        ('peak',     '17:00', '23:00', 11.800000, 8.900000),
        ('off_peak', '23:00', '24:00',  7.250000, 5.500000)
    ) AS w(period_name, start_time, end_time, import_rate, export_credit_rate)
WHERE p.code = 'SEED-TOU-RES';

-- ---------------------------------------------------------------------------
-- Accounts and sites. The temp table carries the index n through the run so
-- later inserts can join on it instead of guessing at ordering.
-- ---------------------------------------------------------------------------
CREATE TEMP TABLE seed_site (
    n            int PRIMARY KEY,
    account_id   uuid,
    site_id      uuid,
    point_id     uuid,
    meter_id     uuid,
    inverter_id  uuid,
    capacity_kw  numeric(8,3),   -- 0 for the non-solar sites
    meter_batch  uuid,
    inv_batch    uuid
) ON COMMIT DROP;

INSERT INTO seed_site (n, capacity_kw)
SELECT n, CASE WHEN n <= 5 THEN (2.5 + n * 0.75)::numeric(8,3) ELSE 0 END
FROM generate_series(1, 8) AS n;

WITH new_accounts AS (
    INSERT INTO account (email, password_hash, full_name, phone, national_id,
                         role, status)
    SELECT format('consumer%s@demo.com', s.n),
           '$argon2id$seed$notarealhash',
           -- The name follows the address, so the two cannot drift: every
           -- screen that identifies an account by name and every one that
           -- identifies it by email agree on which demo account it is.
           format('Consumer %s', s.n),
           format('+8801%s000%s', 700 + s.n, s.n),
           -- Ten digits, the shape registration accepts, with the leading 1
           -- naming the role: consumers are 1_000_000_0NN, workers 2_, the
           -- regulator 3_, suppliers 4_. See scripts/normalize_demo_accounts.py.
           (1000000000 + s.n)::text,
           'consumer', 'active'
    FROM seed_site s
    ORDER BY s.n
    RETURNING account_id, email
)
UPDATE seed_site s
SET account_id = a.account_id
FROM new_accounts a
WHERE a.email = format('consumer%s@demo.com', s.n);

WITH new_sites AS (
    INSERT INTO site (
        account_id, tariff_plan_id, label, address_line, city, district,
        postal_code, latitude, longitude, timezone, connection_type,
        sanctioned_load_kw, energized_on, status
    )
    SELECT s.account_id,
           p.plan_id,
           format('Seed Site %s', to_char(s.n, 'FM00')),
           -- The address names its district, which is what tells
           -- scripts/relocate_demo_sites.py this site is already where it
           -- belongs and must not be given a new door number.
           format('House %s, Road %s, %s', 10 + s.n * 3, 4 + s.n, d.suffix),
           'Dhaka',
           d.district,
           format('12%s0', s.n),
           d.latitude,
           d.longitude,
           'Asia/Dhaka',
           'residential',
           (4.0 + s.n * 0.5)::numeric(8,3),
           CURRENT_DATE - INTERVAL '2 years',
           'active'
    FROM seed_site s
    CROSS JOIN tariff_plan p
    -- Three districts, dealt round-robin: 3 in Dhanmondi, 3 in Badda, 2 in
    -- Uttara. Not eight districts any more -- an official governs exactly one
    -- district, and db/sql/seed_orgs.sql staffs three of them, so a site in a
    -- fourth is a household nobody can approve a meter for. The centroid is
    -- read off the district table rather than computed from `n`, because
    -- site.latitude/longitude is what the simulator's solar geometry will use.
    CROSS JOIN LATERAL (
        SELECT dd.name AS district, dd.latitude, dd.longitude,
               CASE dd.name WHEN 'Badda' THEN 'Middle Badda'
                            WHEN 'Uttara' THEN format('Sector %s, Uttara', 3 + s.n % 6)
                            ELSE dd.name END AS suffix
        FROM district dd
        WHERE dd.name = (ARRAY['Dhanmondi','Badda','Uttara'])[1 + (s.n - 1) % 3]
    ) AS d
    WHERE p.code = 'SEED-TOU-RES'
    ORDER BY s.n
    RETURNING site_id, label
)
UPDATE seed_site s
SET site_id = ns.site_id
FROM new_sites ns
WHERE ns.label = format('Seed Site %s', to_char(s.n, 'FM00'));

-- ---------------------------------------------------------------------------
-- Billing points: one connection per seed site.
--
-- The seed models the simple household -- one meter, one connection. The
-- schema allows several per site (migration d5a7c2b91e40) and rule 7 is
-- enforced per point, but adding a second connection here would change every
-- committed bill number this seed is checked against, so it stays at one.
-- ---------------------------------------------------------------------------
WITH new_points AS (
    INSERT INTO billing_point (site_id, label, reference)
    SELECT s.site_id, 'Main', format('SEED-CONN-%s', to_char(s.n, 'FM0000'))
    FROM seed_site s
    ORDER BY s.n
    RETURNING point_id, reference
)
UPDATE seed_site s
SET point_id = np.point_id
FROM new_points np
WHERE np.reference = format('SEED-CONN-%s', to_char(s.n, 'FM0000'));

-- ---------------------------------------------------------------------------
-- Billing meters: one bidirectional meter per billing point (rule 7).
-- ---------------------------------------------------------------------------
WITH new_meters AS (
    INSERT INTO device (site_id, device_type, serial_no, manufacturer, model,
                        firmware_version, interval_minutes, device_key_hash,
                        installed_at, last_seen_at, status)
    SELECT s.site_id, 'meter', format('SEED-MTR-%s', to_char(s.n, 'FM00')),
           'Hexing', 'HXE310-BD', '2.4.1', 30,
           '$argon2id$seed$devicekey',
           now() - INTERVAL '2 years', now(), 'active'
    FROM seed_site s
    ORDER BY s.n
    RETURNING device_id, serial_no
)
UPDATE seed_site s
SET meter_id = m.device_id
FROM new_meters m
WHERE m.serial_no = format('SEED-MTR-%s', to_char(s.n, 'FM00'));

INSERT INTO meter_spec (device_id, site_id, billing_point_id, meter_flow,
                        billing_role,
                        ct_ratio, max_current_amp, phase_count, seal_no)
SELECT s.meter_id, s.site_id, s.point_id, 'bidirectional', 'billing',
       '1:1', 60.0, 1, format('SEAL-%s', to_char(s.n, 'FM0000'))
FROM seed_site s;

-- ---------------------------------------------------------------------------
-- Solar: sites 1-5 get an inverter, an array and an active agreement.
-- ---------------------------------------------------------------------------
WITH new_inverters AS (
    INSERT INTO device (site_id, parent_device_id, device_type, serial_no,
                        manufacturer, model, firmware_version,
                        interval_minutes, device_key_hash, installed_at,
                        last_seen_at, status)
    SELECT s.site_id, s.meter_id, 'inverter',
           format('SEED-INV-%s', to_char(s.n, 'FM00')),
           'Growatt', 'MIN-5000TL-X', '1.9.0', 30,
           '$argon2id$seed$devicekey',
           now() - INTERVAL '18 months', now(), 'active'
    FROM seed_site s
    WHERE s.capacity_kw > 0
    ORDER BY s.n
    RETURNING device_id, serial_no
)
UPDATE seed_site s
SET inverter_id = i.device_id
FROM new_inverters i
WHERE i.serial_no = format('SEED-INV-%s', to_char(s.n, 'FM00'));

INSERT INTO inverter_spec (device_id, ac_capacity_kw, dc_capacity_kw,
                           mppt_count, phase_count, rated_efficiency,
                           anti_islanding)
SELECT s.inverter_id, s.capacity_kw, (s.capacity_kw * 1.2)::numeric(8,3),
       2, 1, 0.9720, true
FROM seed_site s
WHERE s.inverter_id IS NOT NULL;

INSERT INTO solar_array (site_id, inverter_device_id, label, panel_count,
                         panel_watt_peak, dc_capacity_kw, azimuth_deg,
                         tilt_deg, shading_factor, commissioned_on, status)
SELECT s.site_id, s.inverter_id, 'Rooftop array',
       ceil(s.capacity_kw * 1.2 * 1000 / 550.0)::smallint,
       550,
       (s.capacity_kw * 1.2)::numeric(8,3),
       180, 23, (0.92 + random() * 0.07)::numeric(4,3),
       (CURRENT_DATE - INTERVAL '18 months')::date, 'active'
FROM seed_site s
WHERE s.inverter_id IS NOT NULL;

-- Active agreements for the solar sites.
INSERT INTO net_metering_agreement (
    site_id, billing_point_id, billing_device_id, approval_ref,
    sanctioned_capacity_kw,
    export_cap_pct, settlement_type, credit_rollover_months,
    effective_from, status
)
SELECT s.site_id, s.point_id, s.meter_id,
       format('SEED-NMA-%s', to_char(s.n, 'FM0000')),
       s.capacity_kw, 70.00, 'rollover_only', 12,
       (CURRENT_DATE - INTERVAL '18 months')::date, 'active'
FROM seed_site s
WHERE s.capacity_kw > 0;

-- Pending agreements for the three non-solar sites: the approval queue.
INSERT INTO net_metering_agreement (
    site_id, billing_point_id, billing_device_id, approval_ref,
    sanctioned_capacity_kw,
    export_cap_pct, settlement_type, credit_rollover_months,
    effective_from, status
)
SELECT s.site_id, s.point_id, s.meter_id,
       format('SEED-NMA-PEND-%s', to_char(s.n, 'FM0000')),
       (3.0 + s.n * 0.5)::numeric(8,3), 70.00, 'rollover_only', 12,
       (CURRENT_DATE + INTERVAL '14 days')::date, 'pending'
FROM seed_site s
WHERE s.capacity_kw = 0;

-- ---------------------------------------------------------------------------
-- Field workers.
-- ---------------------------------------------------------------------------
INSERT INTO account (email, password_hash, full_name, phone, national_id,
                     role, status)
VALUES
  ('worker1@demo.com', '$argon2id$seed$notarealhash',
   'Worker 1', '+8801711000001', '2000000001', 'worker', 'active'),
  ('worker2@demo.com', '$argon2id$seed$notarealhash',
   'Worker 2', '+8801711000002', '2000000002', 'worker', 'active');

-- service_district is a foreign key to `district` since migration
-- e7c4b19a2d83, so these are real districts rather than the 'Dhaka North' /
-- 'Dhaka South' labels that used to sit here. Both are districts this estate
-- actually staffs; db/sql/seed_orgs.sql then restates these two rows along
-- with the eight technicians it adds, so the roster lives in one place.
INSERT INTO worker_profile (account_id, employee_code, service_district,
                            max_daily_jobs, availability, hired_on)
SELECT a.account_id,
       CASE a.email WHEN 'worker1@demo.com'
            THEN 'SEED-EMP-001' ELSE 'SEED-EMP-002' END,
       CASE a.email WHEN 'worker1@demo.com'
            THEN 'Badda' ELSE 'Dhanmondi' END,
       5,
       'available',
       (CURRENT_DATE - INTERVAL '3 years')::date
FROM account a
WHERE a.email IN ('worker1@demo.com', 'worker2@demo.com');

INSERT INTO worker_skill (account_id, skill_type, proficiency, certified_on)
SELECT w.account_id, sk.skill_type::worker_skill_type, 'expert',
       (CURRENT_DATE - INTERVAL '2 years')::date
FROM worker_profile w
JOIN account a ON a.account_id = w.account_id
CROSS JOIN (VALUES ('meter_install'), ('meter_swap'), ('inspection')) AS sk(skill_type)
WHERE a.email IN ('worker1@demo.com', 'worker2@demo.com');

-- ---------------------------------------------------------------------------
-- Ingest batches: one per device, so device_reading.ingest_batch_id is real.
-- ---------------------------------------------------------------------------
WITH new_batches AS (
    INSERT INTO ingest_batch (device_id, idempotency_key, reading_count,
                              accepted_count, client_ip)
    SELECT d.device_id,
           format('seed-batch-%s', d.serial_no),
           4320, 4320, '203.0.113.10'::inet
    FROM device d
    WHERE d.serial_no LIKE 'SEED-%'
    RETURNING batch_id, device_id
)
UPDATE seed_site s
SET meter_batch = mb.batch_id,
    inv_batch   = ib.batch_id
FROM (SELECT 1) AS _
LEFT JOIN new_batches mb ON true
LEFT JOIN new_batches ib ON true
WHERE mb.device_id = s.meter_id
  AND (s.inverter_id IS NULL OR ib.device_id = s.inverter_id)
  AND (s.inverter_id IS NOT NULL OR ib.device_id = s.meter_id);

-- ---------------------------------------------------------------------------
-- 90 days of 30-minute readings.
--
-- Household load  : 0.15 kWh base, +0.25 morning (07:00-09:00),
--                   +0.45 evening (18:00-22:00), plus noise.
-- Solar generation: half-sine peaking at 12:00, zero outside 06:00-18:00,
--                   scaled by capacity. kWh = kW * 0.5h for a 30-min interval.
-- Export          : max(0, generation - consumption)
-- Import          : max(0, consumption - generation)
--
-- Hours are read in Asia/Dhaka so the peaks land where a resident would see
-- them, not where UTC puts them.
-- ---------------------------------------------------------------------------
CREATE TEMP TABLE seed_interval ON COMMIT DROP AS
SELECT
    s.n,
    s.site_id,
    s.meter_id,
    s.inverter_id,
    s.capacity_kw,
    s.meter_batch,
    s.inv_batch,
    g.ts,
    -- consumption for this 30-minute interval, kWh
    round((
        0.15
        + CASE WHEN h.hr >= 7  AND h.hr < 9  THEN 0.25 ELSE 0 END
        + CASE WHEN h.hr >= 18 AND h.hr < 22 THEN 0.45 ELSE 0 END
        + random() * 0.06
    )::numeric, 4) AS consumption_kwh,
    -- generation for this 30-minute interval, kWh
    round((
        CASE WHEN s.capacity_kw > 0 AND h.hr > 6 AND h.hr < 18
             THEN s.capacity_kw * 0.5
                  * sin(pi() * (h.hr - 6) / 12.0)
                  * (0.82 + random() * 0.18)
             ELSE 0
        END
    )::numeric, 4) AS generation_kwh
FROM seed_site s
CROSS JOIN LATERAL generate_series(
        (date_trunc('day', (now() AT TIME ZONE 'Asia/Dhaka')
                           - INTERVAL '90 days')) AT TIME ZONE 'Asia/Dhaka',
        (date_trunc('day', (now() AT TIME ZONE 'Asia/Dhaka'))
                           - INTERVAL '30 minutes') AT TIME ZONE 'Asia/Dhaka',
        INTERVAL '30 minutes'
    ) AS g(ts)
CROSS JOIN LATERAL (
    SELECT EXTRACT(hour FROM g.ts AT TIME ZONE 'Asia/Dhaka')
           + EXTRACT(minute FROM g.ts AT TIME ZONE 'Asia/Dhaka') / 60.0 AS hr
) AS h;

-- Meter readings: import/export only (rule 6 -- the grid boundary).
INSERT INTO device_reading (
    device_id, interval_start, interval_minutes,
    import_kwh, export_kwh, generation_kwh,
    voltage_avg, frequency_avg, source, quality, ingest_batch_id
)
SELECT i.meter_id, i.ts, 30,
       greatest(0, i.consumption_kwh - i.generation_kwh)::numeric(12,4),
       greatest(0, i.generation_kwh - i.consumption_kwh)::numeric(12,4),
       NULL,
       round((228 + random() * 8)::numeric, 2),
       round((49.9 + random() * 0.2)::numeric, 3),
       'device', 'good', i.meter_batch
FROM seed_interval i;

-- Inverter readings: generation only.
INSERT INTO device_reading (
    device_id, interval_start, interval_minutes,
    import_kwh, export_kwh, generation_kwh,
    dc_voltage_avg, source, quality, ingest_batch_id
)
SELECT i.inverter_id, i.ts, 30,
       NULL, NULL, i.generation_kwh,
       round((330 + random() * 40)::numeric, 2),
       'device', 'good', i.inv_batch
FROM seed_interval i
WHERE i.inverter_id IS NOT NULL;

-- ---------------------------------------------------------------------------
-- Issues: six, in mixed states. 'duplicate' is skipped -- the schema requires
-- duplicate_of_issue_id to be set exactly when status = 'duplicate'.
-- ---------------------------------------------------------------------------
INSERT INTO issue (
    reported_by_account_id, site_id, device_id, category, severity, status,
    title, description, priority, reported_at, sla_due_at,
    acknowledged_at, resolved_at, closed_at, resolution_notes
)
SELECT s.account_id, s.site_id,
       CASE WHEN v.category IN ('meter_fault', 'data_gap')
            THEN s.meter_id ELSE NULL END,
       v.category::issue_category, v.severity::issue_severity,
       v.status::issue_status, v.title, v.description, v.priority,
       now() - (v.age_days || ' days')::interval,
       now() - (v.age_days || ' days')::interval + v.sla,
       CASE WHEN v.status <> 'open'
            THEN now() - (v.age_days || ' days')::interval + INTERVAL '2 hours'
       END,
       CASE WHEN v.status IN ('resolved', 'closed')
            THEN now() - (v.age_days || ' days')::interval + INTERVAL '2 days'
       END,
       CASE WHEN v.status = 'closed'
            THEN now() - (v.age_days || ' days')::interval + INTERVAL '3 days'
       END,
       CASE WHEN v.status IN ('resolved', 'closed') THEN v.resolution END
FROM (VALUES
    (1, 'export_not_credited', 'high',     'open',        'Export not credited for July', 'Meter shows export but the bill has no credit line.', 2,  4, INTERVAL '8 hours',  NULL),
    (2, 'meter_fault',         'critical', 'acknowledged','Meter display blank',          'No reading since Tuesday morning.',                    1,  6, INTERVAL '4 hours',  NULL),
    (3, 'inverter_fault',      'medium',   'in_progress', 'Inverter tripping at midday',  'Anti-islanding trips around noon on clear days.',      3,  9, INTERVAL '24 hours', NULL),
    (4, 'billing_dispute',     'medium',   'resolved',    'August bill looks too high',   'Charge is roughly double the previous month.',         3, 15, INTERVAL '24 hours', 'Recalculated against TOU windows; original bill was correct.'),
    (5, 'data_gap',            'low',      'closed',      'Missing intervals on the 3rd', 'Six hours of readings absent.',                        4, 22, INTERVAL '72 hours', 'Backfilled from the device buffer.'),
    (6, 'outage',              'high',     'open',        'Power out since 06:00',        'Whole street affected.',                               2,  1, INTERVAL '8 hours',  NULL)
) AS v(n, category, severity, status, title, description, priority, age_days, sla, resolution)
JOIN seed_site s ON s.n = v.n;

INSERT INTO issue_comment (issue_id, comment_id, author_account_id, body,
                           is_internal, created_at)
SELECT i.issue_id, 1, i.reported_by_account_id,
       'Reported via the consumer portal.', false, i.reported_at
FROM issue i
WHERE i.site_id IN (SELECT site_id FROM seed_site);

INSERT INTO issue_comment (issue_id, comment_id, author_account_id, body,
                           is_internal, created_at)
SELECT i.issue_id, 2, w.account_id,
       'Dispatched to the district team for a site visit.', true,
       i.reported_at + INTERVAL '3 hours'
FROM issue i
CROSS JOIN LATERAL (
    SELECT wp.account_id FROM worker_profile wp
    JOIN account a ON a.account_id = wp.account_id
    WHERE a.email = 'worker1@demo.com'
) AS w
WHERE i.site_id IN (SELECT site_id FROM seed_site)
  AND i.status <> 'open';

-- ---------------------------------------------------------------------------
-- Work orders: six across pending / assigned / done.
-- one_live_order_per_issue allows at most one OPEN order per issue, so two
-- orders are raised without an issue (routine scheduled work).
-- ---------------------------------------------------------------------------
CREATE TEMP TABLE seed_order (n int, order_id uuid) ON COMMIT DROP;

WITH creator AS (
    SELECT account_id FROM account
    WHERE email = 'worker1@demo.com'
),
targets AS (
    SELECT v.n, v.order_type, v.status, v.priority, v.link_issue,
           s.site_id, s.meter_id,
           (SELECT i.issue_id FROM issue i
             WHERE i.site_id = s.site_id
             ORDER BY i.reported_at LIMIT 1) AS issue_id
    FROM (VALUES
        (1, 'meter_swap',       'scheduled',   2, true),
        (2, 'inverter_service', 'dispatched',  1, true),
        (3, 'inspection',       'in_progress', 3, true),
        (4, 'seal_check',       'completed',   4, true),
        (5, 'meter_install',    'draft',       3, false),
        (6, 'inspection',       'draft',       5, false)
    ) AS v(n, order_type, status, priority, link_issue)
    JOIN seed_site s ON s.n = v.n
),
inserted AS (
    INSERT INTO work_order (
        issue_id, site_id, device_id, created_by_account_id, order_type,
        status, priority, scheduled_for, started_at, completed_at,
        completion_notes, created_at
    )
    SELECT CASE WHEN t.link_issue THEN t.issue_id END,
           t.site_id, t.meter_id, c.account_id,
           t.order_type::work_order_type, t.status::work_order_status,
           t.priority,
           now() + ((t.n * 2) || ' days')::interval,
           CASE WHEN t.status IN ('in_progress', 'completed')
                THEN now() - INTERVAL '2 days' END,
           CASE WHEN t.status = 'completed'
                THEN now() - INTERVAL '1 day' END,
           CASE WHEN t.status = 'completed'
                THEN 'Seal intact, meter reading verified against portal.' END,
           now() - ((t.n + 3) || ' days')::interval
    FROM targets t CROSS JOIN creator c
    ORDER BY t.n
    RETURNING order_id, site_id
)
INSERT INTO seed_order (n, order_id)
SELECT s.n, ins.order_id
FROM inserted ins
JOIN seed_site s ON s.site_id = ins.site_id;

-- One lead per order (one_lead_per_order), plus an assistant on the busy ones.
INSERT INTO work_order_assignment (order_id, account_id, job_role, status,
                                   assigned_at, responded_at)
SELECT o.order_id, w.account_id, 'lead',
       CASE WHEN o.n IN (4) THEN 'completed'
            WHEN o.n IN (1, 2, 3) THEN 'accepted'
            ELSE 'offered' END::assignment_status,
       now() - INTERVAL '3 days',
       CASE WHEN o.n <= 4 THEN now() - INTERVAL '3 days' + INTERVAL '1 hour' END
FROM seed_order o
CROSS JOIN LATERAL (
    SELECT wp.account_id FROM worker_profile wp
    JOIN account a ON a.account_id = wp.account_id
    WHERE a.email = 'worker1@demo.com'
) AS w;

INSERT INTO work_order_assignment (order_id, account_id, job_role, status,
                                   assigned_at, responded_at)
SELECT o.order_id, w.account_id, 'assistant',
       CASE WHEN o.n = 4 THEN 'completed' ELSE 'accepted' END::assignment_status,
       now() - INTERVAL '3 days',
       now() - INTERVAL '3 days' + INTERVAL '2 hours'
FROM seed_order o
CROSS JOIN LATERAL (
    SELECT wp.account_id FROM worker_profile wp
    JOIN account a ON a.account_id = wp.account_id
    WHERE a.email = 'worker2@demo.com'
) AS w
WHERE o.n IN (2, 3, 4);

COMMIT;
