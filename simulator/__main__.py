"""Synthetic device traffic, over HTTP, exactly as real hardware would send it.

    python -m simulator --mode backfill --days 30
    python -m simulator --mode accelerated --hours 48
    python -m simulator --mode realtime
    python -m simulator --mode headend --utility DESCO

**It never writes to the database.** Everything goes through
`POST /v1/ingest/readings` with an `X-Device-Key` and an `Idempotency-Key`, so
running it exercises authentication, validation, rule 6, rule 8 and the
idempotency contract rather than bypassing all of them -- which is the entire
reason the simulator exists rather than a second seed script.

Four modes. The first three drive every device in a keyfile:

* **backfill** -- months of history in seconds. One batch per device per day.
* **accelerated** -- one wall-clock second is one simulated hour. Enough to
  watch a dashboard fill in without waiting for a real day.
* **realtime** -- posts each interval as it completes, forever. What a device
  actually does.

The fourth is different in kind:

* **headend** -- a utility's head-end system (`simulator/headend.py`). Reads
  no device keyfile: it learns which meters exist from ingest's commissioning
  feed, receives each key through the handshake, uploads the offered history
  and then delivers live, forever. Keys come from `source_keys.json`, written
  by `python -m scripts.issue_source_keys`. This is how a meter registered
  with COMMISSIONING_ENABLED on gets its readings.

Reproducible by construction: `--seed` feeds a hash keyed on
(seed, device, interval), so the same interval always yields the same reading
however many times it is sent, and a retry is a retry rather than new data.
Device keys for the first three modes come from the keyfile written by
`python -m scripts.issue_device_keys`, which skips devices a head-end holds.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import httpx

from simulator.client import Device, aligned_intervals, post_batch, reading_for
from simulator.headend import (
    DEFAULT_SOURCE_KEYS,
    DEFAULT_STATE_DIR,
    POLL_SECONDS,
    run_headends,
)
from simulator.profiles import DHAKA

DEFAULT_KEYFILE = Path(__file__).resolve().parents[1] / "device_keys.json"
DEFAULT_INGEST = "http://127.0.0.1:8100"

#: One batch per device per simulated day. A real device batches what it has
#: buffered; a day is the unit that keeps a backfill run to a sane number of
#: requests without any single one being huge.
BATCH_INTERVALS = 48


def link_solar(devices: list[Device]) -> None:
    """Tell each meter how much solar sits behind its own connection.

    Per billing point, never per site. A household with two connections has two
    meters and two credit balances, and netting one meter against the site's
    whole fleet of arrays would credit one connection for the other's export --
    the error rule 3 exists to prevent.
    """
    solar_by_point: dict[str, Decimal] = defaultdict(Decimal)
    for d in devices:
        if d.is_inverter and d.billing_point_id:
            solar_by_point[d.billing_point_id] += d.ac_capacity_kw
    for d in devices:
        if not d.is_inverter and d.billing_point_id:
            d.point_capacity_kw = solar_by_point.get(d.billing_point_id, Decimal("0"))


class Totals:
    def __init__(self) -> None:
        self.accepted = self.duplicates = self.late = self.rejected = 0
        self.errors: list[str] = []

    def add(self, result: dict) -> None:
        if "error" in result:
            self.errors.append(result["error"])
            return
        self.accepted += result.get("accepted", 0)
        self.duplicates += result.get("duplicates", 0)
        self.late += result.get("late", 0)
        self.rejected += result.get("rejected", 0)
        for o in result.get("outcomes", []):
            if o.get("outcome") == "rejected" and o.get("detail"):
                detail = o["detail"]
                if detail not in self.errors:
                    self.errors.append(detail)

    def report(self, label: str) -> None:
        print(
            f"{label}: {self.accepted} accepted, {self.duplicates} duplicate, "
            f"{self.late} late, {self.rejected} rejected"
        )
        for e in self.errors[:5]:
            print(f"    ! {e}")


async def run_backfill(
    devices: list[Device], base: str, days: int, seed: int, concurrency: int
) -> Totals:
    """Months in seconds. Ends at yesterday's local midnight.

    Stopping at yesterday matches every other window in this system --
    device_health, the nightly rollups, the eligibility test. Today is still
    accumulating intervals, and a part-day dragged into a daily figure reads as
    a collapse in output.
    """
    end = datetime.now(DHAKA).replace(hour=0, minute=0, second=0, microsecond=0)
    start = end - timedelta(days=days)
    totals = Totals()
    limiter = asyncio.Semaphore(concurrency)

    async with httpx.AsyncClient() as client:
        async def one_day(d: Device, day_start: datetime) -> None:
            intervals = aligned_intervals(
                day_start, day_start + timedelta(days=1), d.interval_minutes
            )
            if not intervals:
                return
            readings = [reading_for(d, ts, seed) for ts in intervals]
            async with limiter:
                totals.add(await post_batch(client, base, d, readings, seed))

        tasks = []
        for d in devices:
            cursor = start
            while cursor < end:
                tasks.append(one_day(d, cursor))
                cursor += timedelta(days=1)
        print(f"backfill: {len(devices)} device(s) x {days} day(s) = {len(tasks)} batches")
        await asyncio.gather(*tasks)
    return totals


async def run_accelerated(
    devices: list[Device], base: str, hours: int, seed: int, tick: float
) -> Totals:
    """One wall-clock second is one simulated hour, ending at now.

    Posts in simulated-hour steps so a dashboard visibly fills in, which is the
    point of the mode -- a backfill run produces the same rows instantly and
    shows you nothing happening.
    """
    end = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    start = end - timedelta(hours=hours)
    totals = Totals()

    async with httpx.AsyncClient() as client:
        cursor = start
        while cursor < end:
            nxt = cursor + timedelta(hours=1)
            for d in devices:
                intervals = aligned_intervals(cursor, nxt, d.interval_minutes)
                if not intervals:
                    continue
                readings = [reading_for(d, ts, seed) for ts in intervals]
                totals.add(await post_batch(client, base, d, readings, seed))
            local = cursor.astimezone(DHAKA).strftime("%Y-%m-%d %H:%M")
            print(f"  {local} Dhaka -> {totals.accepted} accepted so far", flush=True)
            cursor = nxt
            await asyncio.sleep(tick)
    return totals


async def run_realtime(devices: list[Device], base: str, seed: int) -> Totals:
    """Post each interval as it completes, forever.

    Waits for the interval to be *over* before sending it. A device reporting
    the half hour it is still in would be sending a partial figure, and the
    ingest service's clock-skew guard would refuse anything further ahead.
    """
    totals = Totals()
    print("realtime: Ctrl-C to stop")
    async with httpx.AsyncClient() as client:
        sent: set[tuple[str, datetime]] = set()
        while True:
            now = datetime.now(timezone.utc)
            for d in devices:
                step = timedelta(minutes=d.interval_minutes)
                # The most recent interval that has fully elapsed.
                epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
                elapsed = ((now - epoch) // step) * step + epoch - step
                if (d.device_id, elapsed) in sent:
                    continue
                result = await post_batch(
                    client, base, d, [reading_for(d, elapsed, seed)], seed
                )
                totals.add(result)
                sent.add((d.device_id, elapsed))
                local = elapsed.astimezone(DHAKA).strftime("%H:%M")
                print(f"  {d.serial_no} {local} -> {result}", flush=True)
            await asyncio.sleep(20)


def load_devices(keyfile: Path, site: str | None) -> list[Device]:
    if not keyfile.exists():
        sys.exit(
            f"no keyfile at {keyfile}\n"
            "Run: python -m scripts.issue_device_keys"
        )
    raw = json.loads(keyfile.read_text(encoding="utf-8"))["devices"]
    devices = [Device(did, spec) for did, spec in raw.items()]
    if site:
        needle = site.lower()
        devices = [
            d for d in devices
            if needle in d.site_label.lower() or needle == d.site_id
        ]
    link_solar(devices)
    return devices


def main() -> None:
    parser = argparse.ArgumentParser(prog="simulator")
    parser.add_argument(
        "--mode", choices=("backfill", "accelerated", "realtime", "headend"),
        default="backfill",
    )
    parser.add_argument("--days", type=int, default=30, help="backfill mode")
    parser.add_argument("--hours", type=int, default=24, help="accelerated mode")
    parser.add_argument(
        "--tick", type=float, default=1.0,
        help="accelerated mode: wall-clock seconds per simulated hour",
    )
    parser.add_argument("--ingest", default=DEFAULT_INGEST)
    parser.add_argument("--keyfile", type=Path, default=DEFAULT_KEYFILE)
    parser.add_argument("--site", help="only devices whose site label contains this")
    parser.add_argument(
        "--seed", type=int, default=42,
        help="runs with the same seed produce identical readings",
    )
    parser.add_argument("--concurrency", type=int, default=8)

    headend = parser.add_argument_group("headend mode")
    headend.add_argument(
        "--utility", help="distribution company code (DESCO, DPDC) or 'all'"
    )
    headend.add_argument("--source-keys", type=Path, default=DEFAULT_SOURCE_KEYS)
    headend.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR)
    headend.add_argument("--poll", type=float, default=POLL_SECONDS,
                         help="seconds between cycles")
    headend.add_argument(
        "--reject-serial", action="append", default=[], metavar="PATTERN",
        help="refuse offered meters whose serial matches (glob; repeatable)",
    )
    args = parser.parse_args()

    if args.mode == "headend":
        if not args.utility:
            parser.error("--mode headend needs --utility (DESCO, DPDC or all)")
        try:
            asyncio.run(run_headends(
                ingest=args.ingest, keyfile=args.source_keys, utility=args.utility,
                state_dir=args.state_dir, seed=args.seed, poll_seconds=args.poll,
                reject_serials=args.reject_serial,
            ))
        except KeyboardInterrupt:
            print("\nstopped")
        return

    devices = load_devices(args.keyfile, args.site)
    if not devices:
        sys.exit("no devices matched")
    meters = sum(1 for d in devices if not d.is_inverter)
    print(
        f"{len(devices)} device(s): {meters} meter(s), "
        f"{len(devices) - meters} inverter(s) -> {args.ingest}"
    )

    if args.mode == "backfill":
        totals = asyncio.run(
            run_backfill(devices, args.ingest, args.days, args.seed, args.concurrency)
        )
    elif args.mode == "accelerated":
        totals = asyncio.run(
            run_accelerated(devices, args.ingest, args.hours, args.seed, args.tick)
        )
    else:
        try:
            totals = asyncio.run(run_realtime(devices, args.ingest, args.seed))
        except KeyboardInterrupt:
            print("\nstopped")
            return
    totals.report(args.mode)


if __name__ == "__main__":
    main()
