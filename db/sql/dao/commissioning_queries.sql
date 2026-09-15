-- Commissioning DAO: the handshake a utility's head-end completes to receive a
-- device's key, and the one statement ingest runs to call it live.
--
-- Every state change here is ONE guarded UPDATE whose WHERE clause is the
-- whole precondition -- status, deadline, owner, retirement, serial -- so there
-- is no window between checking and changing. A statement that matched nothing
-- is diagnosed afterwards by reading the row, purely to choose the sentence.
--
-- Deadlines are compared against now() rather than trusted to the sweep
-- (decision 3): an offer past offer_expires_at is dead whether or not the sweep
-- has run yet.


-- name: source_for_auth
-- Authenticate a head-end. The key hash comes back for Python to verify; argon2
-- lives there, and comparing in SQL would put the secret in the query log.
SELECT ts.source_id, ts.source_key_hash, ts.disabled_at, ts.name,
       ts.distribution_company_id
FROM telemetry_source ts
WHERE ts.source_id = $1;


-- name: source_commissions
-- The head-end's feed: every handshake it should be acting on right now.
--
-- `offered` rows are there to be claimed, `activated` rows are waiting for
-- their first batch, `live` rows are meters it should be delivering for. A
-- head-end reconciles against this list on every poll: anything it holds a
-- key for that is not listed, it stops sending. That is what makes a retired
-- meter go quiet without GridSync having to reach the head-end.
--
-- point_solar_capacity_kw is a SIMULATION hint, and the wire model labels it
-- so. A real head-end would not know what panels sit behind a connection; a
-- synthetic one needs it to produce believable export on a bidirectional
-- meter. Summed over live inverters on this meter's OWN connection, never the
-- site -- netting against another connection's panels is rule 3's error.
SELECT dc.commissioning_id,
       dc.status::text                AS status,
       dc.offered_at,
       dc.offer_expires_at,
       dc.activation_expires_at,
       dc.activation_count,
       dc.live_at,
       dc.backfill_from,
       dc.backfill_to,
       d.device_id,
       d.serial_no,
       d.device_type::text            AS device_type,
       d.interval_minutes,
       ms.meter_flow::text            AS meter_flow,
       COALESCE((
           SELECT sum(ivs.ac_capacity_kw)
           FROM inverter_spec ivs
           JOIN device inv ON inv.device_id = ivs.device_id
           WHERE ivs.billing_point_id = ms.billing_point_id
             AND inv.removed_at IS NULL
       ), 0)::numeric(8,3)            AS point_solar_capacity_kw
FROM device_commissioning dc
JOIN device d ON d.device_id = dc.device_id
LEFT JOIN meter_spec ms ON ms.device_id = d.device_id
WHERE dc.source_id = $1
  AND d.removed_at IS NULL
  AND (   (dc.status = 'offered'   AND dc.offer_expires_at      > now())
       OR (dc.status = 'activated' AND dc.activation_expires_at > now())
       OR  dc.status = 'live')
ORDER BY dc.offered_at, dc.commissioning_id;


-- name: activate_commissioning
-- Claim an offer, or retry a claim, and open (or restart) the window in which
-- the key must sign an accepted batch.
--
-- $1 commissioning_id, $2 source_id, $3 serial_no the head-end claims,
-- $4 activation window, $5 cap on keys minted.
--
-- A retry is legal only while `activated`: no reading has been signed with the
-- previous key, so replacing it breaks nothing. Two concurrent retries queue on
-- the row lock and the second re-evaluates this WHERE after the first commits,
-- so they run in order and the key the caller rotates in afterwards is the
-- later one -- the last response is the working key.
--
-- The serial must match the device: the head-end is asserting it holds THIS
-- meter, and a mismatch means it is about to deliver another meter's readings
-- under this device's identity.
UPDATE device_commissioning dc
SET status                = 'activated',
    activated_at          = now(),
    activation_expires_at = now() + $4::interval,
    activation_count      = dc.activation_count + 1
