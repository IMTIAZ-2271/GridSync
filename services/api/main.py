"""GridSync dashboard API.

Read-mostly HTTP surface over the GridSync database for the React client.

Three conventions govern everything below.

**Every endpoint is authenticated, and authorization has two layers.**
`require_role(...)` decides whether a role may call an endpoint at all;
`visible_site_or_404` decides whether this particular caller may see this
particular row. Both are needed -- a customer may legitimately call
/summary, but only for a site they own. Row scoping is done by selecting a
narrower statement, not by filtering a full result set, so a row the caller
may not see is never fetched. See services/api/auth.py.

**Raw SQL, no ORM.** Statements live in `db/sql/*.sql` and are reached by name
through `queries.sql()`. Handlers translate rows into response models and do
nothing else -- no query construction in Python.

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
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Literal
from uuid import UUID, uuid4

import asyncpg
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, PlainSerializer

from .auth import CurrentAccount, Principal, hash_password, require_role
from .queries import sql
from .routes_auth import router as auth_router

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

# The Vite dev server. Credentials are carried in an Authorization header
# rather than a cookie, so this list is what stops another origin's script from
# reading responses on a logged-in user's behalf -- keep it exact.
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

# /api/auth/* is the only unauthenticated surface: register, login, and the
# /me probe (which authenticates itself).
app.include_router(auth_router)


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
    total_import_kwh: Energy
    total_export_kwh: Energy
    total_generation_kwh: Energy
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
# Onboarding: a customer with no site building one from scratch.
# --------------------------------------------------------------------------

BACKFILL_DAYS = 90

# The onboarding form collects an address, not coordinates -- site.latitude/
# longitude feed the simulator's solar geometry (CLAUDE.md), so something has
# to fill them. Approximate centroids for the districts db/sql/seed_demo.sql
# already uses, with a city-centre fallback for anything else typed in; a
# demo has no geocoder to call.
_DHAKA_DISTRICT_COORDS: dict[str, tuple[Decimal, Decimal]] = {
    "dhanmondi": (Decimal("23.746000"), Decimal("90.376000")),
    "gulshan": (Decimal("23.791000"), Decimal("90.414000")),
    "uttara": (Decimal("23.868000"), Decimal("90.399000")),
    "mirpur": (Decimal("23.806000"), Decimal("90.365000")),
    "bashundhara": (Decimal("23.815000"), Decimal("90.433000")),
    "banani": (Decimal("23.793000"), Decimal("90.404000")),
    "mohammadpur": (Decimal("23.766000"), Decimal("90.359000")),
    "badda": (Decimal("23.780000"), Decimal("90.425000")),
}
_DHAKA_DEFAULT_COORDS = (Decimal("23.780636"), Decimal("90.279429"))


def _district_coordinates(district: str) -> tuple[Decimal, Decimal]:
    return _DHAKA_DISTRICT_COORDS.get(district.strip().lower(), _DHAKA_DEFAULT_COORDS)


def _next_month(d: date) -> date:
    return date(d.year + (d.month == 12), (d.month % 12) + 1, 1)


class TariffPlanOut(BaseModel):
    plan_id: UUID
    code: str
    name: str
    customer_class: str
    currency: str
    fixed_monthly_charge: Money
    tax_rate: Decimal


class SiteCreate(BaseModel):
    address_line: str = Field(min_length=1, max_length=300)
    city: str = Field(min_length=1, max_length=100)
    district: str = Field(min_length=1, max_length=100)
    postal_code: str | None = Field(default=None, max_length=20)
    connection_type: Literal["residential", "commercial", "industrial"] = "residential"
    sanctioned_load_kw: Decimal = Field(gt=0)
    tariff_plan_id: UUID


class MeterRegister(BaseModel):
    serial_no: str = Field(min_length=1, max_length=100)
    manufacturer: str = Field(min_length=1, max_length=100)
    model: str = Field(min_length=1, max_length=100)


class MeterRegisterOut(BaseModel):
    device_id: UUID
    serial_no: str
    backfill_from: date
    backfill_to: date
    readings_backfilled: int


class SolarRegister(BaseModel):
    capacity_kw: Decimal = Field(gt=0, le=1000)
    panel_count: int = Field(gt=0, le=2000)
    azimuth_deg: int = Field(default=180, ge=0, le=359)
    tilt_deg: int = Field(default=23, ge=0, le=90)
    manufacturer: str = Field(default="Growatt", max_length=100)
    model: str = Field(default="MIN-5000TL-X", max_length=100)


class SolarRegisterOut(BaseModel):
    inverter_device_id: UUID
    array_id: UUID
    agreement_id: UUID
    backfill_from: date
    backfill_to: date
    readings_backfilled: int
    # The billing meter's own history for the same window, re-netted against
    # this array's capacity now that it is known. Without this, a freshly
    # onboarded solar site's meter keeps the export-free readings the /meter
    # step wrote before any capacity existed, and the site would show zero
    # export -- and therefore never earn or roll over credit -- forever.
    meter_readings_updated: int


class BillingRunResult(BaseModel):
    period_start: date
    status: Literal["billed", "skipped"]
    bill_id: UUID | None
    reason: str | None


# --------------------------------------------------------------------------
# Authorization helpers
#
# require_role() answers "may this role call this endpoint at all". These
# answer the narrower question: "may this caller see THIS row". Both are
# needed -- a customer may legitimately call /summary, but only for a site
# they own.
# --------------------------------------------------------------------------

async def visible_site_or_404(
    conn: asyncpg.Connection,
    principal: Principal,
    site_id: UUID,
) -> None:
    """Raise unless this caller may read this site.

    404 rather than 403 for a site that exists but belongs to someone else.
    403 would confirm the site exists, which turns this endpoint into a probe
    for which meters are registered. The caller cannot act on the difference
    either way.
    """
    not_found = HTTPException(status_code=404, detail="site not found")

    if principal.sees_every_site:
        if not await conn.fetchval("SELECT 1 FROM site WHERE site_id = $1", site_id):
            raise not_found
        return

    if principal.role == "consumer":
        if not await conn.fetchval(
            sql("account_owns_site"), site_id, principal.account_id
        ):
            raise not_found
        return

    if principal.role == "worker":
        # A worker sees a site for as long as an assignment ties them to it.
        if not await conn.fetchval(
            sql("worker_covers_site"), site_id, principal.account_id
        ):
            raise not_found
        return

    raise not_found


# --------------------------------------------------------------------------
# Sites
# --------------------------------------------------------------------------

@app.get("/api/sites", response_model=list[Site], tags=["sites"])
async def list_sites(conn: Conn, principal: CurrentAccount) -> list[Site]:
    """Sites this caller may see.

    Government and supplier get the fleet; a customer gets the sites they own.
    The narrowing is a different statement rather than a filter over the full
    list, so rows the caller may not see are never fetched.
    """
    if principal.sees_every_site:
        rows = await conn.fetch(sql("list_sites"))
    elif principal.role == "consumer":
        rows = await conn.fetch(sql("sites_for_account"), principal.account_id)
    else:
        # Workers reach sites through their work orders; no field task starts
        # from "browse every site".
        raise HTTPException(
            status_code=403,
            detail="this endpoint is not available to role 'worker'",
        )
    return [Site(**dict(r)) for r in rows]


@app.get("/api/sites/{site_id}/summary", response_model=SiteSummary, tags=["sites"])
async def site_summary(
    conn: Conn,
    site_id: UUID,
    principal: Annotated[
        Principal,
        Depends(require_role("consumer", "government", "supplier", "admin")),
    ],
) -> SiteSummary:
    await visible_site_or_404(conn, principal, site_id)
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
    principal: CurrentAccount,
    days: Annotated[int, Query(ge=1, le=90, description="Trailing window in days")] = 7,
) -> list[Reading]:
    # Workers may read telemetry for a site they are dispatched to -- a data
    # gap or a dead inverter is diagnosed from exactly this series.
    await visible_site_or_404(conn, principal, site_id)
    rows = await conn.fetch(sql("site_readings"), site_id, days)
    return [Reading(**dict(r)) for r in rows]


@app.get("/api/sites/{site_id}/bills", response_model=list[Bill], tags=["sites"])
async def site_bills(
    conn: Conn,
    site_id: UUID,
    principal: Annotated[
        Principal,
        Depends(require_role("consumer", "government", "supplier", "admin")),
    ],
) -> list[Bill]:
    # Deliberately unavailable to workers: what a household was charged is not
    # something a field visit needs to know.
    await visible_site_or_404(conn, principal, site_id)

    # Two queries, not one with json_agg: nesting the line items in JSON would
    # push rate_applied and amount through a JSON number and lose exactness.
    bill_rows = await conn.fetch(sql("site_bills"), site_id)
    item_rows = await conn.fetch(sql("site_bill_line_items"), site_id)

    items: dict[UUID, list[BillLineItem]] = {}
    for r in item_rows:
        d = dict(r)
        items.setdefault(d.pop("bill_id"), []).append(BillLineItem(**d))

    return [Bill(**dict(r), line_items=items.get(r["bill_id"], [])) for r in bill_rows]


# --------------------------------------------------------------------------
# Onboarding
#
# A customer who registered with no meter serial lands here: build a site
# from scratch instead of claiming one. Every step below is scoped to the
# caller's own account -- there is no site_id a consumer can pass that was
# not either just returned to them or already theirs.
# --------------------------------------------------------------------------

@app.get("/api/tariff-plans", response_model=list[TariffPlanOut], tags=["sites"])
async def list_tariff_plans(
    conn: Conn,
    _: CurrentAccount,
    connection_type: Literal["residential", "commercial", "industrial"] | None = None,
) -> list[TariffPlanOut]:
    rows = await conn.fetch(sql("list_tariff_plans"), connection_type)
    return [TariffPlanOut(**dict(r)) for r in rows]


@app.post("/api/sites", response_model=Site, status_code=201, tags=["sites"])
async def create_site(
    conn: Conn,
    payload: SiteCreate,
    principal: Annotated[Principal, Depends(require_role("consumer"))],
) -> Site:
    latitude, longitude = _district_coordinates(payload.district)
    async with conn.transaction():
        try:
            site_id = await conn.fetchval(
                sql("create_site"),
                principal.account_id,
                payload.tariff_plan_id,
                "Home",
                payload.address_line.strip(),
                payload.city.strip(),
                payload.district.strip(),
                payload.postal_code,
                latitude,
                longitude,
                payload.connection_type,
                payload.sanctioned_load_kw,
            )
        except asyncpg.ForeignKeyViolationError:
            raise HTTPException(status_code=422, detail="unknown tariff plan") from None
        row = await conn.fetchrow(sql("get_site"), site_id)
    return Site(**dict(row))


@app.post(
    "/api/sites/{site_id}/meter",
    response_model=MeterRegisterOut,
    status_code=201,
    tags=["sites"],
)
async def register_meter(
    conn: Conn,
    site_id: UUID,
    payload: MeterRegister,
    principal: Annotated[Principal, Depends(require_role("consumer"))],
) -> MeterRegisterOut:
    """Register the site's one bidirectional billing meter (rule 7) and give
    it 90 days of history so the dashboard is not empty the moment it exists.
    """
    await visible_site_or_404(conn, principal, site_id)

    if await conn.fetchval(sql("site_has_billing_meter"), site_id):
        raise HTTPException(
            status_code=409, detail="this site already has a billing meter"
        )

    serial = payload.serial_no.strip()
    device_key_hash = hash_password(secrets.token_hex(32))
    backfill_from = date.today() - timedelta(days=BACKFILL_DAYS)
    backfill_to = date.today() - timedelta(days=1)

    async with conn.transaction():
        try:
            device_id = await conn.fetchval(
                sql("create_meter_device"),
                site_id, serial, payload.manufacturer.strip(),
                payload.model.strip(), device_key_hash,
            )
        except asyncpg.UniqueViolationError:
            raise HTTPException(
                status_code=409,
                detail=f"a device with serial '{serial}' is already registered",
            ) from None

        await conn.execute(sql("create_meter_spec"), device_id, site_id)

        reading_count = await conn.fetchval(
            "SELECT backfill_readings($1, $2, $3, NULL)",
            device_id, backfill_from, backfill_to,
        )

    return MeterRegisterOut(
        device_id=device_id,
        serial_no=serial,
        backfill_from=backfill_from,
        backfill_to=backfill_to,
        readings_backfilled=reading_count,
    )


@app.post(
    "/api/sites/{site_id}/solar",
    response_model=SolarRegisterOut,
    status_code=201,
    tags=["sites"],
)
async def register_solar(
    conn: Conn,
    site_id: UUID,
    payload: SolarRegister,
    principal: Annotated[Principal, Depends(require_role("consumer"))],
) -> SolarRegisterOut:
    """Add an inverter, its array and a pending net-metering agreement, then
    backfill the same 90-day window the meter got so the two lines agree on
    how far back the chart goes -- and re-net the meter's own history against
    this capacity, since it was written before any solar existed to net
    against (see backfill_readings' upsert note in db/sql/backfill.sql).
    """
    await visible_site_or_404(conn, principal, site_id)

    billing_device_id = await conn.fetchval(sql("site_billing_device"), site_id)
    if billing_device_id is None:
        raise HTTPException(
            status_code=409, detail="register a billing meter before adding solar"
        )

    device_key_hash = hash_password(secrets.token_hex(32))
    dc_capacity_kw = (payload.capacity_kw * Decimal("1.2")).quantize(Decimal("0.001"))
    panel_watt_peak = int((dc_capacity_kw * 1000 / payload.panel_count).to_integral_value())
    backfill_from = date.today() - timedelta(days=BACKFILL_DAYS)
    backfill_to = date.today() - timedelta(days=1)

    async with conn.transaction():
        inverter_id = await conn.fetchval(
            sql("create_inverter_device"),
            site_id, billing_device_id, f"ONB-INV-{uuid4().hex[:10].upper()}",
            payload.manufacturer.strip(), payload.model.strip(), device_key_hash,
        )
        await conn.execute(
            sql("create_inverter_spec"), inverter_id, payload.capacity_kw, dc_capacity_kw
        )
        array_id = await conn.fetchval(
            sql("create_solar_array"),
            site_id, inverter_id, payload.panel_count, panel_watt_peak,
            dc_capacity_kw, payload.azimuth_deg, payload.tilt_deg,
        )
        agreement_id = await conn.fetchval(
            sql("create_net_metering_agreement"),
            site_id, billing_device_id, f"ONB-NMA-{uuid4().hex[:10].upper()}",
            payload.capacity_kw,
        )

        reading_count = await conn.fetchval(
            "SELECT backfill_readings($1, $2, $3, $4)",
            inverter_id, backfill_from, backfill_to, payload.capacity_kw,
        )

        # Re-net the meter over the same window now that capacity is known.
        # The /meter step wrote these readings with p_capacity_kw = NULL
        # (export = 0, rule 8's own guard inside backfill_readings still
        # applies -- any interval already frozen or billed is left alone).
        meter_reading_count = await conn.fetchval(
            "SELECT backfill_readings($1, $2, $3, $4)",
            billing_device_id, backfill_from, backfill_to, payload.capacity_kw,
        )

    return SolarRegisterOut(
        inverter_device_id=inverter_id,
        array_id=array_id,
        agreement_id=agreement_id,
        backfill_from=backfill_from,
        backfill_to=backfill_to,
        readings_backfilled=reading_count,
        meter_readings_updated=meter_reading_count,
    )


async def _run_billing_with_retry(
    conn: asyncpg.Connection, site_id: UUID, period_start: date, attempts: int = 3
) -> UUID:
    """run_billing under REPEATABLE READ, retried on serialization failure --
    the isolation and retry contract CLAUDE.md requires of every caller.
    """
    for attempt in range(attempts):
        try:
            async with conn.transaction(isolation="repeatable_read"):
                return await conn.fetchval(
                    "SELECT run_billing($1, $2)", site_id, period_start
                )
        except asyncpg.SerializationError:
            if attempt == attempts - 1:
                raise


@app.post(
    "/api/sites/{site_id}/bill",
    response_model=list[BillingRunResult],
    tags=["sites"],
)
async def bill_site(
    conn: Conn,
    site_id: UUID,
    principal: Annotated[Principal, Depends(require_role("consumer"))],
) -> list[BillingRunResult]:
    """Run every complete month this site has readings for.

    "Complete" is not computed here -- run_billing's own coverage gate (rule
    8) is: a month attempted before it has fully elapsed comes back short of
    the 95% threshold and is reported skipped rather than billed.
    """
    await visible_site_or_404(conn, principal, site_id)

    window = await conn.fetchrow(sql("site_billing_window"), site_id)
    if window is None or window["first_month"] is None:
        raise HTTPException(
            status_code=409, detail="this site has no readings to bill yet"
        )

    results: list[BillingRunResult] = []
    month: date = window["first_month"]
    last_month = date(window["today"].year, window["today"].month, 1)

    while month <= last_month:
        try:
            bill_id = await _run_billing_with_retry(conn, site_id, month)
            results.append(
                BillingRunResult(
                    period_start=month, status="billed", bill_id=bill_id, reason=None
                )
            )
        except asyncpg.CheckViolationError as exc:
            results.append(
                BillingRunResult(
                    period_start=month, status="skipped", bill_id=None,
                    reason=str(exc),
                )
            )
        month = _next_month(month)

    return results


# --------------------------------------------------------------------------
# Issues
# --------------------------------------------------------------------------

@app.get("/api/issues", response_model=list[Issue], tags=["operations"])
async def list_issues(conn: Conn, principal: CurrentAccount) -> list[Issue]:
    if principal.sees_every_site:
        rows = await conn.fetch(sql("list_issues"))
    elif principal.role == "consumer":
        rows = await conn.fetch(sql("issues_for_account"), principal.account_id)
    else:
        rows = await conn.fetch(sql("issues_for_worker"), principal.account_id)
    return [Issue(**dict(r)) for r in rows]


@app.post("/api/issues", response_model=Issue, status_code=201, tags=["operations"])
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


@app.patch(
    "/api/work-orders/{order_id}/status",
    response_model=WorkOrder,
    tags=["operations"],
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
    return _work_order(row)


# --------------------------------------------------------------------------
# Net-metering agreements
# --------------------------------------------------------------------------

@app.get(
    "/api/agreements/pending",
    response_model=list[Agreement],
    tags=["agreements"],
)
async def pending_agreements(
    conn: Conn,
    _: Annotated[
        Principal, Depends(require_role("government", "supplier", "admin"))
    ],
) -> list[Agreement]:
    # The supplier submits these and needs to watch the queue; the government
    # is what decides them.
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
    _: Annotated[Principal, Depends(require_role("government", "admin"))],
) -> Agreement:
    """Approve or terminate a pending agreement.

    Government only. Approving is what lets a site's exports start earning
    credit, so the utility that pays for that export must not also be the party
    that authorizes it.
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
async def analytics_by_area(
    conn: Conn,
    _: Annotated[
        Principal, Depends(require_role("government", "supplier", "admin"))
    ],
) -> list[AreaStats]:
    rows = await conn.fetch(sql("analytics_by_area"))
    return [AreaStats(**dict(r)) for r in rows]
