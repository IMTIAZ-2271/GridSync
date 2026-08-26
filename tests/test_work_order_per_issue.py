"""One LIVE work order per issue -- not one ever.

`dispatchable_issues` deliberately returns a complaint whose order was
cancelled or failed: the fault is still real and somebody has to go again. The
index behind it forbade a second order for an issue outright, so the supplier's
inbox offered exactly the row the schema refused, and raising it was a 500.

What the index is actually for is the race: two dispatchers reading the same
inbox must not send two crews to one complaint. That is about *open* orders, so
terminal ones do not block a new visit.
"""
from __future__ import annotations

import asyncpg
import pytest

from .factories import make_account, make_site, make_work_order

pytestmark = pytest.mark.asyncio


async def _issue(conn, site_id: str, reporter: str) -> str:
    return await conn.fetchval(
        """
        INSERT INTO issue (reported_by_account_id, site_id, category, severity,
                           title, priority)
        VALUES ($1, $2, 'outage', 'high', 'Power out since 06:00', 2)
        RETURNING issue_id
        """,
        reporter, site_id,
    )


async def _setup(conn):
    owner = await make_account(conn)
    site_id = await make_site(conn, owner)
    return site_id, await _issue(conn, site_id, owner)


# ---------------------------------------------------------------------------
# A terminal order does not block the next visit
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("closed_status", ["cancelled", "failed"])
async def test_a_cancelled_or_failed_order_lets_the_issue_be_raised_again(
    conn, closed_status
):
    """The exact 500: the inbox re-offers these, so the schema must allow them."""
    site_id, issue_id = await _setup(conn)
    await make_work_order(conn, site_id, status=closed_status, issue_id=issue_id)

    second = await make_work_order(conn, site_id, status="draft", issue_id=issue_id)

    assert second is not None


async def test_a_completed_order_lets_the_issue_be_raised_again(conn):
    """A disputed visit puts the complaint back in the inbox.

    `dispatchable_issues` stops counting a completed order as coverage once the
    household says it did not fix the problem. The index cannot see
    `consumer_disputed_at` -- it is on another table -- so it treats every
    terminal order the same way and lets the second visit be raised.
    """
    site_id, issue_id = await _setup(conn)
    await make_work_order(conn, site_id, status="completed", issue_id=issue_id)

    second = await make_work_order(conn, site_id, status="draft", issue_id=issue_id)

    assert second is not None


# ---------------------------------------------------------------------------
# Two open orders for one complaint stay forbidden
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "live_status", ["draft", "scheduled", "dispatched", "in_progress"]
)
async def test_two_open_orders_on_one_issue_are_refused(conn, live_status):
    """The race the index exists for: two dispatchers, one complaint."""
    site_id, issue_id = await _setup(conn)
    await make_work_order(conn, site_id, status=live_status, issue_id=issue_id)

    with pytest.raises(asyncpg.UniqueViolationError):
        await make_work_order(conn, site_id, status="draft", issue_id=issue_id)


async def test_orders_without_an_issue_are_never_constrained(conn):
    """A site can hold any number of orders raised without a complaint."""
    site_id, _ = await _setup(conn)

    first = await make_work_order(conn, site_id, status="draft")
    second = await make_work_order(conn, site_id, status="draft")

    assert first != second
