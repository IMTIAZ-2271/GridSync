"""Issues: faults reported against a site, by a customer or a worker."""
from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

import asyncpg
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from .auth import CurrentAccount, visible_site_or_404
from .db import Conn
from .queries import sql

router = APIRouter(tags=["operations"])


# --------------------------------------------------------------------------
# Models
# --------------------------------------------------------------------------

class Issue(BaseModel):
    issue_id: UUID
    site_id: UUID
    site_label: str
    device_id: UUID | None
    bill_id: UUID | None
    category: str
    severity: str
    status: str
    title: str
    description: str | None
    priority: int
    reported_at: datetime
    acknowledged_at: datetime | None
    resolved_at: datetime | None
    reported_by_account_id: UUID
    reported_by_name: str


IssueCategory = Literal[
    "billing_dispute", "meter_fault", "inverter_fault", "outage",
    "export_not_credited", "data_gap", "other",
]
IssueSeverity = Literal["low", "medium", "high", "critical"]


class IssueCreate(BaseModel):
    """A new issue.

    There is deliberately no reporter field. The reporter is the authenticated
    caller, taken from the token in the handler -- accepting it from the body
    would let anyone file an issue as anyone, which is exactly what this
    endpoint allowed before auth existed.
    """

    site_id: UUID
    category: IssueCategory
    title: str = Field(min_length=1, max_length=200)
    description: str | None = None
    severity: IssueSeverity = "medium"
    priority: int = Field(default=3, ge=1, le=5)
    device_id: UUID | None = None
    bill_id: UUID | None = None


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------

@router.get("/api/issues", response_model=list[Issue])
async def list_issues(conn: Conn, principal: CurrentAccount) -> list[Issue]:
    if principal.sees_every_site:
        rows = await conn.fetch(sql("list_issues"))
    elif principal.role == "consumer":
        rows = await conn.fetch(sql("issues_for_account"), principal.account_id)
    else:
        rows = await conn.fetch(sql("issues_for_worker"), principal.account_id)
    return [Issue(**dict(r)) for r in rows]


@router.post("/api/issues", response_model=Issue, status_code=201)
async def create_issue(
    conn: Conn,
    payload: IssueCreate,
    principal: CurrentAccount,
) -> Issue:
    """File an issue against a site the caller can see.

    The reporter comes from the token, never from the body. That closes the
    hole this endpoint had before auth existed, where any client could file an
    issue as any account.
    """
    await visible_site_or_404(conn, principal, payload.site_id)

    try:
        issue_id = await conn.fetchval(
            sql("create_issue"),
            principal.account_id,
            payload.site_id,
            payload.device_id,
            payload.bill_id,
            payload.category,
            payload.severity,
            payload.title,
            payload.description,
            payload.priority,
        )
    except asyncpg.ForeignKeyViolationError as exc:
        # site_id is already validated above, so this is a bad device_id or
        # bill_id. constraint_name names which.
        raise HTTPException(
            status_code=422,
            detail=f"unknown reference: {exc.constraint_name}",
        ) from exc

    row = await conn.fetchrow(sql("get_issue"), issue_id)
    return Issue(**dict(row))
