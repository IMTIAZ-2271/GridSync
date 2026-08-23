"""Sites: the dashboard's core read surface, plus onboarding.

A customer with no site lands in the onboarding flow at the bottom of this
file: POST /api/sites, then /meter, then optionally /solar, then /bill. Every
step is scoped to the caller's own account -- there is no site_id a consumer
can pass that was not either just returned to them or already theirs.
"""
from __future__ import annotations

import secrets
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID, uuid4

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from .auth import CurrentAccount, Principal, hash_password, require_role, visible_site_or_404
from .db import Conn
from .queries import sql
from .types import Energy, Money, Rate

router = APIRouter(tags=["sites"])


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
    # False when this array joined the agreement already covering the site
    # rather than opening a new one -- see the handler.
    agreement_created: bool
    # Arrays now live on this site, and their combined AC capacity. Both count
    # the array just added.
    array_count: int
    site_capacity_kw: Energy
    backfill_from: date
    backfill_to: date
    readings_backfilled: int
    # The billing meter's own history for the same window, re-netted against
    # the site's TOTAL capacity now that it is known. Without this, a freshly
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
# Onboarding helpers
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


# --------------------------------------------------------------------------
# Sites
# --------------------------------------------------------------------------

@router.get("/api/sites", response_model=list[Site])
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


@router.get("/api/sites/{site_id}/summary", response_model=SiteSummary)
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


@router.get("/api/sites/{site_id}/readings", response_model=list[Reading])
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


@router.get("/api/sites/{site_id}/bills", response_model=list[Bill])
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
# --------------------------------------------------------------------------

@router.get("/api/tariff-plans", response_model=list[TariffPlanOut])
async def list_tariff_plans(
    conn: Conn,
    _: CurrentAccount,
    connection_type: Literal["residential", "commercial", "industrial"] | None = None,
) -> list[TariffPlanOut]:
    rows = await conn.fetch(sql("list_tariff_plans"), connection_type)
    return [TariffPlanOut(**dict(r)) for r in rows]


@router.post("/api/sites", response_model=Site, status_code=201)
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


@router.post(
    "/api/sites/{site_id}/meter",
    response_model=MeterRegisterOut,
    status_code=201,
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


@router.post(
    "/api/sites/{site_id}/solar",
    response_model=SolarRegisterOut,
    status_code=201,
)
async def register_solar(
    conn: Conn,
    site_id: UUID,
    payload: SolarRegister,
    principal: Annotated[Principal, Depends(require_role("consumer"))],
) -> SolarRegisterOut:
    """Add an inverter, its array and a net-metering agreement, then backfill
    the same 90-day window the meter got so the two lines agree on how far
    back the chart goes -- and re-net the meter's own history, since it was
    written before any solar existed to net against (see backfill_readings'
    upsert note in db/sql/service/backfill.sql).

    Additive, not once-per-site. `solar_array` is 1-N on `site`, so a site may
    genuinely carry several arrays, and two things follow from that:

    * The meter is re-netted against the site's TOTAL AC capacity, not the
      array just registered. Passing this array's capacity alone would rewrite
      an existing multi-array site's history as if the other arrays were not
      there, silently *reducing* its recorded export.
    * The agreement is reused. `nma_no_overlap` allows one non-terminated
      agreement per site, and rule 1 forbids editing a live one's sanctioned
      capacity -- an uprate is a new agreement with its own effective_from,
      which is a government decision, not something this endpoint may take.
      So a second array joins the existing agreement, and the response says so
      via `agreement_created`.
    """
    await visible_site_or_404(conn, principal, site_id)

    billing_device_id = await conn.fetchval(sql("site_billing_device"), site_id)
    if billing_device_id is None:
        raise HTTPException(
            status_code=409, detail="register a billing meter before adding solar"
        )

    existing = await conn.fetchrow(sql("site_solar_status"), site_id)
    array_count = existing["array_count"] + 1
    site_capacity_kw = existing["capacity_kw"] + payload.capacity_kw
    label = "Rooftop array" if array_count == 1 else f"Rooftop array {array_count}"

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
            site_id, inverter_id, label, payload.panel_count, panel_watt_peak,
            dc_capacity_kw, payload.azimuth_deg, payload.tilt_deg,
        )

        open_agreement = await conn.fetchrow(sql("site_open_agreement"), site_id)
        agreement_created = open_agreement is None
        if agreement_created:
            try:
                agreement_id = await conn.fetchval(
                    sql("create_net_metering_agreement"),
                    site_id, billing_device_id,
                    f"ONB-NMA-{uuid4().hex[:10].upper()}",
                    site_capacity_kw,
                )
            except asyncpg.ExclusionViolationError:
                # Backstop for the race the SELECT above cannot close: two
                # concurrent /solar calls on one site. Without it that
                # surfaces as a 500.
                raise HTTPException(
                    status_code=409,
                    detail="this site already has a net-metering agreement",
                ) from None
        else:
            agreement_id = open_agreement["agreement_id"]

        reading_count = await conn.fetchval(
            "SELECT backfill_readings($1, $2, $3, $4)",
            inverter_id, backfill_from, backfill_to, payload.capacity_kw,
        )

        # Re-net the meter over the same window now that capacity is known.
        # The /meter step wrote these readings with p_capacity_kw = NULL
        # (export = 0, rule 8's own guard inside backfill_readings still
        # applies -- any interval already frozen or billed is left alone).
        # site_capacity_kw, not payload.capacity_kw: the meter measures the
        # whole site at the grid boundary (rule 6), so netting it against one
        # array would understate export on a multi-array site.
        meter_reading_count = await conn.fetchval(
            "SELECT backfill_readings($1, $2, $3, $4)",
            billing_device_id, backfill_from, backfill_to, site_capacity_kw,
        )

    return SolarRegisterOut(
        inverter_device_id=inverter_id,
        array_id=array_id,
        agreement_id=agreement_id,
        agreement_created=agreement_created,
        array_count=array_count,
        site_capacity_kw=site_capacity_kw,
        backfill_from=backfill_from,
        backfill_to=backfill_to,
        readings_backfilled=reading_count,
        meter_readings_updated=meter_reading_count,
    )


@router.post(
    "/api/sites/{site_id}/bill",
    response_model=list[BillingRunResult],
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
