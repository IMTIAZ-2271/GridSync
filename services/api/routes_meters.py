"""Meter assets and meter applications.

The household no longer types a serial number to register a meter. A meter is
the utility's hardware, issued to a consumer against their identity, and what
the household does is say which of *their* meters serves which connection --
see migration c9e2f4a71b83.

A supplier is on none of these endpoints: a private installer fits panels, and
a billing meter belongs to the distribution company, which decision 4 keeps as
a separate entity.

**Approving does not issue a meter.** It used to, in the same transaction --
which meant hardware was handed over on a click, with nobody having been to the
property. Since migration b7d3f5a92c14 the application runs through a visit:

    apply -> the district's officials are notified
          -> an official raises a work order and offers it to a worker
          -> the worker fits the meter and records its serial
          -> the household confirms the installation
          -> the official registers the meter
          -> the household installs it on a connection, and readings begin

`accepted` is therefore unreachable through the decision endpoint. It is set by
`/register`, which is guarded on a completed visit the household has confirmed
and a serial the technician recorded -- three facts, none of them the
official's own assertion.
"""
from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from .auth import Principal, require_role, visible_site_or_404
from .db import Conn
from .notify import notify
from .queries import sql

router = APIRouter(tags=["meters"])


# --------------------------------------------------------------------------
# Models
# --------------------------------------------------------------------------

class MeterAsset(BaseModel):
    """One meter issued to the signed-in consumer."""

    meter_asset_id: UUID
    serial_no: str
    manufacturer: str | None
    model: str | None
    issued_at: datetime
    issued_by: str | None
    available: bool
    # Where it went, when it is not available. All four are NULL together.
    device_id: UUID | None
    site_id: UUID | None
    site_label: str | None
    point_id: UUID | None
    point_label: str | None
    removed_at: datetime | None


class ApplicationVisit(BaseModel):
    """The visit fulfilling an application, as both sides see it.

    Flattened off the application row rather than fetched per card: the
    household's page shows one of these beside every open application, and a
    request each would be N+1 on the page they open most.
    """

    order_id: UUID
    order_type: str
    status: str
    scheduled_for: datetime | None
    started_at: datetime | None
    completed_at: datetime | None
    completion_notes: str | None
    failure_reason: str | None
    installed_serial_no: str | None
    consumer_confirmed_at: datetime | None
    consumer_disputed_at: datetime | None
    consumer_note: str | None


def visit_of(row) -> ApplicationVisit | None:
    """Lift the visit_* columns off an application row, or None if there is
    no visit yet -- which is a normal state, not a missing one."""
    if row["visit_order_id"] is None:
        return None
    return ApplicationVisit(
        order_id=row["visit_order_id"],
        order_type=row["visit_order_type"],
        status=row["visit_status"],
        scheduled_for=row["visit_scheduled_for"],
        started_at=row["visit_started_at"],
        completed_at=row["visit_completed_at"],
        completion_notes=row["visit_completion_notes"],
        failure_reason=row["visit_failure_reason"],
        installed_serial_no=row["visit_installed_serial_no"],
        consumer_confirmed_at=row["visit_confirmed_at"],
        consumer_disputed_at=row["visit_disputed_at"],
        consumer_note=row["visit_note"],
    )


class MeterApplicationCreate(BaseModel):
    site_id: UUID
    reason: str | None = Field(default=None, max_length=2000)


class MeterApplication(BaseModel):
    """The household's view of a request it filed."""

    application_id: UUID
    site_id: UUID
    site_label: str
    district: str
    status: str
    reason: str | None
    submitted_at: datetime
    decided_at: datetime | None
    decision_notes: str | None
    issued_meter_asset_id: UUID | None
    issued_serial_no: str | None
    # False once they have assigned the meter that was issued to them, which is
    # what turns "approved" into "done" on the page without a second status.
    issued_meter_available: bool | None
    # The visit that fulfils it. None until an official raises one.
    visit: ApplicationVisit | None = None


