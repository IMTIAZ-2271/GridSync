"""GridSync dashboard API.

Read-mostly HTTP surface over the GridSync database for the React client. No
auth yet -- this is a portfolio build and every endpoint is open; the pieces
that would need authorization (which account may see which site, who may
approve a net-metering agreement) are marked where they arise.

Two conventions govern everything below.

**Raw SQL, no ORM.** Statements live in `db/sql/api_queries.sql` and are
reached by name through `queries.sql()`. Handlers translate rows into response
models and do nothing else -- no query construction in Python.

**Money and energy cross the wire as strings.** Postgres NUMERIC arrives as
Decimal, and rule 5 forbids FLOAT for money and energy. Serializing a Decimal
as a JSON number would hand it to a JavaScript double, which is precisely the
lossy step the rule exists to prevent, so the `Money`/`Energy`/`Rate` aliases
below pin serialization to str. The client parses them where it needs to plot
them, and keeps the exact string wherever it displays a number a customer is
being asked to pay.
"""
from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Literal
from uuid import UUID

import asyncpg
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, PlainSerializer

from .queries import sql

PROJECT_ROOT = Path(__file__).resolve().parents[2]

# --------------------------------------------------------------------------
# Serialization aliases
# --------------------------------------------------------------------------

# str(Decimal) preserves the scale Postgres sent, so 0.0000 stays "0.0000" and
# the client can tell a measured zero from an absent value.
_as_str = PlainSerializer(str, return_type=str, when_used="json")

Money = Annotated[Decimal, _as_str]   # NUMERIC(14,4)
Energy = Annotated[Decimal, _as_str]  # NUMERIC(12,4)
Rate = Annotated[Decimal, _as_str]    # NUMERIC(10,6)


# --------------------------------------------------------------------------
# Pool
# --------------------------------------------------------------------------

def database_url() -> str:
    """The DSN asyncpg wants.

    load_dotenv gets an explicit path: called bare it resolves against the
    *calling* file and quietly picks up the wrong .env when the app is started
    from another directory. tests/conftest.py and db/migrations/env.py do the
    same thing.
    """
    load_dotenv(PROJECT_ROOT / ".env")
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL is not set; copy .env.example to .env")
    # Alembic's URL carries SQLAlchemy's dialect suffix; asyncpg wants it bare.
    return url.replace("postgresql+asyncpg://", "postgresql://", 1)


async def _init_connection(conn: asyncpg.Connection) -> None:
    """Pin the session TimeZone for every pooled connection.

    Nothing in api_queries.sql depends on this today: the queries compare
    against `now()`, which is an absolute instant, and every timestamptz is
    serialized to UTC on the way out. It is here so that the day someone adds
    a date_trunc, a ::date cast or a bare timestamptz literal to that file --
    all of which resolve against the *session* zone -- they resolve against a
    zone this project named on purpose rather than whatever the server was
    configured with. That is the same discipline CLAUDE.md requires of DDL.
    Asia/Dhaka matches `site.timezone`.
    """
    await conn.execute("SET TIME ZONE 'Asia/Dhaka'")


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.pool = await asyncpg.create_pool(
        database_url(),
        min_size=1,
        max_size=10,
        init=_init_connection,
    )
    try:
        yield
    finally:
        await app.state.pool.close()


app = FastAPI(
    title="GridSync API",
    description="Read-mostly API over the GridSync net-metering database.",
    version="0.1.0",
    lifespan=lifespan,
)

# The Vite dev server. Open because there is no auth and no cookie to protect;
# this list is the thing to revisit first when auth lands.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


async def get_conn() -> AsyncIterator[asyncpg.Connection]:
    """Yield a pooled connection, returned to the pool when the request ends."""
    async with app.state.pool.acquire() as conn:
        yield conn


Conn = Annotated[asyncpg.Connection, Depends(get_conn)]


# --------------------------------------------------------------------------
# Models
# --------------------------------------------------------------------------

