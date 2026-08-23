"""Registration, login, and the current-account probe.

Each role registers differently because each role's identity hangs off a
different key: a household has only itself, a worker has a region and possibly
an employer, an official has a pre-issued code, a supplier has a company. See
the header of db/sql/dao/auth_queries.sql.

Two things are true of all four. Every registration collects a National ID
(consumer requirement 1, worker requirement 1), normalized to digits so
`account.national_id`'s UNIQUE constraint actually catches a repeat
registration. And every one of them runs in a single transaction, so a failure
after the account row -- an unclaimable official code, a company that does not
serve the district -- leaves no half-registered account behind.
"""
from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

import asyncpg
from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, EmailStr, Field

from .auth import (
    SEED_PASSWORD_PLACEHOLDER,
    CurrentAccount,
    hash_password,
    issue_token,
    registration_code,
    verify_password,
)
from .orgs import (
    NATIONAL_ID_HELP,
    normalized_national_id,
    resolve_district,
)
from .db import Conn
from .queries import sql

router = APIRouter(prefix="/api/auth", tags=["auth"])


# --------------------------------------------------------------------------
# Models
# --------------------------------------------------------------------------

class WorkerContext(BaseModel):
    """What a worker's own row says about them, resolved at sign-in.

    Worker requirement 3: the portal must not ask a worker whether they are a
    government or private worker, and must not take their word for it if it
    did. This comes from worker_profile, and `approval_status` is what gates
    a government worker's queue until an official in their district approves
    them.
    """

    worker_kind: str
    approval_status: str
    service_district: str
    rejection_reason: str | None = None
    distribution_company_id: UUID | None = None
    distribution_company_code: str | None = None
    distribution_company_name: str | None = None


class AccountOut(BaseModel):
    account_id: UUID
    email: str
    full_name: str
    phone: str | None = None
    role: str
    status: str
    created_at: datetime | None = None
    national_id: str | None = None
    # Present only for the role it belongs to; null for everyone else.
    worker: WorkerContext | None = None
    supplier_id: UUID | None = None
    supplier_name: str | None = None
    government_district: str | None = None


