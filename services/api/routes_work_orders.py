"""Work orders: field dispatch and status tracking."""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from .auth import Principal, require_role
from .db import Conn
from .notify import notify, notify_site_owner
from .queries import sql

router = APIRouter(tags=["operations"])


# --------------------------------------------------------------------------
# Models
# --------------------------------------------------------------------------

class Assignment(BaseModel):
    account_id: UUID
    worker_name: str
    job_role: str
    status: str
    assigned_at: datetime
    # The two clocks services/jobs sweeps. On the assignment so a worker can see
    # how long they have to answer and a dispatcher can see how long an offer
    # has been sitting -- neither has to know the durations to read the state.
    offer_expires_at: datetime | None = None
    start_deadline_at: datetime | None = None


class WorkOrder(BaseModel):
    order_id: UUID
    site_id: UUID
    site_label: str
    district: str
    issue_id: UUID | None
    device_id: UUID | None
    order_type: str
    status: str
    priority: int
    scheduled_for: datetime | None
    started_at: datetime | None
    completed_at: datetime | None
    completion_notes: str | None
    failure_reason: str | None
    created_at: datetime
    assignments: list[Assignment]


WorkOrderStatus = Literal[
    "draft", "scheduled", "dispatched", "in_progress",
    "completed", "failed", "cancelled",
]


class WorkOrderStatusUpdate(BaseModel):
    status: WorkOrderStatus


def _work_order(row: asyncpg.Record) -> WorkOrder:
    d = dict(row)
    # assignments arrives as a json string from json_agg; the fields inside
    # are all text or timestamps, so no NUMERIC precision is at stake.
    d["assignments"] = json.loads(d["assignments"])
    return WorkOrder(**d)


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------

@router.get("/api/work-orders", response_model=list[WorkOrder])
async def list_work_orders(
    conn: Conn,
    principal: Annotated[
        Principal,
        Depends(require_role("worker", "government", "supplier", "admin")),
    ],
) -> list[WorkOrder]:
    if principal.role == "worker":
        rows = await conn.fetch(sql("work_orders_for_worker"), principal.account_id)
    else:
        rows = await conn.fetch(sql("list_work_orders"))
    return [_work_order(r) for r in rows]


@router.patch(
    "/api/work-orders/{order_id}/status",
    response_model=WorkOrder,
)
async def update_work_order_status(
    conn: Conn,
    order_id: UUID,
    payload: WorkOrderStatusUpdate,
    principal: Annotated[
        Principal, Depends(require_role("worker", "supplier", "admin"))
    ],
) -> WorkOrder:
    """Advance a work order.

    Government is excluded: a regulator approves agreements, it does not
    dispatch the utility's field crews.

    The household is notified of the states it can act on. Consumer
    requirement 10 is "track their assigned worker", and this is the tracking
    half -- the approve-and-rate half needs endpoints that do not exist yet.
    """
    # The UPDATE and the read-back are one transaction so the response cannot
    # show a state some concurrent writer produced in between.
    async with conn.transaction():
        if principal.role == "worker":
            assigned = await conn.fetchval(
                sql("worker_assigned_to_order"), order_id, principal.account_id
            )
            if not assigned:
                raise HTTPException(status_code=404, detail="work order not found")

        updated = await conn.fetchval(
            sql("update_work_order_status"), order_id, payload.status
        )
        if updated is None:
            raise HTTPException(status_code=404, detail="work order not found")
        row = await conn.fetchrow(sql("get_work_order"), order_id)

        # Inside the transaction: if the status update rolls back, so does the
        # notification claiming it happened.
        await _notify_household(conn, row, payload.status)
    return _work_order(row)


