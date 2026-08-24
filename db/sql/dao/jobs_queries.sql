-- Jobs DAO: everything services/jobs sweeps, rolls up or bills.
--
-- Loaded into the same namespace as every other file in db/sql/dao/ (see
-- services/api/queries.py). The jobs runner reaches these by name exactly as a
-- route handler does -- there is no second SQL loader, and no ORM here either.
--
-- Two conventions run through the file:
--
-- * **Every sweep is re-runnable.** A statement that changes state is guarded
--   on the state it expects to find, so a row a concurrent writer moved in
--   between the SELECT and the UPDATE is left alone rather than trampled. That
--   is rule 4 applied to a background job -- the database refuses the second
--   effect, the job does not have to remember it already ran.
--
-- * **Local time is named, never inherited.** Days and months here are
--   Asia/Dhaka days and months, spelled AT TIME ZONE 'Asia/Dhaka' at every
--   site rather than left to the session. services/api/db.py pins the session
--   zone too, but a rollup that silently summarised a different calendar
--   depending on who started the process is precisely the bug class CLAUDE.md
--   makes DDL avoid, and it is no more acceptable in a query.


-- ==========================================================================
-- Assignment deadlines (supplier requirement 5, worker requirement 5)
-- ==========================================================================

-- name: expiring_offers
-- Offers nobody answered inside the window.
--
-- The deadline is READ, not computed: offer_expires_at is stored so that a
-- query between two sweeps is already correct (CLAUDE.md decision 3). This
-- selects rows the sweep should act on; expire_assignment re-checks the status
-- before writing, so an offer accepted in the gap survives.
--
-- $1 caps the batch. A sweep that found ten thousand rows should still finish.
SELECT wa.order_id,
       wa.account_id,
       wa.offer_expires_at            AS deadline_at,
       a.full_name                    AS worker_name,
       w.site_id,
       w.order_type::text             AS order_type,
       w.status::text                 AS order_status,
       w.created_by_account_id,
       s.label                        AS site_label
FROM work_order_assignment wa
JOIN work_order w ON w.order_id = wa.order_id
JOIN site s       ON s.site_id = w.site_id
JOIN account a    ON a.account_id = wa.account_id
WHERE wa.status = 'offered'
  AND wa.offer_expires_at IS NOT NULL
  AND wa.offer_expires_at <= now()
ORDER BY wa.offer_expires_at
LIMIT $1;


-- name: overdue_starts
-- Accepted, past its start deadline, and the work never began.
--
-- The started_at IS NULL guard is the whole point: a worker who turned up and
-- started must not lose the job to a sweep that only looked at the clock.
-- update_work_order_status sets started_at on the first move into in_progress,
-- so this reads the same fact the worker's own action wrote.
SELECT wa.order_id,
       wa.account_id,
       wa.start_deadline_at           AS deadline_at,
       a.full_name                    AS worker_name,
       w.site_id,
       w.order_type::text             AS order_type,
       w.status::text                 AS order_status,
       w.created_by_account_id,
       s.label                        AS site_label
FROM work_order_assignment wa
JOIN work_order w ON w.order_id = wa.order_id
JOIN site s       ON s.site_id = w.site_id
JOIN account a    ON a.account_id = wa.account_id
WHERE wa.status = 'accepted'
  AND wa.start_deadline_at IS NOT NULL
  AND wa.start_deadline_at <= now()
  AND w.started_at IS NULL
  AND w.status NOT IN ('in_progress', 'completed', 'failed', 'cancelled')
ORDER BY wa.start_deadline_at
LIMIT $1;


-- name: expire_assignment
-- $3 is the status the sweep READ, and the UPDATE only fires if the row is
-- still in it. A worker who accepted an offer microseconds before the sweep
-- reached their row keeps the job and the statement reports zero rows -- which
-- is the correct outcome, not a lost update to retry.
UPDATE work_order_assignment
SET status     = 'expired',
    expired_at = now()
WHERE order_id   = $1
  AND account_id = $2
  AND status     = $3::assignment_status
RETURNING order_id;


