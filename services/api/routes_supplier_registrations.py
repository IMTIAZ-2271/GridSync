"""Supplier staff registrations: the government's second approval queue.

An installer's staff account used to be gated by one shared string typed into
the sign-up form. The same string for every firm, never rotated, tied to no
invitation -- and the list of firms it let you attach yourself to is public.
That is now a decision a person takes: `POST /api/auth/register/supplier`
writes a `pending` profile, an official in the district it names compares the
applicant's name, National ID and typed organisation against records the form
cannot reach, and only then does the account become a supplier login.

**Approving is also the moment the organisation becomes real.** The applicant
typed a string and nothing was done with it; `supplier_profile.supplier_id` is
NULL until an official resolves the claim, either by linking it to a firm
already on the books or by creating one from it. That is why this endpoint
takes more than a verdict, and why `supplier_approved_has_firm` exists --
approved and belongs-to-a-firm are one fact, made true in one statement.

Resolving the string is a judgement rather than a lookup because
`supplier_company` is one row per firm however many staff logins it has
(decision 4 in docs/decisions.md). If a typed name matched or created a firm
on its own, three spellings would be three firms with three reputations --
exactly the failure this project already fixed for `district`, which was free
text until migration e7c4b19a2d83. The queue offers an exact case-insensitive
match as `suggested_supplier_id`, a shortcut for the ordinary case and never an
answer: NULL there means "nothing obvious matched", not "this is a new firm".

The queue is otherwise the same shape as the worker one in
`routes_workers.py` -- same scope rule, same 404-not-403, same
decided-once guard, same notification. Two queues that answer the same
question in two different ways would be two things to get wrong.

Two things are worth reading `db/sql/dao/supplier_registration_queries.sql`
for rather than trusting this docstring: scope is the official's own district
and is enforced in SQL, not filtered here; and it is scoped on
`supplier_profile.service_district` -- the one region the applicant registered
for -- so a firm working four districts is four decisions by four officials.
"""
from __future__ import annotations

import re
import uuid as uuid_module
from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, model_validator

from .auth import Principal, official_district_scope, require_role
from .db import Conn
from .notify import notify
from .queries import sql

router = APIRouter(tags=["supplier registrations"])


class PendingSupplier(BaseModel):
    """Everything an official needs to decide, on the row.

    There is nothing to open, because there is nothing else to see: the
    decision is a comparison against records held outside this system. The
    person's name, their National ID and the organisation they typed are
    exactly what that comparison is made on.

    `claimed_organisation` is the typed string and is always present.
    `supplier_id` / `supplier_name` are the firm the claim was resolved to and
    are null until it is. `suggested_*` is an exact case-insensitive match on
    the claim, offered so the ordinary case is one click rather than a scan.
    """

    account_id: UUID
    full_name: str
    email: str
    national_id: str | None
    phone: str | None
    job_title: str | None
    claimed_organisation: str
    service_district: str
    approval_status: str
    rejection_reason: str | None
    approved_at: datetime | None
    supplier_id: UUID | None
    supplier_name: str | None
    suggested_supplier_id: UUID | None
    suggested_supplier_name: str | None
    suggested_supplier_code: str | None
    registered_at: datetime


class NewSupplierCompany(BaseModel):
    """A firm an official is creating because nothing on the books matched."""

    name: str = Field(min_length=2, max_length=200)
    # Optional, and UNIQUE on the table so two firms cannot be registered
    # against one licence. An official will often not have it to hand at this
    # moment, and blocking the decision on paperwork the applicant is not
    # holding either would just push the approval into a drawer.
    license_no: str | None = Field(default=None, max_length=100)


class SupplierDecision(BaseModel):
    """A verdict, and -- when it is an approval -- what the claim resolves to.

    Approving takes exactly one of `supplier_id` (link to a firm that exists)
    or `new_supplier` (create one). Neither is accepted on a rejection: there
    is nothing to link a refused application to, and quietly ignoring the field
    would hide a mis-filled form rather than showing it.
    """

    decision: Literal["approve", "reject"]
    # Free text, and only kept on a rejection. Somebody has to be able to tell
    # the applicant what to fix; "rejected" on its own is not a decision they
    # can act on.
    reason: str | None = None
    supplier_id: UUID | None = None
    new_supplier: NewSupplierCompany | None = None

    @model_validator(mode="after")
    def _resolution_matches_the_verdict(self) -> SupplierDecision:
        both = self.supplier_id is not None and self.new_supplier is not None
        neither = self.supplier_id is None and self.new_supplier is None
        if self.decision == "approve":
            if both:
                raise ValueError(
                    "approving takes either an existing supplier_id or a "
                    "new_supplier, not both"
                )
            if neither:
                raise ValueError(
                    "approving must say which organisation this registration "
                    "belongs to: supplier_id, or new_supplier to create one"
                )
        elif not neither:
            raise ValueError("a rejection does not link an organisation")
        return self