# Which transitions the household hears about, and what they are told. Not
# every status is here on purpose -- 'draft', 'scheduled' and 'dispatched' are
# dispatcher bookkeeping, and a phone buzzing for each of them teaches people
# to ignore the panel.
_HOUSEHOLD_UPDATES: dict[str, tuple[str, str, str]] = {
    "in_progress": (
        "work_order_started", "info",
        "A technician has started work at {site}.",
    ),
    "completed": (
        "work_order_completed", "info",
        "Work at {site} has been marked complete. Please confirm whether the "
        "problem is actually resolved.",
    ),
    "failed": (
        "work_order_completed", "warning",
        "A visit to {site} could not be completed. It will be rescheduled.",
    ),
    "cancelled": (
        "work_order_completed", "warning",
        "A scheduled visit to {site} was cancelled.",
    ),
}


async def _notify_household(
    conn: asyncpg.Connection, order: asyncpg.Record, status: str
) -> None:
    update = _HOUSEHOLD_UPDATES.get(status)
    if update is None:
        return
    kind, severity, body = update

    # Keyed on the order and the status, not on the moment: the API has no
    # state machine, so a dispatcher correcting a mistake can walk an order
    # back into a state it already held, and the household should not be told
    # twice that the same thing happened.
    await notify_site_owner(
        conn,
        order["site_id"],
        kind,
        f"{order['order_type'].replace('_', ' ').capitalize()} — "
        f"{status.replace('_', ' ')}",
        body=body.format(site=order["site_label"]),
        severity=severity,
        entity_type="work_order",
        entity_id=str(order["order_id"]),
        dedupe_key=f"wo:{order['order_id']}:{status}",
    )


# --------------------------------------------------------------------------
# Assignments: offering a job, and answering the offer
#
# The two deadlines in the schema get their values here and nowhere else.
# CLAUDE.md decision 3 says a deadline is STORED so a query between two sweeps
# is already correct -- which means the durations belong on the write path, not
# in the jobs runner's configuration. If they lived in the runner, editing an
# environment variable would retroactively change when an offer made yesterday
# expires, and every query that reads offer_expires_at would start lying.
#
# Three hours to answer an offer (supplier requirement 5); one day from
# accepting to actually starting (worker requirement 5). services/jobs sweeps
# both -- see services/jobs/deadlines.py for what happens when they pass.
# --------------------------------------------------------------------------

# worker_availability is (available, busy, off_duty, on_leave) -- there is no
# 'unavailable'. An earlier version of this file compared against that
# non-existent label, so the check silently never fired and an off-duty worker
# could be handed a three-hour offer they would never see. 'busy' is
# deliberately NOT here: it means loaded, not unreachable, and load is the
# dispatcher's judgement call, made against the open-job count on the worker
# list rather than refused outright by the API.
OFF_SHIFT = frozenset({"off_duty", "on_leave"})

OFFER_TTL = timedelta(hours=3)
START_DEADLINE = timedelta(days=1)


class AssignmentOffer(BaseModel):
    account_id: UUID
    job_role: Literal["lead", "assistant", "inspector"] = "assistant"


class AssignmentResponse(BaseModel):
    decision: Literal["accept", "decline"]
    reason: str | None = None


class AssignmentState(BaseModel):
    order_id: UUID
    account_id: UUID
    worker_name: str
    status: str
    offer_expires_at: datetime | None
    start_deadline_at: datetime | None
    order_status: str


async def _assignment_state(
    conn: asyncpg.Connection, order_id: UUID, account_id: UUID
) -> AssignmentState:
    row = await conn.fetchrow(sql("assignment_context"), order_id, account_id)
    return AssignmentState(
        order_id=row["order_id"],
        account_id=row["account_id"],
        worker_name=row["worker_name"],
        status=row["status"],
        offer_expires_at=row["offer_expires_at"],
        start_deadline_at=row["start_deadline_at"],
        order_status=row["order_status"],
    )