class MeterApplicationQueueRow(BaseModel):
    """The official's view. Carries the applicant, which the consumer's does
    not -- a household knows who it is."""

    application_id: UUID
    account_id: UUID
    account_name: str
    national_id: str | None
    phone: str | None
    site_id: UUID
    site_label: str
    address_line: str
    district: str
    status: str
    reason: str | None
    submitted_at: datetime
    decided_at: datetime | None
    decision_notes: str | None
    issued_meter_asset_id: UUID | None
    issued_serial_no: str | None
    existing_meters: int


class MeterApplicationDecision(BaseModel):
    """What an official (or the applicant, withdrawing) does to a request.

    'completed' is absent: `meter_application_no_completion` forbids it in the
    schema, because registration IS the delivery -- there is no second act the
    way there is for a solar installation.

    'accepted' is accepted by the type but refused by the handler. It is set by
    `/register`, which can check that a visit happened; leaving it out of the
    Literal would have made a stale client's mistake a 422 about a field rather
    than a 409 explaining the flow.
    """

    status: Literal["under_review", "accepted", "rejected", "withdrawn"]
    decision_notes: str | None = Field(default=None, max_length=2000)


class MeterRegistration(BaseModel):
    """Optional detail recorded when the meter is registered.

    No serial. It is read off the work order, where the technician who was
    holding the meter put it -- an official typing a number for hardware they
    never saw is the gap this whole flow closes.
    """

    manufacturer: str | None = Field(default=None, max_length=120)
    model: str | None = Field(default=None, max_length=120)
    notes: str | None = Field(default=None, max_length=2000)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def _application(row) -> MeterApplication:
    """One row into the household's shape, visit and all."""
    d = {k: v for k, v in dict(row).items() if not k.startswith("visit_")}
    return MeterApplication(**d, visit=visit_of(row))


async def _official_scope(
    conn: asyncpg.Connection, principal: Principal
) -> str | None:
    """The district this official may act in, or None for every district.

    Same contract as routes_workers._scope, and for the same reason: None means
    admin (no government_profile row), never "unscoped government".
    """
    if principal.role == "admin":
        return None
    district = await conn.fetchval(sql("official_district"), principal.account_id)
    if district is None:
        raise HTTPException(
            status_code=403, detail="this account governs no district"
        )
    return district


# --------------------------------------------------------------------------
# Consumer
# --------------------------------------------------------------------------

@router.get("/api/meter-assets", response_model=list[MeterAsset])
async def my_meter_assets(
    conn: Conn,
    principal: Annotated[Principal, Depends(require_role("consumer"))],
) -> list[MeterAsset]:
    """Every meter issued to this consumer, available ones first.

    Consumer-only. A meter is issued against an identity, so "whose meters"
    is not a question another role gets to ask here -- the regulator sees the
    hardware through the site instead, where it is installed.
    """
    rows = await conn.fetch(sql("meter_assets_for_account"), principal.account_id)
    return [MeterAsset(**dict(r)) for r in rows]


@router.post(
    "/api/meter-applications", response_model=MeterApplication, status_code=201
)
async def apply_for_meter(
    conn: Conn,
    payload: MeterApplicationCreate,
    principal: Annotated[Principal, Depends(require_role("consumer"))],
) -> MeterApplication:
    """Ask for a meter to be issued, through one of your sites.

    The site is what scopes the request to a district official, so it is
    required rather than optional -- "somewhere" is not a request anybody can
    act on. `visible_site_or_404` is what stops a household applying against
    somebody else's address.
    """
    await visible_site_or_404(conn, principal, payload.site_id)

    reason = (payload.reason or "").strip() or None
    async with conn.transaction():
        try:
            application_id = await conn.fetchval(
                sql("create_meter_application"),
                principal.account_id, payload.site_id, reason,
            )
        except asyncpg.UniqueViolationError:
            # meter_application_one_open. Reading the queue first and deciding
            # would race itself; the index is the check.
            raise HTTPException(
                status_code=409,
                detail="this site already has a meter application waiting",
            ) from None

        # The officials who govern this district hear about it. Inside the
        # transaction, so a rolled-back application cannot leave a notification
        # announcing one -- and deduped on the application, so a retry that
        # somehow got past the index still says it once.
        row = await conn.fetchrow(sql("meter_application_context"), application_id)
        for official in await conn.fetch(
            sql("officials_for_district"), row["district"]
        ):
            await notify(
                conn,
                official["account_id"],
                "meter_application",
                "New meter application",
                body=(
                    f"{principal.full_name} has applied for a meter in "
                    f"{row['district']}."
                    + (f" They said: {reason}" if reason else "")
                ),
                severity="info",
                entity_type="meter_application",
                entity_id=str(application_id),
                dedupe_key=f"meterapp:{application_id}:submitted",
            )

    return await _my_application_or_404(conn, principal, application_id)


