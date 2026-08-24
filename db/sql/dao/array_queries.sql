-- Solar array health.
--
-- Consumer requirement 8 asks for "device health for individual solar panels".
-- CLAUDE.md decision 2 settled that as per **array**, not per panel, and the
-- reason is in the telemetry: nothing in this system knows a panel exists. An
-- inverter reports one generation figure for everything wired to it. Splitting
-- that across twelve panels would be arithmetic dressed as measurement, and the
-- first time it said "panel 7 is failing" it would be inventing a fact.
--
-- What an array CAN be judged on is two things the schema does know: whether
-- its inverter is reporting at all, and how much it produced per kW of the
-- capacity it was built with. The second is the one that catches a real fault --
-- a shaded, soiled or partly-failed array still reports, it just reports less.


-- name: array_health_for_site
-- One row per live array, with its inverter's reporting coverage and its yield.
--
-- The window is the last 7 whole Asia/Dhaka days ending at today's midnight,
-- **excluding today** -- identical to `device_health`, and for the same reason:
-- the backfill writes through yesterday, so a window running to now() would
-- score every healthy array short for no reason but the clock. The zone is
-- named literally rather than taken from the session (CLAUDE.md's rule).
--
-- `specific_yield_kwh_per_kw` is generation divided by the array's DC capacity:
-- roughly comparable between a 3 kW array and a 9 kW one, which raw kWh is not.
-- It is NULL when it cannot be attributed -- see `sole_array_on_inverter`.
WITH bounds AS (
    SELECT (date_trunc('day', (now() AT TIME ZONE 'Asia/Dhaka'))
            - INTERVAL '7 days') AT TIME ZONE 'Asia/Dhaka' AS window_from,
           date_trunc('day', (now() AT TIME ZONE 'Asia/Dhaka'))
               AT TIME ZONE 'Asia/Dhaka' AS window_to
),
gen AS (
    SELECT dr.device_id,
           coalesce(round(sum(dr.generation_kwh), 4), 0)::numeric(12,4) AS generation_kwh,
           count(*)::int  AS intervals_received,
           max(dr.interval_start) AS last_reading_at
    FROM device_reading dr
    CROSS JOIN bounds b
    WHERE dr.interval_start >= b.window_from
      AND dr.interval_start <  b.window_to
      AND dr.generation_kwh IS NOT NULL
    GROUP BY dr.device_id
)
SELECT sa.array_id,
       sa.site_id,
       sa.label,
       sa.status::text        AS status,
       sa.panel_count,
       sa.panel_watt_peak,
       sa.dc_capacity_kw,
       sa.azimuth_deg,
       sa.tilt_deg,
       sa.shading_factor,
       sa.commissioned_on,
       sc.supplier_id         AS installed_by_supplier_id,
       sc.name                AS installed_by_supplier_name,
       sa.inverter_device_id,
       d.serial_no            AS inverter_serial,
       coalesce(g.intervals_received, 0) AS intervals_received,
       -- What the inverter owed over the window at its own declared interval.
       (7 * 1440 / d.interval_minutes)::int AS intervals_expected,
       g.last_reading_at,
       coalesce(g.generation_kwh, 0)::numeric(12,4) AS generation_kwh,
       -- One inverter can carry several arrays, and its generation figure does
       -- not say which produced what. Yield is reported only when the answer is
       -- unambiguous; otherwise NULL, and the UI says why rather than showing a
       -- number that is quietly the sum of two arrays over one array's size.
       (
           SELECT count(*) = 1
           FROM solar_array x
           WHERE x.inverter_device_id = sa.inverter_device_id
             AND x.status <> 'decommissioned'
       ) AS sole_array_on_inverter,
       CASE
           WHEN sa.dc_capacity_kw > 0
            AND (SELECT count(*) FROM solar_array x
                 WHERE x.inverter_device_id = sa.inverter_device_id
                   AND x.status <> 'decommissioned') = 1
           THEN round(coalesce(g.generation_kwh, 0) / sa.dc_capacity_kw, 2)
       END AS specific_yield_kwh_per_kw
FROM solar_array sa
JOIN device d ON d.device_id = sa.inverter_device_id
LEFT JOIN gen g ON g.device_id = sa.inverter_device_id
LEFT JOIN supplier_company sc ON sc.supplier_id = sa.installed_by_supplier_id
WHERE sa.site_id = $1
  AND sa.status <> 'decommissioned'
ORDER BY sa.label;
