"""The commissioning handshake: how a utility's head-end receives a meter's key.

    1. DISCOVER  GET  /v1/source/commissions                 what to act on
    2. ACTIVATE  POST /v1/source/commissions/{id}/activate   claim, get the key
       REJECT    POST /v1/source/commissions/{id}/reject     "not my meter"
    3. CONFIRM   POST /v1/ingest/readings                    the first accepted
                                                             batch makes it live

The head-end polls; GridSync never calls it. A head-end that was down for a day
catches up by being started -- the same property every job in services/jobs
has, and no outbox is needed to survive it being unreachable.

**Two credentials, two jobs.** `X-Source-Id` + `X-Source-Key` authorise the
handshake and nothing else: a source key cannot sign a reading. The device key
it receives signs that one meter's readings, so a single meter can be revoked
without cutting off the utility's fleet.

**Live is proved by data.** Activation shows only that the head-end asked for a
key. `mark_commissioning_live` runs inside ingest's batch transaction, so the
handshake completes exactly when a reading signed with that key is accepted.
Until then the key has never been used, which is what makes a retried
activation safe to answer with a fresh key.

Failure answers follow the rest of the system: another source's handshake is
404, never 403, and every authentication failure is the same 401.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Annotated, Literal
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field

from services.api.auth import hash_password, verify_password
from services.api.db import Conn
from services.api.notify import notify
from services.api.queries import sql
from services.api.types import Energy
from services.ingest.keys import mint_device_key

# --------------------------------------------------------------------------
# Policy
# --------------------------------------------------------------------------

#: How long a key may go unused before the handshake lapses. Long enough for a
#: head-end to fetch a day of stored history for the meter; short enough that
#: a key nobody is using does not sit valid for days. Stored on the row at
#: activation, so changing this never moves an existing deadline.
ACTIVATION_WINDOW = timedelta(hours=1)

#: Keys one handshake may mint. Every retry restarts the activation window, so
#: without a cap a head-end stuck retrying would hold a handshake open forever
#: and the sweep could never fail it.
MAX_ACTIVATIONS = 5

INGEST_PATH = "/v1/ingest/readings"

router = APIRouter(prefix="/v1/source", tags=["commissioning"])


# --------------------------------------------------------------------------
# Source authentication
# --------------------------------------------------------------------------

class SourcePrincipal(BaseModel):
    source_id: UUID
    name: str
    distribution_company_id: UUID


async def authenticate_source(
    conn: Conn,
    x_source_id: Annotated[str | None, Header()] = None,
    x_source_key: Annotated[str | None, Header()] = None,
) -> SourcePrincipal:
    """Turn a source id and key into a head-end, or refuse.

    An unknown source, a disabled one and a wrong key are the same 401 -- the
    same posture as device authentication, for the same reason.
    """
    unauthorized = HTTPException(
        status_code=401,
        detail="source authentication failed",
        headers={"WWW-Authenticate": "SourceKey"},
    )
    if not x_source_id or not x_source_key:
        raise unauthorized
    try:
        source_id = UUID(x_source_id)
    except ValueError:
        raise unauthorized from None

    row = await conn.fetchrow(sql("source_for_auth"), source_id)
    if row is None or row["disabled_at"] is not None:
        raise unauthorized
    if not verify_password(x_source_key, row["source_key_hash"]):
        raise unauthorized
    return SourcePrincipal(
        source_id=row["source_id"],
        name=row["name"],
        distribution_company_id=row["distribution_company_id"],
    )


Source = Annotated[SourcePrincipal, Depends(authenticate_source)]


# --------------------------------------------------------------------------
# Wire shapes
# --------------------------------------------------------------------------

class SimulationHints(BaseModel):
    """Facts a synthetic head-end needs and a real one would not have.

    Kept in their own object so the protocol itself stays honest: nothing
    outside this block is something a real utility head-end could not know.
    """

    #: AC capacity of the live inverters on this meter's own connection.
    point_solar_capacity_kw: Energy


class Commission(BaseModel):
    commissioning_id: UUID
    status: Literal["offered", "activated", "live"]
    device_id: UUID
    serial_no: str
    device_type: Literal["meter", "inverter"]
    interval_minutes: int
    #: NULL for an inverter. Decides whether export may be reported (rule 6).
    meter_flow: Literal["unidirectional", "bidirectional"] | None
    offered_at: datetime
    offer_expires_at: datetime
    activation_expires_at: datetime | None
    activation_count: int
    live_at: datetime | None
    #: The history to upload after activation. Both NULL: upload nothing.
    backfill_from: date | None
    backfill_to: date | None
    simulation: SimulationHints


class CommissionFeed(BaseModel):
    source_id: UUID
    name: str
    commissions: list[Commission]


class ActivateIn(BaseModel):
    #: The meter the head-end asserts it holds. Must be the offered device's.
    serial_no: str = Field(min_length=1, max_length=100)


class ActivationOut(BaseModel):
    commissioning_id: UUID
    device_id: UUID
    status: Literal["activated"] = "activated"
    #: Shown once. Only its argon2 hash is kept, and it cannot be recovered --
    #: a head-end that loses it activates again while that is still allowed.
    device_key: str
    activation_expires_at: datetime
    activation_count: int
    interval_minutes: int
    ingest_path: str = INGEST_PATH
    backfill_from: date | None
    backfill_to: date | None


class RejectIn(BaseModel):
    #: Why, in the head-end's words. Shown to the district office verbatim.
    detail: str = Field(min_length=1, max_length=500)


class RejectOut(BaseModel):
    commissioning_id: UUID
    status: Literal["failed"] = "failed"


# --------------------------------------------------------------------------
# Endpoints
# --------------------------------------------------------------------------

@router.get("/commissions", response_model=CommissionFeed)
async def list_commissions(conn: Conn, source: Source) -> CommissionFeed:
    """Every handshake this head-end should act on now.

    Reconcile against it on every poll: claim what is `offered`, deliver for
    what is `activated` or `live`, and stop sending for any device not listed.
    """
    rows = await conn.fetch(sql("source_commissions"), source.source_id)
    return CommissionFeed(
        source_id=source.source_id,
        name=source.name,
        commissions=[
            Commission(
                **{k: r[k] for k in r.keys() if k != "point_solar_capacity_kw"},
                simulation=SimulationHints(
                    point_solar_capacity_kw=r["point_solar_capacity_kw"]
                ),
            )
            for r in rows
        ],
    )


def _why_not_activated(row: asyncpg.Record | None, serial_no: str) -> HTTPException:
    """Choose the sentence for an activation that matched nothing.

    Ordered by what the head-end can act on: a handshake it cannot see, a
    meter that is gone, a claim for the wrong meter, then the states and
    deadlines that mean GridSync has to offer the meter again.
    """
    if row is None:
        return HTTPException(status_code=404, detail="commissioning not found")
    if row["removed_at"] is not None:
        return HTTPException(status_code=409, detail="this device has been retired")
    if row["serial_no"] != serial_no:
        return HTTPException(
            status_code=422,
            detail="serial_no does not match the device this offer is for",
        )
    status, now = row["status"], row["db_now"]
    if status == "live":
        return HTTPException(
            status_code=409,
            detail=(
                "this meter is already live; its key is not re-issued through "
                "activation"
            ),
        )
    if status in ("failed", "cancelled"):
        return HTTPException(
            status_code=409,
            detail=f"this handshake is '{status}'; the meter must be offered again",
        )
    if row["activation_count"] >= MAX_ACTIVATIONS:
        return HTTPException(
            status_code=409,
            detail=(
                f"activation retried too many times ({MAX_ACTIVATIONS}); the "
                "meter must be offered again"
            ),
        )
    if status == "offered" and row["offer_expires_at"] <= now:
        return HTTPException(status_code=409, detail="this offer has expired")
    if status == "activated" and row["activation_expires_at"] <= now:
        return HTTPException(
            status_code=409,
            detail="the activation window has expired; the meter must be offered again",
        )
    # Matched nothing, yet nothing above explains it: the row changed between
    # the UPDATE and this read. Retrying reads the new state.
    return HTTPException(status_code=409, detail="the handshake changed; retry")


@router.post("/commissions/{commissioning_id}/activate", response_model=ActivationOut)
async def activate_commission(
    conn: Conn,
    source: Source,
    commissioning_id: UUID,
    payload: ActivateIn,
) -> ActivationOut:
    """Claim an offered meter and receive its device key.

    The claim and the key rotation are one transaction: a claim whose key
    failed to store rolls back, so an `activated` row always has a key the
    head-end was actually given.
    """
    serial_no = payload.serial_no.strip()
    key = mint_device_key()
    key_hash = hash_password(key)

    async with conn.transaction():
        claimed = await conn.fetchrow(
            sql("activate_commissioning"),
            commissioning_id, source.source_id, serial_no,
            ACTIVATION_WINDOW, MAX_ACTIVATIONS,
        )
        if claimed is None:
            row = await conn.fetchrow(
                sql("commissioning_for_source"), commissioning_id, source.source_id
            )
            raise _why_not_activated(row, serial_no)

        await conn.fetchrow(sql("rotate_device_key"), claimed["device_id"], key_hash)

    return ActivationOut(
        commissioning_id=claimed["commissioning_id"],
        device_id=claimed["device_id"],
        device_key=key,
        activation_expires_at=claimed["activation_expires_at"],
        activation_count=claimed["activation_count"],
        interval_minutes=claimed["interval_minutes"],
        backfill_from=claimed["backfill_from"],
        backfill_to=claimed["backfill_to"],
    )


@router.post("/commissions/{commissioning_id}/reject", response_model=RejectOut)
async def reject_commission(
    conn: Conn,
    source: Source,
    commissioning_id: UUID,
    payload: RejectIn,
) -> RejectOut:
    """Refuse a meter this head-end does not recognise.

    Terminal. If a key had already been minted it is revoked in the same
    transaction (`revoke_device_key`), so a refused meter leaves no working
    credential behind. The district office is told, because the usual cause is
    a serial recorded wrong at the property and they can send someone back.
    """
    detail = payload.detail.strip()

    async with conn.transaction():
        ended = await conn.fetchrow(
            sql("reject_commissioning"), commissioning_id, source.source_id, detail
        )
        if ended is None:
            row = await conn.fetchrow(
                sql("commissioning_for_source"), commissioning_id, source.source_id
            )
            if row is None:
                raise HTTPException(status_code=404, detail="commissioning not found")
            raise HTTPException(
                status_code=409,
                detail=f"this handshake is '{row['status']}' and cannot be rejected",
            )

        if ended["activation_count"] > 0:
            await conn.fetchval(sql("revoke_device_key"), ended["device_id"])

        officials = await conn.fetch(sql("officials_for_district"), ended["district"])
        for official in officials:
            await notify(
                conn,
                official["account_id"],
                "device_commissioning",
                "A meter could not be connected",
                body=(
                    f"{source.name} refused meter {ended['serial_no']} at "
                    f"{ended['site_label']}: {detail}. Check the serial recorded "
                    "for this installation."
                ),
                severity="warning",
                entity_type="device",
                entity_id=str(ended["device_id"]),
                dedupe_key=f"commissioning:{commissioning_id}:rejected",
            )

    return RejectOut(commissioning_id=ended["commissioning_id"])

