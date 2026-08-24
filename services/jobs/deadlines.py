"""The two assignment-deadline sweeps.

CLAUDE.md decision 3: **deadlines change state.** An offer nobody answered
inside three hours does not merely acquire an overdue badge -- the assignment
becomes 'expired', the order goes back to the supplier who dispatched it, and
both parties are told. An accepted job nobody started inside a day does the
same.

Neither sweep computes a deadline. `offer_expires_at` and `start_deadline_at`
are written when the offer is made and when it is accepted, so a query run
between two sweeps already knows the offer has lapsed; the sweep only makes the
consequences happen. That is why the deadline durations live in the API and not
in this package's config.

Each row is its own transaction. One assignment whose notification insert
deadlocks must not roll back the twenty the sweep already expired, and a sweep
that dies half way through simply resumes on the next tick -- there is no
partial state to reconcile, because there is no cursor.
"""
from __future__ import annotations

import logging
from datetime import datetime

import asyncpg

from ..api.notify import notify
from ..api.queries import sql

log = logging.getLogger(__name__)


def _order_label(order_type: str) -> str:
    return order_type.replace("_", " ")


def _deadline_tag(deadline_at: datetime) -> str:
    """The deadline instant, as the identity of *this* offer.

    A dedupe key must name the event, not the moment the sweep noticed it
    (notify's contract). The order id alone is not enough here: an order can be
    offered, lapse, be re-offered to someone else and lapse again, and the
    second lapse is a genuinely new event that the worker and the dispatcher
    both need to hear about. The deadline is what distinguishes the two offers,
    and it is stable -- every re-run of the sweep computes the same key from the
    same row.
    """
    return deadline_at.isoformat()


async def _expire_one(
    conn: asyncpg.Connection,
    row: asyncpg.Record,
    *,
    from_status: str,
    kind: str,
    worker_title: str,
    worker_body: str,
    worker_severity: str,
    dispatcher_body: str,
) -> bool:
    """Expire one assignment and tell the two people it affects.

    Returns False when the row moved underneath us -- a worker who accepted, or
    another runner that got there first. Nothing is written and nobody is
    notified in that case, which is the point of guarding the UPDATE on the
    status the sweep read rather than trusting the SELECT.
    """
    expired = await conn.fetchval(
        sql("expire_assignment"), row["order_id"], row["account_id"], from_status
    )
    if expired is None:
        return False

    tag = _deadline_tag(row["deadline_at"])
    entity = {"entity_type": "work_order", "entity_id": str(row["order_id"])}

    await notify(
        conn,
        row["account_id"],
        kind,
        worker_title,
        body=worker_body.format(
            order=_order_label(row["order_type"]), site=row["site_label"]
        ),
        severity=worker_severity,
        dedupe_key=f"wo:{row['order_id']}:{kind}:{row['account_id']}:{tag}",
        **entity,
    )

    # Only when the order actually came back does the dispatcher hear about it.
    # A two-person job whose assistant lapsed is still dispatched to its lead,
    # and telling the supplier it needs reassigning would be false.
    released = await conn.fetchval(sql("release_work_order"), row["order_id"])
    if released is not None:
        await notify(
            conn,
            row["created_by_account_id"],
            kind,
            f"Unassigned — {_order_label(row['order_type'])} at {row['site_label']}",
            body=dispatcher_body.format(
                worker=row["worker_name"],
                order=_order_label(row["order_type"]),
                site=row["site_label"],
            ),
            severity="warning",
            dedupe_key=f"wo:{row['order_id']}:released:{tag}",
            **entity,
        )
    return True


async def sweep_expired_offers(pool: asyncpg.Pool, limit: int) -> dict[str, int]:
    """Supplier requirement 5: an offer nobody accepted goes back on the board."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql("expiring_offers"), limit)

    expired = 0
    for row in rows:
        async with pool.acquire() as conn, conn.transaction():
            if await _expire_one(
                conn,
                row,
                from_status="offered",
                kind="work_order_offer_expired",
                worker_title="Job offer expired",
                worker_body=(
                    "The {order} at {site} was offered to you and has now been "
                    "released to another technician."
                ),
                worker_severity="info",
                dispatcher_body=(
                    "{worker} did not respond to the {order} at {site} in time. "
                    "It is back in the queue and needs assigning."
                ),
            ):
                expired += 1

    return {"found": len(rows), "expired": expired}


async def sweep_overdue_starts(pool: asyncpg.Pool, limit: int) -> dict[str, int]:
    """Worker requirement 5: accepted but never started goes back to the supplier.

    Note what this does NOT do: it does not touch an order that has started.
    `overdue_starts` filters on `work_order.started_at IS NULL`, so a worker who
    turned up and pressed start keeps the job however late the sweep runs.
    """
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql("overdue_starts"), limit)

    expired = 0
    for row in rows:
        async with pool.acquire() as conn, conn.transaction():
            if await _expire_one(
                conn,
                row,
                from_status="accepted",
                kind="work_order_start_overdue",
                worker_title="Job reassigned — not started in time",
                worker_body=(
                    "You accepted the {order} at {site} but did not start it "
                    "within a day, so it has been returned to the dispatcher."
                ),
                worker_severity="warning",
                dispatcher_body=(
                    "{worker} accepted the {order} at {site} but never started "
                    "it. It is back in the queue and needs reassigning."
                ),
            ):
                expired += 1

    return {"found": len(rows), "expired": expired}
