"""The admin panel's account management, and the audit trail it leaves.

Admin only (`require_role("admin")`). An admin already reads every district
through the scoped statements elsewhere; this router adds what no other portal
has: changing an account itself.

**Every write is one guarded UPDATE and one audit row, in one transaction.**
The UPDATE is guarded on the value the admin was looking at, so two admins
acting on the same account produce one change and a 409. The audit row is
written by `audit.record`, which does not swallow errors -- an action whose
trail cannot be written does not happen.

**Every write ends the account's sessions** (`sessions_valid_after`, migration
a7c3e9f15b20). A suspension already bites on the next request, since status is
re-read each time; a password reset or a role change would not without the
cut-off, because a token carries its role and outlives its password.

Three guards keep the system administrable:

* **No arbitrary role swaps.** Only `consumer <-> admin`. A worker, an official
  and an installer's staff account each rest on a profile row holding real
  data -- an employer, an official code, a firm and district -- and changing
  `account.role` alone would produce an account whose role points at nothing.
* **An admin cannot change their own status or revoke their own admin.** The
  panel is not where you lock yourself out.
* **The last active admin cannot be suspended, closed or demoted**, decided
  against a locked count (`lock_active_admins`), so two admins demoting each
  other at once cannot leave none.

Admins themselves are created by `scripts/create_admin.py` only.
"""
from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field, field_validator

from . import audit
from .auth import Principal, hash_password, require_role
from .db import Conn
from .queries import sql

router = APIRouter(prefix="/api/admin", tags=["admin"])

Admin = Annotated[Principal, Depends(require_role("admin"))]

AccountStatus = Literal["active", "suspended", "closed"]

#: The shortest temporary password accepted. The account holder is expected to
#: change it; this only keeps the admin from setting "1234".
MIN_PASSWORD_LENGTH = 10


# --------------------------------------------------------------------------
# Wire shapes
# --------------------------------------------------------------------------

class AccountRow(BaseModel):
    account_id: UUID
    email: str
    full_name: str
    phone: str | None
    national_id: str | None
    role: str
    status: AccountStatus
    created_at: datetime
    #: Tokens issued before this are refused. NULL: no admin has ended them.
    sessions_valid_after: datetime | None
    #: The district the account's role carries; NULL for a household or admin.
    district: str | None
    #: For a worker or installer's staff account; NULL for every other role.
    approval_status: str | None
    site_count: int


class AccountPage(BaseModel):
    items: list[AccountRow]
    total: int


class AccountSite(BaseModel):
    site_id: UUID
    label: str
    district: str
    status: str
    connection_count: int


class AccountDetail(AccountRow):
    sites: list[AccountSite]


class Reasoned(BaseModel):
    """Every admin action states why. The reason is the audit row's point: an
    entry saying *what* changed and not *why* is half a record."""

    reason: str = Field(max_length=500)

    @field_validator("reason")
    @classmethod
    def _present(cls, v: str) -> str:
        v = v.strip()
        if len(v) < 3:
            raise ValueError("give a reason (at least 3 characters)")
        return v


class StatusChange(Reasoned):
    status: AccountStatus


class PasswordReset(Reasoned):
    password: str = Field(min_length=MIN_PASSWORD_LENGTH, max_length=200)


class AdminGrant(Reasoned):
    granted: bool


class SessionsRevoked(BaseModel):
    account_id: UUID
    sessions_valid_after: datetime


class Overview(BaseModel):
    #: role -> number of accounts, every status included.
    accounts_by_role: dict[str, int]
    #: status -> number of accounts.
    accounts_by_status: dict[str, int]
    pending_workers: int
    pending_suppliers: int
    open_meter_applications: int
    pending_agreements: int
    recent_audit: list["AuditEntry"]


class AuditEntry(BaseModel):
    audit_id: int
    occurred_at: datetime
    actor_account_id: UUID | None
    actor_email: str | None
    actor_name: str | None
    action: str
    entity_type: str
    entity_id: str | None
    #: A readable name for the entity -- the email, for an account.
    entity_label: str | None
    #: JSON text, exactly as stored.
    before_state: str | None
    after_state: str | None
    client_ip: str | None


