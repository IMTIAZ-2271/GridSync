"""Declining an offer: what a decline is supposed to undo.

A decline and a lapsed deadline mean the same thing to the order -- nobody is
coming -- so they must leave the same state behind. The expiry sweep has always
released the order (CLAUDE.md decision 3); the decline path released only the
assignment, which left the order `dispatched` with nobody on it. The worker who
declined kept it in their queue, kept the site's issues, and could still walk
the order into `in_progress`.

These tests are on the statements rather than on the route because the repo has
no HTTP test harness (no httpx). What they pin is the predicate every one of
those four paths shares: an assignment ties a worker to a job only while it is
live, and 'declined' is not live.
"""
from __future__ import annotations

import pytest

from services.api.queries import sql

from .factories import make_account, make_assignment, make_site, make_work_order, make_worker

pytestmark = pytest.mark.asyncio


async def _declined_order(conn, *, order_status: str = "dispatched"):
    """One dispatched order whose only assignment has just been declined."""
    worker = await make_worker(conn)
    site_owner = await make_account(conn)
    site_id = await make_site(conn, site_owner)
    order_id = await make_work_order(conn, site_id, status=order_status)
    await make_assignment(conn, order_id, worker, status="offered")
    declined = await conn.fetchrow(sql("decline_assignment"), order_id, worker, "busy")
    assert declined is not None, "the offer should have been declinable"
    return worker, site_id, order_id


# ---------------------------------------------------------------------------
# The order comes back
# ---------------------------------------------------------------------------


async def test_declining_returns_the_order_to_draft(conn):
    """The order an offer was declined on needs an assignee, so it is a draft.

    Same end state as the expiry sweep produces, and for the same reason: an
    order nobody is on is not dispatched.
    """
    _, _, order_id = await _declined_order(conn)

    released = await conn.fetchval(sql("release_work_order"), order_id)
    assert released is not None

    status = await conn.fetchval(
        "SELECT status FROM work_order WHERE order_id = $1", order_id
    )
    assert status == "draft"


async def test_a_live_co_assignee_keeps_the_order_dispatched(conn):
    """A two-person job whose assistant declined is still happening.

    `release_work_order`'s own guard, asserted from the decline side: telling
    the supplier this order needs reassigning while its lead is still on it
    would be false.
    """
    lead = await make_worker(conn)
    assistant = await make_worker(conn)
    site_id = await make_site(conn, await make_account(conn))
    order_id = await make_work_order(conn, site_id, status="dispatched")
    await make_assignment(conn, order_id, lead, status="accepted")
    await make_assignment(conn, order_id, assistant, job_role="assistant", status="offered")

    await conn.fetchrow(sql("decline_assignment"), order_id, assistant, None)
    released = await conn.fetchval(sql("release_work_order"), order_id)

    assert released is None
    status = await conn.fetchval(
        "SELECT status FROM work_order WHERE order_id = $1", order_id
    )
    assert status == "dispatched"


async def test_the_decline_reason_is_stored(conn):
    """The reason is a column, not only a line in a notification body."""
    worker, _, order_id = await _declined_order(conn)
    reason = await conn.fetchval(
        "SELECT decline_reason FROM work_order_assignment "
        "WHERE order_id = $1 AND account_id = $2",
        order_id, worker,
    )
    assert reason == "busy"


async def test_declining_without_a_reason_is_allowed(conn):
    """A technician is not made to justify a decline."""
    worker = await make_worker(conn)
    site_id = await make_site(conn, await make_account(conn))
    order_id = await make_work_order(conn, site_id, status="dispatched")
    await make_assignment(conn, order_id, worker, status="offered")

    declined = await conn.fetchrow(sql("decline_assignment"), order_id, worker, None)

    assert declined is not None
    assert declined["status"] == "declined"


# ---------------------------------------------------------------------------
# A declined assignment stops tying the worker to the job
# ---------------------------------------------------------------------------


async def test_a_declined_worker_leaves_their_own_queue(conn):
    """The order is gone from the queue of the worker who said no.

    Left in, it rendered with whatever buttons its status offered -- which is
    how a declined worker was shown 'Start work'.
    """
    worker, _, order_id = await _declined_order(conn)
    await conn.fetchval(sql("release_work_order"), order_id)

    rows = await conn.fetch(sql("work_orders_for_worker"), worker)

    assert [r["order_id"] for r in rows] == []


async def test_a_declined_worker_cannot_advance_the_order(conn):
    """The guard behind PATCH /status, so hiding the button is not the fix.

    `worker_assigned_to_order` is what the route checks. While it matched a
    declined row, a worker who had said no could still start the job by URL.
    """
    worker, _, order_id = await _declined_order(conn)

    assert await conn.fetchval(sql("worker_assigned_to_order"), order_id, worker) is None


async def test_a_declined_worker_no_longer_covers_the_site(conn):
    """Site visibility ends with the assignment that granted it.

    `worker_covers_site` is what lets a worker read a household's complaints.
    Its own comment says "only for as long as an assignment ties them to it";
    a declined assignment does not.
    """
    worker, site_id, _ = await _declined_order(conn)

    assert await conn.fetchval(sql("worker_covers_site"), site_id, worker) is None


async def test_a_declined_worker_no_longer_reads_the_sites_issues(conn):
    """The same predicate, one level up: the issue list itself."""
    worker, site_id, _ = await _declined_order(conn)
    reporter = await conn.fetchval(
        "SELECT account_id FROM site WHERE site_id = $1", site_id
    )
    await conn.execute(
        """
        INSERT INTO issue (reported_by_account_id, site_id, category, severity,
                           title, priority)
        VALUES ($1, $2, 'meter_fault', 'high', 'No reading since Tuesday', 3)
        """,
        reporter, site_id,
    )

    rows = await conn.fetch(sql("issues_for_worker"), worker)

    assert [r["title"] for r in rows] == []


# ---------------------------------------------------------------------------
# What must NOT be hidden
# ---------------------------------------------------------------------------


async def test_finished_work_stays_in_the_queue(conn):
    """A completed assignment still ties the worker to the job.

    The filter excludes assignments that ended without the work happening. A
    worker who did the job keeps it in Closed, and keeps the site it was on --
    otherwise this fix would quietly erase their own history.
    """
    worker = await make_worker(conn)
    site_id = await make_site(conn, await make_account(conn))
    order_id = await make_work_order(conn, site_id, status="completed")
    await make_assignment(conn, order_id, worker, status="completed")

    rows = await conn.fetch(sql("work_orders_for_worker"), worker)

    assert [r["order_id"] for r in rows] == [order_id]
    assert await conn.fetchval(sql("worker_covers_site"), site_id, worker) == 1


async def test_an_open_offer_still_shows_in_the_queue(conn):
    """An unanswered offer is the one row that most needs to be visible."""
    worker = await make_worker(conn)
    site_id = await make_site(conn, await make_account(conn))
    order_id = await make_work_order(conn, site_id, status="dispatched")
    await make_assignment(conn, order_id, worker, status="offered")

    rows = await conn.fetch(sql("work_orders_for_worker"), worker)

    assert [r["order_id"] for r in rows] == [order_id]
