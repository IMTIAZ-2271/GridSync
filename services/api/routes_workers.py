"""Worker registrations: the government's approval queue.

Government requirement 3. **Both kinds of worker land here now.** A private
worker used to be approved by the act of registering -- anyone who filled the
form in could be dispatched to a household's meter -- so the region's
officials decide those too, on the same evidence they get for a government
worker: a name, a National ID, a region, and for a government worker the
utility that employs them.

The queue blocks real work rather than paperwork. `offerable_worker` refuses
to offer a job to a pending profile, and `require_role` refuses the worker
portal outright, so an undecided registration cannot be dispatched to
anything.

Its twin is `routes_supplier_registrations.py`, which does the same for an
installer's staff accounts. Both share `official_district_scope`.

Two things are worth reading the SQL for rather than trusting this docstring:
scope is the official's own district and is enforced in `db/sql/dao/
worker_queries.sql`, not filtered here; and a worker outside that district
answers **404, not 403**, because 403 would confirm the account exists to an
official who has no business learning who is registered next door.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .auth import Principal, official_district_scope, require_role
from .db import Conn
from .notify import notify
from .queries import sql

router = APIRouter(tags=["workers"])


class PendingWorker(BaseModel):
    account_id: UUID
    full_name: str
    email: str
    national_id: str | None
    employee_code: str
    service_district: str
    worker_kind: str
    availability: str
    max_daily_jobs: int
    hired_on: date
    approval_status: str
    rejection_reason: str | None
    approved_at: datetime | None
    distribution_company_id: UUID | None
    distribution_company_name: str | None
    registered_at: datetime


class WorkerDecision(BaseModel):
    decision: Literal["approve", "reject"]
    # Free text, and only kept on a rejection. Somebody has to be able to tell
    # the applicant what to fix; "rejected" on its own is not a decision they
    # can act on.
    reason: str | None = None


@router.get("/api/workers/pending", response_model=list[PendingWorker])
async def pending_workers(
    conn: Conn,
    principal: Annotated[Principal, Depends(require_role("government", "admin"))],
) -> list[PendingWorker]:
    """Registrations awaiting a decision, newest first.

    Government and admin only, and field workers only: an installer's staff
    accounts are the same decision about a different subject and have their own
    queue (`/api/supplier-registrations/pending`). Keeping them apart is what
    lets each page show the evidence its decision actually turns on -- an
    employing utility for one, an organisation and a licence number for the
    other.
    """
    rows = await conn.fetch(
        sql("pending_workers"), await official_district_scope(conn, principal)
    )
    return [PendingWorker(**dict(r)) for r in rows]


@router.patch(
    "/api/workers/{account_id}/approval", response_model=PendingWorker
)
async def decide_worker_approval(
    conn: Conn,
    account_id: UUID,
    payload: WorkerDecision,
    principal: Annotated[Principal, Depends(require_role("government", "admin"))],
) -> PendingWorker:
    """Approve or reject one registration.

    409 rather than a silent overwrite when the row is no longer pending: two
    officials working the same queue is the normal case, and the second one must
    see that a decision was already made rather than quietly replacing whose
    name is on it.
    """
    scope = await official_district_scope(conn, principal)

    async with conn.transaction():
        before = await conn.fetchrow(sql("worker_approval_row"), account_id, scope)
        if before is None:
            raise HTTPException(status_code=404, detail="worker not found")

        approved = payload.decision == "approve"
        decided = await conn.fetchval(
            sql("decide_worker_approval"),
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

        row = await conn.fetchrow(sql("worker_approval_row"), account_id, scope)

        # Inside the transaction, so a rolled-back decision cannot leave a
        # notification announcing it. The worker is the only person who needs
        # telling: this is the answer to something they applied for.
        await notify(
            conn,
            account_id,
            "worker_approval",
            "Registration approved" if approved else "Registration not approved",
            body=(
                f"You can now be assigned work orders in "
                f"{row['service_district']}."
                if approved else
                "Your worker registration was not approved."
                + (f" {row['rejection_reason']}" if row["rejection_reason"] else "")
            ),
            severity="info" if approved else "warning",
            entity_type="worker_profile",
            entity_id=str(account_id),
            # One decision, one notification. The status guard above already
            # makes a second decision impossible, so this only has to survive a
            # retried request.
            dedupe_key=f"worker:{account_id}:approval",
        )
    return PendingWorker(**dict(row))
