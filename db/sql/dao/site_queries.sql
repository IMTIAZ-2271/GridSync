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
      -- A voided bill has been superseded by a correction (rule 1); showing it
      -- as "latest" would show the customer a number nobody owes.
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
) bal ON TRUE
CROSS JOIN LATERAL (
    SELECT COALESCE(SUM(r.import_kwh) FILTER (WHERE ms.billing_role = 'billing'), 0)::numeric(12,4) AS import_kwh,
           COALESCE(SUM(r.export_kwh) FILTER (WHERE ms.billing_role = 'billing'), 0)::numeric(12,4) AS export_kwh,
           COALESCE(SUM(r.generation_kwh), 0)::numeric(12,4)                                        AS generation_kwh
    FROM device_reading r
    JOIN device d ON d.device_id = r.device_id
    LEFT JOIN meter_spec ms ON ms.device_id = d.device_id
    WHERE d.site_id = s.site_id
      AND r.interval_start >= now() - INTERVAL '30 days'
) win
WHERE s.site_id = $1;


-- name: site_readings
-- Interval series for the chart. Meter and inverter report on separate rows of
-- device_reading, so they are folded back together on interval_start: one row
-- out per interval carrying all three measures.
--
-- Grouping (rather than joining meter rows to inverter rows) is what keeps a
-- meter swap mid-window from splitting the series in two -- the replacement
-- device's rows land in the same buckets.
SELECT r.interval_start,
       COALESCE(SUM(r.import_kwh) FILTER (WHERE ms.billing_role = 'billing'), 0)::numeric(12,4) AS import_kwh,
       COALESCE(SUM(r.export_kwh) FILTER (WHERE ms.billing_role = 'billing'), 0)::numeric(12,4) AS export_kwh,
       COALESCE(SUM(r.generation_kwh), 0)::numeric(12,4)                                        AS generation_kwh
FROM device_reading r
JOIN device d ON d.device_id = r.device_id
LEFT JOIN meter_spec ms ON ms.device_id = d.device_id
WHERE d.site_id = $1
  AND r.interval_start >= now() - make_interval(days => $2::int)
GROUP BY r.interval_start
ORDER BY r.interval_start;


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
-- Onboarding: a customer with no site building one from scratch.
-- POST /api/sites, then /meter, then optionally /solar, then /bill.
-- ---------------------------------------------------------------------------

-- name: list_tariff_plans
-- Currently-effective plans, optionally narrowed to one connection type.
-- $1 is nullable -- a NULL parameter makes the filter a no-op rather than a
-- second statement, since the onboarding form may ask before or after the
-- customer has picked a connection type.
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
SELECT pt.point_id,
       pt.label,
       pt.reference,
       pt.created_at,
       d.device_id  AS meter_device_id,
       d.serial_no  AS meter_serial,
       d.last_seen_at AS meter_last_seen_at,
       EXISTS (
           SELECT 1
           FROM solar_array sa
           JOIN device inv ON inv.device_id = sa.inverter_device_id
           WHERE sa.status <> 'decommissioned'
             AND inv.removed_at IS NULL
             AND inv.parent_device_id = d.device_id
       ) AS has_solar
FROM billing_point pt
LEFT JOIN meter_spec ms
       ON ms.billing_point_id = pt.point_id
      AND ms.billing_role = 'billing'
LEFT JOIN device d
       ON d.device_id = ms.device_id
      AND d.removed_at IS NULL
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
-- Always bidirectional/billing: this path only ever registers the one meter
-- rule 7 requires *per billing point*. ct_ratio and phase_count are sane
-- installer defaults, not customer input.
INSERT INTO meter_spec (
    device_id, site_id, billing_point_id, meter_flow, billing_role,
    ct_ratio, phase_count
)
VALUES ($1, $2, $3, 'bidirectional', 'billing', '1:1', 1);


-- name: point_billing_device
SELECT d.device_id
FROM device d
JOIN meter_spec ms ON ms.device_id = d.device_id
WHERE ms.billing_point_id = $1
  AND ms.billing_role = 'billing'
  AND d.removed_at IS NULL;


-- name: create_inverter_device
INSERT INTO device (
    site_id, parent_device_id, device_type, serial_no, manufacturer, model,
    interval_minutes, device_key_hash, installed_at, status
)
VALUES ($1, $2, 'inverter', $3, $4, $5, 30, $6, now(), 'active')
RETURNING device_id;


-- name: create_inverter_spec
-- ac_capacity_kw is the clipping ceiling; dc_capacity_kw is set ~20% above it
-- by the caller, matching db/sql/seed_demo.sql's ratio.
INSERT INTO inverter_spec (
    device_id, ac_capacity_kw, dc_capacity_kw, mppt_count, phase_count,
    rated_efficiency, anti_islanding
)
VALUES ($1, $2, $3, 2, 1, 0.9720, true);


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
INSERT INTO net_metering_agreement (
    site_id, billing_point_id, billing_device_id, approval_ref,
    sanctioned_capacity_kw,
    export_cap_pct, settlement_type, credit_rollover_months,
    effective_from, status
)
VALUES ($1, $2, $3, $4, $5, 70.00, 'rollover_only', 12, CURRENT_DATE,
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
-- Scoped by the inverter's parent meter rather than by site: with several
-- billing meters on one site, netting a point's meter against the site's
-- whole fleet of arrays would credit one connection for another's export.
WITH live AS (
    SELECT sa.array_id, sa.inverter_device_id
    FROM solar_array sa
    JOIN device d ON d.device_id = sa.inverter_device_id
    JOIN meter_spec ms ON ms.device_id = d.parent_device_id
    WHERE ms.billing_point_id = $1
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
       effective_from
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
