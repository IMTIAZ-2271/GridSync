"""Net-metering agreements: the government's approval queue."""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .auth import Principal, require_role
from .db import Conn
from .notify import notify_site_owner
from .queries import sql

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
