-- Ratings and the consumer's verdict: the household's half of a finished visit.
--
-- Consumer requirement 10 has three clauses -- track the assigned worker,
-- confirm the issue was really resolved, rate the worker and the supplier. The
-- first has worked since notifications landed. The other two are here, together,
-- because they are one act: somebody knocks, the household says whether it
-- worked, and says what they thought of the person who did it.
--
-- `rating_avg` from `assignable_workers` (work_order_queries.sql) reads what
-- this file writes, which is what makes supplier requirement 4's sort key real
-- rather than permanently NULL.


-- name: visits_for_account
-- Finished visits to this household's sites, newest first.
--
-- Scoped by ownership in SQL rather than by a filter in the handler, like every
-- other consumer-facing statement here. `completed` only: a cancelled or failed
-- visit is not something to rate, and a job still under way is not something to
-- have an opinion about yet.
--
-- Three aggregates hang off each row, and they are why this is one statement
-- rather than three requests: the crew who actually attended, the supplier firm
-- that dispatched them, and what this account has already said about both.
SELECT w.order_id,
       w.site_id,
       s.label            AS site_label,
       w.order_type::text AS order_type,
       w.completed_at,
       w.completion_notes,
       w.issue_id,
       i.title            AS issue_title,
       i.status::text     AS issue_status,
       i.consumer_confirmed_at,
       i.consumer_disputed_at,
       i.consumer_feedback,
       sc.supplier_id,
       sc.name            AS supplier_name,
       -- Who actually turned up. 'expired' and 'declined' assignees never came,
       -- and offering the household a chance to rate someone who was reassigned
       -- away would be both confusing and unfair to that worker.
       COALESCE(
           json_agg(
               json_build_object(
                   'account_id',  wa.account_id,
                   'worker_name', wacct.full_name,
                   'job_role',    wa.job_role
               ) ORDER BY wa.job_role, wacct.full_name
           ) FILTER (WHERE wa.account_id IS NOT NULL),
           '[]'::json
       ) AS crew,
       -- What this account has already said. Carried on the row so the client
       -- never has to ask a second time to know which buttons to hide.
       -- `rating_one_per_subject` is UNIQUE (order_id, rated_by_account_id,
       -- subject), so a household gives ONE worker rating per visit however
       -- many people attended -- it is a verdict on the visit, not a scorecard
       -- for a crew. Which technician it was recorded against is carried here
       -- so the client can say so rather than implying every name was rated.
       (
           SELECT json_build_object(
                      'stars', sr.stars,
                      'comment', sr.comment,
                      'worker_account_id', sr.worker_account_id)
           FROM service_rating sr
           WHERE sr.order_id = w.order_id
             AND sr.rated_by_account_id = $1
             AND sr.subject = 'worker'
           LIMIT 1
       ) AS worker_rating,
       (
           SELECT json_build_object('stars', sr.stars, 'comment', sr.comment)
           FROM service_rating sr
           WHERE sr.order_id = w.order_id
             AND sr.rated_by_account_id = $1
             AND sr.subject = 'supplier'
           LIMIT 1
       ) AS supplier_rating
FROM work_order w
JOIN site s ON s.site_id = w.site_id
LEFT JOIN issue i ON i.issue_id = w.issue_id
-- The firm, not the individual dispatcher: a household rates the company it
-- dealt with, and `supplier_profile` is what makes several logins one supplier
-- with one reputation.
LEFT JOIN supplier_profile sp ON sp.account_id = w.created_by_account_id
LEFT JOIN supplier_company sc ON sc.supplier_id = sp.supplier_id
LEFT JOIN work_order_assignment wa
       ON wa.order_id = w.order_id
      AND wa.status IN ('accepted', 'completed')
LEFT JOIN account wacct ON wacct.account_id = wa.account_id
WHERE s.account_id = $1
  AND w.status = 'completed'
GROUP BY w.order_id, s.label, i.issue_id, sc.supplier_id, sc.name
ORDER BY w.completed_at DESC NULLS LAST
LIMIT $2;


