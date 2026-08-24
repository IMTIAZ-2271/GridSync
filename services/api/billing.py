"""Calling `run_billing` correctly, in the one place that knows how.

`db/sql/service/billing.sql` does the work; this module owns the contract
around it that CLAUDE.md requires of *every* caller -- REPEATABLE READ, retry on
serialization failure, one call per billing point per month.

It lives here rather than inside routes_sites.py because there are now two
callers: the consumer's `POST /api/sites/{id}/bill` and the nightly pass in
services/jobs/billing.py. The first version of the month-stepping helper existed
only in routes_sites.py, was deleted by an unrelated change, and 500'd that
endpoint for three commits (CLAUDE.md, Known weaknesses). Two copies of an
isolation-level contract would fail more quietly than that.
"""
from __future__ import annotations

from datetime import date
from uuid import UUID

import asyncpg


def next_month(d: date) -> date:
    """The first of the month after the one containing `d`."""
    return date(d.year + (d.month == 12), (d.month % 12) + 1, 1)


async def run_billing_with_retry(
    conn: asyncpg.Connection,
    point_id: UUID,
    period_start: date,
    attempts: int = 3,
) -> UUID:
    """run_billing under REPEATABLE READ, retried on serialization failure.

    The unit is a billing point, not a site (rule 3): a household with two
    connections gets two independent bills for the same month, each with its own
    credit balance.

    Retries are the caller's job because the transaction is: a 40001 aborts
    everything the transaction did, so only whoever opened it can start a new
    one. Three attempts, then the error goes up -- a point that cannot be billed
    after three serialization failures is contended by something that needs
    looking at, not something to spin on.
    """
    for attempt in range(attempts):
        try:
            async with conn.transaction(isolation="repeatable_read"):
                return await conn.fetchval(
                    "SELECT run_billing($1, $2)", point_id, period_start
                )
        except asyncpg.SerializationError:
            if attempt == attempts - 1:
                raise
