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

from .auth import (
    SEED_PASSWORD_PLACEHOLDER,
    CurrentAccount,
    Principal,
    hash_password,
    require_role,
    visible_site_or_404,
)
from .db import Conn
from .orgs import resolve_district
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
    # Which of the site's connections this bill was cut for. A single-meter
    # household never needs it; a site with two shows the same card twice and
    # this is the only thing that tells them apart.
    point_label: str
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
    billing_point_id: UUID
    point_label: str
    point_reference: str | None
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


class BillingPoint(BaseModel):
    """One metering position on a site -- one connection the utility bills."""

    point_id: UUID
    label: str
    reference: str | None
    created_at: datetime
    # NULL while the point exists but its meter has not been registered yet,
    # which is the state the add-a-meter step is for.
    meter_device_id: UUID | None
    meter_serial: str | None
    meter_last_seen_at: datetime | None
    has_solar: bool


class SiteClaim(BaseModel):
    meter_serial: str = Field(min_length=1, max_length=100)


class MeterRegister(BaseModel):
    serial_no: str = Field(min_length=1, max_length=100)
    manufacturer: str = Field(min_length=1, max_length=100)
    model: str = Field(min_length=1, max_length=100)
    # The connection this meter serves. Omitted during onboarding, where the
    # site's first (empty) point is used; supplied when a household adds a
    # second billing meter and wants to name it.
    point_id: UUID | None = None
    point_label: str | None = Field(default=None, min_length=1, max_length=80)
    point_reference: str | None = Field(default=None, max_length=80)


class MeterRegisterOut(BaseModel):
    device_id: UUID
    serial_no: str
    point_id: UUID
    point_label: str
    point_reference: str | None
    backfill_from: date
    backfill_to: date
    readings_backfilled: int


class SolarRegister(BaseModel):
    # Which connection the array feeds. Optional while a site has exactly one
    # billing point; required once it has more, because netting the wrong
    # meter would credit one connection for another's export.
    point_id: UUID | None = None
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
    point_id: UUID
    # False when this array joined the agreement already covering the point
    # rather than opening a new one -- see the handler.
    agreement_created: bool
    # Arrays now live on this billing point, and their combined AC capacity.
    # Both count the array just added.
    array_count: int
    point_capacity_kw: Energy
    backfill_from: date
    backfill_to: date
    readings_backfilled: int
    # The billing meter's own history for the same window, re-netted against
    # the point's TOTAL capacity now that it is known. Without this, a freshly
    # onboarded solar site's meter keeps the export-free readings the /meter
    # step wrote before any capacity existed, and the site would show zero
    # export -- and therefore never earn or roll over credit -- forever.
    meter_readings_updated: int


class BillingRunResult(BaseModel):
    billing_point_id: UUID
    point_label: str
    period_start: date
    status: Literal["billed", "skipped"]
    bill_id: UUID | None
    reason: str | None


# --------------------------------------------------------------------------
# Onboarding helpers
# --------------------------------------------------------------------------

BACKFILL_DAYS = 90

# Districts, distribution companies and supplier companies now live in
# services/api/orgs.py, over the `district` table added by migration
# e7c4b19a2d83. The dict of centroids that used to sit here -- and the
# GET /api/districts that served it -- moved with them: district is a real
# foreign key on five tables now, and a canonical list maintained in a handler
# was exactly how `Dhaka`, `dhaka` and `g` became three rows in the
# regulator's rollup.


def _next_month(d: date) -> date:
    return date(d.year + (d.month == 12), (d.month % 12) + 1, 1)


async def _run_billing_with_retry(
    conn: asyncpg.Connection, point_id: UUID, period_start: date, attempts: int = 3
) -> UUID:
    """run_billing under REPEATABLE READ, retried on serialization failure --
    the isolation and retry contract CLAUDE.md requires of every caller.

    The unit is a billing point, not a site: a household with two connections
    gets two independent bills for the same month.
    """
    for attempt in range(attempts):
        try:
            async with conn.transaction(isolation="repeatable_read"):
                return await conn.fetchval(
                    "SELECT run_billing($1, $2)", point_id, period_start
                )
        except asyncpg.SerializationError:
            if attempt == attempts - 1:
                raise


