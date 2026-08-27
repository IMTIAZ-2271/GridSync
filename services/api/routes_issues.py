"""Issues: faults reported against a site, by a consumer or a worker."""
from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

import asyncpg
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from .auth import CurrentAccount, visible_site_or_404
from .notify import notify
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
    # Consumer requirement 6: who the complaint names. Both optional -- a data
    # gap is nobody's fault until someone looks, and demanding a culprit on
    # every report would only produce wrong ones.
    distribution_company_id: UUID | None = None
    distribution_company_name: str | None = None
    supplier_id: UUID | None = None
    supplier_name: str | None = None
    # Supplier inbox only: is this a complaint about US, or one we are merely
    # involved in? The difference is the whole point of the page.
    against_us: bool | None = None


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
    # At most one of these is meaningful, and which one depends on the
    # category -- a meter fault is the utility's, a bad installation is the
    # installer's. Not enforced as a pair here: the schema allows both, a
    # billing dispute about an export credit can legitimately involve both
    # parties, and refusing that combination would be inventing a rule.
    distribution_company_id: UUID | None = None
    supplier_id: UUID | None = None


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------

@router.get("/api/issues", response_model=list[Issue])
async def list_issues(conn: Conn, principal: CurrentAccount) -> list[Issue]:
    if principal.role == "supplier":
        # Supplier requirement 2. A supplier is a fleet-wide reader for most
        # things, but an inbox is not a fleet: `issues_for_supplier` narrows to
        # complaints named against this firm plus the sites it actually works
        # on, and flags which is which.
        supplier_id = await conn.fetchval(
            "SELECT supplier_id FROM supplier_profile WHERE account_id = $1",
            principal.account_id,
        )
        if supplier_id is None:
            raise HTTPException(
                status_code=403, detail="this account is not attached to a supplier"
            )
        rows = await conn.fetch(sql("issues_for_supplier"), supplier_id)
    elif principal.sees_every_site:
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
            payload.distribution_company_id,
            payload.supplier_id,
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


# The states a triager can put an issue in. 'duplicate' is absent: the CHECK
# `issue_duplicate_status` ties it to duplicate_of_issue_id, so offering it here
# without the id would be a 500 wearing a dropdown. Merging duplicates needs the
# pair and therefore its own endpoint.
IssueStatusValue = Literal[
    "open", "acknowledged", "in_progress", "resolved", "closed",
]


class IssueStatusUpdate(BaseModel):
    status: IssueStatusValue
    # Written only when supplied, so re-entering a state cannot blank a note
    # somebody already left. See the statement.
    resolution_notes: str | None = Field(default=None, max_length=2000)


@router.patch("/api/issues/{issue_id}/status", response_model=Issue)
async def update_issue_status(
    conn: Conn,
    issue_id: UUID,
    payload: IssueStatusUpdate,
    principal: CurrentAccount,
) -> Issue:
    """Advance an issue through its own lifecycle.

    Until this existed the worker triage queue was read-only and an issue's
    status was whatever it was filed as, forever -- a household could watch a
    technician complete a work order while the fault they reported still read
    'open'.

    **Not the consumer's to move.** They file and they will confirm (that is
    `issue.consumer_confirmed_at`, still unwired); calling their own fault
    resolved is not a thing to give them, and neither is reopening it by
    editing a status rather than saying why. `visible_site_or_404` alone would
    have let them, since they can obviously see their own site -- so the role
    check is explicit and separate from the row check.

    **No state machine, deliberately** -- the same posture as
    `PATCH /api/work-orders/{id}/status`. Any of the five is accepted from any
    other, because triage gets things wrong and walking a status back has to be
    possible without a migration. The timestamps are set once by the SQL and
    never rewritten, so the history survives the correction. The client offers
    the sensible next move; that is convenience, not enforcement.
    """
    if principal.role not in ("worker", "supplier", "admin"):
        raise HTTPException(
            status_code=403, detail="this role cannot triage issues"
        )

    async with conn.transaction():
        before = await conn.fetchrow(sql("get_issue"), issue_id)
        if before is None:
            raise HTTPException(status_code=404, detail="issue not found")
        # Scoped after the existence check but before the write: a worker may
        # only touch issues on sites they cover, and `issues_for_worker` is the
        # same predicate their queue is built from.
        await visible_site_or_404(conn, principal, before["site_id"])

        updated = await conn.fetchval(
            sql("update_issue_status"),
            issue_id,
            payload.status,
            (payload.resolution_notes or "").strip() or None,
        )
        if updated is None:
            # The row exists and is visible, so the only way to get here is the
            # statement's `status <> 'duplicate'` guard.
            raise HTTPException(
                status_code=409,
                detail=(
                    "this issue is marked a duplicate; its status follows the "
                    "issue it was merged into"
                ),
            )

        row = await conn.fetchrow(sql("get_issue"), issue_id)

        # Inside the transaction, so a rolled-back status change cannot leave a
        # notification claiming it happened.
        await _notify_reporter(conn, row, payload.status)
    return Issue(**dict(row))


# Which moves the household hears about. 'acknowledged' and 'in_progress' are
# not here: a status crawling through triage is the queue's business, and a
# panel that buzzes for each step teaches people to ignore the panel -- the same
# reasoning that keeps 'dispatched' off the work-order notifications.
_REPORTER_UPDATES: dict[str, tuple[str, str]] = {
    "resolved": (
        "info",
        "The issue you reported at {site} has been marked resolved.",
    ),
    "closed": (
        "info",
        "The issue you reported at {site} has been closed.",
    ),
}


async def _notify_reporter(
    conn: asyncpg.Connection, issue: asyncpg.Record, status: str
) -> None:
    """Tell whoever filed it -- not whoever owns the site.

    Those are usually the same account and sometimes are not: a worker can file
    an issue against a household's site, and the person who wants to know it was
    resolved is the one who raised it.
    """
    update = _REPORTER_UPDATES.get(status)
    if update is None:
        return
    severity, body = update
    await notify(
        conn,
        issue["reported_by_account_id"],
        "issue_updated",
        f"{issue['title'][:80]} — {status}",
        body=body.format(site=issue["site_label"]),
        severity=severity,
        entity_type="issue",
        entity_id=str(issue["issue_id"]),
        # Keyed on the state, not the moment: with no state machine a triager
        # can walk an issue back into a state it already held, and the reporter
        # must not be told twice that the same thing happened.
        dedupe_key=f"issue:{issue['issue_id']}:{status}",
    )


class IssueTarget(BaseModel):
    """A company a complaint from this site could reasonably be against."""

    kind: Literal["distribution", "supplier"]
    id: UUID
    name: str
    # True when this company is actually attached to the site -- the utility on
    # its metering, or the installer of its array -- as opposed to one that
    # merely serves the district. The form preselects an attached one.
    attached: bool


@router.get("/api/sites/{site_id}/issue-targets", response_model=list[IssueTarget])
async def issue_targets(
    conn: Conn, site_id: UUID, principal: CurrentAccount
) -> list[IssueTarget]:
    """Who a complaint from this site could name.

    Consumer requirement 6 asks the household to pick the distribution company
    for a meter fault and the installer for a solar one. Both are already known
    to the system — the utility is on the site's billing points, the installer
    on its arrays — so the form arrives pre-answered rather than asking someone
    to remember who fitted their panels three years ago.

    Candidates, not a decision: a site with two connections may have two
    utilities, and the household confirms which one it means.
    """
    await visible_site_or_404(conn, principal, site_id)
    rows = await conn.fetch(sql("issue_targets_for_site"), site_id)
    return [IssueTarget(**dict(r)) for r in rows]