-- name: create_rating
-- One rating, by one account, for one subject on one visit.
--
-- `rating_subject_target` requires exactly one of worker_account_id /
-- supplier_id to be set, matching the subject -- so the caller passes both and
-- the wrong one arrives NULL. The CHECK is what actually enforces the pairing;
-- this statement does not second-guess it.
--
-- ON CONFLICT DO NOTHING against `rating_one_per_subject`, so a retried request
-- writes one row and the handler can tell a fresh rating from a repeat by
-- whether anything came back. **A rating is not editable.** It is testimony
-- about a particular visit, and one that can be quietly revised afterwards is
-- worth less -- not least because the party being rated has an interest in it
-- changing. The cost is that a misclick stands, which is why the UI asks for
-- stars and comment in one submit rather than saving a star the moment it is
-- pressed.
INSERT INTO service_rating (
    order_id, rated_by_account_id, subject,
    worker_account_id, supplier_id, stars, comment
)
VALUES ($1, $2, $3::rating_subject, $4, $5, $6, $7)
ON CONFLICT (order_id, rated_by_account_id, subject) DO NOTHING
RETURNING rating_id;


-- name: rateable_target
-- Is this a visit this account may rate, and is the named subject part of it?
--
-- One row when yes, none when no -- so the handler answers 404 for a visit that
-- is not theirs, someone else's site, or a worker who was never on the job.
-- Doing it here rather than in Python keeps the ownership test and the
-- membership test in the same place as the write they guard.
SELECT w.order_id,
       w.status::text AS status,
       s.account_id   AS owner_account_id,
       EXISTS (
           SELECT 1 FROM work_order_assignment wa
           WHERE wa.order_id = w.order_id
             AND wa.account_id = $3
             AND wa.status IN ('accepted', 'completed')
       ) AS worker_attended,
       (
           SELECT sp.supplier_id
           FROM supplier_profile sp
           WHERE sp.account_id = w.created_by_account_id
       ) AS supplier_id
FROM work_order w
JOIN site s ON s.site_id = w.site_id
WHERE w.order_id = $1
  AND s.account_id = $2;


-- name: set_issue_verdict
-- The household's answer to "did that actually fix it?".
--
-- **A verdict changes state**, the same principle the deadline sweeps follow.
-- Confirming closes the issue: 'resolved' is the engineer's opinion and
-- 'closed' is the household agreeing with it, which is exactly the distinction
-- the two enum values are for. Disputing sends it back to 'in_progress', so it
-- reappears in the worker triage queue and in the dispatcher's inbox rather
-- than sitting resolved while the fault is still there.
--
-- Guarded three ways: the issue must actually be resolved or closed (there is
-- nothing to confirm about a fault nobody has attended), and it must not
-- already carry a verdict -- `issue_verdict_is_one` forbids holding both
-- timestamps, and this statement forbids replacing one, so a household says its
-- piece once. A second attempt updates nothing and the handler answers 409.
UPDATE issue
SET consumer_confirmed_at = CASE WHEN $2 THEN now() ELSE NULL END,
    consumer_disputed_at  = CASE WHEN $2 THEN NULL ELSE now() END,
    consumer_feedback     = $3,
    status = CASE WHEN $2 THEN 'closed' ELSE 'in_progress' END::issue_status,
    closed_at = CASE WHEN $2 THEN COALESCE(closed_at, now()) ELSE closed_at END
WHERE issue_id = $1
  AND status IN ('resolved', 'closed')
  AND consumer_confirmed_at IS NULL
  AND consumer_disputed_at IS NULL
RETURNING issue_id;


-- name: issue_owner
-- Who may pass a verdict on this issue: the owner of the site it is against.
-- Not the reporter -- a worker can file an issue about somebody's meter, and it
-- is the household that lives with whether it was fixed.
SELECT i.issue_id,
       i.status::text AS status,
       s.account_id   AS owner_account_id,
       i.consumer_confirmed_at,
       i.consumer_disputed_at
FROM issue i
JOIN site s ON s.site_id = i.site_id
WHERE i.issue_id = $1;
