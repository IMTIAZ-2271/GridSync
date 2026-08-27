"""Net-metering agreements: the household applies, the government decides."""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID, uuid4

import asyncpg
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from .auth import Principal, require_role
from .db import Conn
from .notify import notify, notify_site_owner
from .queries import sql
# Shared: the net-metering inspection is guarded on exactly the same three
# facts as the meter installation, so it explains itself the same way.
from .routes_meters import ApplicationVisit, visit_of, why_not_ready

router = APIRouter(tags=["agreements"])


# --------------------------------------------------------------------------
# Models
# --------------------------------------------------------------------------

class Agreement(BaseModel):
    agreement_id: UUID
    site_id: UUID
    site_label: str
    district: str
    account_name: str
    billing_device_id: UUID
    billing_device_serial: str
    approval_ref: str
    sanctioned_capacity_kw: Decimal
    export_cap_pct: Decimal
    settlement_type: str
    credit_rollover_months: int | None
    effective_from: date
    effective_to: date | None
    status: str
    created_at: datetime


class AgreementStatusUpdate(BaseModel):
    """The outcome of reviewing a pending agreement.

    Only 'active' and 'terminated' are reachable here. 'suspended' is an
    operational action on an already-active agreement rather than a review
    decision, and nothing may move back to 'pending'.
    """

    status: Literal["active", "terminated"]


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------

@router.get("/api/agreements/pending", response_model=list[Agreement])
async def pending_agreements(
    conn: Conn,
    _: Annotated[
        Principal, Depends(require_role("government", "supplier", "admin"))
    ],
) -> list[Agreement]:
    # The supplier submits these and needs to watch the queue; the government
    # is what decides them.
    rows = await conn.fetch(sql("list_pending_agreements"))
    return [Agreement(**dict(r)) for r in rows]


@router.patch("/api/agreements/{agreement_id}/status", response_model=Agreement)
async def decide_agreement(
    conn: Conn,
    agreement_id: UUID,
    payload: AgreementStatusUpdate,
    _: Annotated[Principal, Depends(require_role("government", "admin"))],
) -> Agreement:
    """Approve or terminate a pending agreement.

    Government only. Approving is what lets a site's exports start earning
    credit, so the utility that pays for that export must not also be the party
    that authorizes it.
    """
    async with conn.transaction():
        decided = await conn.fetchval(
            sql("decide_agreement"), agreement_id, payload.status
        )
        if decided is None:
            # The UPDATE is guarded on status = 'pending', so zero rows means
            # either no such agreement or someone already decided it. Tell
            # those apart -- 409 is actionable, 404 is not.
            current = await conn.fetchval(
                "SELECT status FROM net_metering_agreement WHERE agreement_id = $1",
                agreement_id,
            )
            if current is None:
                raise HTTPException(status_code=404, detail="agreement not found")
            raise HTTPException(
                status_code=409,
                detail=f"agreement is already '{current}', not pending",
            )
        row = await conn.fetchrow(sql("get_agreement"), agreement_id)

        # Consumer requirement 7: net-metering applications go to the
        # government, and this is the household hearing back. Written inside
        # the transaction so a rolled-back decision cannot leave a
        # notification announcing it.
        approved = payload.status == "active"
        await notify_site_owner(
            conn,
            row["site_id"],
            "net_metering_application",
            "Net metering approved" if approved else "Net metering not approved",
            body=(
                f"Your net-metering agreement for {row['site_label']} is now "
                f"active. Exported energy starts earning credit from "
                f"{row['effective_from']}."
                if approved else
                f"The net-metering agreement for {row['site_label']} was not "
                f"approved. Contact your distribution company for the reason."
            ),
            severity="info" if approved else "warning",
            entity_type="agreement",
            entity_id=str(agreement_id),
            dedupe_key=f"nma:{agreement_id}:{payload.status}",
        )
    return Agreement(**dict(row))


# --------------------------------------------------------------------------
# The consumer's half: applying, and watching for an answer.
#
# Consumer requirement 7. Until 2026-08-27 the household applied by accident --
# POST /api/sites/{id}/solar opened a pending agreement as a side effect of
# registering panels. Registering hardware and asking the regulator for
# permission to sell power back are different acts, months apart in real life,
# so they are now different calls.
# --------------------------------------------------------------------------

