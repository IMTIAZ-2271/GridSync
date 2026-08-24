"""Keeping the telemetry table's partitions ahead of the readings.

`device_reading` is RANGE-partitioned by month. Migration 0f6109903981 created
months through 2027-02 and said, in its own comment, that services/jobs keeps
the window rolling. This is that job.

Two things make it worth running nightly rather than remembering to do it once a
year. A reading with no partition to land in falls into
`device_reading_default`, and while that table holds matching rows, attaching a
real partition for the month requires a full scan of it -- so the cost of being
late is paid at the worst possible moment. And the bound arithmetic is subtle
enough (CLAUDE.md's timezone rule) that it must not be re-derived by hand at 2am:
`create_reading_partition()` is the single place that computes one, and this job
calls it rather than writing DDL of its own.
"""
from __future__ import annotations

import logging
from datetime import date

import asyncpg

from ..api.queries import sql

log = logging.getLogger(__name__)


def _add_months(d: date, months: int) -> date:
    total = (d.year * 12 + d.month - 1) + months
    return date(total // 12, total % 12 + 1, 1)


async def ensure_partitions(pool: asyncpg.Pool, months_ahead: int) -> dict[str, int]:
    """Create this month and the next `months_ahead`, then report the default.

    Idempotent: `create_reading_partition` returns the existing partition's name
    untouched when the month is already there, so the common case writes nothing
    and the counter below reports how many were newly created by diffing the
    names it had not seen before.
    """
    start = date.today().replace(day=1)
    wanted = [_add_months(start, n) for n in range(months_ahead + 1)]

    created: list[str] = []
    async with pool.acquire() as conn:
        existing = {
            r["name"]
            for r in await conn.fetch(
                "SELECT c.relname AS name FROM pg_class c "
                "JOIN pg_inherits i ON i.inhrelid = c.oid "
                "JOIN pg_class p ON p.oid = i.inhparent "
                "WHERE p.relname = 'device_reading'"
            )
        }
        for month in wanted:
            name = await conn.fetchval(sql("ensure_reading_partition"), month)
            if name not in existing:
                created.append(name)

        stray = await conn.fetchval(sql("default_partition_rows"))

    if created:
        log.info("created reading partitions: %s", ", ".join(created))
    if stray:
        # Deliberately a log line and not a notification. Nobody with an account
        # in this system can act on it -- it is an operator's problem, and
        # notification is a user-facing inbox, not an alerting channel.
        log.warning(
            "device_reading_default holds %s row(s): a reading landed outside "
            "every month that exists. Find the month, create its partition, and "
            "move the rows before the table grows.",
            stray,
        )

    return {
        "checked": len(wanted),
        "created": len(created),
        "default_partition_rows": int(stray or 0),
    }