-- name: release_work_order
-- Hand a dispatched order back to whoever dispatched it.
--
-- CLAUDE.md decision 3: a deadline changes state, it does not merely paint an
-- overdue badge. An order nobody is on is not dispatched, so it returns to
-- 'draft' -- the state that means "needs an assignee" -- and shows up in the
-- supplier's queue again.
--
-- Two guards. Only a 'dispatched' order moves: a dispatcher who has already
-- walked the order somewhere else owns that decision, and the sweep must not
-- overwrite it. And only when no assignment is still live: a two-person job
-- whose assistant's offer lapsed is still dispatched to its lead.
UPDATE work_order w
SET status = 'draft'
WHERE w.order_id = $1
  AND w.status = 'dispatched'
  AND NOT EXISTS (
      SELECT 1 FROM work_order_assignment wa
      WHERE wa.order_id = w.order_id
        AND wa.status IN ('offered', 'accepted')
  )
RETURNING w.order_id;


-- ==========================================================================
-- Consumption limit (consumer requirement 5)
-- ==========================================================================

-- name: sites_over_consumption_limit
-- Households whose month-to-date import has reached the share of their own
-- monthly limit they asked to be warned at.
--
-- Month-to-date against the MONTHLY figure, not yesterday against a daily one:
-- the household set a monthly budget, and a per-day trigger on a household
-- load profile fires on any evening someone runs the air conditioning. The
-- daily average is derived here anyway (the migration deliberately does not
-- store it) because it is what makes the alert actionable -- "you are
-- averaging 14 kWh a day against an allowance of 11" tells someone what to
-- change, where a bare percentage does not.
--
-- Import is summed across the site's billing meters, not one of them: rule 3
-- means a site may hold several connections, and the limit is the household's,
-- not the connection's.
WITH bounds AS (
    SELECT date_trunc('month', CURRENT_DATE)::date AS month_start,
           date_trunc('month', CURRENT_DATE::timestamp)
               AT TIME ZONE 'Asia/Dhaka' AS window_from,
           (date_trunc('month', CURRENT_DATE::timestamp) + INTERVAL '1 month')
               AT TIME ZONE 'Asia/Dhaka' AS window_to,
           EXTRACT(day FROM date_trunc('month', CURRENT_DATE::timestamp)
                            + INTERVAL '1 month - 1 day')::numeric AS days_in_month,
           (CURRENT_DATE - date_trunc('month', CURRENT_DATE)::date + 1)::numeric
               AS days_elapsed
),
billing_meter AS (
    SELECT ms.device_id, bp.site_id
    FROM meter_spec ms
    JOIN billing_point bp ON bp.point_id = ms.billing_point_id
    JOIN device d         ON d.device_id = ms.device_id
    WHERE ms.billing_role = 'billing'
      AND d.removed_at IS NULL
),
used AS (
    SELECT bm.site_id,
           round(sum(dr.import_kwh), 4)::numeric(12,4) AS used_kwh
    FROM billing_meter bm
    JOIN device_reading dr ON dr.device_id = bm.device_id
    CROSS JOIN bounds b
    WHERE dr.interval_start >= b.window_from
      AND dr.interval_start <  b.window_to
    GROUP BY bm.site_id
)
SELECT scl.site_id,
       s.label      AS site_label,
       s.account_id,
       b.month_start,
       scl.monthly_kwh,
       scl.notify_at_pct,
       coalesce(u.used_kwh, 0)::numeric(12,4) AS used_kwh,
       round(coalesce(u.used_kwh, 0) / scl.monthly_kwh * 100, 2) AS used_pct,
       round(scl.monthly_kwh / b.days_in_month, 4)               AS daily_allowance_kwh,
       round(coalesce(u.used_kwh, 0) / b.days_elapsed, 4)        AS daily_average_kwh
FROM site_consumption_limit scl
JOIN site s ON s.site_id = scl.site_id
CROSS JOIN bounds b
LEFT JOIN used u ON u.site_id = scl.site_id
WHERE coalesce(u.used_kwh, 0) >= scl.monthly_kwh * scl.notify_at_pct / 100
ORDER BY used_pct DESC
LIMIT $1;


-- ==========================================================================
-- Partition maintenance
-- ==========================================================================

-- name: ensure_reading_partition
-- create_reading_partition() is the ONE place a partition bound is computed
-- (see migration 0f6109903981) and it returns the existing name untouched when
-- the month is already there, so calling it every night is free.
SELECT create_reading_partition($1::date) AS partition_name;


-- name: default_partition_rows
-- The catch-all partition should stay empty. A row in it means a reading
-- landed outside every month that exists -- a clock-skewed device, or a month
-- nobody pre-created -- and attaching a partition for that month later
-- requires a full scan of this table while it holds matching rows. Cheap to
-- watch, expensive to discover late.
SELECT count(*)::bigint AS row_count FROM device_reading_default;


