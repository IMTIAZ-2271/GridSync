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
       a.full_name AS reported_by_name
FROM issue i
JOIN site s ON s.site_id = i.site_id
JOIN account a ON a.account_id = i.reported_by_account_id
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
       a.full_name AS reported_by_name
FROM issue i
JOIN site s ON s.site_id = i.site_id
JOIN account a ON a.account_id = i.reported_by_account_id
WHERE i.issue_id = $1;


-- name: create_issue
-- reported_by_account_id falls back to the site's owner when the client does
-- not supply one. There is no auth to infer a reporter from, and the column is
-- NOT NULL -- attributing an unattributed report to the account that owns the
-- service point is the least wrong guess available. It becomes a session
-- lookup the moment auth exists.
INSERT INTO issue (reported_by_account_id, site_id, device_id, bill_id,
                   category, severity, title, description, priority)
VALUES (COALESCE($1, (SELECT account_id FROM site WHERE site_id = $2)),
        $2, $3, $4, $5::issue_category, $6::issue_severity, $7, $8, $9::smallint)
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
