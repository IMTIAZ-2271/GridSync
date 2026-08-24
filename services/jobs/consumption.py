"""Consumer requirement 5: tell a household when it is running through the
monthly limit it set for itself.

The threshold is the household's own (`site_consumption_limit.notify_at_pct`,
80% by default), measured month-to-date against the monthly figure it set. The
query explains why it is not a per-day trigger; the message here is why the
daily average appears in it anyway -- a percentage tells someone they have a
problem, a kWh-per-day figure against an allowance tells them how big it is.

One alert per site per month. `dedupe_key` names the month, so the sweep can run
every morning and a household that spends the last ten days of August above its
limit is told once, in the morning it crossed. Rule 4 again: the partial unique
index refuses the second insert, the job does not have to remember.
"""
from __future__ import annotations

import logging

import asyncpg

from ..api.notify import notify
from ..api.queries import sql

log = logging.getLogger(__name__)


async def sweep_consumption_limits(pool: asyncpg.Pool, limit: int) -> dict[str, int]:
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql("sites_over_consumption_limit"), limit)

    notified = 0
    for row in rows:
        # A limit is set on a site, but a notification is delivered to an
        # account: site.account_id is who set it and who hears about it.
        # notify_site_owner would re-read the same row, so this uses the
        # account_id the query already carried.
        async with pool.acquire() as conn, conn.transaction():
            written = await notify(
                conn,
                row["account_id"],
                "consumption_threshold",
                f"{row['used_pct']}% of your monthly limit used",
                body=(
                    f"{row['site_label']} has used {row['used_kwh']} kWh of its "
                    f"{row['monthly_kwh']} kWh limit this month — averaging "
                    f"{row['daily_average_kwh']} kWh a day against an allowance "
                    f"of {row['daily_allowance_kwh']} kWh."
                ),
                # 'warning' rather than 'critical': the household is over a
                # budget it set itself, not in any danger. Critical is reserved
                # for something that costs money or stops a bill being issued.
                severity="warning",
                entity_type="site",
                entity_id=str(row["site_id"]),
                dedupe_key=f"limit:{row['site_id']}:{row['month_start']}",
            )
            if written is not None:
                notified += 1

    return {"over_limit": len(rows), "notified": notified}
