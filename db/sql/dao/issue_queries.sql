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