@router.get("/api/meter-applications", response_model=list[MeterApplication])
async def my_meter_applications(
    conn: Conn,
    principal: Annotated[Principal, Depends(require_role("consumer"))],
) -> list[MeterApplication]:
    """This household's own requests, newest first, decided ones included."""
    rows = await conn.fetch(
        sql("meter_applications_for_account"), principal.account_id
    )
    return [_application(r) for r in rows]


# --------------------------------------------------------------------------
# Government
# --------------------------------------------------------------------------

@router.get(
    "/api/meter-applications/queue",
    response_model=list[MeterApplicationQueueRow],
)
async def meter_application_queue(
    conn: Conn,
    principal: Annotated[Principal, Depends(require_role("government", "admin"))],
    include_decided: bool = False,
) -> list[MeterApplicationQueueRow]:
    """Requests awaiting a decision in this official's district, oldest first.

    A fixed sub-path rather than a query parameter on the consumer's list:
    the two return different shapes -- the official needs to know who applied
    and how many meters the site already has -- and one endpoint that changes
    its columns by role is a response nobody can type.
    """
    rows = await conn.fetch(
        sql("meter_applications_queue"),
        await _official_scope(conn, principal),
        include_decided,
    )
    return [MeterApplicationQueueRow(**dict(r)) for r in rows]


@router.patch(
    "/api/meter-applications/{application_id}", response_model=MeterApplication
)
async def decide_meter_application(
    conn: Conn,
    application_id: UUID,
    payload: MeterApplicationDecision,
    principal: Annotated[
        Principal, Depends(require_role("consumer", "government", "admin"))
    ],
) -> MeterApplication:
    """Move a meter application on.

    Two identities with different powers, the same split `routes_applications`
    already uses: the household may only **withdraw** its own request, and an
    official may review, accept or reject one in their district. Anyone else's
    application is a 404 either way -- 403 would confirm it exists.

    Acceptance issues the meter inside the same transaction. An 'accepted' row
    with no `issued_meter_asset_id` would be an approval that handed over
    nothing, and the household's list would stay empty with no way to tell why.
    """
    row = await conn.fetchrow(sql("meter_application_context"), application_id)
    not_found = HTTPException(status_code=404, detail="application not found")
    if row is None:
        raise not_found

    if principal.role == "consumer":
        if row["account_id"] != principal.account_id:
            raise not_found
        if payload.status != "withdrawn":
            raise HTTPException(
                status_code=403,
                detail="a household may only withdraw its own application",
            )
    else:
        scope = await _official_scope(conn, principal)
        if scope is not None and row["district"] != scope:
            raise not_found
        if payload.status == "withdrawn":
            raise HTTPException(
                status_code=403,
                detail="only the applicant may withdraw an application",
            )
        if payload.status == "accepted":
            # Acceptance is what issues the meter, and a meter is issued
            # against a visit that happened -- not against a click. Routed to
            # the endpoint that can check that rather than silently accepted
            # here, so the flow cannot be short-circuited by a stale client.
            raise HTTPException(
                status_code=409,
                detail=(
                    "a meter is registered from a completed visit the "
                    "household has confirmed — use "
                    f"POST /api/meter-applications/{application_id}/register"
                ),
            )

    notes = (payload.decision_notes or "").strip() or None

    async with conn.transaction():
        decided = await conn.fetchrow(
            sql("decide_meter_application"),
            application_id, payload.status, notes,
            principal.account_id, None,
        )
        if decided is None:
            # Somebody decided it first, or the household withdrew it while an
            # official had the row open. The issue above rolls back with this.
            raise HTTPException(
                status_code=409, detail="this application has already been decided"
            )

        if principal.role != "consumer":
            await _notify_applicant(conn, decided, payload.status, notes)

    rows = await conn.fetch(
        sql("meter_applications_for_account"), row["account_id"]
    )
    return next(
        _application(r) for r in rows if r["application_id"] == application_id
    )


