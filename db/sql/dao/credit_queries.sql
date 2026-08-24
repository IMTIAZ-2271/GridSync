-- Net-metering outcomes: what the credit scheme actually did.
--
-- Government requirement 5. The regulator's existing rollup shows energy
-- flowing in both directions; this shows the consequence of that flow -- credit
-- earned for exporting, credit spent against a bill, and the balance rolling
-- forward. It is the one view of `credit_ledger` nobody had built, and the
-- ledger is the thing this whole application exists to demonstrate.
--
-- Everything here reads the ledger rather than recomputing from readings. The
-- ledger is append-only (rule 1) and carries a running balance on every entry,
-- so it IS the record; deriving these figures a second way would be inventing
-- an opportunity to disagree with the bills.


-- name: net_metering_outcomes_by_area
-- Per district: how much credit was earned, how much was spent, and what is
-- still held.
--
-- Earned and applied are summed from the ledger's own entry types. The balance
-- is NOT their difference -- it is the sum of each connection's latest running
-- balance, which is what the next bill will actually be able to spend. Those
-- two agree today and would diverge the moment an 'expired' or 'adjustment'
-- entry is written, and when they diverge the running balance is the true one.
--
-- $1 optionally narrows to one district, matching analytics_by_area, so the
-- regulator's own-region toggle works the same way on both pages.
WITH latest AS (
    -- The newest entry per billing point IS its balance (see run_billing).
    -- entry_id is the append-only sequence, so it orders even when several
    -- entries share a created_at inside one billing transaction.
    SELECT DISTINCT ON (cl.billing_point_id)
           cl.billing_point_id,
           cl.site_id,
           cl.balance_kwh_after,
           cl.balance_amount_after
    FROM credit_ledger cl
    ORDER BY cl.billing_point_id, cl.entry_id DESC
),
moved AS (
    SELECT cl.site_id,
           coalesce(sum(cl.kwh_delta) FILTER (WHERE cl.entry_type = 'earned'), 0)::numeric(12,4)
               AS earned_kwh,
           coalesce(sum(cl.amount_delta) FILTER (WHERE cl.entry_type = 'earned'), 0)::numeric(14,4)
               AS earned_amount,
           -- 'applied' deltas are negative in the ledger; flipped here so the
           -- page can put "earned" and "spent" side by side without the reader
           -- having to remember a sign convention.
           coalesce(-sum(cl.kwh_delta) FILTER (WHERE cl.entry_type = 'applied'), 0)::numeric(12,4)
               AS applied_kwh,
           coalesce(-sum(cl.amount_delta) FILTER (WHERE cl.entry_type = 'applied'), 0)::numeric(14,4)
               AS applied_amount
    FROM credit_ledger cl
    GROUP BY cl.site_id
)
SELECT s.district,
       count(DISTINCT s.site_id)::int AS site_count,
       count(DISTINCT s.site_id) FILTER (
           WHERE l.balance_kwh_after > 0
       )::int AS sites_in_credit,
       coalesce(sum(m.earned_kwh), 0)::numeric(12,4)     AS earned_kwh,
       coalesce(sum(m.earned_amount), 0)::numeric(14,4)  AS earned_amount,
       coalesce(sum(m.applied_kwh), 0)::numeric(12,4)    AS applied_kwh,
       coalesce(sum(m.applied_amount), 0)::numeric(14,4) AS applied_amount,
       coalesce(sum(l.balance_kwh_after), 0)::numeric(12,4)     AS balance_kwh,
       coalesce(sum(l.balance_amount_after), 0)::numeric(14,4)  AS balance_amount,
       -- Of everything ever earned, the share that has been spent against a
       -- bill. The number the scheme is actually judged on: credit nobody can
       -- use is a policy that looks generous and is not.
       CASE
           WHEN coalesce(sum(m.earned_kwh), 0) > 0
           THEN round(coalesce(sum(m.applied_kwh), 0)
                      / sum(m.earned_kwh) * 100, 1)
       END AS applied_pct
FROM site s
LEFT JOIN moved m  ON m.site_id = s.site_id
LEFT JOIN latest l ON l.site_id = s.site_id
WHERE ($1::text IS NULL OR s.district = $1)
  -- Only districts where net metering is actually happening. A district with no
  -- solar reporting zeros across the board is noise on this page; it is already
  -- visible, correctly, on the energy rollup.
  AND EXISTS (
      SELECT 1 FROM net_metering_agreement nma
      JOIN billing_point bp ON bp.point_id = nma.billing_point_id
      WHERE bp.site_id = s.site_id
  )
GROUP BY s.district
ORDER BY s.district;


-- name: net_metering_agreement_summary
-- How many agreements exist, and in what state, for the same optional scope.
--
-- Sits beside the credit figures because the two answer one question together:
-- how many households were let into the scheme, and what did it give them.
SELECT nma.status::text AS status,
       count(*)::int    AS agreement_count,
       coalesce(sum(nma.sanctioned_capacity_kw), 0)::numeric(12,3)
           AS sanctioned_capacity_kw
FROM net_metering_agreement nma
JOIN billing_point bp ON bp.point_id = nma.billing_point_id
JOIN site s ON s.site_id = bp.site_id
WHERE ($1::text IS NULL OR s.district = $1)
GROUP BY nma.status
ORDER BY nma.status;


-- name: credit_ledger_for_site
-- One household's own credit history, newest first.
--
-- The consumer-facing half of the same data. Append-only, so this is the whole
-- story and not a snapshot: every entry says what it was for, which bill it
-- attached to, and what the balance became.
SELECT cl.entry_id,
       cl.billing_point_id,
       bp.label           AS point_label,
       cl.entry_type::text AS entry_type,
       cl.kwh_delta,
       cl.amount_delta,
       cl.balance_kwh_after,
       cl.balance_amount_after,
       cl.expires_on,
       cl.note,
       cl.created_at,
       bpd.period_start
FROM credit_ledger cl
JOIN billing_point bp ON bp.point_id = cl.billing_point_id
LEFT JOIN billing_period bpd ON bpd.period_id = cl.period_id
WHERE cl.site_id = $1
ORDER BY cl.entry_id DESC
LIMIT $2;
