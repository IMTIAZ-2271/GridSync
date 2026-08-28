-- Site DAO: read side of the site/device/onboarding surface.
--
-- Every statement here lives in SQL, not inline in Python: the reading path is
-- raw SQL by design (CLAUDE.md, "no ORM on the billing or reading path").
-- services/api/queries.py loads every file under db/sql/dao/ and splits each
-- on the `-- name:` markers; the name is how a route handler asks for a
-- statement.
--
-- Two invariants show up in almost every aggregate below and are worth stating
-- once:
--
--   * import_kwh / export_kwh are read ONLY from the site's billing meter.
--     The LEFT JOIN to meter_spec plus FILTER (WHERE ms.billing_role =
--     'billing') is what does it -- inverters have no meter_spec row and fall
--     out of the filter; a check_meter is excluded by name.
--
--     Excluding the check meter is about arithmetic, not permissions.
--     docs/decisions.md is explicit that a check_meter legitimately measures
--     the grid boundary too -- billing_role governs whether a reading counts
--     toward a bill, not whether it may exist. But it measures the SAME
--     energy the billing meter does, so summing both would double every kWh
--     on a site that has one. Rule 7 guarantees exactly one 'billing' device
--     per site, which is what makes that filter a safe de-duplicator.
--   * generation_kwh is summed unfiltered, because only inverters report it
--     (rule 6). A meter never knows generation; it knows the import/export
--     split.
--
-- Time windows use now() minus an interval, which is an absolute instant and
-- so is immune to the session-TimeZone hazard that governs DDL here. No query
-- in this file computes a calendar boundary, except site_billing_window,
-- which pins its zone explicitly for exactly that reason.
--
-- Every COALESCE'd zero is cast back to the column's own scale --
-- ::numeric(12,4) for energy, ::numeric(14,4) for money. Without the cast the
-- fallback is an integer literal and serializes as "0" while a measured zero
-- serializes as "0.0000", so a site that reported nothing and a site that
-- genuinely exported nothing would come out looking like different kinds of
-- value to the client.


-- name: list_sites
-- Site picker for the dashboard. has_solar is derived from the presence of a
-- live array rather than stored, so decommissioning the last array flips it.
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
ORDER BY s.label;


-- name: site_summary
-- One row per site: the latest bill, the current credit balance, and 30-day
-- energy totals. Driven FROM site so a missing site yields zero rows and the
-- handler can answer 404 instead of inventing an empty summary.
--
-- $2 narrows every figure to one billing point, or NULL for the whole site.
-- That is consumer requirement 5's second half: a household that has picked a
-- meter is asking about that meter, and a credit balance summed over
-- connections it did not select is a number that belongs to nothing on screen.
-- The filter is repeated in each subquery rather than applied once at the end
-- because the three are independent aggregates over different tables --
-- there is no single row to filter.
--
-- The bill is spread across scalar columns rather than nested as jsonb on
-- purpose: jsonb would round-trip the money through a JSON number and lose the
-- NUMERIC exactness that rule 5 exists to protect.
SELECT s.site_id,
       s.label,
       s.district,
       -- Balances are a running total already materialized on the ledger, so
       -- the newest entry IS the balance. entry_id is the append-only
       -- sequence, which makes it the ordering key -- created_at can tie when
       -- several entries are posted inside one billing transaction.
       COALESCE(bal.balance_kwh_after, 0)::numeric(12,4) AS credit_balance_kwh,
       COALESCE(bal.balance_amount_after, 0)::numeric(14,4) AS credit_balance_amount,
       win.import_kwh     AS window_import_kwh,
       win.export_kwh     AS window_export_kwh,
       win.generation_kwh AS window_generation_kwh,
       lb.bill_id,
       lb.point_label          AS bill_point_label,
       lb.period_start         AS bill_period_start,
       lb.period_end           AS bill_period_end,
       lb.currency             AS bill_currency,
       lb.energy_charge        AS bill_energy_charge,
       lb.export_credit_earned AS bill_export_credit_earned,
       lb.fixed_charge         AS bill_fixed_charge,
       lb.tax_amount           AS bill_tax_amount,
       lb.gross_amount         AS bill_gross_amount,
       lb.credit_applied_kwh    AS bill_credit_applied_kwh,
       lb.credit_applied_amount AS bill_credit_applied_amount,
       lb.credit_closing_kwh    AS bill_credit_closing_kwh,
       lb.amount_due           AS bill_amount_due,
       lb.due_date             AS bill_due_date,
       lb.issued_at            AS bill_issued_at,
       lb.status               AS bill_status