FROM device d
WHERE dc.commissioning_id = $1
  AND dc.source_id = $2
  AND d.device_id = dc.device_id
  AND d.removed_at IS NULL
  AND d.serial_no = $3
  AND dc.activation_count < $5
  AND (   (dc.status = 'offered'   AND dc.offer_expires_at      > now())
       OR (dc.status = 'activated' AND dc.activation_expires_at > now()))
RETURNING dc.commissioning_id, dc.device_id, dc.activation_expires_at,
          dc.activation_count, dc.backfill_from, dc.backfill_to,
          d.interval_minutes;


-- name: rekey_live_commissioning
-- A head-end replaces the key of a LIVE meter it owns, because it lost the one
-- it had -- a host restarted on an empty disk. The handshake does not move:
-- the meter is still live, and a rotation is not a new activation, so neither
-- activation_count nor its window is touched. What records it is the device's
-- own device_key_rotated_at.
--
-- $1 commissioning_id, $2 source_id, $3 serial_no, $4 the new key's hash.
--
-- Live only: before that, activation is the way to get a key and carries the
-- window and cap. Not retired, and the serial must match, for the same reasons
-- activation checks both.
UPDATE device d
SET device_key_hash       = $4,
    device_key_rotated_at = now()
FROM device_commissioning dc
WHERE dc.commissioning_id = $1
  AND dc.source_id = $2
  AND dc.status = 'live'
  AND d.device_id = dc.device_id
  AND d.removed_at IS NULL
  AND d.serial_no = $3
RETURNING dc.commissioning_id, d.device_id, d.interval_minutes;


-- name: commissioning_for_source
-- Why a guarded statement matched nothing. Scoped by source, so another
-- head-end's handshake reads as not found rather than confirming it exists.
SELECT dc.status::text AS status,
       dc.offer_expires_at,
       dc.activation_expires_at,
       dc.activation_count,
       d.serial_no,
       d.removed_at,
       now()           AS db_now
FROM device_commissioning dc
JOIN device d ON d.device_id = dc.device_id
WHERE dc.commissioning_id = $1
  AND dc.source_id = $2;


-- name: reject_commissioning
-- The head-end refuses the meter -- typically a serial it has no record of,
-- which is a technician's typo. Open handshakes only: a LIVE meter is retired
-- by GridSync, not disowned by the head-end after delivering for it.
--
-- Deadlines are deliberately not checked. A rejection of a lapsed offer is
-- still the more informative ending, and it is terminal either way.
--
-- Returns what the caller needs to revoke a minted key and tell the office.
WITH ended AS (
    UPDATE device_commissioning dc
    SET status         = 'failed',
        failed_reason  = 'rejected_by_source',
        failure_detail = $3,
        ended_at       = now()
    WHERE dc.commissioning_id = $1
      AND dc.source_id = $2
      AND dc.status IN ('offered', 'activated')
    RETURNING dc.commissioning_id, dc.device_id, dc.activation_count
)
SELECT e.commissioning_id, e.device_id, e.activation_count,
       d.serial_no, s.site_id, s.label AS site_label, s.district
FROM ended e
JOIN device d ON d.device_id = e.device_id
JOIN site s ON s.site_id = d.site_id;


-- name: mark_commissioning_live
-- Run by ingest inside the batch's transaction, after at least one reading
-- signed with the device's key was accepted or already held. That -- not the
-- head-end's say-so -- is the proof the key reached whatever sends readings.
--
-- Guarded on the stored deadline: a batch arriving after the activation lapsed
-- is still accepted (the key authenticated), but it does not revive a
-- handshake the sweep is about to fail. Matches nothing for a device that was
-- never commissioned, which is every device keyed by issue_device_keys.
UPDATE device_commissioning
SET status  = 'live',
    live_at = now()
WHERE device_id = $1
  AND status = 'activated'
  AND activation_expires_at > now()
RETURNING commissioning_id;