async def _points_on(conn: asyncpg.Connection, site_id: UUID) -> list[asyncpg.Record]:
    return await conn.fetch(sql("site_points"), site_id)


async def _point_for_new_meter(
    conn: asyncpg.Connection, site_id: UUID, payload: "MeterRegister"
) -> asyncpg.Record:
    """Which billing point the meter about to be registered belongs on.

    Three cases, in the order the docstring on register_meter describes them.
    A named point must exist on this site and must still be unmetered; a
    named label opens a new point; silence reuses the site's one empty point
    if that is unambiguous, and otherwise opens a numbered one rather than
    refusing -- adding a meter is the whole point of the call, so there is a
    sensible answer to give.
    """
    if payload.point_id is not None:
        return await _resolve_point(conn, site_id, payload.point_id)

    points = await _points_on(conn, site_id)

    if payload.point_label is None:
        empty = [p for p in points if p["meter_device_id"] is None]
        if len(empty) == 1:
            return empty[0]
        label = f"Meter {len(points) + 1}"
    else:
        label = payload.point_label.strip()

    try:
        created = await conn.fetchrow(
            sql("create_billing_point"), site_id, label, payload.point_reference
        )
    except asyncpg.UniqueViolationError:
        # point_label_per_site or point_reference_unique. Both mean the
        # caller is describing a connection that already exists.
        raise HTTPException(
            status_code=409,
            detail=(
                f"a billing meter named '{label}' or carrying that reference "
                "is already registered"
            ),
        ) from None

    # create_billing_point returns only the three columns it wrote; the rest
    # of the BillingPoint shape is all NULL/false for a point with no meter,
    # and _point_for_new_meter's callers read label/reference/point_id only.
    return created


async def _resolve_point(
    conn: asyncpg.Connection, site_id: UUID, point_id: UUID | None
) -> asyncpg.Record:
    """The billing point an operation applies to, checked against the site.

    Naming no point is only allowed while the answer is unambiguous. Once a
    household has two connections, silently picking one would net the wrong
    meter or bill the wrong balance, so this refuses rather than guesses --
    and the caller has just been handed the list by GET /points, so it has
    the id to send.
    """
    points = await _points_on(conn, site_id)
    if not points:
        raise HTTPException(
            status_code=409, detail="this site has no billing points"
        )

    if point_id is None:
        if len(points) > 1:
            raise HTTPException(
                status_code=422,
                detail=(
                    "this site has several billing meters; name the one to use "
                    "with point_id"
                ),
            )
        return points[0]

    for row in points:
        if row["point_id"] == point_id:
            return row
    raise HTTPException(
        status_code=404, detail="no such billing point on this site"
    )


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
            point_label=row["bill_point_label"],
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


@router.post("/api/sites/claim", response_model=Site, status_code=200)
async def claim_site(
    conn: Conn,
    payload: SiteClaim,
    principal: Annotated[Principal, Depends(require_role("consumer"))],
) -> Site:
    """Take ownership of an existing connection by the serial on its meter.

    This used to be part of customer registration. Consumer requirement 2
    says a billing meter ID is not asked for at sign-up, so it moved here,
    behind a login -- which is also where requirement 3 puts adding meters.
    A household that already has a metered connection claims it; one that does
    not builds a new site instead.

    The site is transferred rather than copied: everything the portal shows is
    keyed on site_id, so readings, bills, credit and issues all come across.
    The old bills keep naming the previous owner, which is what rule 2 is for.
    """
    serial = payload.meter_serial.strip()

    async with conn.transaction():
        site = await conn.fetchrow(
            sql("site_by_meter_serial"), serial, SEED_PASSWORD_PLACEHOLDER
        )
        if site is None:
            raise HTTPException(
                status_code=404,
                detail=(
                    f"no billing meter with serial '{serial}'. "
                    "Check the serial printed on the meter."
                ),
            )
        if not site["is_unclaimed"]:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"{site['site_label']} has already been claimed. If this is "
                    "your meter, contact support."
                ),
            )

        transferred = await conn.fetchval(
            sql("transfer_site"),
            site["site_id"],
            principal.account_id,
            site["current_account_id"],
        )
        if transferred is None:
            # Someone claimed it between the check and the update.
            raise HTTPException(
                status_code=409,
                detail="that meter was claimed while you were claiming it",
            )

        row = await conn.fetchrow(sql("get_site"), site["site_id"])
    return Site(**dict(row))


