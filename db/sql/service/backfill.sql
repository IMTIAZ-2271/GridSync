-- GridSync backfill engine.
--
--   backfill_readings(p_device_id uuid, p_from date, p_to date,
--                      p_capacity_kw numeric DEFAULT NULL) RETURNS integer
--
-- Synthesizes 30-minute interval readings for ONE device over [p_from, p_to]
-- inclusive (whole Asia/Dhaka days), written through a single ingest_batch
-- with source = 'backfill'. Used by the onboarding endpoints (POST
-- /api/sites/{id}/meter, POST /api/sites/{id}/solar) so a freshly registered
-- device has a history to chart and bill against instead of an empty portal.
--
-- Same profile db/sql/seed_demo.sql uses, extracted so both share one curve
-- instead of two copies drifting apart:
--   household load   0.15 kWh base, +0.25 morning (07:00-09:00),
--                     +0.45 evening (18:00-22:00), plus noise
--   solar generation  half-sine peaking at 12:00, zero outside 06:00-18:00,
--                     scaled by p_capacity_kw
--
-- device_type decides the shape written, per rule 6:
--   meter    -> import_kwh/export_kwh netted from consumption against
--               generation (only the grid-boundary meter knows the split;
--               p_capacity_kw is NULL for a site with no solar yet, which
--               nets to plain consumption)
--   inverter -> generation_kwh only, requires p_capacity_kw > 0
--
-- Upserts on rule 4's PRIMARY KEY (device_id, interval_start): re-running
-- over an overlapping window RECOMPUTES those intervals rather than skipping
-- them. That is deliberate, not merely tolerated -- POST /api/sites/{id}/meter
-- registers the billing meter before solar exists (p_capacity_kw NULL, so
-- export nets to zero), and POST /api/sites/{id}/solar re-runs this same call
-- against the meter once the array's capacity is known, so the meter's
-- history stops understating export the moment the site's actual solar
-- capacity exists. Without that re-net every freshly onboarded solar site
-- would show zero export and therefore never earn or roll over credit --
-- the exact behaviour this project exists to demonstrate.
--
-- docs/erd-logical.md documents a reject_into_late_reading trigger (rule 8)
-- that would divert a write against an already-frozen/billed period to
-- late_reading. That trigger does not actually exist in this schema yet (see
-- migration 0f6109903981's own note that it "arrives with the billing
-- module" -- it never did). An upsert is exactly the kind of write rule 8
-- warns about, so this function enforces the boundary itself rather than
-- trusting a guard that isn't there: any interval whose calendar date falls
-- inside a 'frozen', 'billed' or 'closed' billing_period for the device's
-- own billing point is excluded from the write, full stop. A bill already
-- cut from a period can never be contradicted by a later re-net.

CREATE OR REPLACE FUNCTION backfill_readings(
    p_device_id   uuid,
    p_from        date,
    p_to          date,
    p_capacity_kw numeric DEFAULT NULL
) RETURNS integer
    LANGUAGE plpgsql
AS $fn$
DECLARE
    -- Named literally rather than read from site.timezone: this project is a
    -- Bangladesh-only demo and billing.sql / seed_demo.sql both hardcode the
    -- same zone, so there is one answer to keep in step with, not a column to
    -- trust at call time.
    partition_zone constant text := 'Asia/Dhaka';

    v_device_type device_type;
    v_site_id     uuid;

    -- The billing point this device's readings belong to, when it has one.
    -- NULL for a device that serves no connection; the rule-8 guard below
    -- then falls back to the whole site, which refuses more than it must but
    -- never less.
    v_point_id    uuid;
    v_batch_id    uuid;
    v_window_from timestamptz;
    v_window_to   timestamptz;
    v_count       integer;
    v_month       date;
BEGIN
    IF p_to < p_from THEN
        RAISE EXCEPTION 'backfill window is empty: % is before %', p_to, p_from
            USING ERRCODE = '22007';
    END IF;

    SELECT d.device_type, d.site_id INTO v_device_type, v_site_id
    FROM device d
    WHERE d.device_id = p_device_id;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'device % does not exist', p_device_id
            USING ERRCODE = '23503';
    END IF;

    -- A meter names its point directly; an inverter reaches one through the
    -- billing meter it hangs off (see create_inverter_device). Resolving this
    -- is what keeps a second connection at an already-billed site
    -- backfillable: a site-wide guard would see the first point's frozen
    -- periods and refuse to write a single row for the new meter.
    SELECT coalesce(own.billing_point_id, parent.billing_point_id)
      INTO v_point_id
    FROM device d
    LEFT JOIN meter_spec own    ON own.device_id = d.device_id
    LEFT JOIN meter_spec parent ON parent.device_id = d.parent_device_id
    WHERE d.device_id = p_device_id;

    IF v_device_type NOT IN ('meter', 'inverter') THEN
        RAISE EXCEPTION 'backfill_readings supports meter and inverter '
            'devices only, got %', v_device_type;
    END IF;

    IF v_device_type = 'inverter' AND coalesce(p_capacity_kw, 0) <= 0 THEN
        RAISE EXCEPTION 'an inverter backfill requires p_capacity_kw > 0';
    END IF;

    -- Partitions covering the window (idempotent -- see create_reading_partition).
    FOR v_month IN
        SELECT generate_series(date_trunc('month', p_from::timestamp),
                                date_trunc('month', p_to::timestamp),
                                interval '1 month')::date
    LOOP
        PERFORM create_reading_partition(v_month);
    END LOOP;

    v_window_from := date_trunc('day', p_from::timestamp) AT TIME ZONE partition_zone;
    v_window_to   := (date_trunc('day', p_to::timestamp) + interval '1 day')
                          AT TIME ZONE partition_zone;

    INSERT INTO ingest_batch (device_id, idempotency_key, reading_count, accepted_count)
    VALUES (p_device_id, 'backfill-' || gen_random_uuid()::text, 0, 0)
    RETURNING batch_id INTO v_batch_id;

    IF v_device_type = 'meter' THEN
        WITH curve AS (
            SELECT
                g.ts,
                round((
                    0.15
                    + CASE WHEN h.hr >= 7  AND h.hr < 9  THEN 0.25 ELSE 0 END
                    + CASE WHEN h.hr >= 18 AND h.hr < 22 THEN 0.45 ELSE 0 END
                    + random() * 0.06
                )::numeric, 4) AS consumption_kwh,
                round((
                    CASE WHEN coalesce(p_capacity_kw, 0) > 0
                              AND h.hr > 6 AND h.hr < 18
                         THEN p_capacity_kw * 0.5
                              * sin(pi() * (h.hr - 6) / 12.0)
                              * (0.82 + random() * 0.18)
                         ELSE 0
                    END
                )::numeric, 4) AS generation_kwh
            FROM generate_series(v_window_from, v_window_to - interval '30 minutes',
                                  interval '30 minutes') AS g(ts)
            CROSS JOIN LATERAL (
                SELECT EXTRACT(hour FROM g.ts AT TIME ZONE partition_zone)
                       + EXTRACT(minute FROM g.ts AT TIME ZONE partition_zone) / 60.0 AS hr
            ) AS h
        )
        INSERT INTO device_reading (
            device_id, interval_start, interval_minutes,
            import_kwh, export_kwh, generation_kwh,
            voltage_avg, frequency_avg, source, quality, ingest_batch_id
        )
        SELECT p_device_id, c.ts, 30,
               greatest(0, c.consumption_kwh - c.generation_kwh)::numeric(12,4),
               greatest(0, c.generation_kwh - c.consumption_kwh)::numeric(12,4),
               NULL,
               round((228 + random() * 8)::numeric, 2),
               round((49.9 + random() * 0.2)::numeric, 3),
               'backfill', 'good', v_batch_id
        FROM curve c
        WHERE NOT EXISTS (
            SELECT 1 FROM billing_period bp
            WHERE (CASE WHEN v_point_id IS NULL
                        THEN bp.site_id = v_site_id
                        ELSE bp.billing_point_id = v_point_id
                   END)
              AND bp.status IN ('frozen', 'billed', 'closed')
              AND daterange(bp.period_start, bp.period_end, '[]')
                    @> (c.ts AT TIME ZONE partition_zone)::date
        )
        ON CONFLICT (device_id, interval_start) DO UPDATE SET
            import_kwh      = EXCLUDED.import_kwh,
            export_kwh      = EXCLUDED.export_kwh,
            generation_kwh  = EXCLUDED.generation_kwh,
            voltage_avg     = EXCLUDED.voltage_avg,
            frequency_avg   = EXCLUDED.frequency_avg,
            source          = EXCLUDED.source,
            quality         = EXCLUDED.quality,
            ingest_batch_id = EXCLUDED.ingest_batch_id,
            ingested_at     = now();
    ELSE
        WITH curve AS (
            SELECT
                g.ts,
                round((
                    CASE WHEN h.hr > 6 AND h.hr < 18
                         THEN p_capacity_kw * 0.5
                              * sin(pi() * (h.hr - 6) / 12.0)
                              * (0.82 + random() * 0.18)
                         ELSE 0
                    END
                )::numeric, 4) AS generation_kwh
            FROM generate_series(v_window_from, v_window_to - interval '30 minutes',
                                  interval '30 minutes') AS g(ts)
            CROSS JOIN LATERAL (
                SELECT EXTRACT(hour FROM g.ts AT TIME ZONE partition_zone)
                       + EXTRACT(minute FROM g.ts AT TIME ZONE partition_zone) / 60.0 AS hr
            ) AS h
        )
        INSERT INTO device_reading (
            device_id, interval_start, interval_minutes,
            import_kwh, export_kwh, generation_kwh,
            dc_voltage_avg, source, quality, ingest_batch_id
        )
        SELECT p_device_id, c.ts, 30,
               NULL, NULL, c.generation_kwh,
               round((330 + random() * 40)::numeric, 2),
               'backfill', 'good', v_batch_id
        FROM curve c
        WHERE NOT EXISTS (
            SELECT 1 FROM billing_period bp
            WHERE (CASE WHEN v_point_id IS NULL
                        THEN bp.site_id = v_site_id
                        ELSE bp.billing_point_id = v_point_id
                   END)
              AND bp.status IN ('frozen', 'billed', 'closed')
              AND daterange(bp.period_start, bp.period_end, '[]')
                    @> (c.ts AT TIME ZONE partition_zone)::date
        )
        ON CONFLICT (device_id, interval_start) DO UPDATE SET
            generation_kwh  = EXCLUDED.generation_kwh,
            dc_voltage_avg  = EXCLUDED.dc_voltage_avg,
            source          = EXCLUDED.source,
            quality         = EXCLUDED.quality,
            ingest_batch_id = EXCLUDED.ingest_batch_id,
            ingested_at     = now();
    END IF;

    GET DIAGNOSTICS v_count = ROW_COUNT;

    UPDATE ingest_batch
    SET reading_count = v_count, accepted_count = v_count
    WHERE batch_id = v_batch_id;

    UPDATE device SET last_seen_at = now() WHERE device_id = p_device_id;

    RETURN v_count;
END;
$fn$;


COMMENT ON FUNCTION backfill_readings(uuid, date, date, numeric) IS
'Writes synthetic 30-minute readings for one device over [p_from, p_to] '
'(whole Asia/Dhaka days), through a single backfill ingest_batch. meter '
'devices get import/export netted against p_capacity_kw''s solar curve '
'(NULL capacity nets to plain consumption); inverter devices get generation '
'only and require p_capacity_kw > 0. Upserts on (device_id, interval_start), '
'so re-running over an overlapping window recomputes it -- used to re-net a '
'meter''s history once solar capacity becomes known. Any interval inside an '
'already frozen/billed/closed billing_period is skipped regardless (rule 8), '
'since no trigger currently enforces that boundary on device_reading. '
'Extracted from db/sql/seed_demo.sql for reuse by the customer onboarding '
'endpoints.';
