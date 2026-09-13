"""Offer the meters installed before commissioning existed to their head-ends.

    python -m scripts.commission_existing_meters --dry-run
    python -m scripts.commission_existing_meters

Registration offers a meter the moment it is installed (step 3). Meters that
were already on the wall -- the seeded estate -- were never offered, so a
head-end's feed has nothing to say about them. This offers every live billing
meter with no open handshake, exactly as `register_meter` would have, through
the same `offer_commissioning()`.

**The window starts after what the connection already holds.** A seeded meter
has readings through the day the seed ran; its offer runs from the next day,
so the head-end continues the series instead of re-uploading it. When that day
is today the window is today..today, which tells the head-end to start at
today's first interval. A meter with no readings gets the full 90 days.

Safe to re-run: a meter with an open handshake is skipped, and
`one_open_commissioning_per_device` refuses a second one regardless. A meter
whose handshake failed or was cancelled is offered again, which is the usual
reason to run this twice.

After this, any `device_keys.json` entry for these meters is stale: activation
replaces the key. Run `python -m scripts.issue_device_keys` afterwards if the
keyfile modes are still wanted for the inverters -- it skips commissioned
devices now.
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import date

import asyncpg

from services.api.commissioning import offer_commissioning, window_for
from services.api.db import database_url, init_connection
from services.api.queries import sql


async def offer_existing(
    conn: asyncpg.Connection, *, today: date, dry_run: bool = False
) -> list[dict]:
    """Offer every live billing meter that has no open handshake."""
    made = []
    for meter in await conn.fetch(sql("meters_awaiting_commissioning")):
        start, end = window_for(meter["last_day"], today)
        entry = {"serial_no": meter["serial_no"], "window": (start, end), "status": None}
        if not dry_run:
            row = await offer_commissioning(
                conn,
                device_id=meter["device_id"],
                point_id=meter["point_id"],
                meter_asset_id=meter["meter_asset_id"],
                backfill_from=start,
                backfill_to=end,
                requested_by=None,
            )
            entry["status"] = row["status"]
        made.append(entry)
    return made


async def run(dry_run: bool) -> None:
    conn = await asyncpg.connect(database_url())
    await init_connection(conn)
    try:
        async with conn.transaction():
            made = await offer_existing(conn, today=date.today(), dry_run=dry_run)
    finally:
        await conn.close()

    for m in made:
        start, end = m["window"]
        outcome = m["status"] or "would offer"
        print(f"  {m['serial_no']:<22} {outcome:<12} history {start} -> {end}")
    verb = "would offer" if dry_run else "offered"
    print(f"\n{verb} {len(made)} meter(s)")
    failed = [m for m in made if m["status"] == "failed"]
    if failed:
        print(
            f"  ! {len(failed)} have no head-end for their utility "
            "(run python -m scripts.issue_source_keys)"
        )


def main() -> None:
    parser = argparse.ArgumentParser(prog="scripts.commission_existing_meters")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    asyncio.run(run(args.dry_run))


if __name__ == "__main__":
    main()