async def _notify_applicant(
    conn: asyncpg.Connection,
    decided: asyncpg.Record,
    status: str,
    notes: str | None,
) -> None:
    """Tell the household what came back.

    'under_review' is deliberately silent: a status crawling through a queue is
    the regulator's business, and a panel that buzzes for it is one people learn
    to ignore. `dedupe_key` names the state, so re-entering it says nothing.
    """
    if status == "under_review":
        return

    if status == "accepted":
        # Unreachable through the decision endpoint since b7d3f5a92c14 --
        # acceptance is /register's, and it sends its own message naming the
        # serial. Kept so an admin path cannot notify nobody.
        title = "Meter application approved"
        body = "A meter has been issued to you. Add it from the Meters page."
        severity = "info"
    else:
        title = "Meter application not approved"
        body = notes or (
            "Your request for a new meter was not approved. Contact your "
            "distribution company for the reason."
        )
        severity = "warning"

    await notify(
        conn,
        decided["account_id"],
        "meter_application",
        title,
        body=body,
        severity=severity,
        entity_type="meter_application",
        entity_id=str(decided["application_id"]),
        dedupe_key=f"meterapp:{decided['application_id']}:{status}",
    )


async def _my_application_or_404(
    conn: asyncpg.Connection, principal: Principal, application_id: UUID
) -> MeterApplication:
    rows = await conn.fetch(
        sql("meter_applications_for_account"), principal.account_id
    )
    for r in rows:
        if r["application_id"] == application_id:
            return _application(r)
    raise HTTPException(status_code=404, detail="application not found")


# --------------------------------------------------------------------------
# The visit, and the registration it earns
#
# Migration b7d3f5a92c14. An official raises the order; the existing dispatch
# endpoints offer it, the existing deadline sweeps chase it, and the worker's
# own queue completes it. None of that is new -- what is new is that a meter
# cannot be issued without it.
# --------------------------------------------------------------------------

class WorkOrderRequest(BaseModel):
    scheduled_for: datetime | None = None


class RaisedOrder(BaseModel):
    order_id: UUID
    site_id: UUID


def why_not_ready(visit, noun: str = "application") -> str:
    """Which of the preconditions to registering is missing.

    In the order they occur, so an official reads the next thing to happen
    rather than a list. "Not ready" on its own is not something anyone can act
    on. Shared with the net-metering flow, which is guarded identically.
    """
    if visit is None:
        return f"no visit has been raised for this {noun} yet"
    if visit["status"] != "completed":
        return f"the visit is '{visit['status']}', not completed"
    if not visit["installed_serial_no"]:
        return "the technician recorded no meter serial on that visit"
    if visit["consumer_disputed_at"] is not None:
        return "the household disputes that the work was done"
    return "the household has not confirmed the installation yet"


