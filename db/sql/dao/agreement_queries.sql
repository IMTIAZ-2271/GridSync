-- Net-metering agreement DAO. See db/sql/dao/site_queries.sql for the loader
-- convention.


-- name: list_pending_agreements
-- The regulator's approval queue.
--
-- $1 NULL means every district, which is what a supplier (a fleet-wide
-- reader) and an admin get. An OFFICIAL is confined to the district their
-- single-use code was issued for, exactly as `meter_applications_queue` and
-- `pending_workers` already are.
--
-- Without that scope an official saw every pending application in the
-- country and could act on none of them but their own -- every button
-- answering 404, which reads as a broken page rather than as a boundary.
-- Found by walking the flow end to end: gov1 was shown a Dhanmondi
-- application and was refused the moment they ordered its inspection.
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
  AND ($1::text IS NULL OR s.district = $1)
-- Newest first. Every queue in this system leads with the most recent
-- arrival; a reviewer who wants the oldest sorts the column.
ORDER BY nma.created_at DESC;


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


-- ---------------------------------------------------------------------------
-- The consumer's half: applying, and watching for an answer.
--
-- Until 2026-08-27 a household applied for net metering *implicitly* --
-- POST /api/sites/{id}/solar opened a `pending` agreement as a side effect of
-- registering panels, so the application was something that happened to them
-- rather than something they did. These statements are what make it an act:
-- they submit, they can see where it got to, and they can take it back.
-- ---------------------------------------------------------------------------

-- name: point_for_application
-- The connection an application names, and who owns it.
--
-- account_id comes back so the handler can 404 a connection belonging to
-- somebody else rather than 403 it -- the caller cannot act on the difference,
-- and telling them apart makes this a probe for other people's connections.
SELECT pt.point_id,
       pt.site_id,
       pt.label AS point_label,
       s.label  AS site_label,
       s.account_id
FROM billing_point pt
JOIN site s ON s.site_id = pt.site_id
WHERE pt.point_id = $1
  AND pt.retired_at IS NULL;


-- name: net_metering_applications_for_account
-- Every net-metering agreement across this household's sites, whatever state
-- it is in.
--
-- Not filtered to 'pending': the page has to be able to say approved, refused
-- and terminated too, and an application that vanishes the moment it is
-- decided is the thing that sends people to a call centre. Newest first,
-- because the one they just filed is the one they are looking for.
--
-- `array_count` is what tells the page whether applying is even sensible on a
-- connection: the regulator is agreeing to credit exported energy, and a
-- connection with no panels has none to export.
SELECT nma.agreement_id,
       nma.site_id,
       s.label  AS site_label,
       nma.billing_point_id,
       pt.label AS point_label,
       nma.approval_ref,
       nma.sanctioned_capacity_kw,
       nma.export_cap_pct,
       nma.settlement_type,
       nma.credit_rollover_months,
       nma.effective_from,
       nma.effective_to,
       nma.status,
       nma.created_at,
       (SELECT count(*)
        FROM solar_array sa
        JOIN device inv ON inv.device_id = sa.inverter_device_id
        JOIN inverter_spec ivs ON ivs.device_id = sa.inverter_device_id
        WHERE ivs.billing_point_id = nma.billing_point_id
          AND sa.status <> 'decommissioned'
          AND inv.removed_at IS NULL)::int AS array_count,
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
FROM net_metering_agreement nma
JOIN site s ON s.site_id = nma.site_id
JOIN billing_point pt ON pt.point_id = nma.billing_point_id
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
    WHERE wo.agreement_id = nma.agreement_id
    ORDER BY wo.created_at DESC
    LIMIT 1
) visit ON TRUE
WHERE s.account_id = $1
ORDER BY nma.created_at DESC;


-- name: withdraw_net_metering_application
-- A household taking back a request the regulator has not answered yet.
--
-- Guarded on 'pending' so an application cannot be withdrawn out from under a
-- decision that has already been made -- an active agreement is ended by
-- terminating it, which is the regulator's act, not a form the consumer can
-- submit. 'terminated' is the status nma_no_overlap excludes, so withdrawing
-- also frees the connection to apply again.
--
-- effective_to is left NULL, exactly as decide_agreement leaves it when a
-- reviewer terminates: `nma_dates` requires effective_to > effective_from, so
-- closing a same-day application would violate it, and the status is what the
-- exclusion constraint reads anyway.
UPDATE net_metering_agreement
SET status = 'terminated'
WHERE agreement_id = $1
  AND status = 'pending'
RETURNING agreement_id;


-- name: raise_agreement_work_order
-- The regulator's inspection-and-swap visit for a net-metering application.
--
-- `meter_swap` rather than `meter_install`: the connection already has a
-- billing meter, and rule 7 allows exactly one per point. What the visit does
-- is replace it with a bidirectional one and inspect the array while it is
-- there. The point, its periods, its bills and its ledger all stay put -- which
-- is what rule 3 exists to make possible.
--
-- Site is copied from the agreement in SQL, as everywhere else: an order filed
-- against agreement X at address Y would break the audit trail.
INSERT INTO work_order (
    agreement_id, site_id, created_by_account_id,
    order_type, status, priority, scheduled_for
)
SELECT nma.agreement_id,
       nma.site_id,
       $2,
       'meter_swap',
       'draft',
       3,
       $3
FROM net_metering_agreement nma
WHERE nma.agreement_id = $1
RETURNING order_id, site_id;


-- name: agreement_order
-- The live-or-latest inspection for one agreement. Same shape and same reason
-- as meter_application_order: a failed visit can be followed by another, and
-- the card should show the current attempt.
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
WHERE wo.agreement_id = $1
ORDER BY wo.created_at DESC
LIMIT 1;


-- name: register_agreement_meter
-- Issue the bidirectional meter the inspection fitted, on the same evidence
-- the meter flow requires: a completed visit the household has confirmed, and
-- a serial the technician recorded at the property.
WITH visit AS (
    SELECT wo.order_id, wo.installed_serial_no
    FROM work_order wo
    WHERE wo.agreement_id = $1
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


-- name: activate_agreement_after_visit
-- Approve the agreement once the meter that makes it measurable is registered.
--
-- Separate from decide_agreement, and guarded on 'pending' for the same
-- reason: two officials racing produce one decision and a 409. What differs is
-- that this one is not a judgement about the paperwork -- it is the last step
-- of a visit that has already happened.
UPDATE net_metering_agreement
SET status = 'active'
WHERE agreement_id = $1
  AND status = 'pending'
RETURNING agreement_id, site_id;


-- name: agreement_owner
-- Who to notify, and which district's officials own the decision.
SELECT nma.agreement_id,
       nma.site_id,
       nma.billing_point_id,
       nma.status,
       s.account_id,
       s.district,
       s.label AS site_label,
       pt.label AS point_label
FROM net_metering_agreement nma
JOIN site s ON s.site_id = nma.site_id
JOIN billing_point pt ON pt.point_id = nma.billing_point_id
WHERE nma.agreement_id = $1;