FROM site s
LEFT JOIN LATERAL (
    SELECT b.*, bp.period_start, bp.period_end, pt.label AS point_label
    FROM bill b
    JOIN billing_period bp ON bp.period_id = b.period_id
    JOIN billing_point pt ON pt.point_id = b.billing_point_id
    WHERE b.site_id = s.site_id
      AND ($2::uuid IS NULL OR b.billing_point_id = $2)
      -- A voided bill has been superseded by a correction (rule 1); showing it
      -- as "latest" would show the consumer a number nobody owes.
      AND b.status <> 'void'
    ORDER BY bp.period_start DESC, b.issued_at DESC
    LIMIT 1
) lb ON TRUE
LEFT JOIN LATERAL (
    -- Summed over the site's billing points, not read off one row. Each
    -- point keeps its own running balance, so a site with two connections
    -- has two of them and the household's balance is the total. The inner
    -- LIMIT 1 is still "the newest entry IS the balance", applied per point;
    -- CROSS JOIN drops points that have never earned anything.
    SELECT COALESCE(SUM(latest.balance_kwh_after), 0)::numeric(12,4)
               AS balance_kwh_after,
           COALESCE(SUM(latest.balance_amount_after), 0)::numeric(14,4)
               AS balance_amount_after
    FROM billing_point pt
    CROSS JOIN LATERAL (
        SELECT cl.balance_kwh_after, cl.balance_amount_after
        FROM credit_ledger cl
        WHERE cl.billing_point_id = pt.point_id
        ORDER BY cl.entry_id DESC
        LIMIT 1
    ) latest
    WHERE pt.site_id = s.site_id
      AND ($2::uuid IS NULL OR pt.point_id = $2)
) bal ON TRUE
CROSS JOIN LATERAL (
    SELECT COALESCE(SUM(r.import_kwh) FILTER (WHERE ms.billing_role = 'billing'), 0)::numeric(12,4) AS import_kwh,
           COALESCE(SUM(r.export_kwh) FILTER (WHERE ms.billing_role = 'billing'), 0)::numeric(12,4) AS export_kwh,
           COALESCE(SUM(r.generation_kwh), 0)::numeric(12,4)                                        AS generation_kwh
    FROM device_reading r
    JOIN device d ON d.device_id = r.device_id
    LEFT JOIN meter_spec ms ON ms.device_id = d.device_id
    -- An inverter names its own point since migration d4f8a2c61e95, so the
    -- solar half of a per-connection view comes off inverter_spec rather than
    -- being chased through the meter the inverter happens to hang behind.
    LEFT JOIN inverter_spec ivs ON ivs.device_id = d.device_id
    WHERE d.site_id = s.site_id
      AND ($2::uuid IS NULL
           OR ms.billing_point_id = $2
           OR ivs.billing_point_id = $2)
      AND r.interval_start >= now() - INTERVAL '30 days'
) win
WHERE s.site_id = $1;


