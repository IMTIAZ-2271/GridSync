"""Device ingest: the HTTP door real hardware pushes readings through.

The fourth process, and the one CLAUDE.md has listed as the largest remaining
gap since the project started. Until now every reading in the database was
written by `backfill_readings()` -- a Postgres function calling a sine curve --
which is why device health could only ever be *coverage of rows somebody
generated* rather than evidence that a device is alive.

**A device never touches the database directly.** It authenticates with a key,
posts a batch of readings, and is told what happened to each one. Everything
this service enforces is enforced again by a constraint underneath it, because
a service that is careful and a database that refuses are not the same
guarantee (rule 4).

What it validates, and why each one is here rather than left to a trigger:

* **Interval alignment.** `reading_aligned` already refuses a reading that
  straddles a TOU boundary, but a device deserves a sentence naming the
  interval it got wrong rather than a constraint violation.
* **Clock skew.** A reading dated in the future is a device with a bad clock,
  not a reading. Accepting it would put a row in a partition ahead of the
  billing calendar and quietly break the next month's coverage figure.
* **Rule 6 -- who may report what.** An inverter reports `generation_kwh`; only
  a bidirectional meter at the grid boundary can know the import/export split.
  `reading_role_guard` enforces this, and answering 422 with the reason is
  kinder than surfacing its exception.
* **Rule 8 -- never write into a closed period.** A reading for a frozen,
  billed or closed period is diverted to `late_reading` and kept. It is never
  merged into a bill that has already been issued, because a correction is a
  new bill (rule 1), and it is never dropped, because the operator has to be
  able to see what arrived.

Idempotency is by constraint. `ingest_batch.idempotency_key` is UNIQUE, so a
retried batch is recognised and answered with the ORIGINAL outcome rather than
re-applied; `device_reading`'s primary key is `(device_id, interval_start)`, so
even a batch that slips past that writes nothing twice.

Run it with `python -m services.ingest` (port 8100 by default). It opens its
own pool from `services.api.db`, exactly as `services/jobs` does, and never
imports the main API app.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID

import asyncpg
from fastapi import FastAPI, Header, HTTPException, Request
from pydantic import BaseModel, Field, field_validator

from services.api.auth import verify_password
from services.api.db import Conn, create_pool
from services.api.queries import sql

# --------------------------------------------------------------------------
# Policy
# --------------------------------------------------------------------------

#: How far ahead of the server's clock a reading may be dated before it is
#: treated as skew rather than data. One interval's grace, because a device
#: that stamps the START of the interval it is about to finish is not wrong.
MAX_CLOCK_SKEW = timedelta(minutes=90)

#: How far back a device may post without the reading being suspect. Not a
#: rejection -- a device that was offline for a week is exactly what backfill
#: on reconnect is for -- but rule 8 still applies to every interval in it.
MAX_BACKDATE = timedelta(days=90)

#: Readings per request. A device batching a day of 30-minute intervals sends
#: 48; a week is 336. Above this the caller should paginate, and the cap is
#: what stops one request holding a connection for minutes.
MAX_BATCH = 1000


# --------------------------------------------------------------------------
# Wire shapes
# --------------------------------------------------------------------------

class Reading(BaseModel):
    """One interval, as a device reports it.

    Energy fields are `Decimal` and parsed from strings on the wire. Rule 5
    forbids FLOAT for energy, and a JSON number would already have been through
    a double by the time pydantic saw it -- so the device is expected to send
    `"1.2340"`, not `1.234`.
    """

    interval_start: datetime
    interval_minutes: Literal[15, 30, 60] = 30
    import_kwh: Decimal | None = Field(default=None, ge=0)
    export_kwh: Decimal | None = Field(default=None, ge=0)
    generation_kwh: Decimal | None = Field(default=None, ge=0)
    voltage_avg: Decimal | None = Field(default=None, ge=0, le=1000)
    frequency_avg: Decimal | None = Field(default=None, ge=0, le=100)
    dc_voltage_avg: Decimal | None = Field(default=None, ge=0, le=2000)

    @field_validator("interval_start")
    @classmethod
    def _must_be_aware(cls, v: datetime) -> datetime:
        """A naive timestamp is ambiguous, and this system spans zones.

        Refused rather than assumed-UTC: guessing puts the reading in the wrong
        half-hour bucket six hours from where it belongs, and nothing
        downstream would ever flag it.
        """
        if v.tzinfo is None:
            raise ValueError(
                "interval_start must carry a UTC offset, e.g. "
                "2026-08-28T10:00:00+06:00"
            )
        return v


class ReadingBatch(BaseModel):
    device_id: UUID
    readings: list[Reading] = Field(min_length=1, max_length=MAX_BATCH)


class ReadingOutcome(BaseModel):
    interval_start: datetime
    outcome: Literal["accepted", "duplicate", "late", "rejected"]
    #: Present for `late` and `rejected`. The device cannot fix a late reading
    #: and can fix a rejected one, which is why they are different outcomes.
    detail: str | None = None


class BatchResult(BaseModel):
    batch_id: UUID
    device_id: UUID
    #: True when this Idempotency-Key had already been delivered. The counts
    #: are the ORIGINAL batch's, and nothing was written this time.
    replayed: bool
    reading_count: int
    accepted: int
    duplicates: int
    late: int
    rejected: int
    #: Omitted on a replay -- the per-reading outcomes of the original batch
    #: are not retained, only its counts.
    outcomes: list[ReadingOutcome] = Field(default_factory=list)


# --------------------------------------------------------------------------
# App
# --------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.pool = await create_pool()
    try:
        yield
    finally:
        await app.state.pool.close()


app = FastAPI(
    title="GridSync device ingest",
    version="1.0.0",
    lifespan=lifespan,
    description=__doc__,
)


@app.get("/health")
async def health() -> dict[str, str]:
    """Liveness for a supervisor. Deliberately does not touch the database:
    this answers "is the process up", and a pool check would conflate that
    with "is Postgres up"."""
    return {"status": "ok"}


async def authenticate(
    conn: asyncpg.Connection, device_id: UUID, presented_key: str | None
) -> asyncpg.Record:
    """Turn a device id and a key into the device, or refuse.

    Every failure answers the same 401 with the same message. An unknown
    device, a removed one and a wrong key are indistinguishable to the caller
    on purpose -- distinguishing them would make this endpoint an oracle for
    which device ids exist.
    """
    unauthorized = HTTPException(
        status_code=401,
        detail="device authentication failed",
        headers={"WWW-Authenticate": "DeviceKey"},
    )
    if not presented_key:
        raise unauthorized

    device = await conn.fetchrow(sql("device_for_ingest"), device_id)
    if device is None or device["removed_at"] is not None:
        raise unauthorized
    if not verify_password(presented_key, device["device_key_hash"]):
        raise unauthorized
    return device


def _role_error(device: asyncpg.Record, r: Reading) -> str | None:
    """Rule 6, checked before the trigger so the device gets a sentence.

    An inverter knows what it made and nothing about the grid boundary. A
    meter knows the boundary; whether it can split import from export depends
    on whether it is bidirectional, which is a property of the hardware and,
    since net metering became an application, of a regulator's decision.
    """
    if device["device_type"] == "inverter":
        if r.import_kwh is not None or r.export_kwh is not None:
            return (
                "an inverter reports generation_kwh only -- the import/export "
                "split is measured at the meter (rule 6)"
            )
        if r.generation_kwh is None:
            return "generation_kwh is required for an inverter"
        return None

    # A meter.
    if r.generation_kwh is not None:
        return "a meter reports import_kwh/export_kwh, never generation_kwh"
    if r.import_kwh is None:
        return "import_kwh is required for a meter"
    if device["meter_flow"] == "unidirectional":
        if r.export_kwh is not None and r.export_kwh > 0:
            return (
                "this meter is unidirectional and cannot measure export; a "
                "bidirectional meter is issued when net metering is granted"
            )
    elif r.export_kwh is None:
        return "export_kwh is required for a bidirectional meter"
    return None


def _shape_error(device: asyncpg.Record, r: Reading, now: datetime) -> str | None:
    """Everything that makes a reading unusable, in one place."""
    epoch_s = int(r.interval_start.timestamp())
    if epoch_s % (r.interval_minutes * 60) != 0:
        return (
            f"interval_start is not aligned to a {r.interval_minutes}-minute "
            "boundary"
        )
    if r.interval_minutes != device["interval_minutes"]:
        return (
            f"this device is registered at {device['interval_minutes']}-minute "
            f"intervals, got {r.interval_minutes}"
        )
    if r.interval_start > now + MAX_CLOCK_SKEW:
        return "interval_start is in the future -- check the device clock"
    if r.interval_start < now - MAX_BACKDATE:
        return f"interval_start is more than {MAX_BACKDATE.days} days old"
    return _role_error(device, r)


@app.post("/v1/ingest/readings", response_model=BatchResult)
async def ingest_readings(
    request: Request,
    payload: ReadingBatch,
    conn: Conn,
    x_device_key: Annotated[str | None, Header()] = None,
    idempotency_key: Annotated[str | None, Header()] = None,
) -> BatchResult:
    """Accept a batch of readings from one device.

    The whole batch runs in ONE transaction. A batch is what the device
    believes it delivered, and a partial write would leave it with no way to
    find out which half landed -- it would retry, and the retry would be
    answered as a duplicate for the half that succeeded.
    """
    if not idempotency_key:
        raise HTTPException(
            status_code=400,
            detail="Idempotency-Key header is required (rule 4)",
        )

    device = await authenticate(conn, payload.device_id, x_device_key)

    # A key already spent is answered with the original outcome. Checked
    # before the transaction so an ordinary retry costs one SELECT.
    existing = await conn.fetchrow(sql("find_ingest_batch"), idempotency_key)
    if existing is not None:
        if existing["device_id"] != payload.device_id:
            raise HTTPException(
                status_code=409,
                detail="this Idempotency-Key was used by a different device",
            )
        return BatchResult(
            batch_id=existing["batch_id"],
            device_id=existing["device_id"],
            replayed=True,
            reading_count=existing["reading_count"],
            accepted=existing["accepted_count"],
            duplicates=existing["duplicate_count"],
            # late is folded into rejected in the stored counts: the table
            # predates this service and has three buckets, not four.
            late=0,
            rejected=existing["rejected_count"],
        )

    now = datetime.now(timezone.utc)
    client_ip = request.client.host if request.client else None
    outcomes: list[ReadingOutcome] = []

    async with conn.transaction():
        try:
            batch_id = await conn.fetchval(
                sql("open_ingest_batch"),
                payload.device_id, idempotency_key, len(payload.readings),
                client_ip,
            )
        except asyncpg.UniqueViolationError:
            # Two retries raced. The other one won; its outcome is the answer.
            raise HTTPException(
                status_code=409,
                detail="this batch is already being processed; retry",
            ) from None

        # Partitions for every month the batch touches. Idempotent, and the
        # sole owner of partition-bound arithmetic -- writing FOR VALUES by
        # hand resolves against the session zone (CLAUDE.md's DDL rule).
        for month in {r.interval_start.date().replace(day=1) for r in payload.readings}:
            await conn.execute("SELECT create_reading_partition($1)", month)

        for r in payload.readings:
            problem = _shape_error(device, r, now)
            if problem is not None:
                outcomes.append(
                    ReadingOutcome(
                        interval_start=r.interval_start,
                        outcome="rejected",
                        detail=problem,
                    )
                )
                continue

            # Rule 8. Checked per reading rather than per batch: a batch
            # spanning a month boundary can legitimately be half inside a
            # billed period and half outside it.
            closed = await conn.fetchval(
                sql("reading_period_is_open"), payload.device_id, r.interval_start
            )
            if closed is not None:
                reason = "period_billed" if closed in ("billed", "closed") else "period_frozen"
                await conn.fetchval(
                    sql("divert_late_reading"),
                    payload.device_id, r.interval_start,
                    r.import_kwh, r.export_kwh, r.generation_kwh,
                    reason, batch_id,
                )
                outcomes.append(
                    ReadingOutcome(
                        interval_start=r.interval_start,
                        outcome="late",
                        detail=(
                            f"the billing period covering this interval is "
                            f"'{closed}'; kept as a late reading and never "
                            "merged into an issued bill"
                        ),
                    )
                )
                continue

            written = await conn.fetchval(
                sql("insert_reading"),
                payload.device_id, r.interval_start, r.interval_minutes,
                r.import_kwh, r.export_kwh, r.generation_kwh,
                r.voltage_avg, r.frequency_avg, r.dc_voltage_avg,
                batch_id,
            )
            outcomes.append(
                ReadingOutcome(
                    interval_start=r.interval_start,
                    outcome="accepted" if written is not None else "duplicate",
                )
            )

        accepted = sum(1 for o in outcomes if o.outcome == "accepted")
        duplicates = sum(1 for o in outcomes if o.outcome == "duplicate")
        late = sum(1 for o in outcomes if o.outcome == "late")
        rejected = sum(1 for o in outcomes if o.outcome == "rejected")

        # `late` counts as rejected in the stored totals -- the reading did not
        # land in device_reading, which is what the column means.
        await conn.execute(
            sql("close_ingest_batch"), batch_id, accepted, duplicates, rejected + late
        )

        # Only on a delivery that actually contained usable data. A device
        # posting nothing but duplicates has still checked in, but one posting
        # nothing but rejects has not proved it is working.
        if accepted or duplicates:
            newest = max(
                r.interval_start
                for r, o in zip(payload.readings, outcomes)
                if o.outcome in ("accepted", "duplicate")
            )
            await conn.execute(sql("touch_device_seen"), payload.device_id, newest)

    return BatchResult(
        batch_id=batch_id,
        device_id=payload.device_id,
        replayed=False,
        reading_count=len(payload.readings),
        accepted=accepted,
        duplicates=duplicates,
        late=late,
        rejected=rejected,
        outcomes=outcomes,
    )