class TokenOut(BaseModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_in: int
    account: AccountOut


class LoginIn(BaseModel):
    # Plain str, not EmailStr. Login is a lookup, not a validation: rejecting a
    # syntactically odd address here would answer 422 where the honest answer
    # is 401, and EmailStr also refuses reserved domains (.test, .local) that
    # the citext column accepts perfectly well.
    email: str
    password: str


class RegisterBase(BaseModel):
    email: EmailStr
    # 8 is the floor, not a recommendation. There is no rotation, no lockout
    # and no breach check here; a real deployment needs all three.
    password: str = Field(min_length=8, max_length=128)
    full_name: str = Field(min_length=1, max_length=200)
    # Required for every human registering here. account.national_id stays
    # nullable in the column because the seeded demo rows predate the
    # requirement and have none -- making it NOT NULL would mean inventing
    # identity numbers for eight fictional households. It is UNIQUE, so the
    # database still refuses the same NID twice.
    national_id: str = Field(min_length=6, max_length=32)


class CustomerRegisterIn(RegisterBase):
    phone: str | None = Field(default=None, max_length=40)


class WorkerRegisterIn(RegisterBase):
    phone: str | None = Field(default=None, max_length=40)
    worker_kind: Literal["government", "private"]
    service_district: str = Field(min_length=1, max_length=100)
    # Required for a government worker and refused for a private one -- see
    # the handler, and worker_kind_employer on the table.
    distribution_company_id: UUID | None = None
    # Optional, and a demo affordance rather than part of the requirement:
    # supplying the code printed on a seeded worker's badge claims that
    # existing profile instead of creating a new one, so the demo worker keeps
    # the assignments the seed already gave them.
    employee_code: str | None = Field(default=None, max_length=50)


class GovernmentRegisterIn(RegisterBase):
    """Government requirement 1: a unique, pre-issued ID.

    Not the shared secret the other staff role still uses. The code names the
    district its holder governs, so their scope is issued to them rather than
    typed by them.
    """

    official_code: str = Field(min_length=1, max_length=100)


class SupplierRegisterIn(RegisterBase):
    registration_code: str = Field(min_length=1, max_length=100)
    # Which installer this person works for. Companies are seeded; a supplier
    # account joins one rather than inventing one, so the firm a consumer
    # picks from a dropdown and rates is a single row however many staff
    # logins it has.
    supplier_code: str = Field(min_length=1, max_length=50)
    job_title: str | None = Field(default=None, max_length=100)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

_EMAIL_TAKEN = HTTPException(
    status_code=status.HTTP_409_CONFLICT,
    detail="an account with that email already exists",
)

_NID_TAKEN = HTTPException(
    status_code=status.HTTP_409_CONFLICT,
    detail="an account is already registered under that National ID",
)


def _duplicate(exc: asyncpg.UniqueViolationError) -> HTTPException:
    """Which uniqueness the database refused.

    Both account.email and account.national_id are UNIQUE and either can lose
    the race, so the constraint name is read rather than assumed -- telling
    someone their email is taken when it was their NID sends them off to
    recover an account that does not exist.
    """
    name = (getattr(exc, "constraint_name", "") or "") + str(exc)
    return _NID_TAKEN if "national_id" in name else _EMAIL_TAKEN


async def _account_out(
    conn: asyncpg.Connection, account_id: UUID
) -> AccountOut:
    """The account row plus whatever its role hangs off.

    `account_profile` LEFT JOINs the supplier and government profiles, which
    are one row each. The worker context is a second statement rather than a
    third join: it is only ever wanted for one role, and folding it in would
    put five always-null columns on every consumer's sign-in.
    """
    row = await conn.fetchrow(sql("account_profile"), account_id)
    account = AccountOut(**dict(row))

    if account.role == "worker":
        state = await conn.fetchrow(sql("worker_registration_state"), account_id)
        # A 'worker' account with no worker_profile is possible only for a
        # seeded row mid-claim; treat the context as simply absent rather than
        # failing the whole sign-in over it.
        if state is not None:
            account.worker = WorkerContext(**dict(state))

    return account


async def _token_response_for(
    conn: asyncpg.Connection, account_id: UUID
) -> TokenOut:
    account = await _account_out(conn, account_id)
    token, expires_in = issue_token(
        account.account_id, account.role, account.email
    )
    return TokenOut(
        access_token=token, expires_in=expires_in, account=account
    )


# --------------------------------------------------------------------------
# Login
# --------------------------------------------------------------------------

@router.post("/login", response_model=TokenOut)
async def login(conn: Conn, payload: LoginIn) -> TokenOut:
    row = await conn.fetchrow(sql("account_by_email"), payload.email)

    # One message for every failure -- unknown email, wrong password, and an
    # unclaimed seed account are indistinguishable to the caller. Anything more
    # specific is an account-enumeration oracle.
    invalid = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="incorrect email or password",
    )
    if row is None:
        # Still spend the time an argon2 verify would, so a missing account is
        # not detectable by how fast this returns.
        verify_password(payload.password, SEED_PASSWORD_PLACEHOLDER)
        raise invalid
    if not verify_password(payload.password, row["password_hash"]):
        raise invalid
    if row["status"] != "active":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"this account is {row['status']}",
        )

    return await _token_response_for(conn, row["account_id"])


@router.get("/me", response_model=AccountOut)
async def me(conn: Conn, principal: CurrentAccount) -> AccountOut:
    """The signed-in account, with its role context.

    Worker requirement 3 is served here: the portal asks who it is talking to
    and the answer includes `worker.worker_kind` and
    `worker.approval_status`, read from the database rather than from anything
    the worker asserted.
    """
    return await _account_out(conn, principal.account_id)


# --------------------------------------------------------------------------
# Registration
#
# Four roles, four shapes, because each one's identity hangs off a different
# key. What they share: a National ID, and a single transaction that rolls the
# account back if anything after it fails.
# --------------------------------------------------------------------------