@router.post(
    "/api/work-orders/{order_id}/assignments",
    response_model=AssignmentState,
    status_code=201,
)
async def offer_assignment(
    conn: Conn,
    order_id: UUID,
    payload: AssignmentOffer,
    principal: Annotated[Principal, Depends(require_role("supplier", "admin"))],
) -> AssignmentState:
    """Offer a work order to a worker, with a three-hour clock on it.

    Consumer is excluded for the obvious reason and worker for a less obvious
    one: a technician may answer an offer but may not hand themselves a job,
    because the queue is the dispatcher's to balance.

    A worker who is not approved, has left, or is marked unavailable is refused
    here rather than left to hold the offer until it expires. Their approval
    state is the database's answer, not the caller's claim -- the same posture
    as worker requirement 3.
    """
    async with conn.transaction():
        order = await conn.fetchrow(sql("get_work_order"), order_id)
        if order is None:
            raise HTTPException(status_code=404, detail="work order not found")
        if order["status"] in ("completed", "cancelled"):
            raise HTTPException(
                status_code=409,
                detail=f"a {order['status']} work order cannot be assigned",
            )

        worker = await conn.fetchrow(sql("offerable_worker"), payload.account_id)
        if worker is None:
            raise HTTPException(status_code=404, detail="no such worker")
        if worker["approval_status"] != "approved":
            raise HTTPException(
                status_code=409,
                detail=f"worker registration is {worker['approval_status']}",
            )
        if worker["left_on"] is not None:
            raise HTTPException(status_code=409, detail="worker has left")
        if worker["availability"] in OFF_SHIFT:
            raise HTTPException(
                status_code=409,
                detail=f"worker is {worker['availability'].replace('_', ' ')}",
            )
        # Worker requirement 4: a technician only receives requests from their
        # own region. Enforced here rather than left to the picker's filter,
        # because a filter is a convenience and this is a rule -- the worker's
        # queue is scoped by assignment, so the only place region can actually
        # be enforced is the moment an assignment is created.
        #
        # This reverses an earlier judgement in `assignable_workers`, which
        # treated district as a filter on the grounds that a neighbouring
        # district's engineer is sometimes exactly who you want. The
        # requirement is explicit and wins; the picker now defaults to the
        # order's own district to match.
        if worker["service_district"] != order["district"]:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"{worker['full_name']} works in "
                    f"{worker['service_district']}, and this job is in "
                    f"{order['district']}"
                ),
            )

        await conn.fetchrow(
            sql("offer_assignment"),
            order_id, payload.account_id, OFFER_TTL, payload.job_role,
        )
        await conn.fetchval(sql("dispatch_work_order"), order_id)

        state = await _assignment_state(conn, order_id, payload.account_id)
        await notify(
            conn,
            payload.account_id,
            "work_order_offered",
            f"New job offer — {order['order_type'].replace('_', ' ')}",
            body=(
                f"{order['site_label']} needs a "
                f"{order['order_type'].replace('_', ' ')}. Accept within three "
                "hours or it goes to someone else."
            ),
            severity="info",
            entity_type="work_order",
            entity_id=str(order_id),
            # The deadline identifies THIS offer. A re-offer after a decline or
            # an expiry gets a new one, so it notifies again -- see
            # offer_assignment's ON CONFLICT.
            dedupe_key=f"wo:{order_id}:offered:{state.offer_expires_at.isoformat()}",
        )
    return state


