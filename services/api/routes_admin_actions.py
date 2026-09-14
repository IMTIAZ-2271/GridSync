"""Admin actions across the system: stuck work orders, and credit corrections.

Admin only, every write one transaction with its audit row.

**Work-order intervention** -- `PATCH /api/admin/work-orders/{id}`:

* `release` takes everyone off a job that is stuck (a crew that stopped
  answering, a worker who left) and returns the order to `draft`, exactly the
  state the deadline sweeps return an order to, so it reappears in the
  dispatcher's queue. Workers and the dispatcher are told.
* `cancel` ends the order. Live assignments are released, and the household is
  told as well -- they were expecting a visit. A cancelled order no longer
  counts as the complaint's live visit (`one_live_order_per_issue`), so the
  complaint goes back to the dispatcher's inbox.

A finished order (completed, failed, cancelled) is history and is refused. The
status route officials and dispatchers use stays as it is; this exists because
releasing assignments is something that route does not do.

**Credit adjustment** -- `POST /api/admin/billing-points/{id}/credit-adjustments`:
rule 1 forbids editing money, so a correction is a new `credit_ledger` row of
type `adjustment` whose running balance continues the connection's own. Never
below zero: a negative credit balance would be a debt, and a debt is a bill.
The write is serialized against `run_billing` -- see `admin_lock_billing_point`.

**Bill reissue** -- `POST /api/admin/bills/{id}/reissue`: `rebill_period()`
(db/sql/service/billing.sql) voids the bill and issues a corrected replacement
for the same month in one REPEATABLE READ transaction, retried on 40001 like
run_billing. Its guards come back as SQLSTATEs: 55000 (latest bill only, not
void, no payments, same owner, same meter) is 409; 23514 (rule 8 coverage, or no
billing meter) is 422; P0002 is 404. The household is told, with both figures.

Notifications use the `announcement` kind: `notification_kind` has no value for
an admin intervention, and adding one is an irreversible enum change this phase
does not need.
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

import asyncpg
from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field, model_validator

from . import audit
from .db import Conn
from .notify import notify, notify_site_owner
from .queries import sql
from .routes_admin_accounts import Admin, Reasoned
from .types import Energy, Money

router = APIRouter(prefix="/api/admin", tags=["admin"])

FINISHED = {"completed", "failed", "cancelled"}


# --------------------------------------------------------------------------
# Work orders
# --------------------------------------------------------------------------

class WorkOrderIntervention(Reasoned):
    action: Literal["release", "cancel"]


class InterventionOut(BaseModel):
    order_id: UUID
    status: str
    released: list[UUID]


@router.patch("/work-orders/{order_id}", response_model=InterventionOut)
async def intervene_on_work_order(
    conn: Conn, principal: Admin, order_id: UUID, payload: WorkOrderIntervention,
    request: Request,
) -> InterventionOut:
    async with conn.transaction():
        order = await conn.fetchrow(sql("admin_work_order_for_update"), order_id)
        if order is None:
            raise HTTPException(status_code=404, detail="work order not found")
        if order["status"] in FINISHED:
            raise HTTPException(
                status_code=409,
                detail=f"this order is already {order['status']}; it is history now",
            )

        released = await conn.fetch(sql("admin_release_assignments"), order_id)
        if payload.action == "release":
            if not released and order["status"] == "draft":
                raise HTTPException(
                    status_code=409,
                    detail="nobody is on this order; it is already waiting to be dispatched",
                )
            new_status = "draft"
        else:
            new_status = "cancelled"

        if order["status"] != new_status:
            changed = await conn.fetchrow(
                sql("admin_set_work_order_status"), order_id, new_status, order["status"]
            )
            if changed is None:
                raise HTTPException(status_code=409, detail="the order changed; reload")

        await audit.record(
            conn,
            actor_account_id=principal.account_id,
            action="work_order.released" if payload.action == "release" else "work_order.cancelled",
            entity_type="work_order",
            entity_id=order_id,
            before={"status": order["status"],
                    "assignees": [str(r["account_id"]) for r in released]},
            after={"status": new_status, "reason": payload.reason},
            request=request,
        )

        job = f"{order['order_type'].replace('_', ' ')} at {order['site_label']}"
        verb = "cancelled" if payload.action == "cancel" else "taken off you"
        entity = {"entity_type": "work_order", "entity_id": str(order_id)}
        for r in released:
            await notify(
                conn, r["account_id"], "announcement",
                f"Job {verb} by an administrator",
                body=f"The {job} is no longer assigned to you. {payload.reason}",
                severity="warning",
                dedupe_key=f"wo:{order_id}:admin:{payload.action}:{r['account_id']}",
                **entity,
            )
        await notify(
            conn, order["created_by_account_id"], "announcement",
            f"An administrator {'cancelled' if payload.action == 'cancel' else 'released'} a job",
            body=(
                f"The {job} was {'cancelled' if payload.action == 'cancel' else 'returned to your queue'}. "
                f"{payload.reason}"
            ),
            severity="warning",
            dedupe_key=f"wo:{order_id}:admin:{payload.action}:dispatcher",
            **entity,
        )
        if payload.action == "cancel":
            await notify_site_owner(
                conn, order["site_id"], "announcement",
                "A scheduled visit was cancelled",
                body=f"The {order['order_type'].replace('_', ' ')} booked for "
                     f"{order['site_label']} was cancelled. You will hear again "
                     "if another visit is booked.",
                severity="warning",
                dedupe_key=f"wo:{order_id}:admin:cancel:household",
                **entity,
            )

    return InterventionOut(
        order_id=order_id, status=new_status, released=[r["account_id"] for r in released]
    )


# --------------------------------------------------------------------------
# Credit
# --------------------------------------------------------------------------

class CreditAdjustmentIn(Reasoned):
    #: Signed. Positive grants credit, negative removes it.
    kwh_delta: Decimal = Field(max_digits=12, decimal_places=4)
    amount_delta: Decimal = Field(max_digits=14, decimal_places=4)

    @model_validator(mode="after")
    def _moves_something(self) -> "CreditAdjustmentIn":
        if self.kwh_delta == 0 and self.amount_delta == 0:
            raise ValueError("an adjustment must change the kWh or the amount")
        return self


class CreditAdjustmentOut(BaseModel):
    entry_id: int
    billing_point_id: UUID
    created_at: datetime
    balance_kwh: Energy
    balance_amount: Money


class LedgerEntry(BaseModel):
    entry_id: int
    entry_type: str
    kwh_delta: Energy
    amount_delta: Money
    balance_kwh_after: Energy
    balance_amount_after: Money
    period_id: UUID | None
    bill_id: UUID | None
    expires_on: date | None
    note: str | None
    created_at: datetime


class LedgerOut(BaseModel):
    billing_point_id: UUID
    balance_kwh: Energy
    balance_amount: Money
    entries: list[LedgerEntry]


@router.post(
    "/billing-points/{point_id}/credit-adjustments",
    response_model=CreditAdjustmentOut,
    status_code=201,
)
async def adjust_credit(
    conn: Conn, principal: Admin, point_id: UUID, payload: CreditAdjustmentIn,
    request: Request,
) -> CreditAdjustmentOut:
    async with conn.transaction():
        point = await conn.fetchrow(sql("admin_lock_billing_point"), point_id)
        if point is None:
            raise HTTPException(status_code=404, detail="connection not found")
        before = await conn.fetchrow(sql("admin_point_balance"), point_id)
        kwh_after = before["balance_kwh"] + payload.kwh_delta
        amount_after = before["balance_amount"] + payload.amount_delta
        if kwh_after < 0 or amount_after < 0:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"that would leave a negative balance ({kwh_after} kWh, "
                    f"{amount_after}); the connection holds {before['balance_kwh']} kWh "
                    f"worth {before['balance_amount']}"
                ),
            )

        entry = await conn.fetchrow(
            sql("admin_insert_credit_adjustment"),
            point_id, point["site_id"], payload.kwh_delta, payload.amount_delta,
            kwh_after, amount_after, payload.reason,
        )
        await audit.record(
            conn,
            actor_account_id=principal.account_id,
            action="credit.adjustment",
            entity_type="billing_point",
            entity_id=point_id,
            before={"balance_kwh": before["balance_kwh"],
                    "balance_amount": before["balance_amount"]},
            after={"kwh_delta": payload.kwh_delta, "amount_delta": payload.amount_delta,
                   "balance_kwh": kwh_after, "balance_amount": amount_after,
                   "entry_id": entry["entry_id"], "reason": payload.reason},
            request=request,
        )

    return CreditAdjustmentOut(
        entry_id=entry["entry_id"],
        billing_point_id=point_id,
        created_at=entry["created_at"],
        balance_kwh=kwh_after,
        balance_amount=amount_after,
    )


@router.get("/billing-points/{point_id}/ledger", response_model=LedgerOut)
async def point_ledger(
    conn: Conn, _: Admin, point_id: UUID, limit: int = Query(default=100, ge=1, le=500)
) -> LedgerOut:
    exists = await conn.fetchval("SELECT 1 FROM billing_point WHERE point_id = $1", point_id)
    if not exists:
        raise HTTPException(status_code=404, detail="connection not found")
    balance = await conn.fetchrow(sql("admin_point_balance"), point_id)
    rows = await conn.fetch(sql("admin_point_ledger"), point_id, limit)
    return LedgerOut(
        billing_point_id=point_id,
        balance_kwh=balance["balance_kwh"],
        balance_amount=balance["balance_amount"],
        entries=[LedgerEntry(**dict(r)) for r in rows],
    )


# --------------------------------------------------------------------------
# Bills
# --------------------------------------------------------------------------

class BillRow(BaseModel):
    bill_id: UUID
    period_start: date
    period_end: date
    status: str
    amount_due: Money
    gross_amount: Money
    issued_at: datetime
    voided_by_bill_id: UUID | None
    #: Offered a reissue: not void, the connection's latest, no payments.
    reissuable: bool


class ReissueIn(Reasoned):
    #: Bill the connection's unresolved late readings for that month too.
    merge_late_readings: bool = False


class ReissueOut(BaseModel):
    voided_bill_id: UUID
    bill_id: UUID
    period_start: date
    previous_amount_due: Money
    amount_due: Money
    #: Charges before credit. On a solar connection whose credit covers the
    #: bill, the amount due stays 0.00 while these (and the credit used) move.
    previous_gross_amount: Money
    gross_amount: Money
    previous_credit_applied_kwh: Energy
    credit_applied_kwh: Energy


REISSUE_ATTEMPTS = 3


@router.get("/billing-points/{point_id}/bills", response_model=list[BillRow])
async def point_bills(conn: Conn, _: Admin, point_id: UUID) -> list[BillRow]:
    exists = await conn.fetchval("SELECT 1 FROM billing_point WHERE point_id = $1", point_id)
    if not exists:
        raise HTTPException(status_code=404, detail="connection not found")
    return [BillRow(**dict(r)) for r in await conn.fetch(sql("admin_point_bills"), point_id)]


@router.post("/bills/{bill_id}/reissue", response_model=ReissueOut, status_code=201)
async def reissue_bill(
    conn: Conn, principal: Admin, bill_id: UUID, payload: ReissueIn, request: Request,
) -> ReissueOut:
    for attempt in range(REISSUE_ATTEMPTS):
        # REPEATABLE READ like run_billing -- except inside an outer transaction
        # (a test's), where no isolation level can be set and it is a savepoint.
        transaction = (
            conn.transaction() if conn.is_in_transaction()
            else conn.transaction(isolation="repeatable_read")
        )
        try:
            async with transaction:
                before = await conn.fetchrow(sql("admin_bill_summary"), bill_id)
                if before is None:
                    raise HTTPException(status_code=404, detail="bill not found")
                new_id = await conn.fetchval(
                    "SELECT rebill_period($1, $2)", bill_id, payload.merge_late_readings
                )
                after = await conn.fetchrow(sql("admin_bill_summary"), new_id)
                await audit.record(
                    conn,
                    actor_account_id=principal.account_id,
                    action="bill.reissued",
                    entity_type="bill",
                    entity_id=bill_id,
                    before={"bill_id": bill_id, "amount_due": before["amount_due"],
                            "gross_amount": before["gross_amount"],
                            "credit_applied_kwh": before["credit_applied_kwh"],
                            "status": before["status"]},
                    after={"bill_id": new_id, "amount_due": after["amount_due"],
                           "gross_amount": after["gross_amount"],
                           "credit_applied_kwh": after["credit_applied_kwh"],
                           "merge_late_readings": payload.merge_late_readings,
                           "reason": payload.reason},
                    request=request,
                )
                month = before["period_start"].strftime("%B %Y")
                await notify(
                    conn, before["account_id"], "announcement",
                    f"Your bill for {month} was corrected",
                    # Charges AND amount due: where credit covers the bill the
                    # amount due is 0.00 both times, and saying only that would
                    # hide that more of the household's credit was used.
                    body=(
                        f"It was reissued. Charges: BDT {before['gross_amount']:.2f} → "
                        f"BDT {after['gross_amount']:.2f}. Credit used: "
                        f"{before['credit_applied_kwh']} → {after['credit_applied_kwh']} kWh. "
                        f"Amount due: BDT {before['amount_due']:.2f} → "
                        f"BDT {after['amount_due']:.2f}. {payload.reason}"
                    ),
                    severity="info",
                    entity_type="bill",
                    entity_id=str(new_id),
                    dedupe_key=f"bill:{bill_id}:reissued",
                )
            return ReissueOut(
                voided_bill_id=bill_id,
                bill_id=new_id,
                period_start=before["period_start"],
                previous_amount_due=before["amount_due"],
                amount_due=after["amount_due"],
                previous_gross_amount=before["gross_amount"],
                gross_amount=after["gross_amount"],
                previous_credit_applied_kwh=before["credit_applied_kwh"],
                credit_applied_kwh=after["credit_applied_kwh"],
            )
        except asyncpg.SerializationError:
            if attempt == REISSUE_ATTEMPTS - 1:
                raise HTTPException(
                    status_code=409, detail="the connection is busy being billed; try again"
                ) from None
        except asyncpg.NoDataFoundError:
            raise HTTPException(status_code=404, detail="bill not found") from None
        except asyncpg.ObjectNotInPrerequisiteStateError as exc:
            raise HTTPException(status_code=409, detail=exc.args[0]) from None
        except asyncpg.CheckViolationError as exc:
            raise HTTPException(
                status_code=422, detail=f"the corrected bill was refused: {exc.args[0]}"
            ) from None
    raise AssertionError("unreachable")