-- name: site_readings
-- Series for the chart. Meter and inverter report on separate rows of
-- device_reading, so they are folded back together on the bucket: one row out
-- per bucket carrying all three measures.
--
-- Grouping (rather than joining meter rows to inverter rows) is what keeps a
-- meter swap mid-window from splitting the series in two -- the replacement
-- device's rows land in the same buckets.
--
-- $2 is the timeframe (consumer requirement 4) and it picks the window AND the
-- bucket together, because the two cannot be chosen independently: a year of
-- half-hourly points is 17,520 marks on a chart 700px wide, which is a smear,
-- and a day of monthly buckets is one bar. Both are derived here rather than
-- in the handler so the client cannot ask for a combination that does not
-- render, and so the zone is pinned in one place.
--
-- Every timestamp is truncated **in Asia/Dhaka and converted back**, never in
-- the session zone. `date_trunc('day', ts)` alone resolves against the
-- caller's TimeZone, so the same request would bucket differently for a client
-- connecting from another zone and the daily bars would straddle midnight --
-- the same class of bug the partitioning tests exist to catch.
--
-- $3 narrows to one billing point, or NULL for the whole site. Both subtypes
-- name their own point, so the filter is an OR across meter_spec and
-- inverter_spec: without the inverter half, selecting a connection would show
-- its import and hide the generation measured behind the very same meter.
WITH w AS (
    SELECT CASE $2::text
               -- Rolling 23 hours back from the current hour, not midnight
               -- today. All four windows are rolling, and a calendar day is
               -- the one that breaks: readings are written through
               -- *yesterday* (there is no ingest service -- CLAUDE.md, NOT
               -- DONE), so "today" would be a permanently empty chart on the
               -- cheapest timeframe to click.
               WHEN 'day'   THEN date_trunc('hour', now() AT TIME ZONE 'Asia/Dhaka') - INTERVAL '23 hours'
               WHEN 'week'  THEN date_trunc('day', now() AT TIME ZONE 'Asia/Dhaka') - INTERVAL '6 days'
               WHEN 'month' THEN date_trunc('day', now() AT TIME ZONE 'Asia/Dhaka') - INTERVAL '29 days'
               WHEN 'year'  THEN date_trunc('month', now() AT TIME ZONE 'Asia/Dhaka') - INTERVAL '11 months'
           END AT TIME ZONE 'Asia/Dhaka' AS from_ts,
           CASE $2::text
               WHEN 'day'   THEN 'hour'
               WHEN 'week'  THEN 'interval'
               WHEN 'month' THEN 'day'
               WHEN 'year'  THEN 'month'
           END AS bucket
)
SELECT CASE w.bucket
           WHEN 'interval' THEN r.interval_start
           ELSE date_trunc(w.bucket, r.interval_start AT TIME ZONE 'Asia/Dhaka')
                    AT TIME ZONE 'Asia/Dhaka'
       END AS interval_start,
       COALESCE(SUM(r.import_kwh) FILTER (WHERE ms.billing_role = 'billing'), 0)::numeric(12,4) AS import_kwh,
       COALESCE(SUM(r.export_kwh) FILTER (WHERE ms.billing_role = 'billing'), 0)::numeric(12,4) AS export_kwh,
       COALESCE(SUM(r.generation_kwh), 0)::numeric(12,4)                                        AS generation_kwh
FROM w
CROSS JOIN device_reading r
JOIN device d ON d.device_id = r.device_id
LEFT JOIN meter_spec ms ON ms.device_id = d.device_id
LEFT JOIN inverter_spec ivs ON ivs.device_id = d.device_id
WHERE d.site_id = $1
  AND r.interval_start >= w.from_ts
  AND ($3::uuid IS NULL
       OR ms.billing_point_id = $3
       OR ivs.billing_point_id = $3)
GROUP BY 1
ORDER BY 1;


