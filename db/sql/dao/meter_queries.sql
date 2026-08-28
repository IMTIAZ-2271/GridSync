-- Meter assets and meter applications.
--
-- A meter belongs to a person before it belongs to a site: migration
-- c9e2f4a71b83 made `meter_asset` the record of hardware issued to an account,
-- and `device` stayed what it always was -- hardware installed somewhere. The
-- household's part is saying which of *their* meters serves which connection,
-- which is why nothing here takes a serial number typed at a form.
--
-- See db/sql/dao/site_queries.sql for the loader convention.


-- name: meter_assets_for_account
-- Every meter issued to this consumer, available ones first.
--
-- `available` is derived from device_id rather than stored: assigning a meter
-- writes exactly one column, so there is no second flag to fall out of step
-- with it. An assigned meter carries where it went -- a household with three
-- connections needs to be told which one it is looking at, and "assigned" on
-- its own answers nothing.
--
-- A meter whose device has been removed is still unavailable: the device row
-- survives `removed_at`, so the asset stays pointed at the position it served.
-- Re-issuing retired hardware is the utility's decision, not a side effect of
-- someone unbolting it.
SELECT ma.meter_asset_id,
       ma.serial_no,
       ma.manufacturer,
       ma.model,
       ma.issued_at,
       dc.name AS issued_by,
       ma.device_id,
       (ma.device_id IS NULL) AS available,
       d.site_id,
       s.label AS site_label,
       ms.billing_point_id AS point_id,
       pt.label AS point_label,
       d.removed_at
FROM meter_asset ma
LEFT JOIN distribution_company dc ON dc.company_id = ma.issued_by_company_id
LEFT JOIN device d ON d.device_id = ma.device_id
LEFT JOIN site s ON s.site_id = d.site_id
LEFT JOIN meter_spec ms ON ms.device_id = d.device_id
LEFT JOIN billing_point pt ON pt.point_id = ms.billing_point_id
WHERE ma.account_id = $1
ORDER BY (ma.device_id IS NULL) DESC, ma.issued_at DESC;


-- name: claim_meter_asset
-- Take an available meter out of the consumer's stock, atomically.
--
-- The `device_id IS NULL` guard is the whole safety property: two tabs
-- assigning the same meter to two connections produce one winner and one row
-- count of zero, which the handler answers 409. Ownership is in the same
-- WHERE clause rather than checked first, so there is no window between the
-- check and the write -- and a meter belonging to somebody else is
-- indistinguishable from one already in use, which is the right answer to
-- give a caller guessing at ids.
UPDATE meter_asset
SET device_id = $3
WHERE meter_asset_id = $1
  AND account_id = $2
  AND device_id IS NULL
RETURNING meter_asset_id, serial_no, manufacturer, model;


-- name: meter_asset_for_assignment
-- The make and model to stamp on the device row, read before the claim so the
-- INSERT has something to write. Not a substitute for the guard above: this
-- can go stale between the two statements, and claim_meter_asset is what
-- decides.
SELECT meter_asset_id, serial_no, manufacturer, model
FROM meter_asset
WHERE meter_asset_id = $1
  AND account_id = $2;


-- name: issue_meter_asset
-- Hand a meter to a consumer. Called when an official accepts a meter
-- application; there is no other writer, because a household issuing itself
-- hardware is exactly what this table exists to stop.
INSERT INTO meter_asset (
    account_id, serial_no, manufacturer, model, issued_by_company_id
)
VALUES ($1, $2, $3, $4, $5)
RETURNING meter_asset_id, serial_no;


-- name: utility_for_site
-- Which company to record as having issued a meter for this site.
--
-- The site's own connections are asked first: whoever bills the existing
-- meter is who would supply the next one. Only when there is none does this
-- fall back to the district's serving companies, and it takes the
-- lowest-coded one deterministically rather than picking arbitrarily --
-- Badda is served by two, and a query that answered differently on each call
-- would put the same household's meters under different utilities.
--
-- NULL is a legitimate answer (a legacy district nobody serves), and the
-- column is nullable for exactly that case.
SELECT COALESCE(
    (SELECT bp.distribution_company_id
     FROM billing_point bp
     WHERE bp.site_id = $1
       AND bp.distribution_company_id IS NOT NULL
     ORDER BY bp.created_at
     LIMIT 1),
    (SELECT dc.company_id
     FROM site s
     JOIN distribution_company_area dca ON dca.district = s.district
     JOIN distribution_company dc ON dc.company_id = dca.company_id
     WHERE s.site_id = $1
       AND dc.status = 'active'
     ORDER BY dc.code
     LIMIT 1)
) AS company_id;