class NetMeteringApplication(BaseModel):
    """One connection's agreement, as the household sees it."""

    agreement_id: UUID
    site_id: UUID
    site_label: str
    billing_point_id: UUID
    point_label: str
    approval_ref: str
    sanctioned_capacity_kw: Decimal
    export_cap_pct: Decimal
    settlement_type: str
    credit_rollover_months: int | None
    effective_from: date
    effective_to: date | None
    status: str
    created_at: datetime
    # Panels on this connection. Zero means the application cannot be filed
    # yet -- the regulator is agreeing to credit exported energy, and there is
    # nothing here to export.
    array_count: int
    # The inspection-and-swap visit that has to happen before this can go
    # active. None until an official orders one.
    visit: ApplicationVisit | None = None


class NetMeteringApply(BaseModel):
    billing_point_id: UUID


def _nma(row) -> NetMeteringApplication:
    """One row into the household's shape, visit and all."""
    d = {k: v for k, v in dict(row).items() if not k.startswith("visit_")}
    return NetMeteringApplication(**d, visit=visit_of(row))


@router.get(
    "/api/net-metering-applications",
    response_model=list[NetMeteringApplication],
)
async def my_net_metering_applications(
    conn: Conn,
    principal: Annotated[Principal, Depends(require_role("consumer"))],
) -> list[NetMeteringApplication]:
    """Every net-metering agreement across this household's connections.

    Not filtered to pending. An application that disappears the moment it is
    decided is what sends people to a call centre; the page has to be able to
    say approved, refused, and terminated.
    """
    rows = await conn.fetch(
        sql("net_metering_applications_for_account"), principal.account_id
    )
    return [_nma(r) for r in rows]


@router.post(
    "/api/net-metering-applications",
    response_model=NetMeteringApplication,
    status_code=201,
)
async def apply_for_net_metering(
    conn: Conn,
    payload: NetMeteringApply,
    principal: Annotated[Principal, Depends(require_role("consumer"))],
) -> NetMeteringApplication:
    """Ask the regulator to credit what this connection exports.

    Keyed on a **billing point**, not a site (rule 3). A household with two
    connections may have panels on one and not the other, and the agreement,
    the credit ledger and the bill all follow the point.

    Three preconditions, in the order they stop being the caller's fault:
    the connection must be theirs (404), it must have a billing meter -- rule 6
    says only the bidirectional meter can know the import/export split, so
    without one there is no export to credit -- and it must have at least one
    live array. `nma_no_overlap` is what refuses a second live application, and
    it is caught rather than pre-checked so two tabs cannot both win.

    `sanctioned_capacity_kw` is taken from the hardware actually installed on
    the connection, never from the request body: it is the number the regulator
    is being asked to approve, and a caller-chosen one would let a household
    apply for a capacity it has not built.
    """
    point = await conn.fetchrow(sql("point_for_application"), payload.billing_point_id)
    if point is None or point["account_id"] != principal.account_id:
        raise HTTPException(status_code=404, detail="connection not found")

    billing_device_id = await conn.fetchval(
        sql("point_billing_device"), point["point_id"]
    )
    if billing_device_id is None:
        raise HTTPException(
            status_code=409,
            detail=(
                f"'{point['point_label']}' has no billing meter, so it cannot "
                "export anything to credit"
            ),
        )

    solar = await conn.fetchrow(sql("point_solar_status"), point["point_id"])
    if solar["array_count"] == 0:
        raise HTTPException(
            status_code=409,
            detail=(
                f"register the solar array on '{point['point_label']}' before "
                "applying for net metering"
            ),
        )

    async with conn.transaction():
        try:
            agreement_id = await conn.fetchval(
                sql("create_net_metering_agreement"),
                point["site_id"], point["point_id"], billing_device_id,
                f"NMA-{uuid4().hex[:10].upper()}",
                solar["capacity_kw"],
            )
        except asyncpg.ExclusionViolationError:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"'{point['point_label']}' already has a net-metering "
                    "application or agreement"
                ),
            ) from None

        # The officials who govern this district hear about it, the same way
        # they hear about a meter application. Inside the transaction, so a
        # rolled-back application cannot leave a notification announcing one.
        owner = await conn.fetchrow(sql("agreement_owner"), agreement_id)
        for official in await conn.fetch(
            sql("officials_for_district"), owner["district"]
        ):
            await notify(
                conn,
                official["account_id"],
                "net_metering_application",
                "New net-metering application",
                body=(
                    f"{principal.full_name} has applied for net metering on "
                    f"{owner['point_label']} at {owner['site_label']} "
                    f"({owner['district']}), for "
                    f"{solar['capacity_kw']} kW of installed capacity."
                ),
                severity="info",
                entity_type="agreement",
                entity_id=str(agreement_id),
                dedupe_key=f"nma:{agreement_id}:submitted",
            )

    return await _my_application_or_404(conn, principal, agreement_id)