-- name: site_bills
-- Bill headers for one site, newest period first. Line items come from
-- site_bill_line_items and are stitched on in Python -- a json_agg here would
-- push rate_applied and amount through JSON numbers, which is exactly the
-- lossy step rule 5 forbids.
SELECT b.bill_id,
       b.period_id,
       b.billing_point_id,
       pt.label AS point_label,
       pt.reference AS point_reference,
       bp.period_start,
       bp.period_end,
       bp.coverage_pct,
       -- The period's metered totals, frozen when the period was billed. Read
       -- from billing_period rather than re-aggregated from device_reading:
       -- re-summing would quietly pick up any late_reading that arrived after
       -- the bill closed, and disagree with the line items below it.
       bp.total_import_kwh,
       bp.total_export_kwh,
       bp.total_generation_kwh,
       b.currency,
       b.energy_charge,
       b.export_credit_earned,
       b.fixed_charge,
       b.tax_amount,
       b.gross_amount,
       b.credit_opening_kwh,
       b.credit_applied_kwh,
       b.credit_applied_amount,
       b.credit_closing_kwh,
       b.amount_due,
       b.due_date,
       b.issued_at,
       b.status,
       b.voided_by_bill_id
FROM bill b
JOIN billing_period bp ON bp.period_id = b.period_id
JOIN billing_point pt ON pt.point_id = b.billing_point_id
WHERE b.site_id = $1
ORDER BY bp.period_start DESC, pt.label, b.issued_at DESC;


-- name: site_bill_line_items
-- All line items for one site's bills in a single round trip. rate_applied is
-- read straight off the line item, never re-derived from tariff_rate: the
-- snapshot is the whole point (rule 2), and re-joining would silently restate
-- an old bill at today's rates.
SELECT li.bill_id,
       li.sort_order,
       li.line_type,
       li.period_name,
       li.quantity_kwh,
       li.rate_applied,
       li.amount
FROM bill_line_item li
JOIN bill b ON b.bill_id = li.bill_id
WHERE b.site_id = $1
ORDER BY li.bill_id, li.sort_order;


-- ---------------------------------------------------------------------------
-- Onboarding: a consumer with no site building one from scratch.
-- POST /api/sites, then /meter, then optionally /solar, then /bill.
-- ---------------------------------------------------------------------------

-- name: list_tariff_plans
-- Currently-effective plans, optionally narrowed to one connection type.
-- $1 is nullable -- a NULL parameter makes the filter a no-op rather than a
-- second statement, since the onboarding form may ask before or after the
-- consumer has picked a connection type.
SELECT plan_id,
       code,
       name,
       customer_class::text AS customer_class,
       currency,
       fixed_monthly_charge,
       tax_rate
FROM tariff_plan
WHERE effective_from <= CURRENT_DATE
  AND (effective_to IS NULL OR effective_to > CURRENT_DATE)
  AND ($1::site_connection_type IS NULL OR customer_class = $1::site_connection_type)
ORDER BY customer_class, name;


-- name: create_site
-- latitude/longitude are resolved server-side from the district (the form
-- collects an address, not coordinates) -- see services/api/routes_sites.py.
INSERT INTO site (
    account_id, tariff_plan_id, label, address_line, city, district,
    postal_code, latitude, longitude, timezone, connection_type,
    sanctioned_load_kw, energized_on, status
)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, 'Asia/Dhaka',
        $10::site_connection_type, $11, CURRENT_DATE, 'active')
RETURNING site_id;


-- name: get_site
-- Same projection as list_sites / sites_for_account, for one row -- used to
-- render what create_site just inserted.
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
WHERE s.site_id = $1;


-- name: point_has_billing_meter
-- Rule 7 pre-check, before the deferred trigger would catch it at COMMIT --
-- gives the caller a 409 instead of a mid-transaction constraint failure.
--
-- Keyed on the billing point, not the site: since migration d5a7c2b91e40 a
-- site may carry several billing meters, one per point, and asking the old
-- site-wide question would refuse the second connection a household is
-- entitled to add.
SELECT EXISTS (
    SELECT 1
    FROM meter_spec ms
    JOIN device d ON d.device_id = ms.device_id
    WHERE ms.billing_point_id = $1
      AND ms.billing_role = 'billing'
      AND d.removed_at IS NULL
);


