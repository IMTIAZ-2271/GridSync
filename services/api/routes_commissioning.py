"""Staff views of meter commissioning.

`GET /api/commissioning` -- every live billing meter and the state of its latest
handshake with its utility's head-end. District officials see their own
district; suppliers read the whole fleet, as they do on the equipment page.

`POST /api/devices/{device_id}/commissioning` -- offer a meter to its head-end
again. **District officials only** (and admins). They register meters, and they
are who the commissioning sweep and a head-end's rejection notify; a supplier is
a private installer (decision 4) and does not decide which utility network a
meter is on. Another district's meter is 404, as everywhere an official is
scoped.

The retry exists for three situations, and treats them alike:

* a handshake that **failed** (lapsed, or the head-end refused the serial) --
  simply offer again;
* a meter that was **never offered** -- installed before commissioning, or while
  the switch was off;
* a **live or activated** meter whose head-end lost its key -- activation cannot
  re-issue a live key, so the open handshake is cancelled, the key revoked, and
  a fresh offer made. The head-end sees a new commissioning id for the device,
  drops what it held and claims the new one.

An offer still inside its window is 409: the head-end has not asked yet, and
there is nothing to retry. So is any retry while COMMISSIONING_ENABLED is off,
because an offer nothing will claim only manufactures a failure.

Nothing here is available to the household (Consumer 9).
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Annotated, Literal
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, computed_field

from .auth import Principal, require_role
from .commissioning import (
    cancel_commissioning,
    commissioning_enabled,
    offer_commissioning,
    window_for,
)
from .db import Conn
from .queries import sql

router = APIRouter()

ConnectionState = Literal[
    "not_commissioned",
    "offered",
    "offer_lapsed",
    "activated",
    "activation_lapsed",
    "live",
    "failed",
    "cancelled",
]

#: The states a person should act on. Everything else is either working or
#: waiting inside a deadline.
ATTENTION: frozenset[str] = frozenset({
    "not_commissioned", "offer_lapsed", "activation_lapsed", "failed", "cancelled",
})


class MeterConnection(BaseModel):
    device_id: UUID
    serial_no: str
    site_id: UUID
    site_label: str
    district: str
    point_label: str
    last_seen_at: datetime | None
    state: ConnectionState
    commissioning_id: UUID | None
    head_end: str | None
    failed_reason: Literal[
        "no_source", "offer_expired", "activation_expired", "rejected_by_source"
    ] | None
    #: The head-end's own words when it refused the meter.
    failure_detail: str | None
    offered_at: datetime | None
    offer_expires_at: datetime | None
    activation_expires_at: datetime | None
    live_at: datetime | None
    ended_at: datetime | None

    @computed_field
    @property
    def needs_attention(self) -> bool:
        return self.state in ATTENTION


class CommissioningOverview(BaseModel):
    #: False where the switch is off (the hosted estate): every meter reads
    #: `not_commissioned` there by design, and the page should say nothing.
    enabled: bool
    meters: list[MeterConnection]


class RetryOut(BaseModel):
    commissioning_id: UUID
    status: Literal["offered", "failed"]
    offer_expires_at: datetime
    backfill_from: date | None
    backfill_to: date | None


async def _scope(conn: asyncpg.Connection, principal: Principal) -> str | None:
    """The district a government account governs; None for fleet-wide readers.

    Same contract as routes_meters._official_scope: a government account with no
    profile is 403, never silently widened to every district.
    """
    if principal.role != "government":
        return None
    district = await conn.fetchval(sql("official_district"), principal.account_id)
    if district is None:
        raise HTTPException(status_code=403, detail="this account governs no district")
    return district


@router.get("/api/commissioning", response_model=CommissioningOverview)
async def commissioning_overview(
    conn: Conn,
    principal: Annotated[
        Principal, Depends(require_role("government", "supplier", "admin"))
    ],
) -> CommissioningOverview:
    district = await _scope(conn, principal)
    rows = await conn.fetch(sql("commissioning_overview"), district)
    return CommissioningOverview(
        enabled=commissioning_enabled(),
        meters=[MeterConnection(**dict(r)) for r in rows],
    )


@router.post(
    "/api/devices/{device_id}/commissioning",
    response_model=RetryOut,
    status_code=201,
)
async def retry_commissioning(
    conn: Conn,
    device_id: UUID,
    principal: Annotated[Principal, Depends(require_role("government", "admin"))],
) -> RetryOut:
    district = await _scope(conn, principal)
    not_found = HTTPException(status_code=404, detail="meter not found")

    async with conn.transaction():
        meter = await conn.fetchrow(sql("meter_for_retry"), device_id)
        if meter is None or (district is not None and meter["district"] != district):
            raise not_found
        if not commissioning_enabled():
            raise HTTPException(
                status_code=409,
                detail="meter commissioning is switched off on this server",
            )
        if meter["open_status"] == "offered":
            raise HTTPException(
                status_code=409,
                detail="this meter is already waiting for its head-end to claim it",
            )
        if meter["open_status"] in ("activated", "live"):
            # The head-end holds (or held) a key for the current handshake. It
            # ends here, and the key with it, before the new offer exists.
            await cancel_commissioning(conn, device_id)
            await conn.fetchval(sql("revoke_device_key"), device_id)

        start, end = window_for(meter["last_day"], date.today())
        try:
            offer = await offer_commissioning(
                conn,
                device_id=device_id,
                point_id=meter["point_id"],
                meter_asset_id=meter["meter_asset_id"],
                backfill_from=start,
                backfill_to=end,
                requested_by=principal.account_id,
            )
        except asyncpg.UniqueViolationError:
            # Two officials pressed retry at once; the other one's offer stands.
            raise HTTPException(
                status_code=409,
                detail="this meter is already waiting for its head-end to claim it",
            ) from None

    return RetryOut(
        commissioning_id=offer["commissioning_id"],
        status=offer["status"],
        offer_expires_at=offer["offer_expires_at"],
        backfill_from=start if start <= end else None,
        backfill_to=end if start <= end else None,
    )