async def _new_account(
    conn: asyncpg.Connection, payload: RegisterBase, role: str,
    phone: str | None = None,
) -> UUID:
    """Create the account row, translating either uniqueness into a 409."""
    try:
        return await conn.fetchval(
            sql("create_account"),
            payload.email,
            hash_password(payload.password),
            payload.full_name.strip(),
            phone,
            normalized_national_id(payload.national_id),
            role,
        )
    except asyncpg.UniqueViolationError as exc:
        raise _duplicate(exc) from None


@router.post(
    "/register/customer",
    response_model=TokenOut,
    status_code=status.HTTP_201_CREATED,
)
async def register_customer(conn: Conn, payload: CustomerRegisterIn) -> TokenOut:
    """Register a household.

    Consumer requirements 1 and 2: a National ID is required, and a billing
    meter ID is **not** asked for here. Registration creates the account and
    stops. The customer portal detects the empty site list and walks them
    through building a service point (POST /api/sites, then /meter, then
    optionally /solar, then /bill), and adding more billing meters later is
    the same /meter call again.

    Claiming an already-metered connection by its serial used to happen on
    this endpoint. It moved to POST /api/sites/claim, behind a login, for the
    same reason requirement 2 gives: a meter ID is not part of proving who you
    are, and asking for one at the door turns away every household that does
    not have the number to hand.
    """
    async with conn.transaction():
        account_id = await _new_account(
            conn, payload, "consumer", payload.phone
        )
        return await _token_response_for(conn, account_id)


@router.post(
    "/register/worker",
    response_model=TokenOut,
    status_code=status.HTTP_201_CREATED,
)
async def register_worker(conn: Conn, payload: WorkerRegisterIn) -> TokenOut:
    """Register a field worker.

    Worker requirements 1 and 2. A worker declares which kind they are and
    which region they cover:

    * **private** — no company, and usable immediately. They handle private
      jobs in their region and never receive government work orders.
    * **government** — must name the distribution company that employs them,
      and that company must actually serve the region they claim. The profile
      is created `pending` and an official in that district approves it
      (`PATCH /api/workers/{id}/approval`). Until then they can sign in and
      see their own status, and nothing else.

    Passing `employee_code` instead claims a worker profile the seed already
    created. That is a demo affordance, not part of the requirement, and it
    exists because `work_order_assignment` and `worker_skill` both reference
    `worker_profile(account_id)` — creating a fresh account for a seeded
    worker would strand every assignment they already hold.
    """
    if payload.employee_code:
        return await _claim_seeded_worker(conn, payload)

    district, _lat, _lon = await resolve_district(conn, payload.service_district)

    if payload.worker_kind == "government":
        if payload.distribution_company_id is None:
            raise HTTPException(
                status_code=422,
                detail=(
                    "a government worker must name the distribution company "
                    "they work for"
                ),
            )
        serves = await conn.fetchval(
            sql("distribution_company_serves"),
            payload.distribution_company_id,
            district,
        )
        if not serves:
            # Refused rather than warned: the approval in requirement 2 is
            # done by officials of this district, and a worker filed under a
            # company with no presence here would sit in a queue nobody owns.
            raise HTTPException(
                status_code=422,
                detail=f"that distribution company does not serve {district}",
            )
        company_id = payload.distribution_company_id
        approval = "pending"
    else:
        if payload.distribution_company_id is not None:
            raise HTTPException(
                status_code=422,
                detail=(
                    "a private worker does not belong to a distribution "
                    "company"
                ),
            )
        company_id = None
        approval = "approved"

    async with conn.transaction():
        account_id = await _new_account(conn, payload, "worker", payload.phone)
        # Derived from the account id rather than a counter: it has to be
        # unique and it has to be generated inside this transaction, and a
        # sequence would leak how many workers have registered.
        employee_code = f"W-{str(account_id)[:8].upper()}"
        try:
            await conn.execute(
                sql("create_worker_profile"),
                account_id, employee_code, district, payload.worker_kind,
                company_id, approval,
            )
        except asyncpg.UniqueViolationError:
            raise HTTPException(
                status_code=409,
                detail="that employee code is already in use; try again",
            ) from None
        return await _token_response_for(conn, account_id)


