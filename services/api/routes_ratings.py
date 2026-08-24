"""Finished visits: the household's verdict, and its rating of who did the work.

Consumer requirement 10's second and third clauses. The first — track the
assigned worker — has worked since notifications landed; this is the other end
of that loop, and the answer to a promise the system has been making out loud
for days: the work-order completion notification already tells a household
"please confirm whether the problem is actually resolved", and until now there
was nowhere for them to do it.

It is also what finally gives supplier requirement 4 a sort key. `GET
/api/workers` has ordered by `rating_avg` since dispatch landed, and that column
has been NULL for everyone because nothing wrote `service_rating`. This writes
it.

Consumer-only, all of it. A rating is testimony from the person who was there,
and an endpoint any role could call would let a supplier rate itself.
"""
from __future__ import annotations

import json
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

router = APIRouter(tags=["ratings"])


# --------------------------------------------------------------------------
# Models
# --------------------------------------------------------------------------

class CrewMember(BaseModel):
    account_id: UUID
    worker_name: str
    job_role: str


class GivenRating(BaseModel):
    stars: int
    comment: str | None
    # Only on a worker rating: which technician it was recorded against. One
    # worker rating exists per visit (rating_one_per_subject), so naming them
    # is what stops the page implying the whole crew was rated.
    worker_account_id: UUID | None = None


class Visit(BaseModel):
    order_id: UUID
    site_id: UUID
    site_label: str
    order_type: str
    completed_at: datetime | None
    completion_notes: str | None
    issue_id: UUID | None
    issue_title: str | None
    issue_status: str | None
    consumer_confirmed_at: datetime | None
    consumer_disputed_at: datetime | None
    consumer_feedback: str | None
    supplier_id: UUID | None
    supplier_name: str | None
    crew: list[CrewMember]
    worker_rating: GivenRating | None
    supplier_rating: GivenRating | None


class RatingCreate(BaseModel):
    subject: Literal["worker", "supplier"]
    # Required for subject 'worker', ignored for 'supplier' -- the supplier is
    # derived from the order's dispatcher, not chosen by the rater, so a
    # household cannot pin a bad review on a firm that was never involved.
    worker_account_id: UUID | None = None
    stars: int = Field(ge=1, le=5)
    comment: str | None = Field(default=None, max_length=2000)


class VerdictCreate(BaseModel):
    resolved: bool
    feedback: str | None = Field(default=None, max_length=2000)


def _visit(row: asyncpg.Record) -> Visit:
    d = dict(row)
    d["crew"] = json.loads(d["crew"])
    for key in ("worker_rating", "supplier_rating"):
        d[key] = json.loads(d[key]) if d[key] else None
    return Visit(**d)


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------

@router.get("/api/visits", response_model=list[Visit])
async def list_visits(
    conn: Conn,
    principal: Annotated[Principal, Depends(require_role("consumer"))],
    limit: int = Query(default=50, ge=1, le=200),
) -> list[Visit]:
    """Completed visits to this household's sites.

    Scoped by ownership inside `visits_for_account`, not filtered here. Only
    `completed` orders: a cancelled or failed visit is not something to rate,
    and a job still under way is not something to have an opinion about yet.

    Each row already carries what this account has said, so the client never
    needs a second request to know which controls to hide.
    """
    rows = await conn.fetch(sql("visits_for_account"), principal.account_id, limit)
    return [_visit(r) for r in rows]