-- ==========================================================================
-- Rollups: site_daily_summary / site_monthly_summary
-- ==========================================================================

-- name: refresh_daily_summaries
-- Rebuild site_daily_summary for [$1, $2], optionally for one site ($3).
--
-- Recomputed rather than incremented, so a late backfill over an already
-- summarised day is corrected by re-running the window instead of leaving the
-- rollup permanently out of step with the readings. The ON CONFLICT is what
-- makes that safe to repeat (rule 4).
--
-- Where each number comes from is rule 6: import and export are known only to
-- the bidirectional billing meter, generation only to the inverter. Neither is
-- derived from the other, and self_consumption_kwh is a GENERATED column, so
-- it is not written here at all.
--
-- interval_count counts METERED intervals across every billing meter on the
-- site, so a two-connection household reports twice the intervals of a
-- one-connection one. It is a completeness signal for this rollup, not the
-- coverage figure rule 8 gates on -- that one is per billing point and lives
-- on billing_period.
WITH bounds AS (
    SELECT $1::date AS from_date,
           $2::date AS to_date,
           $1::date::timestamp AT TIME ZONE 'Asia/Dhaka'       AS window_from,
           ($2::date + 1)::timestamp AT TIME ZONE 'Asia/Dhaka' AS window_to
),
billing_meter AS (
    SELECT ms.device_id, bp.site_id
    FROM meter_spec ms
    JOIN billing_point bp ON bp.point_id = ms.billing_point_id
    JOIN device d         ON d.device_id = ms.device_id
    WHERE ms.billing_role = 'billing'
      AND d.removed_at IS NULL
      AND ($3::uuid IS NULL OR bp.site_id = $3)
),
metered AS (
    SELECT bm.site_id,
           (dr.interval_start AT TIME ZONE 'Asia/Dhaka')::date AS local_date,
           (dr.interval_start AT TIME ZONE 'Asia/Dhaka')::time AS local_time,
           dr.import_kwh,
           dr.export_kwh
    FROM billing_meter bm
    JOIN device_reading dr ON dr.device_id = bm.device_id
    CROSS JOIN bounds b
    WHERE dr.interval_start >= b.window_from
      AND dr.interval_start <  b.window_to
),
-- Same day_type resolution as run_billing: holiday_calendar wins, then the
-- Bangladesh weekend (dow 5 and 6), else weekday. The two must agree -- a
-- rollup that split peak differently from the bill it sits next to would be
-- read as a billing error.
classified AS (
    SELECT m.*,
           CASE
               WHEN EXISTS (SELECT 1 FROM holiday_calendar h
                             WHERE h.holiday_date = m.local_date)
                   THEN 'holiday'
               WHEN EXTRACT(dow FROM m.local_date) IN (5, 6)
                   THEN 'weekend'
               ELSE 'weekday'
           END::rate_day_type AS day_type
    FROM metered m
),
-- LEFT JOIN, not JOIN: an interval the site's plan has no window for still
-- counts toward import and toward interval_count. Dropping it would make the
-- rollup quietly disagree with the meter.
priced AS (
    SELECT c.site_id, c.local_date, c.import_kwh, c.export_kwh, tr.period_name
    FROM classified c
    JOIN site s ON s.site_id = c.site_id
    LEFT JOIN tariff_rate tr
           ON tr.plan_id  = s.tariff_plan_id
          AND tr.day_type = c.day_type
          AND c.local_time >= tr.start_time
          AND c.local_time <  tr.end_time
),
meter_day AS (
    SELECT site_id,
           local_date,
           coalesce(round(sum(import_kwh), 4), 0)::numeric(12,4) AS import_kwh,
           coalesce(round(sum(export_kwh), 4), 0)::numeric(12,4) AS export_kwh,
           round(max(import_kwh), 4)::numeric(12,4)              AS peak_import_kwh,
           coalesce(round(sum(import_kwh)
               FILTER (WHERE period_name = 'peak'), 4), 0)::numeric(12,4)
                                                                 AS peak_window_kwh,
           coalesce(round(sum(import_kwh)
               FILTER (WHERE period_name IS DISTINCT FROM 'peak'), 4), 0)::numeric(12,4)
                                                                 AS offpeak_window_kwh,
           count(*)::smallint                                    AS interval_count
    FROM priced
    GROUP BY site_id, local_date
),
gen_day AS (
    SELECT d.site_id,
           (dr.interval_start AT TIME ZONE 'Asia/Dhaka')::date AS local_date,
           coalesce(round(sum(dr.generation_kwh), 4), 0)::numeric(12,4) AS generation_kwh
    FROM device_reading dr
    JOIN device d ON d.device_id = dr.device_id
    CROSS JOIN bounds b
    WHERE dr.interval_start >= b.window_from
      AND dr.interval_start <  b.window_to
      AND dr.generation_kwh IS NOT NULL
      AND d.removed_at IS NULL
      AND ($3::uuid IS NULL OR d.site_id = $3)
    GROUP BY d.site_id, 2
)
-- FULL JOIN: a solar-only day with no metered intervals is still a day worth
-- recording, and so is a metered day at a site with no inverter.
INSERT INTO site_daily_summary AS sds (
    site_id, summary_date, import_kwh, export_kwh, generation_kwh,
    peak_import_kwh, import_peak_window_kwh, import_offpeak_window_kwh,
    interval_count, refreshed_at
)
SELECT coalesce(m.site_id, g.site_id),
       coalesce(m.local_date, g.local_date),
       coalesce(m.import_kwh, 0),
       coalesce(m.export_kwh, 0),
       coalesce(g.generation_kwh, 0),
       m.peak_import_kwh,
       m.peak_window_kwh,
       m.offpeak_window_kwh,
       coalesce(m.interval_count, 0),
       now()