-- name: create_billing_point
-- One metering position on a site: the "billing meter ID" a consumer adds.
--
-- point_label_per_site refuses a duplicate label within the site and
-- point_reference_unique refuses a connection number already registered
-- anywhere; both reach the handler as a UniqueViolation and become a 409,
-- rather than silently producing two points that mean the same connection.
INSERT INTO billing_point (site_id, label, reference)
VALUES ($1, btrim($2), nullif(btrim($3), ''))
RETURNING point_id, label, reference;


-- name: site_points
-- Every live billing point on a site, with the meter serving it and whether
-- that connection carries solar. Drives the meter switcher and the "this
-- point has no meter yet" affordance.
--
-- The meter join is LEFT: a point created but not yet metered is a legal
-- state (migration d5a7c2b91e40 gave every existing site one, metered or
-- not), and it is exactly the state the add-a-meter step is for.
--
-- LATERAL, not a plain join. Rule 7 constrains one *active* billing meter per
-- point, but `meter_spec` keeps the retired ones -- so a connection whose
-- meter has been swapped has two billing meter_spec rows, and joining them
-- flat returned the point TWICE: once with the new serial and once with NULLs
-- where the retired device failed `removed_at IS NULL`. Whichever row the
-- client read first decided whether the page thought the connection had a
-- meter at all. Found by the net-metering swap, which is the first thing in
-- the system that retires a meter.
SELECT pt.point_id,
       pt.label,
       pt.reference,
       pt.created_at,
       meter.device_id    AS meter_device_id,
       meter.serial_no    AS meter_serial,
       meter.last_seen_at AS meter_last_seen_at,
       EXISTS (
           SELECT 1
           FROM solar_array sa
           JOIN device inv ON inv.device_id = sa.inverter_device_id
           JOIN inverter_spec ivs ON ivs.device_id = sa.inverter_device_id
           WHERE sa.status <> 'decommissioned'
             AND inv.removed_at IS NULL
             -- Keyed on the point, not on `meter.device_id`. A connection can
             -- carry solar while its meter is mid-swap (or absent), and
             -- hanging this off the meter made has_solar go false for the
             -- moment the old meter was retired.
             AND ivs.billing_point_id = pt.point_id
       ) AS has_solar
FROM billing_point pt
LEFT JOIN LATERAL (
    SELECT d.device_id, d.serial_no, d.last_seen_at
    FROM meter_spec ms
    JOIN device d ON d.device_id = ms.device_id
    WHERE ms.billing_point_id = pt.point_id
      AND ms.billing_role = 'billing'
      AND d.removed_at IS NULL
    -- Belt and braces: rule 7's triggers already guarantee at most one, and
    -- one row out per point is what the caller is entitled to assume.
    LIMIT 1
) meter ON TRUE
WHERE pt.site_id = $1
  AND pt.retired_at IS NULL
ORDER BY pt.created_at, pt.label;


-- name: site_point_ids
-- Just the point ids, oldest first. The billing loop walks these; keeping it
-- separate from site_points avoids dragging the meter and solar joins
-- through a call that only needs keys.
SELECT point_id
FROM billing_point
WHERE site_id = $1 AND retired_at IS NULL
ORDER BY created_at, label;


-- name: create_meter_device
INSERT INTO device (
    site_id, device_type, serial_no, manufacturer, model,
    interval_minutes, device_key_hash, installed_at, status
)
VALUES ($1, 'meter', $2, $3, $4, 30, $5, now(), 'active')
RETURNING device_id;


-- name: create_meter_spec
-- Always billing -- this path registers the one meter rule 7 requires *per
-- billing point*. ct_ratio and phase_count are sane installer defaults, not
-- consumer input.
--
-- $4 is meter_flow, and it is a parameter rather than the literal
-- 'bidirectional' it used to be. An ordinary connection gets a
-- *unidirectional* meter: it measures what the household draws and cannot
-- know an export split (rule 6). A meter only becomes bidirectional as the
-- outcome of a net-metering agreement, which is the one act that makes export
-- measurable. Hardcoding 'bidirectional' made every meter in the system look
-- net-metered, and left `swappable_meters_for_site` with nothing to offer.
INSERT INTO meter_spec (
    device_id, site_id, billing_point_id, meter_flow, billing_role,
    ct_ratio, phase_count
)
VALUES ($1, $2, $3, $4::meter_flow, 'billing', '1:1', 1);


