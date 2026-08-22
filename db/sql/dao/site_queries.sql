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
    SELECT b.*, bp.period_start, bp.period_end
    FROM bill b
    JOIN billing_period bp ON bp.period_id = b.period_id
    WHERE b.site_id = s.site_id
      -- A voided bill has been superseded by a correction (rule 1); showing it
      -- as "latest" would show the customer a number nobody owes.
      AND b.status <> 'void'
    ORDER BY bp.period_start DESC, b.issued_at DESC
    LIMIT 1
) lb ON TRUE
LEFT JOIN LATERAL (
    SELECT cl.balance_kwh_after, cl.balance_amount_after
    FROM credit_ledger cl
    WHERE cl.site_id = s.site_id
    ORDER BY cl.entry_id DESC
    LIMIT 1
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
WHERE b.site_id = $1
ORDER BY bp.period_start DESC, b.issued_at DESC;


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


-- name: site_has_billing_meter
-- Rule 7 pre-check, before the deferred trigger would catch it at COMMIT --
-- gives the caller a 409 instead of a mid-transaction constraint failure.
SELECT EXISTS (
    SELECT 1
    FROM meter_spec ms
    JOIN device d ON d.device_id = ms.device_id
    WHERE ms.site_id = $1
      AND ms.billing_role = 'billing'
      AND d.removed_at IS NULL
);


-- name: create_meter_device
INSERT INTO device (
    site_id, device_type, serial_no, manufacturer, model,
    interval_minutes, device_key_hash, installed_at, status
)
VALUES ($1, 'meter', $2, $3, $4, 30, $5, now(), 'active')
RETURNING device_id;


-- name: create_meter_spec
-- Always bidirectional/billing: the onboarding flow only ever registers the
-- one meter rule 7 requires. ct_ratio and phase_count are sane installer
-- defaults, not customer input.
INSERT INTO meter_spec (device_id, site_id, meter_flow, billing_role, ct_ratio, phase_count)
VALUES ($1, $2, 'bidirectional', 'billing', '1:1', 1);


-- name: site_billing_device
SELECT d.device_id
FROM device d
JOIN meter_spec ms ON ms.device_id = d.device_id
WHERE ms.site_id = $1
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
INSERT INTO solar_array (
    site_id, inverter_device_id, label, panel_count, panel_watt_peak,
    dc_capacity_kw, azimuth_deg, tilt_deg, shading_factor, commissioned_on, status
)
VALUES ($1, $2, 'Rooftop array', $3, $4, $5, $6, $7, 0.950, CURRENT_DATE, 'active')
RETURNING array_id;


-- name: create_net_metering_agreement
-- status = 'pending': a new site's agreement joins the same approval queue
-- db/sql/seed_demo.sql seeds for its non-solar sites, reviewed on
-- /government/agreements rather than auto-approved on registration.
INSERT INTO net_metering_agreement (
    site_id, billing_device_id, approval_ref, sanctioned_capacity_kw,
    export_cap_pct, settlement_type, credit_rollover_months,
    effective_from, status
)
VALUES ($1, $2, $3, $4, 70.00, 'rollover_only', 12, CURRENT_DATE, 'pending')
RETURNING agreement_id;


-- name: site_billing_window
-- The earliest month this site's billing meter has readings for, and
-- "today" read in the same session zone the caller is pinned to -- so the
-- handler's month-by-month loop agrees with the database about which months
-- have actually finished.
SELECT min(date_trunc('month', dr.interval_start AT TIME ZONE 'Asia/Dhaka'))::date AS first_month,
       CURRENT_DATE AS today
FROM device_reading dr
JOIN meter_spec ms ON ms.device_id = dr.device_id
WHERE ms.site_id = $1
  AND ms.billing_role = 'billing';