-- ---------------------------------------------------------------------------
-- Meter applications: what a household files when it has no meter to assign.
-- ---------------------------------------------------------------------------

-- name: create_meter_application
-- meter_application_one_open (partial unique on site_id) is what refuses a
-- second live request, so the handler catches UniqueViolation as a 409 rather
-- than reading the queue first and racing itself.
INSERT INTO meter_application (account_id, site_id, reason)
VALUES ($1, $2, $3)
RETURNING application_id;


-- name: meter_applications_for_account
-- The household's own requests, newest first, including decided ones -- the
-- page has to be able to say "you asked, and this is what came back".
SELECT ma.application_id,
       ma.site_id,
       s.label AS site_label,
       s.district,
       ma.status,
       ma.reason,
       ma.submitted_at,
       ma.decided_at,
       ma.decision_notes,
       ma.issued_meter_asset_id,
       asset.serial_no AS issued_serial_no,
       (asset.device_id IS NULL) AS issued_meter_available,
       visit.order_id           AS visit_order_id,
       visit.order_type         AS visit_order_type,
       visit.status             AS visit_status,
       visit.scheduled_for      AS visit_scheduled_for,
       visit.started_at         AS visit_started_at,
       visit.completed_at       AS visit_completed_at,
       visit.completion_notes   AS visit_completion_notes,
       visit.failure_reason     AS visit_failure_reason,
       visit.installed_serial_no AS visit_installed_serial_no,
       visit.consumer_confirmed_at AS visit_confirmed_at,
       visit.consumer_disputed_at  AS visit_disputed_at,
       visit.consumer_note      AS visit_note
FROM meter_application ma
JOIN site s ON s.site_id = ma.site_id
LEFT JOIN meter_asset asset ON asset.meter_asset_id = ma.issued_meter_asset_id
LEFT JOIN LATERAL (
    -- The current attempt. Newest first, LIMIT 1: a failed visit can be
    -- followed by another, and what the household's card should show is the
    -- one in flight -- earlier ones stay on work_order for anyone who needs
    -- the history.
    SELECT wo.order_id, wo.order_type, wo.status, wo.scheduled_for,
           wo.started_at, wo.completed_at, wo.completion_notes,
           wo.failure_reason, wo.installed_serial_no,
           wo.consumer_confirmed_at, wo.consumer_disputed_at, wo.consumer_note
    FROM work_order wo
    WHERE wo.meter_application_id = ma.application_id
    ORDER BY wo.created_at DESC
    LIMIT 1
) visit ON TRUE
WHERE ma.account_id = $1
ORDER BY ma.submitted_at DESC;


-- name: meter_applications_queue
-- The official's queue, scoped in SQL rather than by a filter in the handler.
--
-- $1 is the district from government_profile, and a NULL makes the predicate
-- a no-op so an admin (who has no profile) sees everything -- the same shape
-- as the worker approval queue. $2 toggles decided rows in, for an official
-- checking what they issued last week.
--
-- Oldest first: a queue sorted by recency buries whoever nobody picked up.
-- `existing_meters` is the number the site already has, which is the one fact
-- that turns "they want a meter" into a decision -- a first connection and a
-- fourth are not the same request.
SELECT ma.application_id,
       ma.account_id,
       a.full_name AS account_name,
       a.national_id,
       a.phone,
       ma.site_id,
       s.label AS site_label,
       s.address_line,
       s.district,
       ma.status,
       ma.reason,
       ma.submitted_at,
       ma.decided_at,
       ma.decision_notes,
       ma.issued_meter_asset_id,
       asset.serial_no AS issued_serial_no,
       (SELECT count(*)
        FROM meter_spec ms2
        JOIN device d2 ON d2.device_id = ms2.device_id
        WHERE ms2.site_id = ma.site_id
          AND d2.removed_at IS NULL)::int AS existing_meters
