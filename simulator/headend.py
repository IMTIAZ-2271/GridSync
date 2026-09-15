"""A utility's head-end system, simulated: `python -m simulator --mode headend`.

A real utility does not let a household's meter talk to a billing system. Its
own network -- the head-end -- owns the meters, and delivers their readings on
their behalf. This is that system for DESCO and DPDC, synthetic in exactly one
respect: the readings come from `simulator/profiles.py` instead of hardware.

**It never touches GridSync's database, and nothing ever calls it.** It polls
ingest, so it has no port, and a head-end that was stopped for a day catches up
by being started. Every cycle:

    1. ASK      GET /v1/source/commissions -- what should I be doing?
    2. MATCH    offered and not held          -> activate (or reject the serial)
                held but no longer listed     -> retired, swapped, lapsed: forget
                live but not held             -> key lost: rekey, deliver again
    3. DELIVER  for every meter held, the intervals owed since its watermark

**Its own state is a SQLite file** (`headend_state/<utility>.sqlite`,
gitignored): each meter's device key and the last interval delivered. The key
is saved the moment activation returns it, because it cannot be fetched again.
A restart resumes from the watermark instead of re-sending history; re-sending
would be harmless (ingest answers `duplicate`) but is wasted work.

**Losing that file is survivable.** On a host whose disk does not outlive a
restart (Render's free tier), every live meter comes back "live but not held".
The head-end asks ingest to rekey each one with its own source key and delivers
again from the offer's starting point -- the re-sent batches carry the same
idempotency keys, so ingest replays its answers and stores nothing twice.

**Where delivery starts.** At the first Dhaka midnight of the offer's history
window, or -- when the offer names no window, which a swap does when the
retired meter already covers the connection -- at the start of the interval the
offer was made in. Not the next one: the retired meter left the feed at that
moment and will never report the interval it was removed in, so starting after
it would leave a hole every swap. Nor earlier: those intervals are the retired
meter's, and site_readings sums across devices. Never further back than ingest accepts.
From there it runs unbroken to the last interval that has finished, one batch
per Dhaka day, using the same reading and idempotency scheme as every other
simulator mode (`simulator/client.py`).
"""
from __future__ import annotations

import asyncio
import fnmatch
import json
import sqlite3
import sys
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from itertools import groupby
from pathlib import Path

import httpx

from simulator.client import Device, aligned_intervals, post_batch, reading_for
from simulator.profiles import DHAKA

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE_KEYS = PROJECT_ROOT / "source_keys.json"
DEFAULT_STATE_DIR = PROJECT_ROOT / "headend_state"

#: Seconds between cycles. Offers wait up to 24 hours to be claimed, so this is
#: about how soon a dashboard fills after a meter is installed, not about
#: meeting a deadline.
POLL_SECONDS = 15.0

#: How far back history may start. One day inside ingest's MAX_BACKDATE of 90
#: days, which is measured to the minute: an offer's window starts at a Dhaka
#: midnight 90 days ago, and the first hours of that day would be refused.
HISTORY_REACH = timedelta(days=89)

#: Backoff when ingest cannot be reached at all.
BACKOFF_START = 5.0
BACKOFF_MAX = 300.0

EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


class SourceAuthError(RuntimeError):
    """The head-end's own credential was refused. Nothing to retry."""


@dataclass
class CycleReport:
    activated: int = 0
    rejected_offers: int = 0
    stopped: int = 0
    went_live: int = 0
    rekeyed: int = 0
    #: Live meters with no key here that a rekey could not recover.
    orphaned: int = 0
    accepted: int = 0
    duplicates: int = 0
    late: int = 0
    rejected: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def eventful(self) -> bool:
        return any((self.activated, self.rejected_offers, self.stopped,
                    self.went_live, self.rekeyed, self.accepted, self.late, self.rejected,
                    self.errors))


# --------------------------------------------------------------------------
# Local state
# --------------------------------------------------------------------------

