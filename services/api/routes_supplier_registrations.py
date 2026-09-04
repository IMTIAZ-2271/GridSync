"""Supplier staff registrations: the government's second approval queue.

An installer's staff account used to be gated by one shared string typed into
the sign-up form. The same string for every firm, never rotated, tied to no
invitation -- and the list of firms it let you attach yourself to is public.
That is now a decision a person takes: `POST /api/auth/register/supplier`
writes a `pending` profile, an official in the district it names compares the
applicant's name, National ID and organisation against records the form cannot
reach, and only then does the account become a supplier login.

The queue is deliberately the same shape as the worker one in
`routes_workers.py` -- same scope rule, same 404-not-403, same
decided-once guard, same notification. Two queues that answer the same
question in two different ways would be two things to get wrong.

Two things are worth reading `db/sql/dao/supplier_registration_queries.sql`
for rather than trusting this docstring: scope is the official's own district
and is enforced in SQL, not filtered here; and it is scoped on
`supplier_profile.service_district` -- the one region the applicant registered
for -- not on every district their firm covers, so a firm working four
districts is four decisions by four officials.
"""
from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .auth import Principal, official_district_scope, require_role
from .db import Conn
from .notify import notify
from .queries import sql

router = APIRouter(tags=["supplier registrations"])


class PendingSupplier(BaseModel):
    """Everything an official needs to decide, on the row.

    There is nothing to open, because there is nothing else to see: the
    decision is a comparison against records held outside this system. Name,
    National ID, the organisation claimed and its licence number are exactly
    what that comparison needs.
    """

    account_id: UUID
    full_name: str
    email: str
    national_id: str | None
    phone: str | None
    job_title: str | None
    supplier_id: UUID
    supplier_code: str
    supplier_name: str
    license_no: str | None
    service_district: str
    approval_status: str
    rejection_reason: str | None
    approved_at: datetime | None
    registered_at: datetime


class SupplierDecision(BaseModel):
    decision: Literal["approve", "reject"]
    # Free text, and only kept on a rejection. Somebody has to be able to tell
    # the applicant what to fix; "rejected" on its own is not a decision they
    # can act on.
    reason: str | None = None


@router.get(
    "/api/supplier-registrations/pending", response_model=list[PendingSupplier]
)
async def pending_supplier_registrations(
    conn: Conn,
    principal: Annotated[Principal, Depends(require_role("government", "admin"))],
) -> list[PendingSupplier]:
    """Staff accounts awaiting a decision in this official's district."""
    rows = await conn.fetch(
        sql("pending_supplier_registrations"),
        await official_district_scope(conn, principal),
    )
    return [PendingSupplier(**dict(r)) for r in rows]


@router.patch(
    "/api/supplier-registrations/{account_id}/approval",
    response_model=PendingSupplier,
)
async def decide_supplier_registration(
    conn: Conn,
    account_id: UUID,
    payload: SupplierDecision,
    principal: Annotated[Principal, Depends(require_role("government", "admin"))],
) -> PendingSupplier:
    """Approve or reject one registration.

    409 rather than a silent overwrite when the row is no longer pending: two
    officials working the same queue is the normal case, and the second must
    see that a decision was already made rather than quietly replacing whose
    name is on it.
    """
    scope = await official_district_scope(conn, principal)

    async with conn.transaction():
        before = await conn.fetchrow(sql("supplier_approval_row"), account_id, scope)
        if before is None:
            raise HTTPException(
                status_code=404, detail="supplier registration not found"
            )

        approved = payload.decision == "approve"
        decided = await conn.fetchval(
            sql("decide_supplier_registration"),
            account_id,
            "approved" if approved else "rejected",
            principal.account_id,
            (payload.reason or "").strip() or None,
            scope,
        )
        if decided is None:
            raise HTTPException(
                status_code=409,
                detail=(
                    "this registration has already been decided "
                    f"({before['approval_status']})"
                ),
            )

        row = await conn.fetchrow(sql("supplier_approval_row"), account_id, scope)

        # Inside the transaction, so a rolled-back decision cannot leave a
        # notification announcing it. The applicant is the only person who
        # needs telling: this is the answer to something they asked for.
        await notify(
            conn,
            account_id,
            "supplier_approval",
            "Registration approved" if approved else "Registration not approved",
            body=(
                f"You can now work for {row['supplier_name']} in "
                f"{row['service_district']}."
                if approved else
                "Your supplier registration was not approved."
                + (f" {row['rejection_reason']}" if row["rejection_reason"] else "")
            ),
            severity="info" if approved else "warning",
            entity_type="supplier_profile",
            entity_id=str(account_id),
            # One decision, one notification. The status guard above already
            # makes a second decision impossible, so this only has to survive a
            # retried request.
            dedupe_key=f"supplier:{account_id}:approval",
        )
    return PendingSupplier(**dict(row))