FROM meter_day m
FULL JOIN gen_day g
       ON g.site_id = m.site_id AND g.local_date = m.local_date
ON CONFLICT (site_id, summary_date) DO UPDATE
SET import_kwh                = EXCLUDED.import_kwh,
    export_kwh                = EXCLUDED.export_kwh,
    generation_kwh            = EXCLUDED.generation_kwh,
    peak_import_kwh           = EXCLUDED.peak_import_kwh,
    import_peak_window_kwh    = EXCLUDED.import_peak_window_kwh,
    import_offpeak_window_kwh = EXCLUDED.import_offpeak_window_kwh,
    interval_count            = EXCLUDED.interval_count,
    refreshed_at              = EXCLUDED.refreshed_at
RETURNING sds.site_id;


-- name: refresh_monthly_summaries
-- Built from site_daily_summary, not from device_reading again.
--
-- One derivation, so the month cannot contradict the days that make it up --
-- and the daily rollup has already resolved holidays, TOU windows and which
-- device may report what. The order matters: run the daily refresh first.
--
-- peak_demand_kw converts the largest single-interval import into power, which
-- needs the interval length. A site whose billing meters report at different
-- lengths is approximated at the shortest of them -- min(), so a 15-minute
-- meter is not flattened by a 60-minute one. Said out loud because it IS an
-- approximation, and the only exact fix is per-point demand, which this table
-- is not keyed for.
WITH interval_len AS (
    SELECT bp.site_id, min(d.interval_minutes)::numeric AS interval_minutes
    FROM meter_spec ms
    JOIN billing_point bp ON bp.point_id = ms.billing_point_id
    JOIN device d         ON d.device_id = ms.device_id
    WHERE ms.billing_role = 'billing'
      AND d.removed_at IS NULL
    GROUP BY bp.site_id
),
rolled AS (
    SELECT sds.site_id,
           date_trunc('month', sds.summary_date)::date          AS month_start,
           round(sum(sds.import_kwh), 4)::numeric(12,4)         AS import_kwh,
           round(sum(sds.export_kwh), 4)::numeric(12,4)         AS export_kwh,
           round(sum(sds.generation_kwh), 4)::numeric(12,4)     AS generation_kwh,
           max(sds.peak_import_kwh)                             AS peak_interval_kwh
    FROM site_daily_summary sds
    WHERE sds.summary_date >= $1::date
      AND sds.summary_date <= $2::date
      AND ($3::uuid IS NULL OR sds.site_id = $3)
    GROUP BY sds.site_id, 2
)
INSERT INTO site_monthly_summary AS sms (
    site_id, month_start, import_kwh, export_kwh, generation_kwh,
    peak_demand_kw, self_sufficiency_pct, refreshed_at
)
SELECT r.site_id,
       r.month_start,
       r.import_kwh,
       r.export_kwh,
       r.generation_kwh,
       round(r.peak_interval_kwh * 60.0 / il.interval_minutes, 3)::numeric(10,3),
       -- Self-sufficiency: the share of what the household actually consumed
       -- that its own panels supplied. self_consumption = generation - export
       -- (rule 6), floored at zero so a meter reporting more export than the
       -- inverter reported generation cannot produce a negative percentage the
       -- CHECK would reject -- that combination is a data fault, and it should
       -- surface as an implausible 0%, not as a failed rollup.
       CASE
           WHEN greatest(r.generation_kwh - r.export_kwh, 0) + r.import_kwh > 0
           THEN least(round(
                    greatest(r.generation_kwh - r.export_kwh, 0)
                    / (greatest(r.generation_kwh - r.export_kwh, 0) + r.import_kwh)
                    * 100, 2), 100)::numeric(5,2)
           ELSE NULL
       END,
       now()