@router.post("/api/sites", response_model=Site, status_code=201)
async def create_site(
    conn: Conn,
    payload: SiteCreate,
    principal: Annotated[Principal, Depends(require_role("consumer"))],
) -> Site:
    district, latitude, longitude = await resolve_district(conn, payload.district)
    # The address, not the constant "Home" this used to write. A customer with
    # one site never noticed, but `site.label` is also what the supplier's
    # fleet table and the government's queue identify a site by, and eight rows
    # all reading "Home" identify nothing. The address is what a utility calls
    # a connection anyway.
    label = payload.address_line.strip()[:80]
    async with conn.transaction():
        try:
            site_id = await conn.fetchval(
                sql("create_site"),
                principal.account_id,
                payload.tariff_plan_id,
                label,
                payload.address_line.strip(),
                payload.city.strip(),
                district,
                payload.postal_code,
                latitude,
                longitude,
                payload.connection_type,
                payload.sanctioned_load_kw,
            )
        except asyncpg.ForeignKeyViolationError:
            raise HTTPException(status_code=422, detail="unknown tariff plan") from None

        # Every site starts with one billing point, so the meter step never
        # has to decide whether to invent one and the migration's backfill
        # and this path produce the same shape. More are added by registering
        # more meters.
        await conn.fetchrow(sql("create_billing_point"), site_id, "Main", None)

        row = await conn.fetchrow(sql("get_site"), site_id)
    return Site(**dict(row))


