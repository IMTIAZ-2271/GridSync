-- Issue DAO. See db/sql/dao/site_queries.sql for the loader convention and the
-- shared aggregate invariants that govern this whole directory.


-- name: list_issues
SELECT i.issue_id,
       i.site_id,
       s.label AS site_label,
       i.device_id,
       i.bill_id,
       i.category,
       i.severity,
       i.status,
       i.title,
       i.description,
       i.priority,
       i.reported_at,
       i.acknowledged_at,
       i.resolved_at,
       i.reported_by_account_id,
       a.full_name AS reported_by_name,
       -- Consumer requirement 6: who the complaint is against. Both nullable
       -- and both optional -- a data gap is nobody's fault until someone looks,
       -- and forcing a name onto every report would just produce wrong ones.
       i.distribution_company_id,
       dc.name AS distribution_company_name,
       i.supplier_id,
       sc.name AS supplier_name
FROM issue i
JOIN site s ON s.site_id = i.site_id
JOIN account a ON a.account_id = i.reported_by_account_id
LEFT JOIN distribution_company dc ON dc.company_id = i.distribution_company_id
LEFT JOIN supplier_company sc ON sc.supplier_id = i.supplier_id
ORDER BY i.reported_at DESC;


-- name: get_issue
-- Same projection as list_issues, for one row. Used to render what POST just
-- created without duplicating the joins in the INSERT's RETURNING clause.
SELECT i.issue_id,
       i.site_id,
       s.label AS site_label,
       i.device_id,
       i.bill_id,
       i.category,
       i.severity,
       i.status,
       i.title,
       i.description,
       i.priority,
       i.reported_at,
       i.acknowledged_at,
       i.resolved_at,
       i.reported_by_account_id,
       a.full_name AS reported_by_name,
       -- Consumer requirement 6: who the complaint is against. Both nullable
       -- and both optional -- a data gap is nobody's fault until someone looks,
       -- and forcing a name onto every report would just produce wrong ones.
       i.distribution_company_id,
       dc.name AS distribution_company_name,
       i.supplier_id,
       sc.name AS supplier_name
FROM issue i
JOIN site s ON s.site_id = i.site_id
JOIN account a ON a.account_id = i.reported_by_account_id
LEFT JOIN distribution_company dc ON dc.company_id = i.distribution_company_id
LEFT JOIN supplier_company sc ON sc.supplier_id = i.supplier_id
WHERE i.issue_id = $1;


-- name: create_issue
-- reported_by_account_id falls back to the site's owner when the client does
-- not supply one. There is no auth to infer a reporter from, and the column is
-- NOT NULL -- attributing an unattributed report to the account that owns the
-- service point is the least wrong guess available. It becomes a session
-- lookup the moment auth exists.
INSERT INTO issue (reported_by_account_id, site_id, device_id, bill_id,
                   category, severity, title, description, priority,
                   distribution_company_id, supplier_id)
VALUES (COALESCE($1, (SELECT account_id FROM site WHERE site_id = $2)),
        $2, $3, $4, $5::issue_category, $6::issue_severity, $7, $8, $9::smallint,
        $10, $11)
RETURNING issue_id;


-- name: update_issue_status
-- Advance an issue's own lifecycle.
--
-- The lifecycle timestamps are maintained HERE, not by the caller, for the same
-- reason update_work_order_status maintains its two: a triager clicking a
-- status button must not be able to leave an issue 'resolved' with a NULL
-- resolved_at. Each is set once on the first arrival at that state and never
-- rewritten -- walking an issue back to 'in_progress' does not erase the fact
-- that it was once acknowledged, and the audit value of the row is in when
-- things first happened, not in where it currently sits.
--
-- acknowledged_at is stamped by ANY move out of 'open', not only by choosing
-- 'acknowledged'. Somebody who reads an issue and goes straight to
-- 'in_progress' has plainly acknowledged it, and a NULL there would make an SLA
-- query treat the most responsive case as the least.
--
-- 'duplicate' is deliberately unreachable through this statement. The CHECK
-- `issue_duplicate_status` ties it to duplicate_of_issue_id being non-NULL, so
-- offering the status without the id would be a constraint violation dressed up
-- as a 500. Marking a duplicate needs the pair, and therefore its own endpoint.
--
-- resolution_notes is only written when one is supplied, so re-entering a state
-- does not blank the note somebody already wrote.
UPDATE issue
SET status = $2::issue_status,
    acknowledged_at = CASE
        WHEN acknowledged_at IS NULL AND $2::issue_status <> 'open'
        THEN now() ELSE acknowledged_at
    END,
    resolved_at = CASE
        WHEN resolved_at IS NULL
         AND $2::issue_status IN ('resolved', 'closed')
        THEN now() ELSE resolved_at
    END,
    closed_at = CASE
        WHEN closed_at IS NULL AND $2::issue_status = 'closed'
        THEN now() ELSE closed_at
    END,
    resolution_notes = COALESCE($3, resolution_notes)
WHERE issue_id = $1
  -- A duplicate has been merged into another issue; its status is owned by that
  -- relationship, not by a triage button. Excluded here rather than left to the
  -- CHECK, which would only catch the move OUT of duplicate, not into it.
  AND status <> 'duplicate'