FROM meter_application ma
JOIN site s ON s.site_id = ma.site_id
JOIN account a ON a.account_id = ma.account_id
LEFT JOIN meter_asset asset ON asset.meter_asset_id = ma.issued_meter_asset_id
WHERE ($1::text IS NULL OR s.district = $1)
  AND ($2::boolean OR ma.status IN ('submitted', 'under_review'))
-- Undecided first, then newest first within each band.
ORDER BY (ma.status IN ('submitted', 'under_review')) DESC,
         ma.submitted_at DESC;


-- name: meter_application_context
-- Who owns it, where it is, and what state it is in -- everything the two
-- authorization checks need in one round trip. A district that does not match
-- the official's is answered 404, not 403: confirming the row exists would
-- tell a stranger who has applied for a meter next door.
SELECT ma.application_id,
       ma.account_id,
       ma.site_id,
       s.district,
       ma.status
FROM meter_application ma
JOIN site s ON s.site_id = ma.site_id
WHERE ma.application_id = $1;


-- name: decide_meter_application
-- Guarded on the status that was read, so two officials working one district
-- produce one decision and a 409 rather than a silent overwrite of whose name
-- is on it.
--
-- decided_at is a CASE rather than a bare now(), because
-- meter_application_decision_timestamps ties it to the status: moving to
-- 'under_review' must leave it NULL and every terminal move must stamp it.
-- Re-entering 'under_review' after a decision is impossible for the same
-- reason -- and would be a lie anyway.
--
-- The ::uuid casts are load-bearing. A bare $4 sitting beside an untyped NULL
-- in the same CASE is inferred as text, and PostgreSQL refuses the assignment
-- at prepare time rather than at runtime -- so the failure is a 500 on the
-- first call, not a silent wrong value.
UPDATE meter_application
SET status = $2::application_status,
    decision_notes = COALESCE($3, decision_notes),
    decided_at = CASE
        WHEN $2::application_status IN ('submitted', 'under_review') THEN NULL
        ELSE now()
    END,
    decided_by_account_id = CASE
        WHEN $2::application_status IN ('submitted', 'under_review') THEN NULL
        ELSE $4::uuid
    END,
    issued_meter_asset_id = COALESCE($5::uuid, issued_meter_asset_id)
WHERE application_id = $1
  AND status IN ('submitted', 'under_review')
RETURNING application_id, account_id, site_id, status;


-- name: transfer_meter_assets
-- Move a site's meters to whoever now owns the site.
--
-- Claiming a connection by its serial transfers `site.account_id`, and before
-- meter_asset existed that was the whole story. It is not any more: a meter is
-- issued to a *person*, so a claimed site whose meters still belonged to the
-- previous holder would leave the new owner looking at connections they own
-- and a Meters page that says they own no meters -- and, worse, would leave
-- the hardware listed under an account that no longer has anything to do with
-- it.
--
-- Scoped to the assets whose device sits on this site, so a claim moves the
-- meters that came with it and nothing else the old holder had spare.
UPDATE meter_asset ma
SET account_id = $2
FROM device d
WHERE d.device_id = ma.device_id
  AND d.site_id = $1
RETURNING ma.meter_asset_id;


-- ---------------------------------------------------------------------------
-- The visit that fulfils an application.
--
-- Approving no longer issues a meter. An official raises a work order, a
-- worker fits the meter and records its serial, the household confirms it
-- happened, and only then is the meter registered. See migration b7d3f5a92c14.
-- ---------------------------------------------------------------------------

-- name: officials_for_district
-- Every government account that governs this district, plus every admin.
--
-- Admins are included because they are unscoped by design everywhere else in
-- this file, and a district nobody has been issued a code for would otherwise
-- have applications land where no one can see them. Returns account ids only;
-- notify() is what turns them into rows.
SELECT gp.account_id
FROM government_profile gp
JOIN account a ON a.account_id = gp.account_id
WHERE gp.district = $1
  AND a.status = 'active'
UNION
SELECT a.account_id
FROM account a
WHERE a.role = 'admin'
  AND a.status = 'active';


