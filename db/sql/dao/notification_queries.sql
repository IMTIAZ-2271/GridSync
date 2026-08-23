-- Notifications: the per-account inbox behind the bell in the layout header.
--
-- Consumer requirement 11 asks for a dedicated notification panel, but the
-- table serves every role -- a worker learns their government registration was
-- approved here, a supplier learns an offer expired, a household learns its
-- work order was completed.
--
-- Rows are written by `services/api/notify.py`, never by the client. There is
-- no "create notification" endpoint on purpose: a notification is something
-- the system observed, and letting a caller post one would make the panel
-- forgeable.


-- name: list_notifications
-- One account's inbox, newest first.
--
-- $2 narrows to unread; $3 caps the page. Ordered by created_at AND the
-- identity column, because several notifications can be written inside one
-- transaction and share a timestamp to the microsecond -- ordering on the
-- timestamp alone would let them shuffle between requests and make the panel
-- appear to reorder itself.
SELECT n.notification_id,
       n.kind::text     AS kind,
       n.severity::text AS severity,
       n.title,
       n.body,
       n.entity_type,
       n.entity_id,
       n.created_at,
       n.read_at
FROM notification n
WHERE n.account_id = $1
  AND ($2::boolean IS NOT TRUE OR n.read_at IS NULL)
ORDER BY n.created_at DESC, n.notification_id DESC
LIMIT $3;


-- name: unread_notification_count
-- Served alongside every list so the badge and the list cannot disagree, and
-- backed by the partial index `notification_unread`.
SELECT count(*)::int AS unread_count
FROM notification
WHERE account_id = $1 AND read_at IS NULL;


-- name: mark_notification_read
-- Scoped to the owner, so a guessed id belonging to someone else updates zero
-- rows and the handler answers 404 -- reading it as "already read" would tell
-- the caller that the id exists.
--
-- Guarded on read_at IS NULL as well: marking an already-read notification
-- must not move its timestamp forward.
UPDATE notification
SET read_at = now()
WHERE notification_id = $1
  AND account_id = $2
  AND read_at IS NULL
RETURNING notification_id;


-- name: mark_all_notifications_read
-- Returns the rows it touched so the handler can report how many, rather than
-- the client having to diff two fetches.
UPDATE notification
SET read_at = now()
WHERE account_id = $1 AND read_at IS NULL
RETURNING notification_id;


-- name: create_notification
-- Written only by services/api/notify.py.
--
-- The ON CONFLICT is rule 4 applied to a background job: the jobs sweep that
-- will write expiry and consumption-limit alerts must be safe to run twice, so
-- a caller passing a dedupe_key gets at-most-once delivery enforced by the
-- database rather than by the job remembering what it already sent. The
-- predicate matches `notification_dedupe` exactly, so the partial index is
-- what the inference resolves to; a NULL dedupe_key skips it and always
-- inserts.
--
-- Returns nothing when the conflict fires, which is how the caller tells a
-- fresh notification from a suppressed duplicate.
INSERT INTO notification (
    account_id, kind, severity, title, body,
    entity_type, entity_id, dedupe_key
)
VALUES ($1, $2::notification_kind, $3::notification_severity, $4, $5,
        $6, $7, $8)
ON CONFLICT (account_id, dedupe_key) WHERE dedupe_key IS NOT NULL
DO NOTHING
RETURNING notification_id;


-- name: site_owner_account
-- Who to tell about something that happened to a site. One row or none.
SELECT s.account_id, s.label AS site_label
FROM site s
WHERE s.site_id = $1;