-- name: point_billing_device
SELECT d.device_id
FROM device d
JOIN meter_spec ms ON ms.device_id = d.device_id
WHERE ms.billing_point_id = $1
  AND ms.billing_role = 'billing'
  AND d.removed_at IS NULL;


-- name: create_inverter_device
-- $2 (parent_device_id) is physical topology and may be NULL: since migration
-- d4f8a2c61e95 an inverter can be installed on a site that has no meter at
-- all, which is the ordinary case when panels are fitted before the household
-- applies for net metering.
INSERT INTO device (
    site_id, parent_device_id, device_type, serial_no, manufacturer, model,
    interval_minutes, device_key_hash, installed_at, status
)
VALUES ($1, $2, 'inverter', $3, $4, $5, 30, $6, now(), 'active')
RETURNING device_id;


-- name: create_inverter_spec
-- ac_capacity_kw is the clipping ceiling; dc_capacity_kw is set ~20% above it
-- by the caller, matching db/sql/seed_demo.sql's ratio.
--
-- $3 (billing_point_id) is the connection this inverter generates behind, and
-- is NULL until net metering is granted. That is the normal state of freshly
-- installed panels, not an error: generation is measured by the inverter and
-- has no bearing on a bill until a bidirectional meter exists (rule 6).
INSERT INTO inverter_spec (
    device_id, site_id, billing_point_id,
    ac_capacity_kw, dc_capacity_kw, mppt_count, phase_count,
    rated_efficiency, anti_islanding
)
VALUES ($1, $2, $3, $4, $5, 2, 1, 0.9720, true);


-- name: create_solar_array
-- label is a parameter, not a literal: a site may carry several arrays
-- (solar_array is 1-N on site), so the second one must not also be called
-- "Rooftop array".
INSERT INTO solar_array (
    site_id, inverter_device_id, label, panel_count, panel_watt_peak,
    dc_capacity_kw, azimuth_deg, tilt_deg, shading_factor, commissioned_on, status
)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, 0.950, CURRENT_DATE, 'active')
RETURNING array_id;


-- name: create_net_metering_agreement
-- status = 'pending': a new site's agreement joins the same approval queue
-- db/sql/seed_demo.sql seeds for its non-solar sites, reviewed on
-- /government/agreements rather than auto-approved on registration.
--
-- $6 is the inverter the application was assessed against. Stored rather than
-- re-derived: the verdict is a measurement of one specific roof over one
-- specific 30 days, and an agreement that did not name the hardware would be
-- a decision nobody could re-check afterwards.
INSERT INTO net_metering_agreement (
    site_id, billing_point_id, billing_device_id, approval_ref,
    sanctioned_capacity_kw, inverter_device_id,
    export_cap_pct, settlement_type, credit_rollover_months,
    effective_from, status
)
VALUES ($1, $2, $3, $4, $5, $6, 70.00, 'rollover_only', 12, CURRENT_DATE,
        'pending')
RETURNING agreement_id;