def _company_code(name: str) -> str:
    """A unique code for a firm nobody is going to type a code for.

    Readable rather than opaque, since it still shows up in seeds and the
    occasional query -- but uniqueness is what actually matters, so a random
    tail is appended rather than trusting the slug. Derived from a fresh uuid
    for the same reason `register_worker` derives `employee_code` that way: a
    counter would leak how many firms exist.
    """
    slug = re.sub(r"[^A-Z0-9]+", "-", name.upper()).strip("-")[:24] or "SUPPLIER"
    return f"{slug}-{uuid_module.uuid4().hex[:6].upper()}"


async def _resolve_organisation(
    conn: asyncpg.Connection, payload: SupplierDecision
) -> UUID:
    """The firm an approval links to: an existing one, or one created here.

    The model validator has already guaranteed exactly one of the two is set,
    so this only has to check that an id names an *active* firm. A suspended or
    closed installer must not gain new staff, and a miss is 422 rather than 404
    because the official picked from a list -- it means their page is stale,
    not that they were probing.
    """
    if payload.supplier_id is not None:
        company = await conn.fetchrow(
            sql("supplier_company_for_linking"), payload.supplier_id
        )
        if company is None:
            raise HTTPException(
                status_code=422,
                detail="that organisation is not an active installer",
            )
        return company["supplier_id"]

    new = payload.new_supplier
    assert new is not None  # guaranteed by SupplierDecision's validator
    try:
        created = await conn.fetchrow(
            sql("create_supplier_company"),
            _company_code(new.name),
            new.name,
            (new.license_no or "").strip() or None,
        )
    except asyncpg.UniqueViolationError:
        # license_no is the only field an official types that can collide --
        # the code carries a random tail. Naming it matters, because the fix is
        # a different action: link to the firm that already holds the licence.
        raise HTTPException(
            status_code=409,
            detail=(
                "an installer is already registered against that licence "
                "number -- link this registration to that firm instead"
            ),
        ) from None
    return created["supplier_id"]


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
    """Approve or reject one registration, and resolve the organisation.

    An approval says which firm the typed claim belongs to -- an existing one
    by id, or one created here from `new_supplier`. Both paths end in the same
    place: `supplier_id` is written by the same UPDATE that writes the status,
    because `supplier_approved_has_firm` makes those one fact rather than two
    steps something could interrupt between.

    409 rather than a silent overwrite when the row is no longer pending: two
    officials working the same queue is the normal case, and the second must
    see that a decision was already made rather than quietly replacing whose
    name is on it. A firm created moments earlier rolls back with it -- all of
    this is one transaction, so a losing official leaves no stray company
    behind.
    """
    scope = await official_district_scope(conn, principal)

    async with conn.transaction():
        before = await conn.fetchrow(sql("supplier_approval_row"), account_id, scope)
        if before is None:
            raise HTTPException(
                status_code=404, detail="supplier registration not found"
            )

        approved = payload.decision == "approve"
        supplier_id = await _resolve_organisation(conn, payload) if approved else None

        decided = await conn.fetchval(
            sql("decide_supplier_registration"),
            account_id,
            "approved" if approved else "rejected",
            principal.account_id,
            (payload.reason or "").strip() or None,
            scope,
            supplier_id,
        )
        if decided is None:
            raise HTTPException(
                status_code=409,
                detail=(
                    "this registration has already been decided "
                    f"({before['approval_status']})"
                ),
            )

        if approved:
            # The official has just asserted that this firm works this
            # district. Recording it is what keeps a newly created installer
            # visible to households -- requirement 7's supplier list is
            # filtered by district, so a firm with no service area is a firm
            # nobody can choose.
            await conn.execute(
                sql("add_supplier_service_area"),
                supplier_id,
                before["service_district"],
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
                # The firm the official linked, not the string that was typed.
                # This is the first time the applicant learns which one their
                # claim resolved to, and it may not be spelled the way they
                # wrote it.
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
