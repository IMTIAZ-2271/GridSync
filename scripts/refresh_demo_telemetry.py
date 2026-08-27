"""Bring the seeded demo estate's telemetry up to yesterday.

`db/sql/seed_demo.sql` writes 90 days of readings ending on the day it is run,
and then time passes. A week later every demo account's equipment page reads
`silent`, the supplier's fleet shows 21 of 29 devices needing attention, and --
since the overview gained a timeframe selector -- the **default Week view is an
empty chart**. All of that is the feature reporting correctly: there is no
ingest service (CLAUDE.md, NOT DONE). It just makes a working demo look broken.

This closes the gap the way the onboarding endpoints do, by calling
`backfill_readings()` per device from its last stored reading through
yesterday.

Two things make it safe to run against a database that has been billed:

* `backfill_readings()` enforces rule 8 itself -- any interval whose
  billing_period is already frozen, billed or closed is excluded from the
  upsert. Committed bills cannot move.
* It upserts on `(device_id, interval_start)`, so re-running is idempotent
  rather than additive.

Capacity is resolved the same way `POST /api/sites/{id}/solar` resolves it: an
inverter is netted against its own AC rating, and a billing meter against its
billing point's TOTAL capacity. Passing one array's rating to a meter serving
two would understate export (rule 6 -- the meter measures everything behind the
connection at the grid boundary).

    python -m scripts.refresh_demo_telemetry            # bring everything up to date
    python -m scripts.refresh_demo_telemetry --dry-run  # say what it would write
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import date, timedelta
from pathlib import Path

import asyncpg

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.api.db import database_url  # noqa: E402

# What each device already has, and what to net it against. Written as one
# query rather than a loop of them so "which devices are behind" is a single
# answer taken at one instant.
STALE_DEVICES = """
SELECT d.device_id,
       d.device_type,
       s.label AS site_label,
       max(dr.interval_start AT TIME ZONE 'Asia/Dhaka')::date AS newest,
       CASE
           WHEN d.device_type = 'inverter' THEN inv.ac_capacity_kw
           -- The point's whole fleet, summed over DISTINCT inverters: one
           -- inverter can drive several arrays, so summing per array would
           -- double-count its clipping ceiling.
           ELSE (
               SELECT sum(i2.ac_capacity_kw)
               FROM inverter_spec i2
               JOIN device inv2 ON inv2.device_id = i2.device_id
               JOIN meter_spec pms ON pms.device_id = inv2.parent_device_id
               WHERE pms.billing_point_id = ms.billing_point_id
                 AND inv2.removed_at IS NULL
           )
       END AS capacity_kw
FROM device d
JOIN site s ON s.site_id = d.site_id
LEFT JOIN device_reading dr ON dr.device_id = d.device_id
LEFT JOIN inverter_spec inv ON inv.device_id = d.device_id
LEFT JOIN meter_spec ms ON ms.device_id = d.device_id
WHERE d.reports_telemetry
  AND d.removed_at IS NULL
GROUP BY d.device_id, d.device_type, s.label, inv.ac_capacity_kw,
         ms.billing_point_id
HAVING max(dr.interval_start) IS NOT NULL
ORDER BY s.label, d.device_type
"""


async def main(dry_run: bool) -> int:
    through = date.today() - timedelta(days=1)
    conn = await asyncpg.connect(database_url())
    try:
        rows = await conn.fetch(STALE_DEVICES)
        behind = [r for r in rows if r["newest"] < through]
        if not behind:
            print(f"Every reporting device already has readings through {through}.")
            return 0

        print(f"{len(behind)} of {len(rows)} devices are behind {through}.")
        written = 0
        for r in behind:
            # From the last day it has, not the day after: that day is very
            # likely partial (the seed stops mid-day), and the upsert
            # completes it rather than leaving a dent in the series.
            start = r["newest"]
            if dry_run:
                print(f"  would fill {r['site_label']:<14} {r['device_type']:<8} "
                      f"{start} -> {through}")
                continue
            count = await conn.fetchval(
                "SELECT backfill_readings($1, $2, $3, $4)",
                r["device_id"], start, through, r["capacity_kw"],
            )
            written += count
            print(f"  {r['site_label']:<14} {r['device_type']:<8} "
                  f"{start} -> {through}  {count:>5} rows")

        if not dry_run:
            print(f"\n{written} readings written or refreshed.")
            print("Rule 8 kept every frozen, billed or closed period untouched.")
        return 0
    finally:
        await conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run", action="store_true",
        help="report what would be filled without writing anything",
    )
    args = parser.parse_args()
    raise SystemExit(asyncio.run(main(args.dry_run)))