-- name: point_solar_status
-- What solar this billing point already carries, before another array is
-- added.
--
-- capacity_kw is the AC total, summed over DISTINCT inverters: that is the
-- unit backfill_readings' p_capacity_kw is in (it scales a half-sine peaking
-- at the inverter's AC rating), and one inverter can drive more than one
-- array, so summing per array would double-count its clipping ceiling.
--
-- Decommissioned arrays and removed inverters are excluded -- an array that
-- no longer exists must not keep inflating the meter's netted export.
--
-- Scoped by inverter_spec.billing_point_id rather than by site: with several
-- billing meters on one site, netting a point's meter against the site's
-- whole fleet of arrays would credit one connection for another's export.
-- Panels not yet attached to any connection are correctly invisible here.
WITH live AS (
    SELECT sa.array_id, sa.inverter_device_id
    FROM solar_array sa
    JOIN device d ON d.device_id = sa.inverter_device_id
    JOIN inverter_spec ivs ON ivs.device_id = sa.inverter_device_id
    WHERE ivs.billing_point_id = $1
      AND sa.status <> 'decommissioned'
      AND d.removed_at IS NULL
)
SELECT (SELECT count(*) FROM live)::int AS array_count,
       coalesce((
           SELECT sum(inv.ac_capacity_kw)
           FROM inverter_spec inv
           WHERE inv.device_id IN (SELECT DISTINCT inverter_device_id FROM live)
       ), 0)::numeric AS capacity_kw;


-- name: point_open_agreement
-- This billing point's live net-metering agreement, if it has one.
--
-- nma_no_overlap is a GiST exclusion on (billing_point_id,
-- [effective_from, effective_to)) for every status except 'terminated', so a
-- point can hold at most one open-ended agreement at a time -- two
-- connections at one site each get their own. A second array joins the
-- agreement already covering the point rather than opening a competing one --
-- inserting blindly raises an exclusion violation, and UPDATEing the
-- sanctioned capacity of a live agreement would contradict rule 1 (a raised
-- capacity is a new agreement with its own effective_from, not an edit).
SELECT agreement_id,
       sanctioned_capacity_kw,
       status,
       effective_from,
       -- The inverter this agreement was granted for. The meter installation
       -- reads it to bring those panels into the connection: attaching them
       -- any earlier would claim an export the connection could not yet
       -- measure.
       inverter_device_id
FROM net_metering_agreement
WHERE billing_point_id = $1
  AND status <> 'terminated'
ORDER BY effective_from DESC
LIMIT 1;


-- name: point_billing_window
-- The earliest month this point's billing meter has readings for, and
-- "today" read in the same session zone the caller is pinned to -- so the
-- handler's month-by-month loop agrees with the database about which months
-- have actually finished.
SELECT min(date_trunc('month', dr.interval_start AT TIME ZONE 'Asia/Dhaka'))::date AS first_month,
       CURRENT_DATE AS today
FROM device_reading dr
JOIN meter_spec ms ON ms.device_id = dr.device_id
WHERE ms.billing_point_id = $1
  AND ms.billing_role = 'billing';


-- name: get_consumption_limit
-- The household's monthly budget, plus what it has spent against it so far.
--
-- Usage is computed here rather than left to the client because it is the same
-- arithmetic the jobs runner alerts on (sites_over_consumption_limit in
-- jobs_queries.sql), and two derivations of "how much have I used" that could
-- disagree is worse than one that is duplicated. Both read month-to-date import
-- across ALL of the site's billing meters -- rule 3, the limit is the
-- household's, not the connection's.
--
-- Returns a row even when no limit is set (monthly_kwh IS NULL), so the settings
-- form can show current usage before the household has decided on a figure.
WITH bounds AS (
    SELECT date_trunc('month', CURRENT_DATE)::date AS month_start,
           date_trunc('month', CURRENT_DATE::timestamp)
               AT TIME ZONE 'Asia/Dhaka' AS window_from,
           (date_trunc('month', CURRENT_DATE::timestamp) + INTERVAL '1 month')
               AT TIME ZONE 'Asia/Dhaka' AS window_to,
           EXTRACT(day FROM date_trunc('month', CURRENT_DATE::timestamp)
                            + INTERVAL '1 month - 1 day')::numeric AS days_in_month
),
used AS (
    SELECT round(sum(dr.import_kwh), 4)::numeric(12,4) AS used_kwh
    FROM meter_spec ms
    JOIN billing_point bp ON bp.point_id = ms.billing_point_id
    JOIN device d         ON d.device_id = ms.device_id
    JOIN device_reading dr ON dr.device_id = d.device_id
    CROSS JOIN bounds b
    WHERE bp.site_id = $1
      AND ms.billing_role = 'billing'
      AND d.removed_at IS NULL
      AND dr.interval_start >= b.window_from
      AND dr.interval_start <  b.window_to
)
SELECT b.month_start,
       scl.monthly_kwh,
       scl.notify_at_pct,
       scl.updated_at,
       coalesce(u.used_kwh, 0)::numeric(12,4) AS used_kwh,
       CASE WHEN scl.monthly_kwh IS NULL THEN NULL
            ELSE round(scl.monthly_kwh / b.days_in_month, 4)
       END AS daily_allowance_kwh