@router.post(
    "/api/meter-applications/{application_id}/work-order",
    response_model=RaisedOrder,
    status_code=201,
)
async def raise_meter_work_order(
    conn: Conn,
    application_id: UUID,
    payload: WorkOrderRequest,
    principal: Annotated[Principal, Depends(require_role("government", "admin"))],
) -> RaisedOrder:
    """Order the installation.

    The order lands `draft` -- the state that means "needs an assignee" -- and
    is offered to a worker through `POST /api/work-orders/{id}/assignments`,
    the same endpoint and the same three-hour clock a supplier's dispatcher
    uses. Nothing about the assignment lifecycle is special-cased for this
    flow; that is the point of raising a real work order rather than inventing
    a parallel one.

    Site and priority are copied from the application in SQL, so an order
    cannot be filed against application X at address Y.

    The application moves to `under_review`, which is what "a visit is booked"
    looks like on a status the schema already had. There is deliberately no
    `in_progress` enum value: the stage is derivable from the order's own
    status, and a second place to record it is a second place to be wrong.
    """
    row = await conn.fetchrow(sql("meter_application_context"), application_id)
    not_found = HTTPException(status_code=404, detail="application not found")
    if row is None:
        raise not_found
    scope = await _official_scope(conn, principal)
    if scope is not None and row["district"] != scope:
        raise not_found
    if row["status"] not in ("submitted", "under_review"):
        raise HTTPException(
            status_code=409,
            detail=f"this application is already '{row['status']}'",
        )

    async with conn.transaction():
        try:
            order = await conn.fetchrow(
                sql("raise_application_work_order"),
                application_id, principal.account_id,
                "meter_install", payload.scheduled_for,
            )
        except asyncpg.UniqueViolationError:
            # one_order_per_meter_application: the race two officials working
            # one district can produce. A cancelled or failed visit does not
            # block the next, which is what the partial index is for.
            raise HTTPException(
                status_code=409,
                detail="a visit is already open for this application",
            ) from None

        # Only moves it if it was still 'submitted'. Re-raising after a failed
        # visit leaves the application where it is.
        if row["status"] == "submitted":
            await conn.fetchrow(
                sql("decide_meter_application"),
                application_id, "under_review", None, principal.account_id, None,
            )

        await notify(
            conn,
            row["account_id"],
            "meter_application",
            "A visit has been scheduled",
            body=(
                "Your district office has ordered the installation of your "
                "meter. A technician will be assigned shortly, and you will be "
                "asked to confirm once the work is done."
            ),
            severity="info",
            entity_type="meter_application",
            entity_id=str(application_id),
            dedupe_key=f"meterapp:{application_id}:ordered:{order['order_id']}",
        )

    return RaisedOrder(order_id=order["order_id"], site_id=order["site_id"])


@router.post(
    "/api/meter-applications/{application_id}/register",
    response_model=MeterApplication,
)
async def register_applied_meter(
    conn: Conn,
    application_id: UUID,
    principal: Annotated[Principal, Depends(require_role("government", "admin"))],
    payload: MeterRegistration | None = None,
) -> MeterApplication:
    """Issue the meter the visit actually fitted, and close the application.

    Three things have to be true, and none of them is the official's own
    assertion: the visit is `completed`, the household has **confirmed** it,
    and the technician recorded a serial. `register_applied_meter` checks all
    three inside its own WHERE clause and returns no row otherwise, so there is
    no window between checking and issuing.

    The serial is never taken from this request. It comes off the work order,
    where the person holding the meter put it.
    """
    row = await conn.fetchrow(sql("meter_application_context"), application_id)
    not_found = HTTPException(status_code=404, detail="application not found")
    if row is None:
        raise not_found
    scope = await _official_scope(conn, principal)
    if scope is not None and row["district"] != scope:
        raise not_found

    make = (payload.manufacturer or "").strip() or None if payload else None
    model = (payload.model or "").strip() or None if payload else None
    notes = (payload.notes or "").strip() or None if payload else None

    async with conn.transaction():
        company_id = await conn.fetchval(sql("utility_for_site"), row["site_id"])
        try:
            issued = await conn.fetchrow(
                sql("register_applied_meter"),
                application_id, row["account_id"], make, model, company_id,
            )
        except asyncpg.UniqueViolationError:
            raise HTTPException(
                status_code=409,
                detail="that meter serial is already registered",
            ) from None

        if issued is None:
            visit = await conn.fetchrow(
                sql("meter_application_order"), application_id
            )
            raise HTTPException(status_code=409, detail=why_not_ready(visit))

        decided = await conn.fetchrow(
            sql("decide_meter_application"),
            application_id, "accepted", notes,
            principal.account_id, issued["meter_asset_id"],
        )
        if decided is None:
            raise HTTPException(
                status_code=409,
                detail="this application has already been decided",
            )

        await notify(
            conn,
            row["account_id"],
            "meter_application",
            "Your meter has been registered",
            body=(
                f"Meter {issued['serial_no']} is now registered to you. Add it "
                "to a connection from the Meters page and your readings will "
                "begin."
            ),
            severity="info",
            entity_type="meter_application",
            entity_id=str(application_id),
            dedupe_key=f"meterapp:{application_id}:registered",
        )

    rows = await conn.fetch(
        sql("meter_applications_for_account"), row["account_id"]
    )
    return next(
        _application(r) for r in rows if r["application_id"] == application_id
    )
