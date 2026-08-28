-- Ingest DAO: the statements POST /v1/ingest/readings runs.
--
-- Loaded into the same namespace as every other file under db/sql/dao/, so
-- services/ingest asks for these by name exactly as a route handler does.
--
-- Rule 4 governs this whole file: retries and re-runs are safe because the
-- database refuses duplicates, not because the service is careful. Two
-- constraints do it -- `ingest_batch.idempotency_key` is UNIQUE, and
-- `device_reading` is keyed on (device_id, interval_start).


-- name: device_for_ingest
-- Authenticate a device: who it says it is, and what it is allowed to report.
--
-- Returns the key hash for the caller to verify. Deliberately NOT verified in
-- SQL -- argon2 lives in Python and a hash comparison in the database would
-- put the secret in the query log.
--
-- billing_role comes back so the service can enforce rule 6 before the trigger
-- does: an inverter reports generation only, and only a meter at the grid
-- boundary knows the import/export split. Answering 422 with a sentence beats
-- surfacing a trigger's exception.
SELECT d.device_id,
       d.device_key_hash,
       d.device_type,
       d.interval_minutes,
       d.removed_at,
       d.status,
       ms.billing_role,
       ms.meter_flow
FROM device d
LEFT JOIN meter_spec ms ON ms.device_id = d.device_id
WHERE d.device_id = $1;


-- name: find_ingest_batch
-- The batch this Idempotency-Key already created, if any.
--
-- A replay must return the ORIGINAL outcome, not re-apply the readings, so the
-- counts come back with it. Scoped by device as well as key: a key that
-- belongs to another device is a broken client, and answering it with someone
-- else's counts would be worse than refusing.
SELECT batch_id, device_id, reading_count, accepted_count,
       duplicate_count, rejected_count, received_at
FROM ingest_batch
WHERE idempotency_key = $1;


-- name: open_ingest_batch
-- Claim the Idempotency-Key. UNIQUE on the key is what makes two concurrent
-- retries produce one batch; the loser catches UniqueViolation and re-reads.
INSERT INTO ingest_batch (device_id, idempotency_key, reading_count, client_ip)
VALUES ($1, $2, $3, $4::inet)
RETURNING batch_id;


-- name: close_ingest_batch
-- Fill in what happened. Separate from the insert because the counts are not
-- known until the readings have been attempted, and `batch_counts_within_total`
-- is written with '<=' precisely so the row may exist before they are.
UPDATE ingest_batch
SET accepted_count  = $2,
    duplicate_count = $3,
    rejected_count  = $4
WHERE batch_id = $1;


-- name: insert_reading
-- One reading. ON CONFLICT DO NOTHING is rule 4 in one line: a device that
-- retries a batch it already delivered writes nothing and is told so, rather
-- than being refused or -- far worse -- silently overwriting a reading the
-- billing engine has already used.
--
-- RETURNING is what tells accepted from duplicate: DO NOTHING yields no row on
-- conflict, so a NULL return means the reading was already held.
INSERT INTO device_reading (
    device_id, interval_start, interval_minutes,
    import_kwh, export_kwh, generation_kwh,
    voltage_avg, frequency_avg, dc_voltage_avg,
    source, quality, ingest_batch_id
)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, 'device', 'good', $10)
ON CONFLICT (device_id, interval_start) DO NOTHING
RETURNING interval_start;


-- name: divert_late_reading
-- Rule 8's other half: a reading for a period that is already frozen, billed
-- or closed never touches device_reading. It is kept, in full, so the operator
-- can see what arrived and decide -- a correction is a new bill (rule 1), not
-- an edit to the one that exists.
INSERT INTO late_reading (
    device_id, interval_start, import_kwh, export_kwh, generation_kwh,
    reason, ingest_batch_id
)
VALUES ($1, $2, $3, $4, $5, $6::late_reason, $7)
RETURNING late_id;


-- name: reading_period_is_open
-- Whether this device's billing point has a period covering this instant that
-- has already been closed to new readings.
--
-- Returns the offending status, or NULL when the interval is free to write.
-- Both subtypes name their own point since migration d4f8a2c61e95, so this
-- resolves through meter_spec OR inverter_spec -- an inverter whose panels
-- belong to no connection has no period to violate, and correctly yields NULL.
-- period_start / period_end are DATEs and the range is inclusive of both, so
-- the comparison is the same `daterange(..., '[]') @> ts::date` that
-- backfill_readings uses. Written the same way on purpose: two spellings of
-- rule 8's boundary that could disagree would be worse than one repeated.
-- The zone is named literally -- ::date on a bare timestamptz resolves against
-- the session zone, which would put the boundary on a different day for a
-- client connecting from elsewhere.
SELECT bp.status::text
FROM device d
LEFT JOIN meter_spec    ms  ON ms.device_id  = d.device_id
LEFT JOIN inverter_spec ivs ON ivs.device_id = d.device_id
JOIN billing_period bp
  ON bp.billing_point_id = COALESCE(ms.billing_point_id, ivs.billing_point_id)
WHERE d.device_id = $1
  AND bp.status IN ('frozen', 'billed', 'closed')
  AND daterange(bp.period_start, bp.period_end, '[]')
        @> ($2::timestamptz AT TIME ZONE 'Asia/Dhaka')::date
LIMIT 1;


-- name: touch_device_seen
-- Stamp last_seen_at from a real delivery. This is what turns device_health
-- from coverage-of-backfilled-rows into something a heartbeat actually feeds:
-- before ingest existed, nothing but backfill_readings ever wrote this column,
-- which is why CLAUDE.md called the health verdict coverage-derived.
UPDATE device
SET last_seen_at = greatest(coalesce(last_seen_at, $2::timestamptz), $2::timestamptz)
WHERE device_id = $1;


-- name: rotate_device_key
-- Issue a new key for a device. The plaintext is generated by the caller,
-- shown once and never stored -- only this hash is kept, exactly as an
-- account password is.
UPDATE device
SET device_key_hash = $2,
    device_key_rotated_at = now()
WHERE device_id = $1
RETURNING device_id, serial_no, device_type;


-- name: devices_needing_keys
-- Every live telemetry device, for the provisioning script. Ordered so a
-- re-run assigns keys in a stable order and its output diffs cleanly.
--
-- meter_flow and billing_point_id come back because the simulator needs both
-- to produce a plausible reading. A unidirectional meter must never report an
-- export figure (rule 6, and the ingest service refuses it), and a
-- bidirectional one has to be netted against the inverters on its OWN
-- connection -- netting it against the site's whole fleet would credit one
-- connection for another's export, the same error rule 3 exists to prevent.
SELECT d.device_id, d.serial_no, d.device_type, d.interval_minutes,
       s.label AS site_label, s.site_id,
       COALESCE(ivs.ac_capacity_kw, 0)::numeric        AS ac_capacity_kw,
       ms.meter_flow::text                             AS meter_flow,
       COALESCE(ms.billing_point_id, ivs.billing_point_id) AS billing_point_id
FROM device d
JOIN site s ON s.site_id = d.site_id
LEFT JOIN inverter_spec ivs ON ivs.device_id = d.device_id
LEFT JOIN meter_spec    ms  ON ms.device_id  = d.device_id
WHERE d.removed_at IS NULL
  AND d.reports_telemetry
ORDER BY s.label, d.device_type, d.serial_no;
