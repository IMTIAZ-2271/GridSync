"""Registration, login, and the current-account probe.

Registration is where a new user picks up existing seeded data. Each role does
it differently because each role's data hangs off a different key -- see the
header of db/sql/dao/auth_queries.sql for why.
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
from .db import Conn
from .queries import sql

router = APIRouter(prefix="/api/auth", tags=["auth"])


# --------------------------------------------------------------------------
# Models
# --------------------------------------------------------------------------

class AccountOut(BaseModel):
    account_id: UUID
    email: str
    full_name: str
    phone: str | None = None
    role: str
    status: str
    created_at: datetime | None = None


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


class CustomerRegisterIn(RegisterBase):
    phone: str | None = Field(default=None, max_length=40)
    # Optional: an empty (or omitted) serial creates the account with no site
    # at all, rather than 404ing. The customer portal then walks them through
    # /api/sites -> /meter -> /solar -> /bill instead of claiming one that
    # already exists.
    meter_serial: str | None = Field(default=None, max_length=100)


class WorkerRegisterIn(RegisterBase):
    employee_code: str = Field(min_length=1, max_length=50)


class StaffRegisterIn(RegisterBase):
    registration_code: str = Field(min_length=1, max_length=100)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

_EMAIL_TAKEN = HTTPException(
    status_code=status.HTTP_409_CONFLICT,
    detail="an account with that email already exists",
)


def _token_response(row: asyncpg.Record) -> TokenOut:
    token, expires_in = issue_token(row["account_id"], row["role"], row["email"])
    return TokenOut(
        access_token=token,
        expires_in=expires_in,
        account=AccountOut(**dict(row)),
    )


async def _profile(conn: asyncpg.Connection, account_id: UUID) -> asyncpg.Record:
    return await conn.fetchrow(sql("account_profile"), account_id)


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

    return _token_response(await _profile(conn, row["account_id"]))


@router.get("/me", response_model=AccountOut)
async def me(conn: Conn, principal: CurrentAccount) -> AccountOut:
    return AccountOut(**dict(await _profile(conn, principal.account_id)))


# --------------------------------------------------------------------------
# Registration
# --------------------------------------------------------------------------

@router.post(
    "/register/customer",
    response_model=TokenOut,
    status_code=status.HTTP_201_CREATED,
)
async def register_customer(conn: Conn, payload: CustomerRegisterIn) -> TokenOut:
    """Claim a service point with the serial printed on its billing meter, or
    register with no meter at all.

    Given a serial, the account is genuinely new and the site is transferred
    to it -- everything the customer portal shows is keyed on site_id, so
    readings, bills, credit and issues all come across; the old bills keep
    naming the previous owner, which is what rule 2 is for.

    Given no serial, this is a household with no service point yet: create
    the account and stop there. The customer portal detects the empty site
    list and walks them through building one (POST /api/sites, then /meter,
    then optionally /solar, then /bill) instead of claiming existing data.
    """
    serial = (payload.meter_serial or "").strip()

    if not serial:
        async with conn.transaction():
            try:
                account_id = await conn.fetchval(
                    sql("create_account"),
                    payload.email,
                    hash_password(payload.password),
                    payload.full_name.strip(),
                    payload.phone,
                    "consumer",
                )
            except asyncpg.UniqueViolationError:
                raise _EMAIL_TAKEN from None
            return _token_response(await _profile(conn, account_id))

    async with conn.transaction():
        site = await conn.fetchrow(
            sql("site_by_meter_serial"),
            serial,
            SEED_PASSWORD_PLACEHOLDER,
        )
        if site is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=(
                    f"no billing meter with serial '{serial}'. "
                    "Check the serial printed on the meter."
                ),
            )
        if not site["is_unclaimed"]:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"{site['site_label']} has already been claimed. If this is "
                    "your meter, contact support."
                ),
            )

        try:
            account_id = await conn.fetchval(
                sql("create_account"),
                payload.email,
                hash_password(payload.password),
                payload.full_name.strip(),
                payload.phone,
                "consumer",
            )
        except asyncpg.UniqueViolationError:
            raise _EMAIL_TAKEN from None

        transferred = await conn.fetchval(
            sql("transfer_site"),
            site["site_id"],
            account_id,
            site["current_account_id"],
        )
        if transferred is None:
            # Someone claimed it between the check and the update. The
            # transaction rolls back, including the account just created.
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="that meter was claimed while you were registering",
            )

        return _token_response(await _profile(conn, account_id))


@router.post(
    "/register/worker",
    response_model=TokenOut,
    status_code=status.HTTP_201_CREATED,
)
async def register_worker(conn: Conn, payload: WorkerRegisterIn) -> TokenOut:
    """Claim a worker profile with its employee code.

    Unlike the customer flow this claims the *existing* account rather than
    creating one. work_order_assignment and worker_skill both reference
    worker_profile(account_id), which is the account's own primary key, so a
    new account would strand every assignment this worker already has.
    """
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
                None,
                SEED_PASSWORD_PLACEHOLDER,
            )
        except asyncpg.UniqueViolationError:
            raise _EMAIL_TAKEN from None

        if claimed is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="that employee code was claimed while you were registering",
            )

        return _token_response(await _profile(conn, claimed))


@router.post(
    "/register/{role}",
    response_model=TokenOut,
    status_code=status.HTTP_201_CREATED,
)
async def register_staff(
    conn: Conn,
    role: Literal["government", "supplier"],
    payload: StaffRegisterIn,
) -> TokenOut:
    """Register a regulator or utility user against a shared code.

    These roles read every site in the system, so a shared code with no
    rotation and no per-invite tracking is the weakest part of this design. It
    is a demo affordance, not a model for issuing regulator access.
    """
    expected = registration_code(role)
    if payload.registration_code.strip() != expected:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="that registration code is not valid",
        )

    async with conn.transaction():
        try:
            account_id = await conn.fetchval(
                sql("create_account"),
                payload.email,
                hash_password(payload.password),
                payload.full_name.strip(),
                None,
                role,
            )
        except asyncpg.UniqueViolationError:
            raise _EMAIL_TAKEN from None

        return _token_response(await _profile(conn, account_id))
