"""Populating site_daily_summary and site_monthly_summary.

Both tables have existed since migration a4092df65997 and both have been empty
ever since. They are a cache over `device_reading`, not a record: nothing is
billed from them and nothing here is append-only, which is exactly why the job
can rebuild a window rather than having to increment it.

Rebuilding is what makes the rollup survive a late backfill.
`backfill_readings()` upserts, so readings can arrive for a day that was
summarised last night; a job that only ever added yesterday would leave those
days permanently wrong, and nothing would ever notice because a summary looks
the same whether it is right or not. So the daily pass re-summarises the last
few days every night, and the monthly pass is derived from the daily one -- one
derivation, so a month can never contradict the days inside it.

Order matters: daily first, monthly second. The runner registers them as one
job for that reason rather than as two the scheduler could interleave.
"""
from __future__ import annotations

import logging
from datetime import date, timedelta
from uuid import UUID

import asyncpg

from ..api.queries import sql

log = logging.getLogger(__name__)


async def refresh_rollups(
    pool: asyncpg.Pool,
    lookback_days: int,
    *,
    site_id: UUID | None = None,
    through: date | None = None,
) -> dict[str, int]:
    """Re-summarise the last `lookback_days` whole days, then their months.

    The window ends YESTERDAY, not today. Today is still accumulating
    intervals, and a summary row for a day that is not over would read as a
    collapse in consumption to anyone looking at the table -- the same reason
    `device_health` excludes today from its coverage window.
    """
    to_date = (through or date.today()) - timedelta(days=1)
    from_date = to_date - timedelta(days=max(lookback_days, 1) - 1)

    # The months to re-derive are the months the daily window touches. A
    # lookback that straddles the 1st refreshes both, which is what closes out
    # the previous month on the morning of the 1st.
    month_from = from_date.replace(day=1)

    async with pool.acquire() as conn, conn.transaction():
        daily = await conn.fetch(
            sql("refresh_daily_summaries"), from_date, to_date, site_id
        )
        monthly = await conn.fetch(
            sql("refresh_monthly_summaries"), month_from, to_date, site_id
        )

    log.info(
        "rollups refreshed: %s day-rows over %s..%s, %s month-rows",
        len(daily), from_date, to_date, len(monthly),
    )
    return {
        "days": (to_date - from_date).days + 1,
        "daily_rows": len(daily),
        "monthly_rows": len(monthly),
    }
