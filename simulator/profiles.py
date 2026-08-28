"""The generation and consumption model, as pure functions of time.

Kept separate from the client so it can be read, tested and plotted without a
database or a server. Every function here is deterministic given its seed: a
run must be reproducible, or a difference between two runs tells you nothing.

**The curves match `db/sql/service/backfill.sql` on purpose.** The simulator is
not a second opinion about what a Dhaka household does -- it is the same model
delivered over HTTP instead of written straight into the table, so a chart
cannot develop a visible seam on the day the estate switched from backfilled
history to live telemetry. If one changes, the other has to.

    consumption   0.15 kWh per half hour baseline,
                  +0.25 in the 07:00-09:00 morning peak,
                  +0.45 in the 18:00-22:00 evening peak,
                  plus a little noise
    generation    half-sine peaking at noon, zero outside 06:00-18:00,
                  scaled by the inverter's AC rating

Rule 6 decides who may report which of them: an inverter reports generation and
never the import/export split, a meter reports the split and never generation.
That is enforced by the ingest service and again by `reading_role_guard`; this
module only produces the numbers.
"""
from __future__ import annotations

import hashlib
import math
import random
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

#: Every calendar decision in this project is made in Asia/Dhaka, including
#: which half-hour of the local day an interval falls in. Reading the hour off
#: a UTC timestamp would put the evening peak in the middle of the afternoon.
DHAKA = ZoneInfo("Asia/Dhaka")

QUANT = Decimal("0.0001")


def _rng(seed: int, device_id: str, ts: datetime) -> random.Random:
    """A generator that depends only on (seed, device, interval).

    Deliberately not one long stream. A device that is restarted, or that
    re-sends an interval, must produce the SAME number it produced before --
    otherwise a retry would look like a different reading, and the whole
    idempotency story would be a lie at the level of the data even while the
    primary key held.
    """
    digest = hashlib.sha256(
        f"{seed}:{device_id}:{int(ts.timestamp())}".encode()
    ).digest()
    return random.Random(int.from_bytes(digest[:8], "big"))


def local_hour(ts: datetime) -> float:
    """Hour of the Dhaka day, with minutes as a fraction."""
    local = ts.astimezone(DHAKA)
    return local.hour + local.minute / 60.0


def consumption_kwh(ts: datetime, device_id: str, seed: int) -> Decimal:
    """What the household drew in this interval, before any solar."""
    hr = local_hour(ts)
    base = 0.15
    if 7 <= hr < 9:
        base += 0.25
    if 18 <= hr < 22:
        base += 0.45
    base += _rng(seed, device_id, ts).random() * 0.06
    return Decimal(repr(base)).quantize(QUANT)


def generation_kwh(
    ts: datetime, device_id: str, seed: int, capacity_kw: Decimal
) -> Decimal:
    """What an array of this AC rating made in this interval.

    Zero outside daylight rather than a small number: an inverter that is not
    generating reports zero, and a trickle at midnight is the kind of detail
    that makes a demo dataset obviously synthetic.
    """
    if capacity_kw <= 0:
        return Decimal("0.0000")
    hr = local_hour(ts)
    if not (6 < hr < 18):
        return Decimal("0.0000")
    # The 0.5 converts kW to kWh over a half hour; the noise band is the
    # weather. Same shape and same constants as backfill.sql.
    shape = math.sin(math.pi * (hr - 6) / 12.0)
    noise = 0.82 + _rng(seed, device_id, ts).random() * 0.18
    value = float(capacity_kw) * 0.5 * shape * noise
    return Decimal(repr(value)).quantize(QUANT)


def meter_split(
    consumption: Decimal, generation: Decimal, bidirectional: bool
) -> tuple[Decimal, Decimal | None]:
    """Rule 6: what the meter at the grid boundary actually sees.

    `self_consumption = generation - export`, so what crosses the boundary is
    the difference between the two, and which direction it crosses in is the
    sign. A **unidirectional** meter cannot see the export half at all -- it
    returns None for it, and reports only what was drawn. That is not a
    simplification: it is why a household needs the meter swapped before net
    metering can credit anything.
    """
    if not bidirectional:
        return max(Decimal("0"), consumption - generation).quantize(QUANT), None
    imported = max(Decimal("0"), consumption - generation).quantize(QUANT)
    exported = max(Decimal("0"), generation - consumption).quantize(QUANT)
    return imported, exported


def voltage(ts: datetime, device_id: str, seed: int) -> Decimal:
    r = _rng(seed, device_id + ":v", ts)
    return Decimal(repr(228 + r.random() * 8)).quantize(Decimal("0.01"))


def frequency(ts: datetime, device_id: str, seed: int) -> Decimal:
    r = _rng(seed, device_id + ":f", ts)
    return Decimal(repr(49.9 + r.random() * 0.2)).quantize(Decimal("0.001"))