async def _claim_seeded_worker(
    conn: asyncpg.Connection, payload: WorkerRegisterIn
) -> TokenOut:
    """Take over a worker profile the seed created, keeping its assignments."""
    async with conn.transaction():
        profile = await conn.fetchrow(
            sql("worker_profile_by_employee_code"),
            payload.employee_code.strip(),
            SEED_PASSWORD_PLACEHOLDER,
        )
        if profile is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=(
                    f"no active worker profile with code "
                    f"'{payload.employee_code}'."
                ),
            )
        if not profile["is_unclaimed"]:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"employee code '{payload.employee_code}' has already been "
                    "registered."
                ),
            )

        try:
            claimed = await conn.fetchval(
                sql("claim_account"),
                profile["account_id"],
                payload.email,
                hash_password(payload.password),
                payload.full_name.strip(),
                payload.phone,
                SEED_PASSWORD_PLACEHOLDER,
            )
        except asyncpg.UniqueViolationError as exc:
            raise _duplicate(exc) from None

        if claimed is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="that employee code was claimed while you were registering",
            )

        # The NID belongs to the person, not to the seeded placeholder row.
        try:
            await conn.execute(
                "UPDATE account SET national_id = $2 WHERE account_id = $1",
                claimed, normalized_national_id(payload.national_id),
            )
        except asyncpg.UniqueViolationError:
            raise _NID_TAKEN from None

        return await _token_response_for(conn, claimed)


@router.post(
    "/register/government",
    response_model=TokenOut,
    status_code=status.HTTP_201_CREATED,
)
async def register_government(
    conn: Conn, payload: GovernmentRegisterIn
) -> TokenOut:
    """Register a regulator against a pre-issued official code.

    Government requirement 1. Each code is issued to one named official,
    carries the district they govern, and is claimable exactly once — so an
    official's scope comes from what was issued to them rather than from what
    they type about themselves, and a leaked code burns one seat rather than
    handing out regulator access indefinitely.

    This replaces the single shared secret for this role. Supplier
    registration still uses one; see `register_supplier`.
    """
    code_row = await conn.fetchrow(
        sql("government_code_for_claim"), payload.official_code
    )
    # Unknown and already-claimed get the same 403 on purpose: distinguishing
    # them tells someone probing codes when they have guessed a real one.
    invalid = HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="that official code is not valid or has already been used",
    )
    if code_row is None or code_row["is_claimed"]:
        raise invalid

    async with conn.transaction():
        account_id = await _new_account(conn, payload, "government")

        claimed = await conn.fetchrow(
            sql("claim_government_code"), code_row["code"], account_id
        )
        if claimed is None:
            # Someone claimed it between the check and the update. The
            # transaction rolls back, taking the account with it.
            raise invalid

        await conn.execute(
            sql("create_government_profile"),
            account_id, claimed["district"], claimed["code"],
        )
        return await _token_response_for(conn, account_id)


@router.post(
    "/register/supplier",
    response_model=TokenOut,
    status_code=status.HTTP_201_CREATED,
)
async def register_supplier(conn: Conn, payload: SupplierRegisterIn) -> TokenOut:
    """Register a solar installer's staff account against their company.

    Two things are checked: the shared registration code, and which company
    this person works for. The company matters because it — not the account —
    is what a consumer picks from a dropdown, applies to, and rates, so a firm
    with three staff logins stays one supplier with one reputation.

    The shared code is still the weakest part of this: unlike government's
    per-official codes it does not rotate and is not tied to an invitation, so
    anyone holding it can attach themselves to any seeded company. Recorded as
    a known weakness rather than quietly ignored.
    """
    if payload.registration_code.strip() != registration_code("supplier"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="that registration code is not valid",
        )

    company = await conn.fetchrow(
        sql("supplier_company_by_code"), payload.supplier_code
    )
    if company is None:
        raise HTTPException(
            status_code=422,
            detail=f"no active supplier with code '{payload.supplier_code}'",
        )

    async with conn.transaction():
        account_id = await _new_account(conn, payload, "supplier")
        await conn.execute(
            sql("create_supplier_profile"),
            account_id, company["supplier_id"], payload.job_title,
        )
        return await _token_response_for(conn, account_id)