class Site(BaseModel):
    site_id: UUID
    label: str
    district: str
    account_name: str
    has_solar: bool


class LatestBill(BaseModel):
    """The bill headline shown on a site card."""

    bill_id: UUID
    period_start: date
    period_end: date
    currency: str
    energy_charge: Money
    export_credit_earned: Money
    fixed_charge: Money
    tax_amount: Money
    gross_amount: Money
    credit_applied_kwh: Energy
    credit_applied_amount: Money
    credit_closing_kwh: Energy
    amount_due: Money
    due_date: date | None
    issued_at: datetime
    status: str


class EnergyWindow(BaseModel):
    """Totals over a trailing window, in kWh."""

    days: int
    import_kwh: Energy
    export_kwh: Energy
    generation_kwh: Energy
    # generation - export, i.e. what the household consumed from its own
    # panels without it ever crossing the meter (rule 6).
    self_consumption_kwh: Energy


class SiteSummary(BaseModel):
    site_id: UUID
    label: str
    district: str
    credit_balance_kwh: Energy
    credit_balance_amount: Money
    last_30_days: EnergyWindow
    latest_bill: LatestBill | None


class Reading(BaseModel):
    interval_start: datetime
    import_kwh: Energy
    export_kwh: Energy
    generation_kwh: Energy


class BillLineItem(BaseModel):
    sort_order: int
    line_type: str
    period_name: str | None
    quantity_kwh: Energy | None
    # The rate as it was applied when the bill was cut, snapshotted onto the
    # line item. Never re-derived from tariff_rate (rule 2).
    rate_applied: Rate | None
    amount: Money