@router.post("/api/work-orders/{order_id}/rating", response_model=Visit)
async def rate_visit(
    conn: Conn,
    order_id: UUID,
    payload: RatingCreate,
    principal: Annotated[Principal, Depends(require_role("consumer"))],
) -> Visit:
    """Rate the technician, or the firm that sent them.

    One rating per subject per visit, and **not editable** — see the note on
    `create_rating`. A second attempt is a 409 rather than a silent overwrite.

    The supplier is derived from the order's dispatcher rather than taken from
    the body: a household rates the firm it actually dealt with, and letting the
    caller name one would let a bad review be pinned on a company that was never
    involved.
    """
    if payload.subject == "worker" and payload.worker_account_id is None:
        raise HTTPException(
            status_code=422, detail="worker_account_id is required to rate a worker"
        )

    async with conn.transaction():
        target = await conn.fetchrow(
            sql("rateable_target"), order_id, principal.account_id,
            payload.worker_account_id,
        )
        # 404 covers three different "no": no such order, not this household's
        # site, and — below — a worker who was never on the job. None of them is
        # information this caller is owed.
        if target is None:
            raise HTTPException(status_code=404, detail="visit not found")
        if target["status"] != "completed":
            raise HTTPException(
                status_code=409, detail="only a completed visit can be rated"
            )

        if payload.subject == "worker":
            if not target["worker_attended"]:
                raise HTTPException(
                    status_code=404, detail="that technician was not on this visit"
                )
            worker_id, supplier_id = payload.worker_account_id, None
        else:
            if target["supplier_id"] is None:
                raise HTTPException(
                    status_code=409,
                    detail="this visit was not dispatched by a supplier firm",
                )
            worker_id, supplier_id = None, target["supplier_id"]

        rating_id = await conn.fetchval(
            sql("create_rating"), order_id, principal.account_id,
            payload.subject, worker_id, supplier_id,
            payload.stars, (payload.comment or "").strip() or None,
        )
        if rating_id is None:
            raise HTTPException(
                status_code=409,
                detail=f"you have already rated the {payload.subject} for this visit",
            )

        # The worker hears about their own rating. The supplier firm does not:
        # a notification is delivered to an account, a firm is not an account,
        # and picking one of its staff logins at random to tell would be
        # arbitrary. `GET /api/workers` is where a firm's numbers surface.
        if payload.subject == "worker":
            await notify(
                conn,
                worker_id,
                "rating_request",
                f"You were rated {payload.stars} out of 5",
                body=(payload.comment or "").strip() or None,
                severity="info",
                entity_type="work_order",
                entity_id=str(order_id),
                dedupe_key=f"rating:{order_id}:{principal.account_id}:worker",
            )

        rows = await conn.fetch(
            sql("visits_for_account"), principal.account_id, 200
        )
    match = next((r for r in rows if r["order_id"] == order_id), None)
    return _visit(match)


@router.post("/api/issues/{issue_id}/verdict", response_model=dict)
async def set_issue_verdict(
    conn: Conn,
    issue_id: UUID,
    payload: VerdictCreate,
    principal: Annotated[Principal, Depends(require_role("consumer"))],
) -> dict:
    """Confirm the fault was really fixed, or say it was not.

    **The verdict changes state**, the same principle the deadline sweeps
    follow. Confirming closes the issue — 'resolved' is the engineer's opinion
    and 'closed' is the household agreeing with it, which is what those two enum
    values are for. Disputing sends it back to `in_progress`, so it returns to
    the worker triage queue *and* to the dispatcher's inbox: `dispatchable_issues`
    stops counting a completed visit as coverage once the household has disputed
    it, which is what stops a disputed fault sitting invisible behind an order
    that already exists.

    The site's owner passes the verdict, not the reporter. A worker can file an
    issue about somebody's meter, and it is the household that lives with
    whether it was fixed.
    """
    async with conn.transaction():
        issue = await conn.fetchrow(sql("issue_owner"), issue_id)
        if issue is None or issue["owner_account_id"] != principal.account_id:
            raise HTTPException(status_code=404, detail="issue not found")

        updated = await conn.fetchval(
            sql("set_issue_verdict"), issue_id, payload.resolved,
            (payload.feedback or "").strip() or None,
        )
        if updated is None:
            already = (
                issue["consumer_confirmed_at"] is not None
                or issue["consumer_disputed_at"] is not None
            )
            raise HTTPException(
                status_code=409,
                detail=(
                    "you have already answered for this issue" if already else
                    f"an issue that is {issue['status']} has nothing to confirm yet"
                ),
            )

    return {
        "issue_id": str(issue_id),
        "resolved": payload.resolved,
        "status": "closed" if payload.resolved else "in_progress",
    }
