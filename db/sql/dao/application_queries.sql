-- Solar installation applications: the household asks an installer to fit
-- panels, and the installer works the queue.
--
-- Consumer requirement 7's install half and supplier requirement 1. The
-- *net-metering* half of requirement 7 is a different thing entirely and
-- already works: that is the regulator approving an export agreement
-- (`net_metering_agreement`), and it happens after the panels exist. This is
-- the step before -- choosing who fits them.
--
-- Keyed on a BILLING POINT, not a site (rule 3). A household with two
-- connections can fit panels on one and not the other, and
-- `solar_application_one_open` is a partial unique index on the point, so each
-- connection may hold exactly one live application while its neighbour holds
-- another.


-- name: create_solar_application
-- One application. `submitted` is the table's default; the composite FK
-- `application_point_fk` against `billing_point (point_id, site_id)` is what
-- stops a caller pairing someone else's connection with their own site, so the
-- handler does not have to re-check that pairing itself.
--
-- The UNIQUE partial index raises on a second open application for the same
-- connection, which the handler turns into a 409. Deliberately not an upsert: a
-- household that wants different terms withdraws and applies again, so the
-- installer sees that they changed their mind rather than finding the request
-- silently rewritten under them.
INSERT INTO solar_application (
    site_id, billing_point_id, account_id, supplier_id,
    requested_capacity_kw, panel_count, notes
)
VALUES ($1, $2, $3, $4, $5, $6, $7)
RETURNING application_id;


-- name: solar_applications_for_account
-- A household's own applications, newest first, whatever their state.
--
-- A withdrawn or rejected one stays visible: it is the record of having asked,
-- and hiding it would make the page look like nothing ever happened.
SELECT sa.application_id,
       sa.site_id,
       s.label            AS site_label,
       s.district,
       sa.billing_point_id,
       bp.label           AS point_label,
       sa.supplier_id,
       sc.name            AS supplier_name,
       sc.contact_email::text AS supplier_email,
       sc.contact_phone   AS supplier_phone,
       sa.status::text    AS status,
       sa.requested_capacity_kw,
       sa.panel_count,
       sa.notes,
       sa.submitted_at,
       sa.decided_at,
       sa.decision_notes,
       sa.installed_array_id
FROM solar_application sa
JOIN site s            ON s.site_id = sa.site_id
JOIN billing_point bp  ON bp.point_id = sa.billing_point_id
JOIN supplier_company sc ON sc.supplier_id = sa.supplier_id
WHERE sa.account_id = $1
ORDER BY sa.submitted_at DESC;


-- name: solar_applications_for_supplier
-- The installer's inbox: applications addressed to their firm.
--
-- Open ones first and oldest-first within that, because a queue sorted purely
-- by recency buries whoever nobody answered -- the same ordering rule as the
-- worker triage queue and the official's approval queue. Decided ones follow,
-- newest first, since those are history rather than work.
--
-- $2 optionally hides the decided ones.
SELECT sa.application_id,
       sa.site_id,
       s.label            AS site_label,
       s.district,
       s.address_line,
       sa.billing_point_id,
       bp.label           AS point_label,
       sa.account_id,
       a.full_name        AS account_name,
       a.phone            AS account_phone,
       sa.supplier_id,
       sc.name            AS supplier_name,
       sa.status::text    AS status,
       sa.requested_capacity_kw,
       sa.panel_count,
       sa.notes,
       sa.submitted_at,
       sa.decided_at,
       sa.decision_notes,
       sa.installed_array_id,
       -- Whether this connection already has panels. An application against a
       -- point that is already generating is not necessarily wrong (an uprate
       -- is real work), but the installer should see it before quoting.
       EXISTS (
           SELECT 1 FROM solar_array sar
           WHERE sar.site_id = sa.site_id
             AND sar.status <> 'decommissioned'
       ) AS site_has_solar
FROM solar_application sa
JOIN site s              ON s.site_id = sa.site_id
JOIN billing_point bp    ON bp.point_id = sa.billing_point_id
JOIN account a           ON a.account_id = sa.account_id
JOIN supplier_company sc ON sc.supplier_id = sa.supplier_id
WHERE sa.supplier_id = $1
  AND ($2::boolean IS NOT TRUE
       OR sa.status IN ('submitted', 'under_review'))
-- Open applications first, then newest first inside each band. The band is
-- what stops a decided application outranking one still waiting; the DESC is
-- the global ordering rule. Previously the open band was oldest-first.
ORDER BY (sa.status IN ('submitted', 'under_review')) DESC,
         sa.submitted_at DESC;


-- name: solar_application_context
-- One application, with the two identities that may act on it.
--
-- Returned to the handler so it can decide 404 vs 403 vs 409 without a second
-- query: the household that owns it, and the firm it was addressed to.
SELECT sa.application_id,
       sa.account_id,
       sa.supplier_id,
       sa.status::text AS status,
       sa.site_id,
       sa.billing_point_id
FROM solar_application sa
WHERE sa.application_id = $1;


-- name: decide_solar_application
-- Move an application along.
--
-- `application_decision_timestamps` is
-- `(status IN ('submitted','under_review')) = (decided_at IS NULL)`, so
-- decided_at has to be set by exactly the transitions that leave the open
-- states and cleared by none -- hence the CASE rather than a bare now(). Moving
-- to 'under_review' keeps it NULL; anything else stamps it.
--
-- Guarded on the CURRENT status the caller read ($3), so two people working the
-- same queue produce one decision and the second gets a 409 instead of quietly
-- replacing the first -- and whose name is on `decided_by_account_id` stays
-- true.
UPDATE solar_application
SET status        = $2::application_status,
    decided_at    = CASE
                        WHEN $2::application_status
                             IN ('submitted', 'under_review')
                        THEN NULL ELSE now()
                    END,
    decided_by_account_id = CASE
                        WHEN $2::application_status
                             IN ('submitted', 'under_review')
                        THEN NULL ELSE $4::uuid
                    END,
    decision_notes = COALESCE($5, decision_notes)
WHERE application_id = $1
  AND status = $3::application_status
RETURNING application_id;


-- name: supplier_serves_district
-- Does this installer actually work in this district?
--
-- Consumer requirement 7 asks for a supplier "in the consumer's nearby region".
-- The dropdown is filtered by district already, but a filtered dropdown is a
-- convenience and this is the check -- a request that names a firm which does
-- not serve the site is refused rather than quietly accepted because the UI
-- would not normally have offered it.
SELECT EXISTS (
    SELECT 1 FROM supplier_service_area a
    WHERE a.supplier_id = $1 AND a.district = $2
) AS serves;