-- name: raise_application_work_order
-- The official's order. Site and priority are read from the application in
-- SQL, never taken from the caller, for the same reason create_work_order
-- copies them from an issue: an order filed against application X at address Y
-- would break the only audit trail saying why the visit happened.
--
-- `one_order_per_meter_application` is what refuses a second live order, so
-- the handler catches UniqueViolation rather than reading first and racing.
INSERT INTO work_order (
    meter_application_id, site_id, created_by_account_id,
    order_type, status, priority, scheduled_for
)
SELECT ma.application_id,
       ma.site_id,
       $2,
       $3::work_order_type,
       'draft',
       3,
       $4
FROM meter_application ma
WHERE ma.application_id = $1
RETURNING order_id, site_id;


-- name: meter_application_order
-- The live-or-latest visit for one application, and everything the household's
-- card needs to say what stage it is at.
--
-- Newest first, LIMIT 1: a failed visit can be followed by another, and what
-- the page should show is the current attempt. The earlier ones are still on
-- the order table for anyone who needs the history.
SELECT wo.order_id,
       wo.order_type,
       wo.status,
       wo.scheduled_for,
       wo.started_at,
       wo.completed_at,
       wo.completion_notes,
       wo.failure_reason,
       wo.installed_serial_no,
       wo.consumer_confirmed_at,
       wo.consumer_disputed_at,
       wo.consumer_note
FROM work_order wo
WHERE wo.meter_application_id = $1
ORDER BY wo.created_at DESC
LIMIT 1;


-- name: register_applied_meter
-- Issue the meter the visit actually fitted.
--
-- The serial comes from the work order, not from the caller: the technician
-- recorded it at the property, and an official re-typing a number for hardware
-- they never saw is exactly the gap this whole flow exists to close.
--
-- Guarded on the household having confirmed. The confirmation is the evidence
-- the meter is on the wall; registering without it would be back where
-- c9e2f4a71b83 started, issuing hardware on somebody's say-so.
WITH visit AS (
    SELECT wo.order_id, wo.installed_serial_no
    FROM work_order wo
    WHERE wo.meter_application_id = $1
      AND wo.status = 'completed'
      AND wo.consumer_confirmed_at IS NOT NULL
      AND wo.installed_serial_no IS NOT NULL
    ORDER BY wo.completed_at DESC
    LIMIT 1
)
INSERT INTO meter_asset (
    account_id, serial_no, manufacturer, model, issued_by_company_id
)
SELECT $2, visit.installed_serial_no, $3, $4, $5
FROM visit
RETURNING meter_asset_id, serial_no;


-- name: retire_point_billing_meter
-- Take the live billing meter off a connection, so its replacement can take
-- over the same point.
--
-- Rule 7 allows exactly one ACTIVE billing meter per billing point, and it is
-- enforced by DEFERRED constraint triggers -- which is what makes a swap
-- expressible at all: between this UPDATE and the INSERT that follows it the
-- point momentarily has none, or briefly two, and only COMMIT is checked.
--
-- The device row survives, and so do its readings. `device_health` filters
-- `removed_at IS NULL` so the retired meter stops being reported as a fault,
-- while `site_readings` does not filter it, so the connection's history stays
-- unbroken across the swap. That asymmetry is deliberate: a decommissioned
-- meter is history, not a fault, but the energy it measured is still the
-- household's.
UPDATE device d
SET removed_at = now(),
    status = 'removed'
FROM meter_spec ms
WHERE ms.device_id = d.device_id
  AND ms.billing_point_id = $1
  AND ms.billing_role = 'billing'
  AND d.removed_at IS NULL
RETURNING d.device_id, d.serial_no;


-- name: point_reading_horizon
-- The last day this connection has any reading for, across every device that
-- has ever served it.
--
-- A swapped-in meter must NOT be backfilled over ground the retired one
-- already covers: `site_readings` sums across every device on the site, so
-- overlapping history would double the household's recorded import. Asking
-- the point rather than the device is the whole point -- the history belongs
-- to the connection (rule 3), not to whichever box was bolted to the wall.
SELECT max(dr.interval_start AT TIME ZONE 'Asia/Dhaka')::date AS last_day
FROM device_reading dr
JOIN meter_spec ms ON ms.device_id = dr.device_id
WHERE ms.billing_point_id = $1;
