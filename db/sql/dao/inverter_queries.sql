-- Inverter DAO: the household's own generating hardware, and whether any of
-- it is producing enough to justify net metering.
--
-- An inverter became a first-class connection in migration d4f8a2c61e95. It is
-- installed against a site, it names its own billing point (NULL until net
-- metering is granted), and it is never a meter: generation is measured by the
-- inverter, the import/export split only ever by the bidirectional meter at
-- the grid boundary (rule 6). Nothing in this file converts one into the
-- other, and nothing may.


-- name: inverters_for_account
-- Every inverter this account owns, with the two measurements that decide
-- whether it can carry a net-metering application.
--
-- Parameters are the policy, not the query:
--   $1 account_id
--   $2 surplus_ratio    generation must beat consumption by this factor
--   $3 peak_sun_hours   kWh per kW per day a healthy array yields here
--   $4 perf_floor       fraction of that yield below which the array is faulty
--   $5 min_days         days of readings before a verdict is possible at all
--
-- They are passed in rather than written as literals so the numbers live once,
-- named, in services/api/routes_inverters.py, and so a test can drive the
-- boundary from either side without editing SQL.
--
-- THE WINDOW is the last 30 whole Asia/Dhaka days, ending at today's midnight
-- and therefore excluding today. Today is still accumulating intervals, and a
-- part-day dragged into a daily average reads as a collapse in output -- the
-- same reason device_health and the nightly rollups both stop at yesterday.
-- The zone is named literally: date_trunc on a bare timestamptz resolves
-- against the session zone, so the same request would measure a different set
-- of days for a client connecting from elsewhere.
--
-- CONSUMPTION, AND WHY IT IS NOT SIMPLY IMPORT -- OR SIMPLY IMPORT PLUS
-- GENERATION.
--
-- On a NET-METERED connection the meter is bidirectional and reports the
-- split, so the household's real consumption is
--
--     import - export + generation
--
-- -- energy in from the grid, less what went back out, plus what was made on
-- site and used there (rule 6: self-consumption = generation - export).
--
-- On a connection that is NOT yet net-metered, the meter is unidirectional
-- and reports the grid draw, full stop. Its panels' output is not reflected
-- in that figure at all, so adding generation to it would count the same
-- energy twice and inflate the household's apparent need -- which is the
-- direction that wrongly REFUSES a perfectly good array.
--
-- Hence `FILTER (WHERE ivs.billing_point_id IS NOT NULL)`: only generation
-- from an inverter that has actually joined a connection is added back,
-- because only that generation is inside the meter's arithmetic. For everyone
-- else consumption is the grid draw, which is also precisely the figure a
-- utility would assess an application against.
--
-- This is why a unidirectional meter is deliberately never netted against
-- solar anywhere in the system: once import reads `load - generation` and
-- export cannot be measured, the household's consumption is unrecoverable
-- whenever the panels out-produce the house.
--
-- Consumption is measured across the whole SITE, not one connection. The
-- panels are wired into the property and offset whatever the property draws,
-- and at the moment this test runs the household has not yet chosen which
-- meter to swap -- so there is no connection to scope it to.
WITH w AS (
    SELECT (date_trunc('day', now() AT TIME ZONE 'Asia/Dhaka') - INTERVAL '30 days')
               AT TIME ZONE 'Asia/Dhaka' AS from_ts,
           date_trunc('day', now() AT TIME ZONE 'Asia/Dhaka')
               AT TIME ZONE 'Asia/Dhaka' AS to_ts
),
-- What each of this account's sites consumed over the window, and on how many
-- distinct days it has readings at all.
site_load AS (
    SELECT d.site_id,
           COALESCE(SUM(r.import_kwh)
                    FILTER (WHERE ms.billing_role = 'billing'), 0)::numeric(14,4) AS import_kwh,
           COALESCE(SUM(r.export_kwh)
                    FILTER (WHERE ms.billing_role = 'billing'), 0)::numeric(14,4) AS export_kwh,
           COALESCE(SUM(r.generation_kwh)
                    FILTER (WHERE ivs.billing_point_id IS NOT NULL), 0)::numeric(14,4)
                                                                                  AS generation_kwh,
           COUNT(DISTINCT (r.interval_start AT TIME ZONE 'Asia/Dhaka')::date)
               FILTER (WHERE ms.billing_role = 'billing')                         AS meter_days
    FROM w
    CROSS JOIN site s
    JOIN device d ON d.site_id = s.site_id
    JOIN device_reading r ON r.device_id = d.device_id
    LEFT JOIN meter_spec    ms  ON ms.device_id  = d.device_id
    LEFT JOIN inverter_spec ivs ON ivs.device_id = d.device_id
    WHERE s.account_id = $1
      AND r.interval_start >= w.from_ts
      AND r.interval_start <  w.to_ts
    GROUP BY d.site_id
),
inv AS (
    SELECT d.device_id,
           d.serial_no,
           d.site_id,
           d.manufacturer,
           d.model,
           d.installed_at,
           d.last_seen_at,
           d.status,
           ivs.ac_capacity_kw,
           ivs.billing_point_id,
           COALESCE(SUM(r.generation_kwh), 0)::numeric(14,4) AS generation_kwh,
           COUNT(DISTINCT (r.interval_start AT TIME ZONE 'Asia/Dhaka')::date) AS gen_days
    FROM w
    CROSS JOIN device d
    JOIN inverter_spec ivs ON ivs.device_id = d.device_id
    JOIN site s ON s.site_id = d.site_id
    LEFT JOIN device_reading r
           ON r.device_id = d.device_id
          AND r.interval_start >= w.from_ts
          AND r.interval_start <  w.to_ts
    WHERE s.account_id = $1
      AND d.removed_at IS NULL
    GROUP BY d.device_id, d.serial_no, d.site_id, d.manufacturer, d.model,
             d.installed_at, d.last_seen_at, d.status,
             ivs.ac_capacity_kw, ivs.billing_point_id
),
scored AS (
    SELECT inv.*,
           s.label AS site_label,
           (SELECT COUNT(*) FROM solar_array sa
             WHERE sa.inverter_device_id = inv.device_id
               AND sa.status <> 'decommissioned')::int AS array_count,
           COALESCE(sl.meter_days, 0) AS meter_days,
           -- Daily averages. NULLIF keeps a device with no readings from
           -- dividing by zero; it reports NULL, which the verdict below reads
           -- as "cannot say yet" rather than as zero output.
           (inv.generation_kwh / NULLIF(inv.gen_days, 0))::numeric(12,4) AS generation_daily_kwh,
           ((COALESCE(sl.import_kwh, 0)
             - COALESCE(sl.export_kwh, 0)
             + COALESCE(sl.generation_kwh, 0))
            / NULLIF(sl.meter_days, 0))::numeric(12,4) AS consumption_daily_kwh,
           -- What a healthy array of this rating yields here in a day.
           (inv.ac_capacity_kw * $3::numeric)::numeric(12,4) AS expected_daily_kwh
    FROM inv
    JOIN site s ON s.site_id = inv.site_id
    LEFT JOIN site_load sl ON sl.site_id = inv.site_id
)
SELECT device_id,
       serial_no,
       site_id,
       site_label,
       manufacturer,
       model,
       installed_at,
       last_seen_at,
       status,
       ac_capacity_kw,
       billing_point_id,
       array_count,
       gen_days,
       meter_days,
       generation_kwh,
       generation_daily_kwh,
       consumption_daily_kwh,
       expected_daily_kwh,
       -- The bar this inverter has to clear: the household's own daily need,
       -- plus the surplus that makes the exercise worth the regulator's time.
       -- An array that exactly covers consumption exports almost nothing, so
       -- the agreement would earn no credit and the whole flow would have run
       -- for nothing.
       (consumption_daily_kwh * $2::numeric)::numeric(12,4) AS required_daily_kwh,
       (expected_daily_kwh * $4::numeric)::numeric(12,4)    AS performance_floor_kwh,
       -- Three independent verdicts, reported separately so the page can say
       -- WHICH test failed. A single boolean would leave a household staring
       -- at a refusal with nothing to act on.
       (gen_days >= $5::int AND meter_days >= $5::int)      AS has_enough_history,
       (generation_daily_kwh >= consumption_daily_kwh * $2::numeric)
                                                            AS meets_demand,
       (generation_daily_kwh >= expected_daily_kwh * $4::numeric)
                                                            AS meets_performance,
       (    gen_days >= $5::int
        AND meter_days >= $5::int
        AND generation_daily_kwh >= consumption_daily_kwh * $2::numeric
        AND generation_daily_kwh >= expected_daily_kwh * $4::numeric
       )                                                    AS eligible
