"""Solar installation applications: a household asks an installer to fit panels.

Consumer requirement 7's install half and supplier requirement 1 — the same
object seen from both ends, which is why they are one router.

The *net-metering* half of requirement 7 is a different thing and has worked
since the government portal landed: that is the regulator approving an export
agreement, and it happens **after** the panels exist. This is the step before —
choosing who fits them. Conflating the two would have produced one queue that
neither the installer nor the regulator could work.

Two identities may touch an application and they may do different things. The
household submits it and may withdraw it. The installer's staff review, accept,
reject, and mark it done. Neither can do the other's half, and an application
belonging to neither is a 404.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from .auth import Principal, require_role
from .db import Conn
from .notify import notify
from .queries import sql
from .types import Energy

router = APIRouter(tags=["solar applications"])

# What each side may set, and from where. The household's only move is to
# withdraw; everything else is the installer's. Kept as data rather than as
# branches so the two role checks below read as one rule each.
CONSUMER_MOVES: dict[str, tuple[str, ...]] = {
    "withdrawn": ("submitted", "under_review"),
}
SUPPLIER_MOVES: dict[str, tuple[str, ...]] = {
    "under_review": ("submitted",),
    "accepted": ("submitted", "under_review"),
    "rejected": ("submitted", "under_review"),
    # 'completed' is the installer saying the panels are on the roof. It is
    # deliberately reachable only from 'accepted': a job nobody agreed to
    # cannot have been finished.
    "completed": ("accepted",),
}


class SolarApplication(BaseModel):
    application_id: UUID
    site_id: UUID
    site_label: str
    district: str
    billing_point_id: UUID
    point_label: str
    supplier_id: UUID
    supplier_name: str
    status: str
    requested_capacity_kw: Energy
    panel_count: int | None
    notes: str | None
    submitted_at: datetime
    decided_at: datetime | None
    decision_notes: str | None
    installed_array_id: UUID | None
    # Consumer view only.
    supplier_email: str | None = None
    supplier_phone: str | None = None
    # Supplier view only -- who to call, and whether this roof already has
    # panels (an uprate is real work, but the installer should see it first).
    address_line: str | None = None
    account_id: UUID | None = None
    account_name: str | None = None
    account_phone: str | None = None
    site_has_solar: bool | None = None


class ApplicationCreate(BaseModel):
    billing_point_id: UUID
    supplier_id: UUID
    requested_capacity_kw: Decimal = Field(gt=0, le=1000)
    panel_count: int | None = Field(default=None, gt=0, le=10000)
    notes: str | None = Field(default=None, max_length=2000)


class ApplicationDecision(BaseModel):
    status: Literal[
        "under_review", "accepted", "rejected", "withdrawn", "completed"
    ]
    notes: str | None = Field(default=None, max_length=2000)


async def _supplier_id_for(conn: asyncpg.Connection, principal: Principal) -> UUID:
    """The firm this staff account belongs to.

    A supplier login with no `supplier_profile` gets 403 rather than being
    silently treated as every firm at once. That state is not hypothetical:
    `supplier@demo.com` was exactly this until seed_orgs.sql was taught to
    attach a profile — see CLAUDE.md.
    """
    supplier_id = await conn.fetchval(
        "SELECT supplier_id FROM supplier_profile WHERE account_id = $1",
        principal.account_id,
    )
    if supplier_id is None:
        raise HTTPException(
            status_code=403, detail="this account is not attached to a supplier"
        )
    return supplier_id


@router.post(
    "/api/solar-applications", response_model=SolarApplication, status_code=201
)
async def create_application(
    conn: Conn,
    payload: ApplicationCreate,
    principal: Annotated[Principal, Depends(require_role("consumer"))],
) -> SolarApplication:
    """Apply to have panels fitted on one of your connections.

    Keyed on a billing point, not a site (rule 3): a household with two
    connections may fit panels on one and not the other, and each may hold its
    own live application.

    The installer must actually serve the site's district. The dropdown is
    already filtered, but a filtered dropdown is a convenience and this is the
    check — a request naming a firm that does not work there is refused rather
    than accepted because the UI would not normally have offered it.
    """
    point = await conn.fetchrow(
        "SELECT bp.point_id, bp.site_id, s.district "
        "FROM billing_point bp JOIN site s ON s.site_id = bp.site_id "
        "WHERE bp.point_id = $1 AND s.account_id = $2",
        payload.billing_point_id, principal.account_id,
    )
    if point is None:
        raise HTTPException(status_code=404, detail="connection not found")

    serves = await conn.fetchval(
        sql("supplier_serves_district"), payload.supplier_id, point["district"]
    )
    if not serves:
        raise HTTPException(
            status_code=422,
            detail=f"that installer does not work in {point['district']}",
        )

    async with conn.transaction():
        try:
            await conn.fetchval(
                sql("create_solar_application"),
                point["site_id"], point["point_id"], principal.account_id,
                payload.supplier_id, payload.requested_capacity_kw,
                payload.panel_count,
                (payload.notes or "").strip() or None,
            )
        except asyncpg.UniqueViolationError:
            # solar_application_one_open. Withdraw the live one and apply again;
            # see the note on create_solar_application for why this is not an
            # upsert.
            raise HTTPException(
                status_code=409,
                detail=(
                    "this connection already has an application waiting. "
                    "Withdraw it before applying again."
                ),
            ) from None
        except asyncpg.ForeignKeyViolationError:
            raise HTTPException(status_code=422, detail="no such installer") from None

        rows = await conn.fetch(
            sql("solar_applications_for_account"), principal.account_id
        )
    return SolarApplication(**dict(rows[0]))


@router.get("/api/solar-applications", response_model=list[SolarApplication])
async def list_applications(
    conn: Conn,
    principal: Annotated[
        Principal, Depends(require_role("consumer", "supplier", "admin"))
    ],
    open_only: bool = Query(default=False),
) -> list[SolarApplication]:
    """The household's own applications, or the installer's inbox.

    Which one you get is decided by the role, not by a parameter: these are two
    scoped statements, so a row the caller may not see is never fetched.
    Government is absent on purpose — the regulator's involvement starts at the
    net-metering agreement, once there is something to export.
    """
    if principal.role == "consumer":
        rows = await conn.fetch(
            sql("solar_applications_for_account"), principal.account_id
        )
    else:
        supplier_id = await _supplier_id_for(conn, principal)
        rows = await conn.fetch(
            sql("solar_applications_for_supplier"), supplier_id, open_only
        )
    return [SolarApplication(**dict(r)) for r in rows]


@router.patch(
    "/api/solar-applications/{application_id}", response_model=SolarApplication
)
async def decide_application(
    conn: Conn,
    application_id: UUID,
    payload: ApplicationDecision,
    principal: Annotated[
        Principal, Depends(require_role("consumer", "supplier", "admin"))
    ],
) -> SolarApplication:
    """Move an application along, from whichever end you are.

    The legal moves are a table (`CONSUMER_MOVES` / `SUPPLIER_MOVES`), and they
    encode two things worth stating: a household may only ever withdraw, and
    'completed' is reachable only from 'accepted' — a job nobody agreed to
    cannot have been finished.

    The write is guarded on the status that was read, so two people working the
    same queue produce one decision and the second gets a 409 rather than
    quietly replacing the first. That also keeps `decided_by_account_id`
    truthful.
    """
    async with conn.transaction():
        app = await conn.fetchrow(sql("solar_application_context"), application_id)
        if app is None:
            raise HTTPException(status_code=404, detail="application not found")

        if principal.role == "consumer":
            if app["account_id"] != principal.account_id:
                raise HTTPException(status_code=404, detail="application not found")
            allowed = CONSUMER_MOVES
        else:
            supplier_id = await _supplier_id_for(conn, principal)
            if app["supplier_id"] != supplier_id and principal.role != "admin":
                raise HTTPException(status_code=404, detail="application not found")
            allowed = SUPPLIER_MOVES

        froms = allowed.get(payload.status)
        if froms is None:
            raise HTTPException(
                status_code=403,
                detail=f"your role cannot set an application to {payload.status}",
            )
        if app["status"] not in froms:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"an application that is {app['status']} cannot become "
                    f"{payload.status}"
                ),
            )

        decided = await conn.fetchval(
            sql("decide_solar_application"),
            application_id, payload.status, app["status"],
            principal.account_id, (payload.notes or "").strip() or None,
        )
        if decided is None:
            raise HTTPException(
                status_code=409, detail="someone else decided this first"
            )

        await _announce(conn, app, payload, principal)

        rows = await conn.fetch(
            sql("solar_applications_for_account"), app["account_id"]
        ) if principal.role != "consumer" else await conn.fetch(
            sql("solar_applications_for_account"), principal.account_id
        )
    match = next(r for r in rows if r["application_id"] == application_id)
    return SolarApplication(**dict(match))


# What the household is told, and what the installer is told. A withdrawal
# travels the other way -- the firm has been holding a slot for it.
_HOUSEHOLD: dict[str, tuple[str, str, str]] = {
    "under_review": ("info", "Your solar application is being reviewed",
                     "{supplier} has picked up your application."),
    "accepted": ("info", "Your solar application was accepted",
                 "{supplier} has accepted your application and will be in touch."),
    "rejected": ("warning", "Your solar application was not accepted",
                 "{supplier} could not take on this installation."),
    "completed": ("info", "Your installation is recorded as complete",
                  "{supplier} has marked the work finished. Apply for net "
                  "metering next so your exports start earning credit."),
}


async def _announce(
    conn: asyncpg.Connection,
    app: asyncpg.Record,
    payload: ApplicationDecision,
    principal: Principal,
) -> None:
    """Tell the other party. Never the one who acted."""
    if principal.role == "consumer":
        # A withdrawal reaches the firm, not the household that just did it.
        # There is no single account for a company, so this goes nowhere yet --
        # `supplier_profile` can hold several logins and picking one at random
        # would be arbitrary. The inbox is where the firm sees it, and the row
        # simply leaves the open queue.
        return

    supplier_name = await conn.fetchval(
        "SELECT name FROM supplier_company WHERE supplier_id = $1",
        app["supplier_id"],
    )
    update = _HOUSEHOLD.get(payload.status)
    if update is None:
        return
    severity, title, body = update
    await notify(
        conn,
        app["account_id"],
        "solar_application",
        title,
        body=body.format(supplier=supplier_name)
        + (f" {payload.notes.strip()}" if payload.notes and payload.notes.strip() else ""),
        severity=severity,
        entity_type="solar_application",
        entity_id=str(app["application_id"]),
        # Keyed on the state, not the moment: the status guard already makes a
        # repeat impossible, so this only has to survive a retried request.
        dedupe_key=f"solar_app:{app['application_id']}:{payload.status}",
    )
