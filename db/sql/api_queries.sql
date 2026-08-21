-- GridSync read-side API queries.
--
-- Every statement the dashboard API runs lives here, not inline in Python: the
-- reading path is raw SQL by design (CLAUDE.md, "no ORM on the billing or
-- reading path"). services/api/queries.py loads this file and splits it on the
-- `-- name:` markers; the name is how main.py asks for a statement.
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
-- in this file computes a calendar boundary.
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


-- name: list_issues
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
ORDER BY i.reported_at DESC;


-- name: get_issue
-- Same projection as list_issues, for one row. Used to render what POST just
-- created without duplicating the joins in the INSERT's RETURNING clause.
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
WHERE i.issue_id = $1;


-- name: create_issue
-- reported_by_account_id falls back to the site's owner when the client does
-- not supply one. There is no auth to infer a reporter from, and the column is
-- NOT NULL -- attributing an unattributed report to the account that owns the
-- service point is the least wrong guess available. It becomes a session
-- lookup the moment auth exists.
INSERT INTO issue (reported_by_account_id, site_id, device_id, bill_id,
                   category, severity, title, description, priority)
VALUES (COALESCE($1, (SELECT account_id FROM site WHERE site_id = $2)),
        $2, $3, $4, $5::issue_category, $6::issue_severity, $7, $8, $9::smallint)
RETURNING issue_id;


-- name: list_work_orders
-- Assignments are aggregated as json because they carry no NUMERIC -- names,
-- roles and statuses survive a JSON round trip unchanged.
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
GROUP BY w.order_id, s.label
ORDER BY w.created_at DESC;


-- name: get_work_order
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
WHERE w.order_id = $1
GROUP BY w.order_id, s.label;


-- name: update_work_order_status
-- The lifecycle timestamps are maintained here rather than left to the caller,
-- so a dispatcher clicking a status button cannot leave an order 'completed'
-- with a NULL completed_at. Both are set once and never rewritten -- moving
-- back to an earlier status does not erase the fact that work started.
UPDATE work_order
SET status = $2::work_order_status,
    started_at = CASE
        WHEN started_at IS NULL
         AND $2::work_order_status IN ('in_progress', 'completed', 'failed')
        THEN now()
        ELSE started_at
    END,
    completed_at = CASE
        WHEN completed_at IS NULL
         AND $2::work_order_status IN ('completed', 'failed')
        THEN now()
        ELSE completed_at
    END
WHERE order_id = $1
RETURNING order_id;


-- name: list_pending_agreements
SELECT nma.agreement_id,
       nma.site_id,
       s.label AS site_label,
       s.district,
       a.full_name AS account_name,
       nma.billing_device_id,
       d.serial_no AS billing_device_serial,
       nma.approval_ref,
       nma.sanctioned_capacity_kw,
       nma.export_cap_pct,
       nma.settlement_type,
       nma.credit_rollover_months,
       nma.effective_from,
       nma.effective_to,
       nma.status,
       nma.created_at
FROM net_metering_agreement nma
JOIN site s ON s.site_id = nma.site_id
JOIN account a ON a.account_id = s.account_id
JOIN device d ON d.device_id = nma.billing_device_id
WHERE nma.status = 'pending'
ORDER BY nma.created_at;


-- name: get_agreement
SELECT nma.agreement_id,
       nma.site_id,
       s.label AS site_label,
       s.district,
       a.full_name AS account_name,
       nma.billing_device_id,
       d.serial_no AS billing_device_serial,
       nma.approval_ref,
       nma.sanctioned_capacity_kw,
       nma.export_cap_pct,
       nma.settlement_type,
       nma.credit_rollover_months,
       nma.effective_from,
       nma.effective_to,
       nma.status,
       nma.created_at
FROM net_metering_agreement nma
JOIN site s ON s.site_id = nma.site_id
JOIN account a ON a.account_id = s.account_id
JOIN device d ON d.device_id = nma.billing_device_id
WHERE nma.agreement_id = $1;


-- name: decide_agreement
-- Guarded on status = 'pending' so two reviewers racing on the same agreement
-- cannot both win: the loser updates zero rows and the handler answers 409.
UPDATE net_metering_agreement
SET status = $2::nma_status
WHERE agreement_id = $1
  AND status = 'pending'
RETURNING agreement_id;


-- name: analytics_by_area
-- Rollup by district over all recorded telemetry.
--
-- The per-site totals are computed in a lateral and only then grouped, so a
-- district's site_count counts sites, not reading rows -- and a site that has
-- never reported still counts, with zeros.
SELECT s.district,
       COUNT(*) AS site_count,
       COUNT(*) FILTER (WHERE t.has_solar) AS solar_site_count,
       COALESCE(SUM(t.import_kwh), 0)::numeric(12,4) AS total_import_kwh,
       COALESCE(SUM(t.export_kwh), 0)::numeric(12,4) AS total_export_kwh,
       COALESCE(SUM(t.generation_kwh), 0)::numeric(12,4) AS total_generation_kwh
FROM site s
CROSS JOIN LATERAL (
    SELECT COALESCE(SUM(r.import_kwh) FILTER (WHERE ms.billing_role = 'billing'), 0)::numeric(12,4) AS import_kwh,
           COALESCE(SUM(r.export_kwh) FILTER (WHERE ms.billing_role = 'billing'), 0)::numeric(12,4) AS export_kwh,
           COALESCE(SUM(r.generation_kwh), 0)::numeric(12,4)                                        AS generation_kwh,
           EXISTS (
               SELECT 1 FROM solar_array sa
               WHERE sa.site_id = s.site_id
                 AND sa.status <> 'decommissioned'
           ) AS has_solar
    FROM device_reading r
    JOIN device d ON d.device_id = r.device_id
    LEFT JOIN meter_spec ms ON ms.device_id = d.device_id
    WHERE d.site_id = s.site_id
) t
GROUP BY s.district
ORDER BY s.district;
