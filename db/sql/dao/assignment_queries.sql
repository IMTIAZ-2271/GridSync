-- Work-order assignment DAO: offering a job, and answering the offer.
--
-- Separate from work_order_queries.sql because it is a different lifecycle.
-- The work order's own status is dispatcher bookkeeping with deliberately no
-- state machine behind it (see routes_work_orders.py); an ASSIGNMENT is a
-- two-party agreement with a clock on it, and the clock is the reason
-- services/jobs exists.
--
-- Both deadlines are written here and nowhere else. CLAUDE.md decision 3:
-- deadlines are STORED so that a query between two sweeps is already correct.
-- The sweep in services/jobs/deadlines.py only ever reads them.


-- name: offerable_worker
-- May this account be offered a job at all?
--
-- Approval is checked here rather than at the sweep: a government worker whose
-- registration is still pending has no business holding an offer, and finding
-- that out three hours later when it expires helps nobody.
SELECT wp.account_id,
       a.full_name,
       wp.approval_status::text AS approval_status,
       wp.availability::text    AS availability,
       wp.left_on
FROM worker_profile wp
JOIN account a ON a.account_id = wp.account_id
WHERE wp.account_id = $1;


-- name: offer_assignment
-- Offer one order to one worker, with the clock started.
--
-- $3 is the offer window as an interval, passed by the caller rather than
-- hard-coded: routes_work_orders.py owns the policy, this owns the write.
--
-- ON CONFLICT re-offers: a worker who declined, or whose earlier offer expired,
-- can be offered the same order again -- crews are small and the second-best
-- technician is often the first one who was busy. The re-offer is a NEW offer,
-- so it gets a new deadline and clears the previous response, and because the
-- sweep's dedupe key is built from the deadline instant, a second lapse
-- notifies again rather than being silently swallowed as a duplicate.
INSERT INTO work_order_assignment (
    order_id, account_id, job_role, status, assigned_at, offer_expires_at
)
VALUES ($1, $2, $4::assignment_role, 'offered', now(), now() + $3::interval)
ON CONFLICT (order_id, account_id) DO UPDATE
SET job_role          = EXCLUDED.job_role,
    status            = 'offered',
    assigned_at       = now(),
    offer_expires_at  = EXCLUDED.offer_expires_at,
    responded_at      = NULL,
    released_at       = NULL,
    decline_reason    = NULL,
    start_deadline_at = NULL,
    expired_at        = NULL
RETURNING order_id, account_id, job_role::text AS job_role,
          status::text AS status, offer_expires_at;


-- name: dispatch_work_order
-- An order with somebody on it is dispatched.
--
-- Narrow on purpose: only a 'draft' or 'scheduled' order moves. An order
-- already in progress that gains a second crew member does not go backwards,
-- and a completed one certainly does not. This is the inverse of
-- release_work_order in jobs_queries.sql, and the pair is the whole state loop
-- the deadline sweeps close.
UPDATE work_order
SET status = 'dispatched'
WHERE order_id = $1
  AND status IN ('draft', 'scheduled')
RETURNING order_id;


-- name: accept_assignment
-- Accepting starts the SECOND clock: worker requirement 5 gives a day to
-- actually start, after which services/jobs hands the order back.
--
-- Guarded on 'offered', so an offer the sweep expired a second earlier cannot
-- be accepted afterwards -- the worker gets a 409 and the order stays with
-- whoever it was released to.
UPDATE work_order_assignment
SET status            = 'accepted',
    responded_at      = now(),
    start_deadline_at = now() + $3::interval
WHERE order_id   = $1
  AND account_id = $2
  AND status     = 'offered'
RETURNING order_id, account_id, status::text AS status, start_deadline_at;


-- name: decline_assignment
-- Declining clears the offer deadline: there is nothing left to expire, and a
-- stored deadline on a dead row would make `expiring_offers` wrong the moment
-- someone loosened its status filter.
UPDATE work_order_assignment
SET status           = 'declined',
    responded_at     = now(),
    released_at      = now(),
    decline_reason   = $3,
    offer_expires_at = NULL
WHERE order_id   = $1
  AND account_id = $2
  AND status     = 'offered'
RETURNING order_id, account_id, status::text AS status;


-- name: assignment_context
-- Who to tell, and what to call the job. One row per assignment.
SELECT wa.order_id,
       wa.account_id,
       wa.status::text     AS status,
       wa.offer_expires_at,
       wa.start_deadline_at,
       a.full_name         AS worker_name,
       w.site_id,
       w.order_type::text  AS order_type,
       w.status::text      AS order_status,
       w.priority,
       w.scheduled_for,
       w.created_by_account_id,
       s.label             AS site_label
FROM work_order_assignment wa
JOIN work_order w ON w.order_id = wa.order_id
JOIN site s       ON s.site_id = w.site_id
JOIN account a    ON a.account_id = wa.account_id
WHERE wa.order_id = $1 AND wa.account_id = $2;