@router.delete(
    "/api/net-metering-applications/{agreement_id}",
    response_model=NetMeteringApplication,
)
async def withdraw_net_metering_application(
    conn: Conn,
    agreement_id: UUID,
    principal: Annotated[Principal, Depends(require_role("consumer"))],
) -> NetMeteringApplication:
    """Take back an application the regulator has not answered yet.

    Only while it is pending. An agreement that has been approved is ended by
    *terminating* it, which is the regulator's act -- a household walking out of
    a live agreement by pressing a button in its own portal would strand the
    credit ledger mid-month.

    Returns the withdrawn row rather than 204, so the page can render what
    happened to it without a second request.
    """
    mine = {
        r["agreement_id"]: r
        for r in await conn.fetch(
            sql("net_metering_applications_for_account"), principal.account_id
        )
    }
    if agreement_id not in mine:
        raise HTTPException(status_code=404, detail="application not found")

    withdrawn = await conn.fetchval(
        sql("withdraw_net_metering_application"), agreement_id
    )
    if withdrawn is None:
        raise HTTPException(
            status_code=409,
            detail=(
                f"this application is already '{mine[agreement_id]['status']}' "
                "and can no longer be withdrawn"
            ),
        )
    return await _my_application_or_404(conn, principal, agreement_id)


async def _my_application_or_404(
    conn: asyncpg.Connection, principal: Principal, agreement_id: UUID
) -> NetMeteringApplication:
    rows = await conn.fetch(
        sql("net_metering_applications_for_account"), principal.account_id
    )
    for r in rows:
        if r["agreement_id"] == agreement_id:
            return _nma(r)
    raise HTTPException(status_code=404, detail="application not found")


# --------------------------------------------------------------------------
# The inspection, and the approval it earns
#
# Migration b7d3f5a92c14, and the same shape as the meter flow: a regulator no
# longer approves an export agreement without anyone having looked at the roof.
# What differs is the order type -- `meter_swap`, because the connection
# already has a billing meter and rule 7 allows exactly one per point. The
# visit inspects the array and replaces that meter with a bidirectional one;
# the point, its periods, its bills and its credit ledger stay put, which is
# what rule 3 exists to make possible.
# --------------------------------------------------------------------------

class AgreementWorkOrderRequest(BaseModel):
    scheduled_for: datetime | None = None


class RaisedAgreementOrder(BaseModel):
    order_id: UUID
    site_id: UUID


class AgreementRegistration(BaseModel):
    """Optional detail recorded when the swapped meter is registered.

    No serial, for the same reason the meter flow has none: it is read off the
    work order, where the technician put it.
    """

    manufacturer: str | None = Field(default=None, max_length=120)
    model: str | None = Field(default=None, max_length=120)


async def _agreement_scope(
    conn: asyncpg.Connection, principal: Principal
) -> str | None:
    """The district this official may act in, or None for every district."""
    if principal.role == "admin":
        return None
    district = await conn.fetchval(sql("official_district"), principal.account_id)
    if district is None:
        raise HTTPException(
            status_code=403, detail="this account governs no district"
        )
    return district