RETURNING issue_id;


-- name: issue_targets_for_site
-- Who a complaint from this site would most likely be against.
--
-- Consumer requirement 6 wants the household to pick the distribution company
-- for a meter fault and the installer for a solar one. Both are already known
-- to the system -- the utility is on the site's billing points, the installer
-- on its arrays -- so the form arrives pre-answered rather than asking a
-- household to remember who fitted their panels three years ago.
--
-- Returned as candidates, not as a decision: a site with two connections may
-- have two utilities, and the household should confirm which one it means.
-- UNION over the four sources would return a company twice when it is both
-- attached to this site AND serves the district, because the rows differ in
-- `attached` and UNION dedupes on the whole row. Collapsed with bool_or so each
-- company appears once, attached if ANY source says so.
WITH candidate AS (
    SELECT 'distribution' AS kind, dc.company_id AS id, dc.name, TRUE AS attached
    FROM billing_point bp
    JOIN distribution_company dc ON dc.company_id = bp.distribution_company_id
    WHERE bp.site_id = $1
    UNION ALL
    SELECT 'distribution', dc.company_id, dc.name, FALSE
    FROM site s
    JOIN distribution_company_area a ON a.district = s.district
    JOIN distribution_company dc ON dc.company_id = a.company_id
    WHERE s.site_id = $1
    UNION ALL
    SELECT 'supplier', sc.supplier_id, sc.name, TRUE
    FROM solar_array sa
    JOIN supplier_company sc ON sc.supplier_id = sa.installed_by_supplier_id
    WHERE sa.site_id = $1 AND sa.status <> 'decommissioned'
    UNION ALL
    SELECT 'supplier', sc.supplier_id, sc.name, FALSE
    FROM site s
    JOIN supplier_service_area a ON a.district = s.district
    JOIN supplier_company sc ON sc.supplier_id = a.supplier_id
    WHERE s.site_id = $1 AND sc.status = 'active'
)
SELECT kind, id, name, bool_or(attached) AS attached
FROM candidate
GROUP BY kind, id, name
-- Attached first within each kind, so the form can preselect the top one.
ORDER BY kind, bool_or(attached) DESC, name;


-- name: issues_for_supplier
-- Supplier requirement 2: the installer's complaint inbox.
--
-- Two kinds of row, and the distinction matters more than it looks. An issue
-- **named against this firm** (consumer requirement 6's dropdown) is a
-- complaint about their own work. An issue on a site they have an array on, or
-- a work order against, is one they are merely involved in. `against_us` says
-- which, so the page can lead with the ones that are actually theirs rather
-- than burying them in the fleet.
--
-- Unresolved first, then oldest-first within that -- the same ordering rule as
-- every other queue here, so nobody waiting is buried by whoever complained
-- most recently.
SELECT i.issue_id,
       i.site_id,
       s.label AS site_label,
       s.district,
       i.device_id,
       i.bill_id,
       i.category,
       i.severity,
       i.status,
       i.title,
       i.description,
       i.priority,
       i.reported_at,
       i.acknowledged_at,
       i.resolved_at,
       i.reported_by_account_id,
       a.full_name AS reported_by_name,
       i.distribution_company_id,
       dc.name AS distribution_company_name,
       i.supplier_id,
       sc.name AS supplier_name,
       -- IS NOT DISTINCT FROM, not `=`. An issue that names nobody has a
       -- NULL supplier_id, and `NULL = $1` is NULL rather than FALSE -- so
       -- the flag came back null AND, worse, the ORDER BY below sorted
       -- those nulls FIRST under DESC, putting other people's complaints
       -- above the firm's own. This yields a definite boolean.
       (i.supplier_id IS NOT DISTINCT FROM $1) AS against_us
FROM issue i
JOIN site s ON s.site_id = i.site_id
JOIN account a ON a.account_id = i.reported_by_account_id
LEFT JOIN distribution_company dc ON dc.company_id = i.distribution_company_id
LEFT JOIN supplier_company sc ON sc.supplier_id = i.supplier_id
WHERE i.supplier_id = $1
   OR EXISTS (
       SELECT 1 FROM solar_array sa
       WHERE sa.site_id = i.site_id
         AND sa.installed_by_supplier_id = $1
         AND sa.status <> 'decommissioned'
   )
   OR EXISTS (
       SELECT 1 FROM work_order w
       WHERE w.issue_id = i.issue_id
         AND w.created_by_account_id IN (
             SELECT account_id FROM supplier_profile WHERE supplier_id = $1
         )
   )
-- Three bands, then newest first inside the last one. Unresolved above
-- closed; complaints named against this firm above ones it is merely near;
-- and within that, the most recent arrival on top.
--
-- IS NOT DISTINCT FROM, not '=': (i.supplier_id = $1) is NULL rather than
-- FALSE for an issue naming nobody, and DESC sorts NULLs first in PostgreSQL,
-- which put every unrelated complaint above the firm's own.
ORDER BY (i.status IN ('resolved', 'closed', 'duplicate')) ASC,
         (i.supplier_id IS NOT DISTINCT FROM $1) DESC,
         i.reported_at DESC;