-- ---------------------------------------------------------------------------
-- The API's half: offering a registered meter, and cancelling on retirement.
-- ---------------------------------------------------------------------------


-- name: commissioning_route
-- Which head-end a new meter on this connection belongs to.
--
-- The utility is decided in this order, first answer wins:
--
--   1. the connection's own distribution company -- whoever bills the
--      connection runs the network its meter talks to;
--   2. the company that issued the meter -- needed because a connection opened
--      through the API carries no company (create_billing_point never sets
--      one), and without this every household's second meter would read as
--      having no head-end;
--   3. the district's serving company, lowest code first -- the same
--      deterministic fallback utility_for_site uses, so Badda's two utilities
--      cannot answer differently on two calls.
--
-- $1 point_id, $2 meter_asset_id (may be NULL). A company with no head-end, or
-- only a disabled one, yields source_id NULL, which the caller records as
-- `no_source` rather than skipping.
WITH company AS (
    SELECT COALESCE(
        (SELECT bp.distribution_company_id
         FROM billing_point bp
         WHERE bp.point_id = $1),
        (SELECT ma.issued_by_company_id
         FROM meter_asset ma
         WHERE ma.meter_asset_id = $2),
        (SELECT dc.company_id
         FROM billing_point bp
         JOIN site s ON s.site_id = bp.site_id
         JOIN distribution_company_area dca ON dca.district = s.district
         JOIN distribution_company dc ON dc.company_id = dca.company_id
         WHERE bp.point_id = $1
           AND dc.status = 'active'
         ORDER BY dc.code
         LIMIT 1)
    ) AS company_id
)
SELECT c.company_id, ts.source_id
FROM company c
LEFT JOIN telemetry_source ts
       ON ts.distribution_company_id = c.company_id
      AND ts.disabled_at IS NULL;


-- name: offer_commissioning
-- A new offer. `one_open_commissioning_per_device` is what refuses a second
-- open one; the caller runs this in the transaction that created the device,
-- so there is never a first one to collide with.
--
-- $1 device, $2 source, $3 requested_by, $4 offer window, $5/$6 history window
-- (both NULL for "upload nothing"). The deadline is computed here from now()
-- and stored, never recomputed (decision 3).
INSERT INTO device_commissioning (
    device_id, source_id, status, requested_by_account_id,
    offer_expires_at, backfill_from, backfill_to
)
VALUES ($1, $2, 'offered', $3, now() + $4::interval, $5, $6)
RETURNING commissioning_id, status::text AS status, offer_expires_at;


-- name: record_commissioning_without_source
-- The same offer, for a utility no enabled head-end serves: written already
-- failed, so the equipment page can say why this meter will never report.
-- offer_expires_at is still set -- the column is NOT NULL, and the value says
-- what the deadline would have been.
INSERT INTO device_commissioning (
    device_id, source_id, status, requested_by_account_id,
    offer_expires_at, backfill_from, backfill_to,
    ended_at, failed_reason
)
VALUES ($1, NULL, 'failed', $2, now() + $3::interval, $4, $5,
        now(), 'no_source')
RETURNING commissioning_id, status::text AS status, offer_expires_at;


-- name: cancel_device_commissioning
-- A retired device's open handshake ends here. Whatever state it reached --
-- offered, activated or live -- it is cancelled rather than failed: nothing
-- went wrong, the meter left.
UPDATE device_commissioning
SET status   = 'cancelled',
    ended_at = now()
WHERE device_id = $1
  AND status IN ('offered', 'activated', 'live')
RETURNING commissioning_id;


-- ---------------------------------------------------------------------------
-- Provisioning scripts: head-end credentials, and offering the meters that
-- were installed before commissioning existed.
-- ---------------------------------------------------------------------------


-- name: utilities_for_source_keys
-- Every active distribution company, and whether it already has a head-end.
SELECT dc.company_id, dc.code, dc.name, ts.source_id
FROM distribution_company dc
LEFT JOIN telemetry_source ts ON ts.distribution_company_id = dc.company_id
WHERE dc.status = 'active'
ORDER BY dc.code;