class HeadEndState:
    """The head-end's own records: which meters it holds keys for, and how far
    it has delivered each. Plain sqlite3 -- one writer, tiny, synchronous."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path)
        self._db.row_factory = sqlite3.Row
        self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS meter (
                device_id        TEXT PRIMARY KEY,
                commissioning_id TEXT NOT NULL,
                serial_no        TEXT NOT NULL,
                device_key       TEXT NOT NULL,
                interval_minutes INTEGER NOT NULL,
                deliver_from     TEXT NOT NULL,
                watermark        TEXT
            )
            """
        )
        self._db.commit()

    def held(self) -> dict[str, sqlite3.Row]:
        return {r["device_id"]: r for r in self._db.execute("SELECT * FROM meter")}

    def hold(self, *, device_id: str, commissioning_id: str, serial_no: str,
             device_key: str, interval_minutes: int, deliver_from: datetime) -> None:
        self._db.execute(
            """
            INSERT INTO meter (device_id, commissioning_id, serial_no, device_key,
                               interval_minutes, deliver_from, watermark)
            VALUES (?, ?, ?, ?, ?, ?, NULL)
            ON CONFLICT (device_id) DO UPDATE SET
                commissioning_id = excluded.commissioning_id,
                device_key       = excluded.device_key,
                interval_minutes = excluded.interval_minutes,
                deliver_from     = excluded.deliver_from
            """,
            (device_id, commissioning_id, serial_no, device_key,
             interval_minutes, deliver_from.isoformat()),
        )
        self._db.commit()

    def advance(self, device_id: str, watermark: datetime) -> None:
        self._db.execute(
            "UPDATE meter SET watermark = ? WHERE device_id = ?",
            (watermark.isoformat(), device_id),
        )
        self._db.commit()

    def forget(self, device_id: str) -> None:
        self._db.execute("DELETE FROM meter WHERE device_id = ?", (device_id,))
        self._db.commit()

    def close(self) -> None:
        self._db.close()


# --------------------------------------------------------------------------
# Time
# --------------------------------------------------------------------------