@router.get("/api/sites/{site_id}/points", response_model=list[BillingPoint])
async def list_billing_points(
    conn: Conn, site_id: UUID, principal: CurrentAccount
) -> list[BillingPoint]:
    """The site's billing meters, one row per connection.

    Open to workers as well as the household: a field visit for "the meter is
    dead" needs to know which of the two meters on the wall is which.
    """
    await visible_site_or_404(conn, principal, site_id)
    return [BillingPoint(**dict(r)) for r in await _points_on(conn, site_id)]


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
    """Register a bidirectional billing meter on this site and give it 90 days
    of history so the dashboard is not empty the moment it exists.

    Additive. A household may hold several billing meters, one per billing
    point (rule 7 constrains the point, not the site, since migration
    d5a7c2b91e40), so this is both the onboarding step and the "add another
    meter" action:

    * `point_id` names an existing connection that has no meter yet -- which
      is what onboarding does, using the empty point `POST /api/sites`
      created.
    * `point_label` opens a new connection under that name.
    * Neither: reuse the site's single unmetered point if there is exactly
      one, otherwise open a new one labelled "Meter 2", "Meter 3" and so on.

    The backfill is scoped by billing point too, so adding a second meter to a
    site whose first is already billed still gets its own history -- see the
    rule-8 guard in db/sql/service/backfill.sql.
    """
    await visible_site_or_404(conn, principal, site_id)

    serial = payload.serial_no.strip()
    device_key_hash = hash_password(secrets.token_hex(32))
    backfill_from = date.today() - timedelta(days=BACKFILL_DAYS)
    backfill_to = date.today() - timedelta(days=1)

    async with conn.transaction():
        point = await _point_for_new_meter(conn, site_id, payload)

        if await conn.fetchval(sql("point_has_billing_meter"), point["point_id"]):
            raise HTTPException(
                status_code=409,
                detail=f"'{point['label']}' already has a billing meter",
            )

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

        await conn.execute(
            sql("create_meter_spec"), device_id, site_id, point["point_id"]
        )

        reading_count = await conn.fetchval(
            "SELECT backfill_readings($1, $2, $3, NULL)",
            device_id, backfill_from, backfill_to,
        )

    return MeterRegisterOut(
        device_id=device_id,
        serial_no=serial,
        point_id=point["point_id"],
        point_label=point["label"],
        point_reference=point["reference"],
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

    Additive, not once-per-connection. `solar_array` is 1-N, so a connection
    may genuinely carry several arrays, and two things follow from that:

    * The meter is re-netted against the billing point's TOTAL AC capacity,
      not the array just registered. Passing this array's capacity alone would
      rewrite an existing multi-array history as if the other arrays were not
      there, silently *reducing* recorded export.
    * The agreement is reused. `nma_no_overlap` allows one non-terminated
      agreement per billing point, and rule 1 forbids editing a live one's
      sanctioned capacity -- an uprate is a new agreement with its own
      effective_from, which is a government decision, not something this
      endpoint may take. So a second array joins the existing agreement, and
      the response says so via `agreement_created`.

    Everything here is scoped to one billing point rather than the whole site.
    A household with two connections has two meters, two agreements and two
    credit balances; netting the site's total capacity into whichever meter
    answered first would credit one connection for the other's export.
    """
    await visible_site_or_404(conn, principal, site_id)

    point = await _resolve_point(conn, site_id, payload.point_id)
    point_id = point["point_id"]

    billing_device_id = await conn.fetchval(sql("point_billing_device"), point_id)
    if billing_device_id is None:
        raise HTTPException(
            status_code=409,
            detail=f"register a billing meter on '{point['label']}' before adding solar",
        )

    existing = await conn.fetchrow(sql("point_solar_status"), point_id)
    array_count = existing["array_count"] + 1
    point_capacity_kw = existing["capacity_kw"] + payload.capacity_kw
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

        open_agreement = await conn.fetchrow(sql("point_open_agreement"), point_id)
        agreement_created = open_agreement is None
        if agreement_created:
            try:
                agreement_id = await conn.fetchval(
                    sql("create_net_metering_agreement"),
                    site_id, point_id, billing_device_id,
                    f"ONB-NMA-{uuid4().hex[:10].upper()}",
                    point_capacity_kw,
                )
            except asyncpg.ExclusionViolationError:
                # Backstop for the race the SELECT above cannot close: two
                # concurrent /solar calls on one point. Without it that
                # surfaces as a 500.
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"'{point['label']}' already has a net-metering "
                        "agreement"
                    ),
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
        # point_capacity_kw, not payload.capacity_kw: the meter measures
        # everything behind this connection at the grid boundary (rule 6), so
        # netting it against one array would understate export wherever the
        # connection carries more than one.
        meter_reading_count = await conn.fetchval(
            "SELECT backfill_readings($1, $2, $3, $4)",
            billing_device_id, backfill_from, backfill_to, point_capacity_kw,
        )

    return SolarRegisterOut(
        inverter_device_id=inverter_id,
        array_id=array_id,
        agreement_id=agreement_id,
        point_id=point_id,
        agreement_created=agreement_created,
        array_count=array_count,
        point_capacity_kw=point_capacity_kw,
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
    """Run every complete month every one of this site's connections has
    readings for.

    One bill per billing point per month: a household with two meters gets two
    bills for June, each with its own credit balance, because that is what the
    utility issues.

    "Complete" is not computed here -- run_billing's own coverage gate (rule
    8) is: a month attempted before it has fully elapsed comes back short of
    the 95% threshold and is reported skipped rather than billed. A point with
    no meter yet has no window and is passed over silently; it is a legal
    state, not a failure.
    """
    await visible_site_or_404(conn, principal, site_id)

    points = await _points_on(conn, site_id)
    results: list[BillingRunResult] = []

    for point in points:
        point_id = point["point_id"]
        window = await conn.fetchrow(sql("point_billing_window"), point_id)
        if window is None or window["first_month"] is None:
            continue

        month: date = window["first_month"]
        last_month = date(window["today"].year, window["today"].month, 1)

        while month <= last_month:
            try:
                bill_id = await _run_billing_with_retry(conn, point_id, month)
                results.append(
                    BillingRunResult(
                        billing_point_id=point_id, point_label=point["label"],
                        period_start=month, status="billed", bill_id=bill_id,
                        reason=None,
                    )
                )
            except asyncpg.CheckViolationError as exc:
                results.append(
                    BillingRunResult(
                        billing_point_id=point_id, point_label=point["label"],
                        period_start=month, status="skipped", bill_id=None,
                        reason=str(exc),
                    )
                )
            month = _next_month(month)

    if not results:
        raise HTTPException(
            status_code=409, detail="this site has no readings to bill yet"
        )

    return results