-- name: upsert_telemetry_source
-- Create a utility's head-end, or rotate its key in place.
--
-- In place, never delete-and-recreate: the source_id is what every
-- commissioning row points at, and RESTRICT would refuse the delete anyway.
-- Rotating does not disturb a single device -- device keys are separate.
-- `created` is the xmax trick: a row the INSERT branch wrote has xmax 0.
INSERT INTO telemetry_source (distribution_company_id, name, source_key_hash)
VALUES ($1, $2, $3)
ON CONFLICT (distribution_company_id) DO UPDATE
SET source_key_hash = EXCLUDED.source_key_hash,
    key_rotated_at  = now()
RETURNING source_id, (xmax = 0) AS created;


-- name: meters_awaiting_commissioning
-- Live billing meters with no open handshake: the seeded estate, and any meter
-- whose handshake failed or was cancelled.
--
-- last_day is the CONNECTION's reading horizon, as point_reading_horizon
-- computes it for a swap, so the offered window starts after whatever the
-- connection already holds and never re-covers it. Billing meters only --
-- rule 7's one meter per point is the thing a head-end delivers for, and
-- inverters are not commissioned in this phase.
SELECT d.device_id,
       d.serial_no,
       ms.billing_point_id              AS point_id,
       ma.meter_asset_id,
       (SELECT max(dr.interval_start AT TIME ZONE 'Asia/Dhaka')::date
        FROM device_reading dr
        JOIN meter_spec ms2 ON ms2.device_id = dr.device_id
        WHERE ms2.billing_point_id = ms.billing_point_id) AS last_day
FROM device d
JOIN meter_spec ms ON ms.device_id = d.device_id
LEFT JOIN meter_asset ma ON ma.device_id = d.device_id
WHERE d.removed_at IS NULL
  AND d.reports_telemetry
  AND ms.billing_role = 'billing'
  AND NOT EXISTS (
      SELECT 1
      FROM device_commissioning dc
      WHERE dc.device_id = d.device_id
        AND dc.status IN ('offered', 'activated', 'live')
  )
ORDER BY d.serial_no;


-- ---------------------------------------------------------------------------
-- The commissioning sweep, and the staff overview and retry.
-- ---------------------------------------------------------------------------


-- name: revoke_device_key
-- Replace a device's key hash with a value no key can match. Not a freshly
-- minted hash of a key nobody holds -- a literal that is visibly not argon2, so
-- anyone reading the row can see the credential was revoked rather than
-- rotated. verify_password() treats an unparseable hash as a mismatch.
UPDATE device
SET device_key_hash       = '$revoked$',
    device_key_rotated_at = now()
WHERE device_id = $1
RETURNING device_id;


-- name: expiring_commissionings
-- Open handshakes past their stored deadline, oldest deadline first. The
-- sweep re-checks each inside its own transaction; this list only says where
-- to look. Live rows never qualify: their activation deadline is in the past by
-- design once they are live.
SELECT dc.commissioning_id,
       dc.device_id,
       dc.status::text AS status,
       CASE dc.status WHEN 'offered' THEN dc.offer_expires_at
                      ELSE dc.activation_expires_at END AS deadline_at,
       d.serial_no,
       s.label    AS site_label,
       s.district,
       ts.name    AS head_end
FROM device_commissioning dc
JOIN device d ON d.device_id = dc.device_id
JOIN site s ON s.site_id = d.site_id
LEFT JOIN telemetry_source ts ON ts.source_id = dc.source_id
WHERE (dc.status = 'offered'   AND dc.offer_expires_at      <= now())
   OR (dc.status = 'activated' AND dc.activation_expires_at <= now())
ORDER BY deadline_at
LIMIT $1;


