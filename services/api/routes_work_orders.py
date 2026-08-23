"""Work orders: field dispatch and status tracking."""
from __future__ import annotations

import json
from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .auth import Principal, require_role
from .db import Conn
from .notify import notify_site_owner
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


class WorkOrder(BaseModel):
    order_id: UUID
    site_id: UUID
    site_label: str
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
