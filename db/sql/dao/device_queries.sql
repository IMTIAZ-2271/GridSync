-- Device DAO: the telemetry-health read surface for one site.
--
-- Answers a question the portal could not ask before: is the equipment on
-- this site actually reporting? A silent billing meter is money -- rule 8
-- refuses to bill an incomplete period, so a gap here becomes a bill that
-- does not arrive rather than a bill that is wrong. A silent inverter is
-- credit not earned.
--
-- Health is DERIVED from interval coverage, not read off device.status.
-- `status` is a manual/dispatch flag that nothing currently writes except by
-- hand, and `last_seen_at` is only touched by backfill_readings(), so neither
-- is a heartbeat. What the database genuinely knows is how many intervals a
-- device was expected to produce over a window versus how many rows it
-- actually has, and that is what these statements compute. When
-- services/ingest lands and starts stamping last_seen_at per batch, the
-- silence test below can move onto it; the coverage test stays either way.


-- name: device_health
-- Telemetry-reporting devices with their 7-day coverage, for one site or for
-- the whole fleet.
--
-- $1 is the site filter: a uuid narrows to that site, NULL returns every
-- device on every site. One statement rather than two because the customer's
-- equipment page and the supplier's fleet inventory ask exactly the same
-- question at different scopes, and two copies of this arithmetic would
-- eventually disagree about what "degraded" means. The caller decides scope;
-- authorization is the route's job, not this query's.
--
-- Scope: `reports_telemetry` is the filter that answers "which devices should
-- logically have a health check". A device flagged false produces no readings
-- by design, so a coverage figure for it would read 0% forever and mean
-- nothing. Removed devices drop out too -- a decommissioned meter is history,
-- not a fault.
--
-- The window is the last 7 WHOLE Asia/Dhaka days, ending at today's midnight,
-- and excluding today on purpose. Demo telemetry is written by
-- backfill_readings() through yesterday (the onboarding endpoints pass
-- backfill_to = today - 1), so a window running to now() would report every
-- healthy site as partially covered for no reason other than the clock. The
-- zone is named literally rather than left to the session, per CLAUDE.md:
-- date_trunc against a different session TimeZone would slide the window by
-- hours and quietly change every coverage figure.
--
-- The window is clipped at the device's FIRST reading, not at installed_at.
-- That distinction matters here: POST /api/sites/{id}/meter registers a
-- device today and immediately backfills 90 days of history behind it, so
-- installed_at is routinely *later* than the readings it owns. Clipping on
-- installed_at scored every freshly onboarded device 0-of-0 intervals and
-- reported it as 'unknown' while it sat on 4,320 perfectly good rows.
-- installed_at is still the fallback for a device that has never reported at
-- all -- one installed an hour ago has genuinely owed nothing yet.
--
-- The reading scan is bounded to 90 days so it touches the recent partitions
-- rather than every month device_reading holds. 90 days is the horizon the
-- backfill writes and the portal charts; a device silent for longer than that
-- reads as 'no_data' rather than 'silent', which is the same call to action.
WITH bounds AS (
    SELECT date_trunc('day', now() AT TIME ZONE 'Asia/Dhaka')
               AT TIME ZONE 'Asia/Dhaka' AS window_to,
           (date_trunc('day', now() AT TIME ZONE 'Asia/Dhaka') - interval '7 days')
               AT TIME ZONE 'Asia/Dhaka' AS window_from
),
live AS (
    SELECT d.device_id,
           d.site_id,
           si.label AS site_label,
           si.district,
           d.device_type,
           d.serial_no,
           d.manufacturer,
           d.model,
           d.firmware_version,
           d.interval_minutes,
           d.installed_at,
           d.status
    FROM device d
    JOIN site si ON si.site_id = d.site_id
    WHERE ($1::uuid IS NULL OR d.site_id = $1)
      AND d.removed_at IS NULL
      AND d.reports_telemetry
),
horizon AS (
    -- How far this device's telemetry actually reaches, within the retained
    -- horizon. Drives both the window clip and the silence test.
    SELECT l.device_id,
           min(r.interval_start) AS first_reading_at,
           max(r.interval_start) AS last_reading_at
    FROM live l
    LEFT JOIN device_reading r
           ON r.device_id = l.device_id
          AND r.interval_start >= now() - interval '90 days'
    GROUP BY l.device_id
),
measured AS (
    SELECT l.*,
           h.first_reading_at,
           h.last_reading_at,
           greatest(b.window_from,
                    coalesce(h.first_reading_at, l.installed_at)) AS measured_from,
           b.window_to AS measured_to
    FROM live l
    JOIN horizon h ON h.device_id = l.device_id
    CROSS JOIN bounds b
),
expected AS (
    -- Whole intervals the device owed over its measured slice of the window.
    -- floor(), not round(): a partial interval was never due. Its own CTE
    -- because coverage_pct and the health verdict both divide by it, and
    -- three copies of this arithmetic would eventually disagree.
    SELECT m.*,
           greatest(0, floor(
               EXTRACT(epoch FROM (m.measured_to - m.measured_from))
               / (m.interval_minutes * 60)
           ))::bigint AS intervals_expected
    FROM measured m
),
counted AS (
    SELECT e.device_id,
           count(r.interval_start) AS intervals_received,
           count(r.interval_start) FILTER (
               WHERE r.quality <> 'good'
           ) AS intervals_suspect
    FROM expected e
    LEFT JOIN device_reading r
           ON r.device_id = e.device_id
          AND r.interval_start >= e.measured_from
          AND r.interval_start <  e.measured_to
    GROUP BY e.device_id
)
SELECT m.device_id,
       m.site_id,
       m.site_label,
       m.district,
       m.device_type::text                       AS device_type,
       m.serial_no,
       m.manufacturer,
       m.model,
       m.firmware_version,
       m.interval_minutes,
       m.installed_at,
       m.status::text                            AS status,
       m.measured_from                           AS window_from,
       m.measured_to                             AS window_to,
       m.last_reading_at,
       c.intervals_received,
       c.intervals_suspect,
       m.intervals_expected,

       CASE WHEN m.intervals_expected > 0
            THEN round(100.0 * c.intervals_received / m.intervals_expected, 1)
       END::numeric(5,1)                         AS coverage_pct,

       -- What this device is FOR, which is what decides how much a gap costs.
       -- Rule 7: exactly one meter per site is 'billing'; the others never
       -- feed a bill. NULL for an inverter.
       ms.billing_role::text                     AS billing_role,
       ms.meter_flow::text                       AS meter_flow,
       inv.ac_capacity_kw,

       -- Arrays hanging off this inverter. A decommissioned array is not a
       -- fault, so it is excluded from both the count and the capacity.
       coalesce(arr.array_count, 0)              AS array_count,
       arr.dc_capacity_kw,
       arr.array_status,

       -- Derived health, in the order a reader would triage it: a device
       -- flagged faulty is faulty whatever its rows say; then silence; then
       -- how complete the window is. 'unknown' covers a device too new to
       -- have owed a single interval yet.
       CASE
           WHEN m.status = 'faulty'                             THEN 'faulty'
           WHEN m.last_reading_at IS NULL                       THEN 'no_data'
           WHEN m.last_reading_at < now() - interval '48 hours' THEN 'silent'
           WHEN m.intervals_expected = 0                        THEN 'unknown'
           WHEN c.intervals_received * 100
                < m.intervals_expected * 90                     THEN 'degraded'
           ELSE 'healthy'
       END AS health
FROM expected m
JOIN counted c ON c.device_id = m.device_id
LEFT JOIN meter_spec ms ON ms.device_id = m.device_id
LEFT JOIN inverter_spec inv ON inv.device_id = m.device_id
LEFT JOIN LATERAL (
    SELECT count(*)::int                  AS array_count,
           sum(sa.dc_capacity_kw)         AS dc_capacity_kw,
           -- max(), not min(): of the two statuses that survive the filter,
           -- 'offline' sorts after 'active', so this reports the worst array
           -- rather than the most flattering one.
           max(sa.status::text)           AS array_status
    FROM solar_array sa
    WHERE sa.inverter_device_id = m.device_id
      AND sa.status <> 'decommissioned'
) arr ON true
-- Billing meter first: it is the device whose silence costs the customer
-- money, so it should never be something the reader has to scroll for.
ORDER BY m.site_label,
         (ms.billing_role = 'billing') DESC NULLS LAST,
         m.device_type,
         m.serial_no;