FROM scored
-- Newest first, per the global ordering rule. installed_at then device_id, so
-- two inverters registered in the same transaction still order deterministically.
ORDER BY installed_at DESC, device_id DESC;


-- name: swappable_meters_for_site
-- The billing meters at one site that could be swapped for a bidirectional
-- one, for the second dropdown of the net-metering application.
--
-- Excluded, and why:
--   * meters that are already bidirectional -- the swap has happened, or the
--     connection was built that way; there is nothing to replace.
--   * connections already carrying a live net-metering agreement, which
--     nma_no_overlap would refuse anyway. Filtering here means the household
--     is never offered a choice the next request rejects.
--   * removed devices. rule 7 keeps retired meter_spec rows, so this is the
--     LATERAL/LIMIT 1 shape site_points had to adopt for the same reason: a
--     swapped connection otherwise comes back twice, once with NULLs.
SELECT pt.point_id      AS billing_point_id,
       pt.label         AS point_label,
       pt.reference     AS point_reference,
       m.device_id      AS meter_device_id,
       m.serial_no      AS meter_serial,
       m.installed_at,
       m.last_seen_at
FROM billing_point pt
JOIN LATERAL (
    SELECT d.device_id, d.serial_no, d.installed_at, d.last_seen_at
    FROM meter_spec ms
    JOIN device d ON d.device_id = ms.device_id
    WHERE ms.billing_point_id = pt.point_id
      AND ms.billing_role = 'billing'
      AND ms.meter_flow = 'unidirectional'
      AND d.removed_at IS NULL
    LIMIT 1
) m ON TRUE
WHERE pt.site_id = $1
  AND pt.retired_at IS NULL
  AND NOT EXISTS (
      SELECT 1 FROM net_metering_agreement nma
      WHERE nma.billing_point_id = pt.point_id
        AND nma.status IN ('pending', 'active', 'suspended')
  )
ORDER BY pt.created_at DESC, pt.label;


-- name: inverter_for_account
-- One inverter, if this account owns it. Used to turn a chosen inverter id
-- into a site before anything is written -- a caller naming somebody else's
-- inverter gets no row, and therefore a 404 rather than a leak.
SELECT d.device_id, d.site_id, d.serial_no, ivs.ac_capacity_kw,
       ivs.billing_point_id, s.district
FROM device d
JOIN inverter_spec ivs ON ivs.device_id = d.device_id
JOIN site s ON s.site_id = d.site_id
WHERE d.device_id = $1
  AND s.account_id = $2
  AND d.removed_at IS NULL;


-- name: attach_inverter_to_point
-- Bring the panels into a connection. Called once, when the bidirectional
-- meter that makes their export measurable is registered -- never at install
-- time, because until that meter exists there is no export to attribute
-- (rule 6).
--
-- Guarded on the inverter still being unattached, so re-running a registration
-- cannot silently move an inverter off the connection it already serves.
UPDATE inverter_spec
SET billing_point_id = $2
WHERE device_id = $1
  AND billing_point_id IS NULL
RETURNING device_id;