FROM bounds b
LEFT JOIN site_consumption_limit scl ON scl.site_id = $1
LEFT JOIN used u ON true;


-- name: set_consumption_limit
-- Upsert, because a household has one budget and setting a new one replaces it.
--
-- Deliberately NOT append-only. Rule 1 covers money -- bills, rates, the credit
-- ledger -- because a bill has to stay correct forever. A consumption limit is a
-- preference: it says what to warn me about from now on, nothing was ever
-- charged against it, and keeping a version history of somebody changing a
-- number in a settings box would be storage without a reader.
INSERT INTO site_consumption_limit (site_id, monthly_kwh, notify_at_pct,
                                    set_by_account_id, updated_at)
VALUES ($1, $2, $3, $4, now())
ON CONFLICT (site_id) DO UPDATE
SET monthly_kwh       = EXCLUDED.monthly_kwh,
    notify_at_pct     = EXCLUDED.notify_at_pct,
    set_by_account_id = EXCLUDED.set_by_account_id,
    updated_at        = now()
RETURNING site_id;


-- name: clear_consumption_limit
-- Turning the warning off is deleting the row, not setting the limit absurdly
-- high: the sweep's WHERE clause is what decides who gets told, and a household
-- with no row is simply not considered.
DELETE FROM site_consumption_limit WHERE site_id = $1 RETURNING site_id;


-- name: site_solar_status
-- What solar this SITE already carries, before another array is added.
--
-- The site-wide twin of point_solar_status, and the one /solar uses. Since
-- migration d4f8a2c61e95 an inverter is installed against the site and joins
-- a billing point only when net metering is granted, so at the moment panels
-- are registered the site is the only scope they have.
--
-- capacity_kw is the AC total summed over DISTINCT inverters, for the same
-- reason as point_solar_status: one inverter can drive several arrays, and
-- summing per array would double-count its clipping ceiling.
WITH live AS (
    SELECT sa.array_id, sa.inverter_device_id
    FROM solar_array sa
    JOIN device d ON d.device_id = sa.inverter_device_id
    WHERE d.site_id = $1
      AND sa.status <> 'decommissioned'
      AND d.removed_at IS NULL
)
SELECT (SELECT count(*) FROM live)::int AS array_count,
       coalesce((
           SELECT sum(inv.ac_capacity_kw)
           FROM inverter_spec inv
           WHERE inv.device_id IN (SELECT DISTINCT inverter_device_id FROM live)
       ), 0)::numeric AS capacity_kw;


-- name: site_billing_meters
-- Every live billing meter on a site.
--
-- Used by POST /api/sites/{id}/solar to decide whether re-netting the meter
-- against newly registered panels is unambiguous. One meter: the panels
-- plainly offset that connection's import. More than one: there is no
-- non-arbitrary answer until net metering attaches the inverter to a point
-- (rule 3), so nothing is re-netted.
SELECT d.device_id, ms.billing_point_id, ms.meter_flow::text AS meter_flow
FROM meter_spec ms
JOIN device d ON d.device_id = ms.device_id
WHERE ms.site_id = $1
  AND ms.billing_role = 'billing'
  AND d.removed_at IS NULL
ORDER BY d.installed_at DESC, d.device_id DESC;