class AuditPage(BaseModel):
    items: list[AuditEntry]
    total: int


Overview.model_rebuild()


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def _row(record: asyncpg.Record) -> AccountRow:
    return AccountRow(**{k: record[k] for k in AccountRow.model_fields})


async def _account_or_404(conn: asyncpg.Connection, account_id: UUID) -> asyncpg.Record:
    row = await conn.fetchrow(sql("admin_account"), account_id)
    if row is None:
        raise HTTPException(status_code=404, detail="account not found")
    return row


def _not_self(principal: Principal, account_id: UUID, what: str) -> None:
    if principal.account_id == account_id:
        raise HTTPException(
            status_code=409,
            detail=f"you cannot {what} your own account; ask another admin",
        )


async def _keep_an_admin(conn: asyncpg.Connection, target: asyncpg.Record) -> None:
    """Refuse a change that would leave no active admin. Only matters when the
    target is currently an active admin; the lock is what makes it race-free."""
    if target["role"] != "admin" or target["status"] != "active":
        return
    active = {r["account_id"] for r in await conn.fetch(sql("lock_active_admins"))}
    if active - {target["account_id"]}:
        return
    raise HTTPException(
        status_code=409,
        detail="this is the last active admin; grant admin to someone else first",
    )


# --------------------------------------------------------------------------
# Reads
# --------------------------------------------------------------------------

@router.get("/overview", response_model=Overview)
async def overview(conn: Conn, _: Admin) -> Overview:
    by_role: dict[str, int] = {}
    by_status: dict[str, int] = {}
    for r in await conn.fetch(sql("admin_overview_accounts")):
        by_role[r["role"]] = by_role.get(r["role"], 0) + r["n"]
        by_status[r["status"]] = by_status.get(r["status"], 0) + r["n"]
    pending = await conn.fetchrow(sql("admin_overview_pending"))
    recent = await conn.fetch(sql("admin_audit"), None, None, None, None, 10, 0)
    return Overview(
        accounts_by_role=by_role,
        accounts_by_status=by_status,
        **dict(pending),
        recent_audit=[AuditEntry(**{k: r[k] for k in AuditEntry.model_fields}) for r in recent],
    )