FROM rolled r
LEFT JOIN interval_len il ON il.site_id = r.site_id
ON CONFLICT (site_id, month_start) DO UPDATE
SET import_kwh           = EXCLUDED.import_kwh,
    export_kwh           = EXCLUDED.export_kwh,
    generation_kwh       = EXCLUDED.generation_kwh,
    peak_demand_kw       = EXCLUDED.peak_demand_kw,
    self_sufficiency_pct = EXCLUDED.self_sufficiency_pct,
    refreshed_at         = EXCLUDED.refreshed_at
RETURNING sms.site_id;


-- ==========================================================================
-- Scheduled billing
-- ==========================================================================

-- name: unbilled_point_months
-- Every (billing point, complete month) that has readings and no bill.
--
-- The series stops at LAST month: the current one has not finished, and
-- run_billing's rule 8 gate would refuse it anyway. Attempting it nightly
-- would freeze the open period on every pass for nothing.
--
-- Already-billed months are excluded rather than left to run_billing's
-- idempotency. It would return the existing bill_id harmlessly, but the job
-- would then report thousands of "billed" results that issued nothing.
WITH meter AS (
    SELECT bp.point_id,
           bp.site_id,
           bp.label,
           min(dr.interval_start) AS first_reading
    FROM billing_point bp
    JOIN meter_spec ms ON ms.billing_point_id = bp.point_id
                      AND ms.billing_role = 'billing'
    JOIN device d      ON d.device_id = ms.device_id AND d.removed_at IS NULL
    JOIN device_reading dr ON dr.device_id = d.device_id
    GROUP BY bp.point_id, bp.site_id, bp.label
),
months AS (
    SELECT m.point_id, m.site_id, m.label, gs::date AS period_start
    FROM meter m
    CROSS JOIN LATERAL generate_series(
        date_trunc('month', m.first_reading AT TIME ZONE 'Asia/Dhaka'),
        date_trunc('month', CURRENT_DATE::timestamp) - INTERVAL '1 month',
        INTERVAL '1 month'
    ) gs
)
SELECT mo.point_id, mo.site_id, mo.label, mo.period_start
FROM months mo
LEFT JOIN billing_period bpd
       ON bpd.billing_point_id = mo.point_id
      AND bpd.period_start     = mo.period_start
      AND bpd.status           = 'billed'
WHERE bpd.period_id IS NULL
ORDER BY mo.point_id, mo.period_start
LIMIT $1;


-- name: open_billing_run
-- The audit row for one nightly pass. triggered_by is NULL: nobody triggered
-- it, and inventing a service account to satisfy a nullable column would put a
-- fiction in the audit trail.
INSERT INTO billing_run (triggered_by, period_start, period_end, status)
VALUES (NULL, $1, $2, 'running')
RETURNING run_id;


-- name: finish_billing_run
-- 'succeeded' / 'partial' / 'failed' is decided by the caller from what it
-- counted, not inferred here.
UPDATE billing_run
SET finished_at     = now(),
    sites_processed = $2,
    bills_issued    = $3,
    failures        = $4,
    status          = $5::billing_run_status,
    error_summary   = $6
WHERE run_id = $1
RETURNING run_id;


-- name: attach_period_to_run
-- billing_period.billing_run_id exists so a bill can be traced to the pass
-- that produced it. run_billing does not know it is being run by a job, so the
-- job stamps it afterwards -- and only when the column is still empty, so a
-- re-run never rewrites which pass first billed the period.
UPDATE billing_period
SET billing_run_id = $2
WHERE billing_point_id = $1
  AND period_start = $3
  AND billing_run_id IS NULL
RETURNING period_id;