-- name: expire_commissioning
-- Fail one lapsed handshake, guarded on the status the sweep read AND on the
-- deadline still being past: a head-end whose batch landed a moment ago made
-- the row live, and a retry that re-activated it moved the deadline. Either
-- way this matches nothing and the sweep leaves it alone.
UPDATE device_commissioning
SET status        = 'failed',
    failed_reason = CASE $2 WHEN 'offered' THEN 'offer_expired'
                            ELSE 'activation_expired' END::commissioning_failure,
    ended_at      = now()
WHERE commissioning_id = $1
  AND status = $2::commissioning_status
  AND CASE $2 WHEN 'offered' THEN offer_expires_at
              ELSE activation_expires_at END <= now()
RETURNING commissioning_id, activation_count;


-- name: commissioning_overview
-- Every live billing meter, with the state of its latest handshake. $1 is a
-- district, or NULL for the whole fleet.
--
-- `state` resolves the stored deadlines here, so the page is right between
-- sweeps: an offer past its window reads `offer_lapsed` whether or not the
-- sweep has failed it yet. `not_commissioned` is a meter that was never offered
-- -- normal where commissioning is off, a gap where it is on.
WITH latest AS (
    SELECT d.device_id, d.serial_no, d.last_seen_at,
           s.site_id, s.label AS site_label, s.district,
           bp.label AS point_label,
           dc.commissioning_id, dc.status::text AS status,
           dc.failed_reason::text AS failed_reason, dc.failure_detail,
           dc.offered_at, dc.offer_expires_at, dc.activation_expires_at,
           dc.live_at, dc.ended_at,
           ts.name AS head_end
    FROM device d
    JOIN meter_spec ms ON ms.device_id = d.device_id
    JOIN site s ON s.site_id = d.site_id
    JOIN billing_point bp ON bp.point_id = ms.billing_point_id
    LEFT JOIN LATERAL (
        SELECT *
        FROM device_commissioning c
        WHERE c.device_id = d.device_id
        ORDER BY c.offered_at DESC, c.commissioning_id
        LIMIT 1
    ) dc ON true
    LEFT JOIN telemetry_source ts ON ts.source_id = dc.source_id
    WHERE d.removed_at IS NULL
      AND d.reports_telemetry
      AND ms.billing_role = 'billing'
      AND ($1::text IS NULL OR s.district = $1)
)
SELECT l.*,
       CASE
           WHEN l.commissioning_id IS NULL                          THEN 'not_commissioned'
           WHEN l.status = 'offered'   AND l.offer_expires_at      <= now() THEN 'offer_lapsed'
           WHEN l.status = 'activated' AND l.activation_expires_at <= now() THEN 'activation_lapsed'
           ELSE l.status::text
       END AS state
FROM latest l
ORDER BY l.site_label, l.point_label, l.serial_no;


-- name: meter_for_retry
-- What a retry needs to know about one device, or no row when it is not a live
-- billing meter -- a retired meter or an inverter reads as not found.
-- open_status is the handshake a retry must deal with first, if any.
SELECT d.device_id,
       d.serial_no,
       s.district,
       ms.billing_point_id AS point_id,
       ma.meter_asset_id,
       (SELECT c.status::text
        FROM device_commissioning c
        WHERE c.device_id = d.device_id
          AND c.status IN ('offered', 'activated', 'live')) AS open_status,
       (SELECT max(dr.interval_start AT TIME ZONE 'Asia/Dhaka')::date
        FROM device_reading dr
        JOIN meter_spec ms2 ON ms2.device_id = dr.device_id
        WHERE ms2.billing_point_id = ms.billing_point_id) AS last_day
FROM device d
JOIN meter_spec ms ON ms.device_id = d.device_id
JOIN site s ON s.site_id = d.site_id
LEFT JOIN meter_asset ma ON ma.device_id = d.device_id
WHERE d.device_id = $1
  AND d.removed_at IS NULL
  AND d.reports_telemetry
  AND ms.billing_role = 'billing';