class Bill(BaseModel):
    bill_id: UUID
    period_id: UUID
    period_start: date
    period_end: date
    coverage_pct: Decimal | None
    currency: str
    energy_charge: Money
    export_credit_earned: Money
    fixed_charge: Money
    tax_amount: Money
    gross_amount: Money
    credit_opening_kwh: Energy
    credit_applied_kwh: Energy
    credit_applied_amount: Money
    credit_closing_kwh: Energy
    amount_due: Money
    due_date: date | None
    issued_at: datetime
    status: str
    voided_by_bill_id: UUID | None
    line_items: list[BillLineItem]


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

    reported_by_account_id is optional and defaults to the site's owner, since
    there is no auth to infer a reporter from and the column is NOT NULL. That
    also means a client can currently file an issue as anyone, which is the
    single largest thing this API is missing -- the field comes off the body
    and out of the session when auth lands.
    """

    site_id: UUID
    category: IssueCategory
    title: str = Field(min_length=1, max_length=200)
    description: str | None = None
    severity: IssueSeverity = "medium"
    priority: int = Field(default=3, ge=1, le=5)
    reported_by_account_id: UUID | None = None
    device_id: UUID | None = None
    bill_id: UUID | None = None


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


class Agreement(BaseModel):
    agreement_id: UUID
    site_id: UUID
    site_label: str
    district: str
    account_name: str
    billing_device_id: UUID
    billing_device_serial: str
    approval_ref: str
    sanctioned_capacity_kw: Decimal
    export_cap_pct: Decimal
    settlement_type: str
    credit_rollover_months: int | None
    effective_from: date
    effective_to: date | None
    status: str
    created_at: datetime


class AgreementStatusUpdate(BaseModel):
    """The outcome of reviewing a pending agreement.

    Only 'active' and 'terminated' are reachable here. 'suspended' is an
    operational action on an already-active agreement rather than a review
    decision, and nothing may move back to 'pending'.
    """

    status: Literal["active", "terminated"]


class AreaStats(BaseModel):
    district: str
    site_count: int
    solar_site_count: int
    total_import_kwh: Energy
    total_export_kwh: Energy
    total_generation_kwh: Energy


# --------------------------------------------------------------------------
# Sites
# --------------------------------------------------------------------------

@app.get("/api/sites", response_model=list[Site], tags=["sites"])
async def list_sites(conn: Conn) -> list[Site]:
    rows = await conn.fetch(sql("list_sites"))
    return [Site(**dict(r)) for r in rows]


@app.get("/api/sites/{site_id}/summary", response_model=SiteSummary, tags=["sites"])
async def site_summary(conn: Conn, site_id: UUID) -> SiteSummary:
    row = await conn.fetchrow(sql("site_summary"), site_id)
    if row is None:
        raise HTTPException(status_code=404, detail="site not found")

    # bill_id is NULL when the LEFT JOIN LATERAL found no bill -- a site
    # energized this month has telemetry and a credit balance but has never
    # been billed, and that is a normal state, not an error.
    latest_bill = None
    if row["bill_id"] is not None:
        latest_bill = LatestBill(
            bill_id=row["bill_id"],
            period_start=row["bill_period_start"],
            period_end=row["bill_period_end"],
            currency=row["bill_currency"],
            energy_charge=row["bill_energy_charge"],
            export_credit_earned=row["bill_export_credit_earned"],
            fixed_charge=row["bill_fixed_charge"],
            tax_amount=row["bill_tax_amount"],
            gross_amount=row["bill_gross_amount"],
            credit_applied_kwh=row["bill_credit_applied_kwh"],
            credit_applied_amount=row["bill_credit_applied_amount"],
            credit_closing_kwh=row["bill_credit_closing_kwh"],
            amount_due=row["bill_amount_due"],
            due_date=row["bill_due_date"],
            issued_at=row["bill_issued_at"],
            status=row["bill_status"],
        )

    generation = row["window_generation_kwh"]
    export = row["window_export_kwh"]
    return SiteSummary(
        site_id=row["site_id"],
        label=row["label"],
        district=row["district"],
        credit_balance_kwh=row["credit_balance_kwh"],
        credit_balance_amount=row["credit_balance_amount"],
        last_30_days=EnergyWindow(
            days=30,
            import_kwh=row["window_import_kwh"],
            export_kwh=export,
            generation_kwh=generation,
            self_consumption_kwh=generation - export,
        ),
        latest_bill=latest_bill,
    )


@app.get("/api/sites/{site_id}/readings", response_model=list[Reading], tags=["sites"])
async def site_readings(
    conn: Conn,
    site_id: UUID,
    days: Annotated[int, Query(ge=1, le=90, description="Trailing window in days")] = 7,
) -> list[Reading]:
    # 404 on an unknown site rather than returning [], which a client would
    # otherwise read as "this site is silent" -- a very different alarm.
    if not await conn.fetchval("SELECT 1 FROM site WHERE site_id = $1", site_id):
        raise HTTPException(status_code=404, detail="site not found")
    rows = await conn.fetch(sql("site_readings"), site_id, days)
    return [Reading(**dict(r)) for r in rows]


@app.get("/api/sites/{site_id}/bills", response_model=list[Bill], tags=["sites"])
async def site_bills(conn: Conn, site_id: UUID) -> list[Bill]:
    if not await conn.fetchval("SELECT 1 FROM site WHERE site_id = $1", site_id):
        raise HTTPException(status_code=404, detail="site not found")

    # Two queries, not one with json_agg: nesting the line items in JSON would
    # push rate_applied and amount through a JSON number and lose exactness.
    bill_rows = await conn.fetch(sql("site_bills"), site_id)
    item_rows = await conn.fetch(sql("site_bill_line_items"), site_id)

    items: dict[UUID, list[BillLineItem]] = {}
    for r in item_rows:
        d = dict(r)
        items.setdefault(d.pop("bill_id"), []).append(BillLineItem(**d))

    return [
        Bill(**dict(r), line_items=items.get(r["bill_id"], []))
        for r in bill_rows
    ]


# --------------------------------------------------------------------------
# Issues
# --------------------------------------------------------------------------

@app.get("/api/issues", response_model=list[Issue], tags=["operations"])
async def list_issues(conn: Conn) -> list[Issue]:
    rows = await conn.fetch(sql("list_issues"))
    return [Issue(**dict(r)) for r in rows]


@app.post("/api/issues", response_model=Issue, status_code=201, tags=["operations"])
async def create_issue(conn: Conn, payload: IssueCreate) -> Issue:
    try:
        issue_id = await conn.fetchval(
            sql("create_issue"),
            payload.reported_by_account_id,
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
        # A bad site_id, account_id, device_id or bill_id is the client's
        # mistake, not a server fault. constraint_name names which one.
        raise HTTPException(
            status_code=422,
            detail=f"unknown reference: {exc.constraint_name}",
        ) from exc

    row = await conn.fetchrow(sql("get_issue"), issue_id)
    return Issue(**dict(row))


# --------------------------------------------------------------------------
# Work orders
# --------------------------------------------------------------------------

def _work_order(row: asyncpg.Record) -> WorkOrder:
    d = dict(row)
    # assignments arrives as a json string from json_agg; the fields inside
    # are all text or timestamps, so no NUMERIC precision is at stake.
    d["assignments"] = json.loads(d["assignments"])
    return WorkOrder(**d)


@app.get("/api/work-orders", response_model=list[WorkOrder], tags=["operations"])
async def list_work_orders(conn: Conn) -> list[WorkOrder]:
    rows = await conn.fetch(sql("list_work_orders"))
    return [_work_order(r) for r in rows]


@app.patch(
    "/api/work-orders/{order_id}/status",
    response_model=WorkOrder,
    tags=["operations"],
)
async def update_work_order_status(
    conn: Conn,
    order_id: UUID,
    payload: WorkOrderStatusUpdate,
) -> WorkOrder:
    # The UPDATE and the read-back are one transaction so the response cannot
    # show a state some concurrent writer produced in between.
    async with conn.transaction():
        updated = await conn.fetchval(
            sql("update_work_order_status"), order_id, payload.status
        )
        if updated is None:
            raise HTTPException(status_code=404, detail="work order not found")
        row = await conn.fetchrow(sql("get_work_order"), order_id)
    return _work_order(row)


# --------------------------------------------------------------------------
# Net-metering agreements
# --------------------------------------------------------------------------

@app.get(
    "/api/agreements/pending",
    response_model=list[Agreement],
    tags=["agreements"],
)
async def pending_agreements(conn: Conn) -> list[Agreement]:
    rows = await conn.fetch(sql("list_pending_agreements"))
    return [Agreement(**dict(r)) for r in rows]


@app.patch(
    "/api/agreements/{agreement_id}/status",
    response_model=Agreement,
    tags=["agreements"],
)
async def decide_agreement(
    conn: Conn,
    agreement_id: UUID,
    payload: AgreementStatusUpdate,
) -> Agreement:
    """Approve or terminate a pending agreement.

    Approving is what lets a site's exports start earning credit, so in a
    system with auth this is an admin-only route. There is no auth yet.
    """
    async with conn.transaction():
        decided = await conn.fetchval(
            sql("decide_agreement"), agreement_id, payload.status
        )
        if decided is None:
            # The UPDATE is guarded on status = 'pending', so zero rows means
            # either no such agreement or someone already decided it. Tell
            # those apart -- 409 is actionable, 404 is not.
            current = await conn.fetchval(
                "SELECT status FROM net_metering_agreement WHERE agreement_id = $1",
                agreement_id,
            )
            if current is None:
                raise HTTPException(status_code=404, detail="agreement not found")
            raise HTTPException(
                status_code=409,
                detail=f"agreement is already '{current}', not pending",
            )
        row = await conn.fetchrow(sql("get_agreement"), agreement_id)
    return Agreement(**dict(row))


# --------------------------------------------------------------------------
# Analytics
# --------------------------------------------------------------------------

@app.get("/api/analytics/by-area", response_model=list[AreaStats], tags=["analytics"])
async def analytics_by_area(conn: Conn) -> list[AreaStats]:
    rows = await conn.fetch(sql("analytics_by_area"))
    return [AreaStats(**dict(r)) for r in rows]
