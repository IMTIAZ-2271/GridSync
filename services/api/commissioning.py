"""Offering a registered meter to its utility's head-end.

The API's half of the commissioning handshake: the offer, and the cancellation
when a meter leaves. The head-end's half -- claiming the offer and receiving
the key -- is services/ingest/commissioning.py, the device-facing door.

**Behind a switch, `COMMISSIONING_ENABLED`, off by default.** On, a registered
meter gets an offer and no SQL-generated readings: its history arrives from the
head-end through ingest. Off, registration backfills 90 days in SQL exactly as
it always has. The default is off because the environment that sets nothing is
the hosted one -- it runs no ingest service and no head-end, so an offer there
would be a meter that never reports. Deploying this code cannot change the
hosted estate; only setting the variable can. Locally, `.env` turns it on.

Read per call rather than once at import, so a test can drive both modes and a
restart is all a change needs.
"""
from __future__ import annotations

import os
from datetime import date, timedelta
from uuid import UUID

import asyncpg

from .queries import sql

#: How long a head-end has to claim a new meter. A head-end polls every few
#: seconds, so a day is not about speed: it is long enough that one being
#: restarted overnight does not fail every meter installed that evening.
#: Stored on the row at offer time (decision 3).
OFFER_WINDOW = timedelta(hours=24)


#: How much history an offer asks the head-end for, at most. The same 90 days
#: registration's SQL backfill writes (routes_sites.BACKFILL_DAYS).
HISTORY_DAYS = 90


def window_for(last_day: date | None, today: date) -> tuple[date, date]:
    """The history to offer when re-offering a meter already on the wall: the
    day after the connection's last reading, at most HISTORY_DAYS back, and
    **never later than today**.

    For the SAME device only -- an existing meter, or a retry. Its readings for
    today may already be held (a live meter whose head-end lost its key has
    reported until a moment ago), and a window starting tomorrow would leave the
    head-end owing nothing, so the handshake could never go live. Starting at
    today's midnight instead re-sends intervals the device already has; ingest
    answers them `duplicate` with identical values, and a batch of duplicates
    still proves the key works. Never use this for a swap: a different device
    re-sending those intervals would double the connection's energy, which is
    why register_meter offers a swap no window at all.
    """
    start = today - timedelta(days=HISTORY_DAYS)
    if last_day is not None:
        start = max(start, last_day + timedelta(days=1))
    start = min(start, today)
    return start, max(start, today - timedelta(days=1))


def commissioning_enabled() -> bool:
    raw = os.environ.get("COMMISSIONING_ENABLED", "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


async def offer_commissioning(
    conn: asyncpg.Connection,
    *,
    device_id: UUID,
    point_id: UUID,
    meter_asset_id: UUID | None,
    backfill_from: date,
    backfill_to: date,
    requested_by: UUID | None,
) -> asyncpg.Record:
    """Offer a device to the head-end of the utility that runs its connection.

    Call inside the transaction that created the device, so a registration that
    rolls back leaves no offer for a meter that does not exist.

    An empty window (`backfill_from > backfill_to`, which a swap produces when
    the retired meter already covers yesterday) is stored as NULL on both --
    "upload nothing" -- because the table refuses an inverted range.

    When no enabled head-end serves the utility, the row is written anyway as
    `failed` / `no_source`, so the gap is visible rather than a silent meter.
    """
    route = await conn.fetchrow(
        sql("commissioning_route"), point_id, meter_asset_id
    )
    source_id = route["source_id"] if route else None
    window = (backfill_from, backfill_to) if backfill_from <= backfill_to else (None, None)

    if source_id is None:
        return await conn.fetchrow(
            sql("record_commissioning_without_source"),
            device_id, requested_by, OFFER_WINDOW, *window,
        )
    return await conn.fetchrow(
        sql("offer_commissioning"),
        device_id, source_id, requested_by, OFFER_WINDOW, *window,
    )


async def cancel_commissioning(conn: asyncpg.Connection, device_id: UUID) -> None:
    """End any open handshake for a device that has just been retired.

    Unconditional -- not gated on the switch -- because a handshake opened while
    commissioning was on must not outlive its device after it is turned off.
    Ingest already refuses a removed device's readings; this is what takes the
    meter off its head-end's feed and records why.
    """
    await conn.execute(sql("cancel_device_commissioning"), device_id)