@router.get("/accounts", response_model=AccountPage)
async def list_accounts(
    conn: Conn,
    _: Admin,
    q: str | None = Query(default=None, max_length=200),
    role: Literal["consumer", "worker", "government", "supplier", "admin"] | None = None,
    status: AccountStatus | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> AccountPage:
    needle = (q or "").strip() or None
    rows = await conn.fetch(sql("admin_accounts"), needle, role, status, limit, offset)
    total = rows[0]["total"] if rows else 0
    return AccountPage(items=[_row(r) for r in rows], total=total)


@router.get("/accounts/{account_id}", response_model=AccountDetail)
async def account_detail(conn: Conn, _: Admin, account_id: UUID) -> AccountDetail:
    row = await _account_or_404(conn, account_id)
    sites = await conn.fetch(sql("admin_account_sites"), account_id)
    return AccountDetail(
        **_row(row).model_dump(),
        sites=[AccountSite(**dict(s)) for s in sites],
    )


@router.get("/audit", response_model=AuditPage)
async def audit_log(
    conn: Conn,
    _: Admin,
    actor: UUID | None = None,
    entity_type: str | None = Query(default=None, max_length=100),
    entity_id: str | None = Query(default=None, max_length=200),
    action: str | None = Query(default=None, max_length=100),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> AuditPage:
    rows = await conn.fetch(
        sql("admin_audit"), actor, entity_type, entity_id, action, limit, offset
    )
    return AuditPage(
        items=[AuditEntry(**{k: r[k] for k in AuditEntry.model_fields}) for r in rows],
        total=rows[0]["total"] if rows else 0,
    )


# --------------------------------------------------------------------------
# Writes
# --------------------------------------------------------------------------

@router.patch("/accounts/{account_id}/status", response_model=AccountRow)
async def change_status(
    conn: Conn, principal: Admin, account_id: UUID, payload: StatusChange, request: Request
) -> AccountRow:
    """Suspend, close or reactivate an account. Ends its sessions."""
    async with conn.transaction():
        target = await _account_or_404(conn, account_id)
        _not_self(principal, account_id, "change the status of")
        if target["status"] == payload.status:
            raise HTTPException(
                status_code=409, detail=f"this account is already {payload.status}"
            )
        if payload.status != "active":
            await _keep_an_admin(conn, target)

        changed = await conn.fetchval(
            sql("admin_set_account_status"), account_id, payload.status, target["status"]
        )
        if changed is None:
            raise HTTPException(
                status_code=409, detail="the account changed while you were looking; reload"
            )
        await audit.record(
            conn,
            actor_account_id=principal.account_id,
            action="account.status",
            entity_type="account",
            entity_id=account_id,
            before={"status": target["status"]},
            after={"status": payload.status, "reason": payload.reason},
            request=request,
        )
        return _row(await _account_or_404(conn, account_id))


@router.post("/accounts/{account_id}/sessions/revoke", response_model=SessionsRevoked)
async def revoke_sessions(
    conn: Conn, principal: Admin, account_id: UUID, payload: Reasoned, request: Request
) -> SessionsRevoked:
    """Sign an account out everywhere. Allowed on yourself -- that is how an
    admin who fears a token has leaked ends it."""
    async with conn.transaction():
        await _account_or_404(conn, account_id)
        cutoff = await conn.fetchval(sql("admin_revoke_sessions"), account_id)
        await audit.record(
            conn,
            actor_account_id=principal.account_id,
            action="account.sessions_revoked",
            entity_type="account",
            entity_id=account_id,
            after={"sessions_valid_after": cutoff, "reason": payload.reason},
            request=request,
        )
    return SessionsRevoked(account_id=account_id, sessions_valid_after=cutoff)


@router.post("/accounts/{account_id}/password", response_model=AccountRow)
async def reset_password(
    conn: Conn, principal: Admin, account_id: UUID, payload: PasswordReset, request: Request
) -> AccountRow:
    """Set a temporary password and end every session. The audit row records
    that it happened and why -- never the password, never the hash."""
    password_hash = hash_password(payload.password)
    async with conn.transaction():
        await _account_or_404(conn, account_id)
        _not_self(principal, account_id, "reset the password of")
        await conn.fetchval(sql("admin_set_password"), account_id, password_hash)
        await audit.record(
            conn,
            actor_account_id=principal.account_id,
            action="account.password_reset",
            entity_type="account",
            entity_id=account_id,
            after={"reason": payload.reason},
            request=request,
        )
        return _row(await _account_or_404(conn, account_id))


@router.put("/accounts/{account_id}/admin", response_model=AccountRow)
async def set_admin(
    conn: Conn, principal: Admin, account_id: UUID, payload: AdminGrant, request: Request
) -> AccountRow:
    """Grant admin to a household account, or return an admin to a household."""
    async with conn.transaction():
        target = await _account_or_404(conn, account_id)
        if payload.granted:
            if target["role"] == "admin":
                raise HTTPException(status_code=409, detail="this account is already an admin")
            if target["role"] != "consumer":
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"a {target['role']} account cannot be made admin: its role "
                        "rests on profile data (employer, official code or firm) "
                        "that an admin account would carry for nothing. Use a "
                        "separate account."
                    ),
                )
            new_role, from_role, action = "admin", "consumer", "account.admin_granted"
        else:
            if target["role"] != "admin":
                raise HTTPException(status_code=409, detail="this account is not an admin")
            _not_self(principal, account_id, "revoke admin from")
            await _keep_an_admin(conn, target)
            new_role, from_role, action = "consumer", "admin", "account.admin_revoked"

        changed = await conn.fetchval(sql("admin_set_role"), account_id, new_role, from_role)
        if changed is None:
            raise HTTPException(
                status_code=409, detail="the account changed while you were looking; reload"
            )
        await audit.record(
            conn,
            actor_account_id=principal.account_id,
            action=action,
            entity_type="account",
            entity_id=account_id,
            before={"role": from_role},
            after={"role": new_role, "reason": payload.reason},
            request=request,
        )
        return _row(await _account_or_404(conn, account_id))