def _align_down(ts: datetime, step: timedelta) -> datetime:
    return EPOCH + ((ts - EPOCH) // step) * step


def _align_up(ts: datetime, step: timedelta) -> datetime:
    offset = (ts - EPOCH) % step
    return ts if offset == timedelta(0) else ts + (step - offset)


def _last_elapsed(now: datetime, step: timedelta) -> datetime:
    """Start of the most recent interval that has fully finished. A meter
    reporting the interval it is still in would be sending a partial figure."""
    return EPOCH + ((now - EPOCH) // step) * step - step


def _dhaka_midnight(day: date) -> datetime:
    return datetime.combine(day, time(0), tzinfo=DHAKA).astimezone(timezone.utc)


def _deliver_from(commission: dict) -> datetime:
    step = timedelta(minutes=commission["interval_minutes"])
    if commission["backfill_from"]:
        return _dhaka_midnight(date.fromisoformat(commission["backfill_from"]))
    return _align_down(datetime.fromisoformat(commission["offered_at"]), step)


# --------------------------------------------------------------------------
# The head-end
# --------------------------------------------------------------------------

class HeadEnd:
    """One utility's head-end. Given an httpx client already pointed at ingest,
    so the same object runs against a real server or an in-process app."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        source_id: str,
        source_key: str,
        state_path: Path,
        seed: int = 42,
        reject_serials: Iterable[str] = (),
        log: Callable[[str], None] = print,
    ):
        self.client = client
        self.headers = {"X-Source-Id": source_id, "X-Source-Key": source_key}
        self.state = HeadEndState(state_path)
        self.seed = seed
        self.reject_serials = list(reject_serials)
        self.log = log
        #: Live meters with no key here, already reported -- said once, not
        #: every fifteen seconds.
        self._orphans_reported: set[str] = set()

    def held_device_ids(self) -> set[str]:
        return set(self.state.held())

    # -- one cycle ---------------------------------------------------------

    async def cycle(self, now: datetime | None = None) -> CycleReport:
        now = now or datetime.now(timezone.utc)
        report = CycleReport()

        response = await self.client.get("/v1/source/commissions", headers=self.headers)
        if response.status_code == 401:
            raise SourceAuthError("ingest refused this head-end's source key")
        response.raise_for_status()
        listed = {c["device_id"]: c for c in response.json()["commissions"]}

        held = self.state.held()
        for device_id, row in held.items():
            listed_now = listed.get(device_id)
            # Not listed: retired, swapped, lapsed or refused. A different
            # handshake for the same device: the old key belongs to an attempt
            # that ended, so it is dropped and the new offer claimed below.
            if listed_now is None or listed_now["commissioning_id"] != row["commissioning_id"]:
                self.state.forget(device_id)
                report.stopped += 1
                self.log(f"  {row['serial_no']:<22} no longer in the feed -> stopped")

        held = self.state.held()
        for device_id, commission in listed.items():
            if device_id not in held:
                await self._claim(commission, report)

        held = self.state.held()
        for device_id, row in held.items():
            await self._deliver(row, listed[device_id], now, report)

        return report

    async def _claim(self, commission: dict, report: CycleReport) -> None:
        serial = commission["serial_no"]
        cid = commission["commissioning_id"]

        if commission["status"] == "live":
            await self._rekey(commission, report)
            return

        if commission["status"] == "offered" and any(
            fnmatch.fnmatch(serial, pattern) for pattern in self.reject_serials
        ):
            response = await self.client.post(
                f"/v1/source/commissions/{cid}/reject",
                headers=self.headers,
                json={"detail": f"serial {serial} is not in this utility's inventory"},
            )
            if response.status_code == 200:
                report.rejected_offers += 1
                self.log(f"  {serial:<22} not in inventory -> rejected")
            else:
                report.errors.append(f"reject {serial}: HTTP {response.status_code}")
            return

        response = await self.client.post(
            f"/v1/source/commissions/{cid}/activate",
            headers=self.headers,
            json={"serial_no": serial},
        )
        if response.status_code != 200:
            report.errors.append(
                f"activate {serial}: HTTP {response.status_code} {response.text[:200]}"
            )
            return
        activation = response.json()
        # Saved before anything else: this key is shown once.
        self.state.hold(
            device_id=commission["device_id"],
            commissioning_id=cid,
            serial_no=serial,
            device_key=activation["device_key"],
            interval_minutes=activation["interval_minutes"],
            deliver_from=_deliver_from(commission),
        )
        report.activated += 1
        window = (
            f"history {commission['backfill_from']} -> {commission['backfill_to']}"
            if commission["backfill_from"] else "no history owed"
        )
        self.log(
            f"  {serial:<22} activated (key {activation['activation_count']}), {window}"
        )

    async def _rekey(self, commission: dict, report: CycleReport) -> None:
        """A live meter this head-end owns but holds no key for: its state was
        lost. Ask for a fresh key and deliver again from the offer's start."""
        serial = commission["serial_no"]
        cid = commission["commissioning_id"]
        response = await self.client.post(
            f"/v1/source/commissions/{cid}/rekey",
            headers=self.headers,
            json={"serial_no": serial},
        )
        if response.status_code != 200:
            report.orphaned += 1
            if cid not in self._orphans_reported:
                self._orphans_reported.add(cid)
                self.log(
                    f"  {serial:<22} live, no key held, rekey refused "
                    f"(HTTP {response.status_code}) -> GridSync must offer it again"
                )
            return
        rekeyed = response.json()
        # Saved before anything else, exactly as an activation's key is.
        self.state.hold(
            device_id=commission["device_id"],
            commissioning_id=cid,
            serial_no=serial,
            device_key=rekeyed["device_key"],
            interval_minutes=rekeyed["interval_minutes"],
            deliver_from=_deliver_from(commission),
        )
        report.rekeyed += 1
        self.log(f"  {serial:<22} live, key lost -> rekeyed, delivering again")

    async def _deliver(self, row: sqlite3.Row, commission: dict, now: datetime,
                       report: CycleReport) -> None:
        minutes = row["interval_minutes"]
        step = timedelta(minutes=minutes)
        start = (
            datetime.fromisoformat(row["watermark"]) + step
            if row["watermark"] else datetime.fromisoformat(row["deliver_from"])
        )
        start = max(start, _align_up(now - HISTORY_REACH, step))
        end = _last_elapsed(now, step)
        if end < start:
            return

        device = Device(row["device_id"], {
            "device_key": row["device_key"],
            "serial_no": row["serial_no"],
            "device_type": commission["device_type"],
            "interval_minutes": minutes,
            "site_id": "",
            "site_label": "",
            "meter_flow": commission["meter_flow"],
        })
        # Rule 6: only a bidirectional meter is netted against solar, and only
        # against the panels on its own connection -- which is what the hint is.
        if device.bidirectional:
            device.point_capacity_kw = Decimal(
                commission["simulation"]["point_solar_capacity_kw"]
            )

        intervals = aligned_intervals(start, end + step, minutes)
        for day, group in groupby(intervals, key=lambda ts: ts.astimezone(DHAKA).date()):
            batch = list(group)
            readings = [reading_for(device, ts, self.seed) for ts in batch]
            try:
                # Scoped to the handshake: a re-offered device re-sends intervals
                # it delivered before, and those must reach ingest as a new
                # delivery (duplicates of held readings), not as a replay of a
                # batch that belonged to a handshake that has ended.
                result = await post_batch(self.client, "", device, readings, self.seed,
                                          key_scope=row["commissioning_id"])
            except httpx.HTTPError as exc:
                report.errors.append(f"{device.serial_no}: {exc!r}")
                return  # retried from the same watermark next cycle

            if "error" in result:
                if result.get("status") == 401:
                    # Revoked: the meter was retired or its handshake failed.
                    self.state.forget(device.device_id)
                    report.stopped += 1
                    self.log(f"  {device.serial_no:<22} key refused -> stopped")
                else:
                    report.errors.append(f"{device.serial_no}: {result['error']}")
                return

            report.accepted += result["accepted"]
            report.duplicates += result["duplicates"]
            report.late += result["late"]
            report.rejected += result["rejected"]
            if result.get("went_live"):
                report.went_live += 1
                self.log(f"  {device.serial_no:<22} first batch accepted -> LIVE")
            # Advanced past rejected and late readings too: those are answers,
            # not failures, and re-sending them would get the same answer.
            self.state.advance(device.device_id, batch[-1])

    # -- forever -----------------------------------------------------------

    async def run(self, poll_seconds: float = POLL_SECONDS) -> None:
        failures = 0
        while True:
            try:
                report = await self.cycle()
                failures = 0
                if report.eventful:
                    self.log(
                        f"  cycle: {report.accepted} accepted, {report.duplicates} "
                        f"duplicate, {report.late} late, {report.rejected} rejected; "
                        f"{len(self.state.held())} meter(s) held"
                    )
                for error in report.errors[:5]:
                    self.log(f"    ! {error}")
                delay = poll_seconds
            except httpx.HTTPError as exc:
                failures += 1
                delay = min(BACKOFF_START * 2 ** (failures - 1), BACKOFF_MAX)
                self.log(f"  ingest unreachable ({exc!r}); retrying in {delay:.0f}s")
            await asyncio.sleep(delay)


# --------------------------------------------------------------------------
# Entry point, called from simulator/__main__.py
# --------------------------------------------------------------------------

def load_sources(keyfile: Path, utility: str) -> dict[str, dict]:
    if not keyfile.exists():
        sys.exit(f"no head-end keys at {keyfile}\nRun: python -m scripts.issue_source_keys")
    sources = json.loads(keyfile.read_text(encoding="utf-8"))["sources"]
    if utility.lower() == "all":
        return sources
    match = {code: s for code, s in sources.items() if code.lower() == utility.lower()}
    if not match:
        sys.exit(f"no head-end key for '{utility}' in {keyfile} (have: {', '.join(sources)})")
    return match


async def run_headends(*, ingest: str, keyfile: Path, utility: str, state_dir: Path,
                       seed: int, poll_seconds: float, reject_serials: list[str]) -> None:
    sources = load_sources(keyfile, utility)
    async with httpx.AsyncClient(base_url=ingest) as client:
        headends = []
        for code, source in sources.items():
            prefix = f"[{code}]"
            state_path = state_dir / f"{code}.sqlite"
            # Flushed: a quiet cycle logs nothing, so when output is redirected
            # to a file this line would otherwise sit in the buffer and a
            # running head-end would look like one that never started.
            print(f"{prefix} head-end -> {ingest}  (state: {state_path})", flush=True)
            headends.append(HeadEnd(
                client,
                source_id=source["source_id"],
                source_key=source["source_key"],
                state_path=state_path,
                seed=seed,
                reject_serials=reject_serials,
                log=lambda line, p=prefix: print(f"{p}{line}", flush=True),
            ))
        try:
            await asyncio.gather(*(h.run(poll_seconds) for h in headends))
        except SourceAuthError as exc:
            sys.exit(f"{exc}. Re-issue with: python -m scripts.issue_source_keys")