@router.patch(
    "/api/work-orders/{order_id}/assignment",
    response_model=AssignmentState,
)
async def respond_to_assignment(
    conn: Conn,
    order_id: UUID,
    payload: AssignmentResponse,
    principal: Annotated[Principal, Depends(require_role("worker"))],
) -> AssignmentState:
    """Accept or decline an offer made to the caller.

    Deliberately keyed on the token, not on a path parameter: a worker answers
    their own offer and nobody else's, so there is no id to guess.

    Both statements are guarded on status 'offered', so an offer the jobs sweep
    expired a moment earlier answers 409 rather than quietly reviving a job that
    has already been released to somebody else. That race is the whole reason
    the guard is in the SQL and not in an `if` up here.
    """
    async with conn.transaction():
        before = await conn.fetchrow(
            sql("assignment_context"), order_id, principal.account_id
        )
        if before is None:
            raise HTTPException(status_code=404, detail="no offer for you here")

        if payload.decision == "accept":
            updated = await conn.fetchrow(
                sql("accept_assignment"),
                order_id, principal.account_id, START_DEADLINE,
            )
        else:
            updated = await conn.fetchrow(
                sql("decline_assignment"),
                order_id, principal.account_id, payload.reason,
            )

        if updated is None:
            raise HTTPException(
                status_code=409,
                detail=f"this offer is no longer open (it is {before['status']})",
            )

        state = await _assignment_state(conn, order_id, principal.account_id)

        # A decline and a lapsed deadline mean the same thing to the order --
        # nobody is coming -- so they leave the same state behind. The expiry
        # sweep has always called this (CLAUDE.md decision 3); declining used to
        # release only the assignment, which left the order 'dispatched' with
        # nobody on it, still offering its next status to the worker who had
        # just said no. release_work_order carries its own guard, so a
        # two-person job whose assistant declined stays dispatched to its lead.
        accepted = payload.decision == "accept"
        released = (
            None if accepted
            else await conn.fetchval(sql("release_work_order"), order_id)
        )

        # The dispatcher hears both answers. An acceptance is what tells them to
        # stop looking for someone; a decline is what tells them to start again,
        # and waiting three hours for the sweep to say so would waste the whole
        # offer window.
        job = before["order_type"].replace("_", " ")
        await notify(
            conn,
            before["created_by_account_id"],
            "work_order_offered",
            f"{state.worker_name} {'accepted' if accepted else 'declined'} — {job}",
            body=(
                f"{before['site_label']}: {state.worker_name} has accepted and "
                "has one day to start."
                if accepted else
                f"{before['site_label']}: {state.worker_name} declined"
                + (f" — {payload.reason}" if payload.reason else "")
                # Only claim it needs reassigning when it actually came back.
                # While a co-assignee still holds the order the visit is still
                # happening, and the sweep is careful about exactly this.
                + (". The order needs reassigning."
                   if released is not None
                   else ". The rest of the crew still has it.")
            ),
            severity="info" if accepted else "warning",
            entity_type="work_order",
            entity_id=str(order_id),
            dedupe_key=(
                f"wo:{order_id}:{payload.decision}:{principal.account_id}:"
                f"{before['offer_expires_at'].isoformat()}"
                if before["offer_expires_at"] else None
            ),
        )
    return state


# --------------------------------------------------------------------------
# Dispatch: raising an order, and choosing who to offer it to
#
# Supplier requirement 3. Until now a work order could only be created by hand
# in SQL, which made the whole assignment lifecycle -- offer, accept, the two
# deadline sweeps -- reachable only for rows somebody had inserted manually.
# --------------------------------------------------------------------------

class WorkOrderCreate(BaseModel):
    """Raise a visit.

    Either `issue_id` or `site_id`, not both and not neither. An order raised
    from an issue inherits that issue's site and device **in SQL**, so a
    dispatcher cannot file an order against issue X on site Y -- that pairing is
    the audit trail behind "this visit happened because of that complaint".
    """

    issue_id: UUID | None = None
    site_id: UUID | None = None
    device_id: UUID | None = None
    order_type: Literal[
        "meter_install", "meter_swap", "meter_removal", "inverter_service",
        "inspection", "seal_check", "disconnection", "reconnection",
    ]
    # 1 is the top of this scale, not the bottom. Left unset it inherits the
    # issue's own priority, which is the number the household's complaint
    # already carried.
    priority: int | None = Field(default=None, ge=1, le=5)
    scheduled_for: datetime | None = None


class DispatchableIssue(BaseModel):
    issue_id: UUID
    site_id: UUID
    site_label: str
    district: str
    device_id: UUID | None
    device_serial: str | None
    category: str
    severity: str
    status: str
    title: str
    description: str | None
    priority: int
    reported_at: datetime
    reported_by_name: str


class AssignableWorker(BaseModel):
    account_id: UUID
    full_name: str
    employee_code: str
    service_district: str
    worker_kind: str
    availability: str
    max_daily_jobs: int
    distribution_company_name: str | None
    open_jobs: int
    # NULL until something writes service_rating. Deliberately not defaulted to
    # 0: "not yet rated" and "rated badly" must not look the same.
    rating_avg: Decimal | None
    rating_count: int


