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


-- ---------------------------------------------------------------------------
-- Read/unread for lists (as opposed to notifications, above).
--
-- `notification` is an inbox: a row per event, written by the system, marked
-- read one at a time. That is the wrong shape for "has anything new landed in
-- the agreements table since I last looked", which is a question about a
-- whole list rather than about individual events -- and most list rows never
-- generate a notification at all.
--
-- `list_view_state` answers it with one row per account per list. A row in a
-- list is unread when it is newer than that account's `last_viewed_at` for
-- that list. Bounded by (accounts x lists) rather than growing with every row
-- every viewer has ever seen, and needing no reaper.
-- ---------------------------------------------------------------------------

-- name: list_view_states
-- Every watermark this account holds. One request; the client keeps them and
-- compares against the rows it already has, so no list's scoping rules have to
-- be re-implemented as a counting query that could drift from the list itself.
SELECT view_key, last_viewed_at
FROM list_view_state
WHERE account_id = $1;


-- name: mark_view_seen
-- Move this account's watermark for one list to now, and return where it WAS.
--
-- Returning the previous value is the whole mechanism. The page marks itself
-- seen on open, gets back the watermark it is replacing, and highlights the
-- rows newer than that one -- so the arrivals are visible on exactly the visit
-- that clears them, and the next load has moved on and highlights nothing.
-- Reading and writing in one statement also means two tabs cannot both claim
-- to be the visit that cleared it.
-- Written as two CTEs rather than a subquery inside RETURNING. Every CTE in
-- one statement sees the same snapshot -- the one taken before the statement
-- ran -- so `prev` reads the value the upsert is about to overwrite. A
-- subquery in RETURNING would be relying on the same guarantee far less
-- legibly, and on a reader knowing it.
--
-- `previous_viewed_at` is NULL the first time this account has ever opened
-- the list, which the caller reads as "every row is new". That is correct,
-- and it is why a first visit lights up.
WITH prev AS (
    SELECT last_viewed_at
    FROM list_view_state
    WHERE account_id = $1 AND view_key = $2
),
upsert AS (
    INSERT INTO list_view_state (account_id, view_key, last_viewed_at)
    VALUES ($1, $2, now())
    ON CONFLICT (account_id, view_key) DO UPDATE
    SET last_viewed_at = now()
    RETURNING last_viewed_at
)
SELECT (SELECT last_viewed_at FROM prev)   AS previous_viewed_at,
       (SELECT last_viewed_at FROM upsert) AS last_viewed_at;