@router.post(
    "/api/agreements/{agreement_id}/work-order",
    response_model=RaisedAgreementOrder,
    status_code=201,
)
async def raise_agreement_work_order(
    conn: Conn,
    agreement_id: UUID,
    payload: AgreementWorkOrderRequest,
    principal: Annotated[Principal, Depends(require_role("government", "admin"))],
) -> RaisedAgreementOrder:
    """Order the inspection and meter swap for a net-metering application.

    This is what "approve" now means on the regulator's queue. The agreement
    stays `pending` -- it is not active until a bidirectional meter is on the
    wall and registered, because until then there is nothing that can measure
    the export the agreement promises to credit (rule 6).

    Raised as a real work order, so the offer clock, the start-deadline sweep
    and the worker's own queue all apply without a special case.
    """
    row = await conn.fetchrow(sql("agreement_owner"), agreement_id)
    not_found = HTTPException(status_code=404, detail="agreement not found")
    if row is None:
        raise not_found
    scope = await _agreement_scope(conn, principal)
    if scope is not None and row["district"] != scope:
        raise not_found
    if row["status"] != "pending":
        raise HTTPException(
            status_code=409,
            detail=f"this agreement is already '{row['status']}'",
        )

    async with conn.transaction():
        try:
            order = await conn.fetchrow(
                sql("raise_agreement_work_order"),
                agreement_id, principal.account_id, payload.scheduled_for,
            )
        except asyncpg.UniqueViolationError:
            raise HTTPException(
                status_code=409,
                detail="an inspection is already open for this application",
            ) from None

        await notify(
            conn,
            row["account_id"],
            "net_metering_application",
            "An inspection has been scheduled",
            body=(
                f"Your district office has ordered an inspection of "
                f"{row['point_label']} and the export-capable meter it needs. "
                "You will be asked to confirm once the work is done."
            ),
            severity="info",
            entity_type="agreement",
            entity_id=str(agreement_id),
            dedupe_key=f"nma:{agreement_id}:ordered:{order['order_id']}",
        )

    return RaisedAgreementOrder(
        order_id=order["order_id"], site_id=order["site_id"]
    )


@router.post(
    "/api/agreements/{agreement_id}/register",
    response_model=NetMeteringApplication,
)
async def register_agreement_meter(
    conn: Conn,
    agreement_id: UUID,
    principal: Annotated[Principal, Depends(require_role("government", "admin"))],
    payload: AgreementRegistration | None = None,
) -> NetMeteringApplication:
    """Register the swapped meter and activate the agreement.

    One act, deliberately, and in this order: the meter is issued first and the
    agreement activated second, inside one transaction. An active agreement
    with no meter to measure export would promise credit the system cannot
    calculate; if the issue fails, the activation rolls back with it.

    Guarded on exactly what the meter flow is guarded on -- a completed visit,
    confirmed by the household, carrying a serial the technician recorded.
    """
    row = await conn.fetchrow(sql("agreement_owner"), agreement_id)
    not_found = HTTPException(status_code=404, detail="agreement not found")
    if row is None:
        raise not_found
    scope = await _agreement_scope(conn, principal)
    if scope is not None and row["district"] != scope:
        raise not_found

    make = (payload.manufacturer or "").strip() or None if payload else None
    model = (payload.model or "").strip() or None if payload else None

    async with conn.transaction():
        company_id = await conn.fetchval(sql("utility_for_site"), row["site_id"])
        try:
            issued = await conn.fetchrow(
                sql("register_agreement_meter"),
                agreement_id, row["account_id"], make, model, company_id,
            )
        except asyncpg.UniqueViolationError:
            raise HTTPException(
                status_code=409,
                detail="that meter serial is already registered",
            ) from None

        if issued is None:
            visit = await conn.fetchrow(sql("agreement_order"), agreement_id)
            raise HTTPException(
                status_code=409, detail=why_not_ready(visit, "application")
            )

        activated = await conn.fetchval(
            sql("activate_agreement_after_visit"), agreement_id
        )
        if activated is None:
            raise HTTPException(
                status_code=409,
                detail=f"this agreement is already '{row['status']}'",
            )

        await notify(
            conn,
            row["account_id"],
            "net_metering_application",
            "Net metering approved",
            body=(
                f"Meter {issued['serial_no']} is registered to you and net "
                f"metering is now active on {row['point_label']}. Install the "
                "meter from the Meters page — it replaces the one it was "
                "swapped for, and your exports start earning credit."
            ),
            severity="info",
            entity_type="agreement",
            entity_id=str(agreement_id),
            dedupe_key=f"nma:{agreement_id}:registered",
        )

    rows = await conn.fetch(
        sql("net_metering_applications_for_account"), row["account_id"]
    )
    return next(
        _nma(r) for r in rows if r["agreement_id"] == agreement_id
    )
