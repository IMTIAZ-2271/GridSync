"""What every simulator mode shares: a device, its reading, and delivering it.

Moved out of `simulator/__main__.py` unchanged when the head-end mode arrived
(`simulator/headend.py`), so the keyfile modes and the head-end produce the
same numbers from the same curves and deliver them with the same idempotency
scheme. A reading re-sent by either is the same reading.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import httpx

from simulator.profiles import (
    consumption_kwh,
    frequency,
    generation_kwh,
    meter_split,
    voltage,
)


class Device:
    """One device from the keyfile, plus the solar it is netted against."""

    def __init__(self, device_id: str, spec: dict):
        self.device_id = device_id
        self.key = spec["device_key"]
        self.serial_no = spec["serial_no"]
        self.device_type = spec["device_type"]
        self.interval_minutes = int(spec["interval_minutes"])
        self.site_id = spec["site_id"]
        self.site_label = spec["site_label"]
        self.billing_point_id = spec.get("billing_point_id")
        self.meter_flow = spec.get("meter_flow")
        self.ac_capacity_kw = Decimal(spec.get("ac_capacity_kw") or "0")
        #: Filled in by `link_solar`: the AC capacity of every inverter on this
        #: meter's own connection. Zero for a meter with no solar behind it.
        self.point_capacity_kw = Decimal("0")

    @property
    def is_inverter(self) -> bool:
        return self.device_type == "inverter"

    @property
    def bidirectional(self) -> bool:
        return self.meter_flow == "bidirectional"


def reading_for(d: Device, ts: datetime, seed: int) -> dict:
    """One interval, in the shape the ingest API accepts.

    Energy values are serialized as **strings**. Rule 5 forbids energy through
    a float, and a JSON number is a double by the time it reaches the server --
    so the string is not fussiness, it is the only lossless way to send a
    NUMERIC over JSON.
    """
    body: dict = {
        "interval_start": ts.isoformat(),
        "interval_minutes": d.interval_minutes,
        "frequency_avg": str(frequency(ts, d.device_id, seed)),
    }

    if d.is_inverter:
        # Rule 6: generation only. Sending an import figure here is refused by
        # the ingest service, and rightly -- an inverter cannot see the grid
        # boundary.
        body["generation_kwh"] = str(
            generation_kwh(ts, d.device_id, seed, d.ac_capacity_kw)
        )
        return body

    consumption = consumption_kwh(ts, d.device_id, seed)
    generation = generation_kwh(ts, d.device_id + ":solar", seed, d.point_capacity_kw)
    imported, exported = meter_split(consumption, generation, d.bidirectional)
    body["import_kwh"] = str(imported)
    if exported is not None:
        body["export_kwh"] = str(exported)
    body["voltage_avg"] = str(voltage(ts, d.device_id, seed))
    return body


async def post_batch(
    client: httpx.AsyncClient, base: str, d: Device, readings: list[dict], seed: int,
    key_scope: str = "",
) -> dict:
    """Deliver one batch, with an Idempotency-Key derived from its contents.

    Derived rather than random, on purpose. A device that crashes mid-delivery
    and retries must present the SAME key, or the server has no way to know it
    is the same batch and rule 4's protection never engages. The key is a
    uuid5 over (device, first interval, count), which is stable across process
    restarts and unique per batch.

    `key_scope` narrows that to one delivery attempt. The head-end passes the
    commissioning id: when the same device is offered again, the batches it
    re-sends are a new delivery, not a retry of the old one. Empty for the
    keyfile modes, whose keys are therefore unchanged.
    """
    scope = f":{key_scope}" if key_scope else ""
    key = uuid.uuid5(
        uuid.NAMESPACE_URL,
        f"gridsync:{seed}:{d.device_id}:{readings[0]['interval_start']}:{len(readings)}{scope}",
    )
    response = await client.post(
        f"{base}/v1/ingest/readings",
        headers={"X-Device-Key": d.key, "Idempotency-Key": str(key)},
        json={"device_id": d.device_id, "readings": readings},
        timeout=60.0,
    )
    if response.status_code >= 400:
        return {
            "error": f"HTTP {response.status_code}: {response.text[:300]}",
            "status": response.status_code,
        }
    return response.json()


def aligned_intervals(
    start: datetime, end: datetime, minutes: int
) -> list[datetime]:
    """Every interval boundary in [start, end), aligned to the minute grid.

    Alignment matters: `reading_aligned` refuses anything that straddles a TOU
    boundary, and the ingest service refuses it first with a better message.
    """
    step = timedelta(minutes=minutes)
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    offset = (start - epoch) % step
    cursor = start if offset == timedelta(0) else start + (step - offset)
    out = []
    while cursor < end:
        out.append(cursor)
        cursor += step
    return out