@router.post("/api/work-orders", response_model=WorkOrder, status_code=201)
async def create_work_order(
    conn: Conn,
    payload: WorkOrderCreate,
    principal: Annotated[Principal, Depends(require_role("supplier", "admin"))],
) -> WorkOrder:
    """Raise a work order, optionally against an issue.

    Supplier and admin. A worker cannot raise their own job for the same reason
    they cannot assign themselves one: the queue is the dispatcher's to balance.
    Government is excluded because a regulator does not dispatch the utility's
    crews -- the same line drawn on the status endpoint.

    The order starts in `draft`. Offering it to somebody is what moves it to
    `dispatched`, and the deadline sweep is what can move it back.
    """
    if (payload.issue_id is None) == (payload.site_id is None):
        raise HTTPException(
            status_code=422,
            detail="give either issue_id or site_id, not both and not neither",
        )

    async with conn.transaction():
        if payload.issue_id is not None:
            issue = await conn.fetchrow(sql("get_issue"), payload.issue_id)
            if issue is None:
                raise HTTPException(status_code=404, detail="issue not found")

        try:
            order_id = await conn.fetchval(
                sql("create_work_order"),
                principal.account_id,
                payload.issue_id,
                payload.site_id,
                payload.device_id,
                payload.order_type,
                payload.priority,
                payload.scheduled_for,
            )
        except asyncpg.ForeignKeyViolationError as exc:
            # site_id or device_id names something that does not exist. The
            # issue path is already checked above, so this is the bare-site one.
            raise HTTPException(
                status_code=422, detail=f"unknown reference: {exc.constraint_name}"
            ) from exc
        except asyncpg.UniqueViolationError as exc:
            # one_live_order_per_issue. Since migration a1c4e8b70d3f this is
            # only reachable as the race the index exists for: two dispatchers
            # reading the same inbox raising a visit for one complaint at the
            # same moment. It used to be reachable by a single click -- the
            # inbox re-offers a complaint whose visit was cancelled or failed,
            # and the index forbade the second order -- which surfaced as a 500.
            raise HTTPException(
                status_code=409,
                detail=(
                    "a visit is already open for that complaint — refresh the "
                    "inbox to see who raised it"
                ),
            ) from exc

        row = await conn.fetchrow(sql("get_work_order"), order_id)
    return _work_order(row)


@router.get("/api/work-orders/dispatchable-issues",
            response_model=list[DispatchableIssue])
async def dispatchable_issues(
    conn: Conn,
    _: Annotated[Principal, Depends(require_role("supplier", "admin"))],
) -> list[DispatchableIssue]:
    """Unresolved issues with no live work order against them.

    The dispatcher's inbox: complaints nobody has raised a visit for. An issue
    whose order was cancelled or failed comes back into this list, because the
    fault is still real.
    """
    rows = await conn.fetch(sql("dispatchable_issues"))
    return [DispatchableIssue(**dict(r)) for r in rows]


@router.get("/api/workers", response_model=list[AssignableWorker])
async def assignable_workers(
    conn: Conn,
    _: Annotated[
        Principal, Depends(require_role("supplier", "government", "admin"))
    ],
    district: str | None = None,
    with_capacity: bool = False,
) -> list[AssignableWorker]:
    """Who a job can be offered to, best-rated then least-loaded.

    The same approved / employed / on-shift gate `offerable_worker` applies at
    the moment of offering, so the dispatcher never sees a name the next call
    would refuse.

    `?district=` narrows to one service district. Worker requirement 4 wants the
    queue itself scoped by region; this is the dispatcher's half of that, and it
    is a filter rather than a hard rule because a neighbouring district's
    engineer is sometimes exactly who you want.
    """
    rows = await conn.fetch(sql("assignable_workers"), district, with_capacity)
    return [AssignableWorker(**dict(r)) for r in rows]
