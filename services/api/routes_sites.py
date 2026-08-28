"""Sites: the dashboard's core read surface, plus onboarding.

A consumer with no site lands in the onboarding flow at the bottom of this
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
from .billing import next_month, run_billing_with_retry
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
    # Which of the household's own meters to install. There is no serial field
    # any more: a meter is hardware the utility issued (migration
    # c9e2f4a71b83), and typing a number at a form let a consumer conjure
    # hardware nobody owns. If they have none available they apply for one --
    # POST /api/meter-applications.
    meter_asset_id: UUID
    # The connection this meter serves. Omitted during onboarding, where the
    # site's first (empty) point is used; supplied when a household adds a
    # second billing meter and wants to name it.
    point_id: UUID | None = None
    point_label: str | None = Field(default=None, min_length=1, max_length=80)
    point_reference: str | None = Field(default=None, max_length=80)
    # Retire the meter currently serving this connection and take its place.
    # This is what a net-metering approval ends in: the connection already has
    # a billing meter, rule 7 allows one, and the replacement is bidirectional.
    replace_existing: bool = False


class MeterRegisterOut(BaseModel):
    device_id: UUID
    serial_no: str
    point_id: UUID
    point_label: str
    point_reference: str | None
    backfill_from: date
    backfill_to: date
    readings_backfilled: int
    # The meter this one replaced, when it replaced one. Its readings stay on
    # the connection -- only the device is retired.
    replaced_serial_no: str | None = None


class SolarRegister(BaseModel):
    """Panels and the inverter that runs them.

    There is deliberately no `point_id`. An inverter is not part of a
    connection when it is installed -- it becomes part of one only when net
    metering is granted and a bidirectional meter goes on the wall, which is
    the act that makes its export measurable at all (rule 6). Registering
    panels against a connection would be asserting that, months early.
    """

    capacity_kw: Decimal = Field(gt=0, le=1000)
    panel_count: int = Field(gt=0, le=2000)
    azimuth_deg: int = Field(default=180, ge=0, le=359)
    tilt_deg: int = Field(default=23, ge=0, le=90)
    manufacturer: str = Field(default="Growatt", max_length=100)
    model: str = Field(default="MIN-5000TL-X", max_length=100)


class SolarRegisterOut(BaseModel):
    inverter_device_id: UUID
    inverter_serial_no: str
    array_id: UUID
    # Arrays on this site and their combined AC capacity, both counting the
    # one just added. Site-wide rather than per-connection: until net metering
    # attaches an inverter to a billing point, the site is the only scope the
    # panels have.
    array_count: int
    site_capacity_kw: Energy
    backfill_from: date
    backfill_to: date
    readings_backfilled: int


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

    Government and supplier get the fleet; a consumer gets the sites they own.
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
    point_id: Annotated[
        UUID | None,
        Query(description="Narrow every figure to one billing point"),
    ] = None,
) -> SiteSummary:
    """The three headline figures, for the whole site or for one connection.

    `point_id` is consumer requirement 5: once a household picks a meter, the
    credit balance, the latest bill and the 30-day window are all that meter's.
    A balance summed across connections they did not select is a number that
    belongs to nothing else on the screen.

    An unknown or foreign point is not an error -- the scoped subqueries simply
    match nothing and the site reports zeros. The point still has to be on a
    site this caller may read, which `visible_site_or_404` has already settled.
    """
    await visible_site_or_404(conn, principal, site_id)
    row = await conn.fetchrow(sql("site_summary"), site_id, point_id)
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
    timeframe: Annotated[
        Literal["day", "week", "month", "year"],
        Query(description="Window and bucket size, chosen together"),
    ] = "week",
    point_id: Annotated[
        UUID | None, Query(description="Narrow to one billing point")
    ] = None,
) -> list[Reading]:
    """The chart series.

    `timeframe` picks the window and the bucket as one choice, in SQL -- see
    site_readings in db/sql/dao/site_queries.sql. They cannot be chosen
    independently: a year of half-hourly points is a smear and a day of monthly
    buckets is one bar, so there is no combination worth exposing.

    Workers may read this for a site they are dispatched to -- a data gap or a
    dead inverter is diagnosed from exactly this series.
    """
    await visible_site_or_404(conn, principal, site_id)
    rows = await conn.fetch(sql("site_readings"), site_id, timeframe, point_id)
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

    This used to be part of consumer registration. Consumer requirement 2
    says a billing meter ID is not asked for at sign-up, so it moved here,
    behind a login -- which is also where requirement 3 puts adding meters.
    A household that already has a metered connection claims it; one that does
    not builds a new site instead.

    The site is transferred rather than copied: everything the portal shows is
    keyed on site_id, so readings, bills, credit and issues all come across --
    and so do the meters, which are keyed on the *account* rather than the site
    and would otherwise stay listed under the previous holder.
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

        # The meters come with it. A meter_asset is issued to a person
        # (migration c9e2f4a71b83), so transferring only the site would leave
        # the new owner holding connections whose hardware is still listed
        # under the previous holder -- and their Meters page saying they own
        # no meters at all.
        await conn.execute(
            sql("transfer_meter_assets"), site["site_id"], principal.account_id
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
    # The address, not the constant "Home" this used to write. A consumer with
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
    """Install one of the household's own meters on this site and give it 90
    days of history so the dashboard is not empty the moment it exists.

    The meter must already be **issued to this consumer and unassigned** --
    `meter_asset.device_id IS NULL`. That is the whole shape of the change in
    migration c9e2f4a71b83: the utility issues hardware against an identity,
    and the household's part is choosing which connection it serves. A caller
    with nothing available applies at POST /api/meter-applications; there is no
    path here that invents a serial.

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

    device_key_hash = hash_password(secrets.token_hex(32))
    backfill_from = date.today() - timedelta(days=BACKFILL_DAYS)
    backfill_to = date.today() - timedelta(days=1)

    asset = await conn.fetchrow(
        sql("meter_asset_for_assignment"),
        payload.meter_asset_id, principal.account_id,
    )
    if asset is None:
        # A meter belonging to somebody else reads the same as one that does
        # not exist. The caller cannot act on the difference, and telling them
        # apart would turn this into a probe for other people's hardware.
        raise HTTPException(status_code=404, detail="meter not found")
    serial = asset["serial_no"]

    async with conn.transaction():
        point = await _point_for_new_meter(conn, site_id, payload)
        replaced = None

        if await conn.fetchval(sql("point_has_billing_meter"), point["point_id"]):
            if not payload.replace_existing:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"'{point['label']}' already has a billing meter. Send "
                        "replace_existing to swap it."
                    ),
                )
            # Rule 7's triggers are DEFERRED, so the point may briefly hold
            # none or two inside this transaction; only COMMIT is checked.
            replaced = await conn.fetchrow(
                sql("retire_point_billing_meter"), point["point_id"]
            )

        if payload.replace_existing:
            # Never re-cover ground the retired meter already holds:
            # site_readings sums across every device on the site, so an
            # overlapping backfill would double the household's import. In the
            # ordinary case the point is covered through yesterday and this
            # window is empty, which is correct -- the new meter starts
            # measuring now, and the connection keeps its history.
            last_day = await conn.fetchval(
                sql("point_reading_horizon"), point["point_id"]
            )
            if last_day is not None:
                backfill_from = max(backfill_from, last_day + timedelta(days=1))

        try:
            device_id = await conn.fetchval(
                sql("create_meter_device"),
                site_id, serial, asset["manufacturer"] or "Unknown",
                asset["model"] or "Unknown", device_key_hash,
            )
        except asyncpg.UniqueViolationError:
            raise HTTPException(
                status_code=409,
                detail=f"a device with serial '{serial}' is already registered",
            ) from None

        # The claim is what actually decides, and it is inside the transaction
        # with the device it points at: two tabs assigning one meter to two
        # connections produce one winner, and the loser's device row rolls back
        # with it rather than being left orphaned under a serial nobody holds.
        claimed = await conn.fetchval(
            sql("claim_meter_asset"),
            payload.meter_asset_id, principal.account_id, device_id,
        )
        if claimed is None:
            raise HTTPException(
                status_code=409,
                detail=f"meter '{serial}' is already assigned to a connection",
            )

        # Unidirectional unless this connection is net-metered. A meter that
        # can measure export is the *outcome* of a net-metering agreement, not
        # an option at install time -- rule 6 puts the import/export split at
        # the grid boundary, and only the regulator decides a connection may
        # sit there. An ordinary household meter must never look net-metered.
        agreement = await conn.fetchrow(sql("point_open_agreement"), point["point_id"])
        net_metered = agreement is not None and agreement["status"] == "active"
        meter_flow = "bidirectional" if net_metered else "unidirectional"

        await conn.execute(
            sql("create_meter_spec"),
            device_id, site_id, point["point_id"], meter_flow,
        )

        # The panels join the connection here and nowhere else. Until a meter
        # that can measure export is actually on the wall, an inverter
        # attached to a billing point would be claiming an export nothing can
        # see. attach_inverter_to_point is guarded on the inverter still being
        # unattached, so re-running this cannot move one off a connection it
        # already serves.
        solar_capacity_kw = None
        if net_metered and agreement["inverter_device_id"] is not None:
            await conn.fetchval(
                sql("attach_inverter_to_point"),
                agreement["inverter_device_id"], point["point_id"],
            )
            solar_capacity_kw = await conn.fetchval(
                "SELECT ac_capacity_kw FROM inverter_spec WHERE device_id = $1",
                agreement["inverter_device_id"],
            )

        # p_capacity_kw is the solar the new meter nets against. NULL on an
        # ordinary install (no panels behind this connection, so import is
        # plain consumption); the inverter's rating once the swap has made
        # export measurable, which is what starts the connection earning
        # credit. The window is already clipped past the retired meter's last
        # reading above, so this never re-covers ground and never double-counts.
        reading_count = (
            await conn.fetchval(
                "SELECT backfill_readings($1, $2, $3, $4)",
                device_id, backfill_from, backfill_to, solar_capacity_kw,
            )
            if backfill_from <= backfill_to
            else 0
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
        replaced_serial_no=replaced["serial_no"] if replaced else None,
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
    """Register an inverter and the array it runs, and backfill its generation.

    **The inverter is its own connection, not an attachment to a meter.** It
    is created against the site with `inverter_spec.billing_point_id` NULL,
    and it stays that way until net metering is granted -- registering it
    against a billing point would assert months early that this connection can
    sell power back.

    Two consequences worth being explicit about, because both are changes:

    * **A site needs no billing meter for this.** Panels are fitted by a
      private installer and a billing meter is issued by the distribution
      company; they are different organisations doing different things at
      different times (decision 4), and requiring one before the other made
      the ordinary sequence -- panels first, net metering later -- impossible
      to record.

    * **The billing meter is not re-netted here, and that is a considered
      choice rather than an omission.** Netting a *unidirectional* meter
      against solar destroys the one figure the net-metering test needs: once
      import reads `load - generation` and export cannot be measured at all,
      the household's actual consumption is unrecoverable whenever the panels
      out-produce the house. So an un-swapped meter keeps reporting the grid
      draw, which is also exactly the figure a utility assesses an application
      against -- your billed consumption. The netted split begins when the
      bidirectional meter that can actually measure it is installed.

    Additive. `solar_array` is 1-N on the site, so a household may add a
    second array later, and each gets its own inverter.
    """
    await visible_site_or_404(conn, principal, site_id)

    existing = await conn.fetchrow(sql("site_solar_status"), site_id)
    array_count = existing["array_count"] + 1
    site_capacity_kw = existing["capacity_kw"] + payload.capacity_kw
    label = "Rooftop array" if array_count == 1 else f"Rooftop array {array_count}"

    serial_no = f"ONB-INV-{uuid4().hex[:10].upper()}"
    device_key_hash = hash_password(secrets.token_hex(32))
    dc_capacity_kw = (payload.capacity_kw * Decimal("1.2")).quantize(Decimal("0.001"))
    panel_watt_peak = int((dc_capacity_kw * 1000 / payload.panel_count).to_integral_value())
    backfill_from = date.today() - timedelta(days=BACKFILL_DAYS)
    backfill_to = date.today() - timedelta(days=1)

    async with conn.transaction():
        inverter_id = await conn.fetchval(
            sql("create_inverter_device"),
            # parent_device_id is NULL: the inverter is not defined by a meter
            # any more, and on a site with no meter there is nothing to point
            # at. Physical topology is recorded at the swap, when a technician
            # has actually seen how it is wired.
            site_id, None, serial_no,
            payload.manufacturer.strip(), payload.model.strip(), device_key_hash,
        )
        await conn.execute(
            sql("create_inverter_spec"),
            inverter_id, site_id, None, payload.capacity_kw, dc_capacity_kw,
        )
        array_id = await conn.fetchval(
            sql("create_solar_array"),
            site_id, inverter_id, label, payload.panel_count, panel_watt_peak,
            dc_capacity_kw, payload.azimuth_deg, payload.tilt_deg,
        )

        # 90 days of generation, so the eligibility test for net metering has
        # something real to measure. It needs 30 days of it (with a 14-day
        # floor), and a household that has just had panels fitted would
        # otherwise have to wait two weeks before it could even be assessed.
        reading_count = await conn.fetchval(
            "SELECT backfill_readings($1, $2, $3, $4)",
            inverter_id, backfill_from, backfill_to, payload.capacity_kw,
        )

    return SolarRegisterOut(
        inverter_device_id=inverter_id,
        inverter_serial_no=serial_no,
        array_id=array_id,
        array_count=array_count,
        site_capacity_kw=site_capacity_kw,
        backfill_from=backfill_from,
        backfill_to=backfill_to,
        readings_backfilled=reading_count,
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
                bill_id = await run_billing_with_retry(conn, point_id, month)
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
            month = next_month(month)

    if not results:
        raise HTTPException(
            status_code=409, detail="this site has no readings to bill yet"
        )

    return results


# --------------------------------------------------------------------------
# Consumption limit -- consumer requirement 5, settings half
#
# The jobs runner has read `site_consumption_limit` every morning since the
# runner landed; this is what finally lets a household write one. The alert
# itself lives in services/jobs/consumption.py.
#
# Consumer-only, deliberately. A regulator or an installer looking at somebody
# else's site has no business setting the warning threshold on their electricity
# bill, and the alert is delivered to the site owner regardless of who set it --
# so an endpoint any role could call would let a stranger make a household's
# phone buzz.
# --------------------------------------------------------------------------

class ConsumptionLimit(BaseModel):
    """The budget, and what has been spent against it this month.

    `used_kwh` is served alongside the limit rather than left to the client to
    assemble from the readings endpoint: it is the same month-to-date arithmetic
    the jobs sweep alerts on, and two derivations of "how much have I used" that
    could disagree is worse than one repeated in a single SQL file.
    """

    site_id: UUID
    month_start: date
    used_kwh: Energy
    monthly_kwh: Energy | None
    notify_at_pct: Rate | None
    daily_allowance_kwh: Energy | None
    updated_at: datetime | None


class ConsumptionLimitUpdate(BaseModel):
    # 1 kWh is a nonsense budget and 100000 is a small factory. Bounded here
    # rather than only by the CHECK so the caller gets a 422 naming the field
    # instead of a 500 from a constraint violation.
    monthly_kwh: Decimal = Field(gt=0, le=100000)
    # Matches limit_notify_pct on the table.
    notify_at_pct: Decimal = Field(default=Decimal("80.00"), ge=1, le=100)


def _limit(site_id: UUID, row: asyncpg.Record) -> ConsumptionLimit:
    return ConsumptionLimit(
        site_id=site_id,
        month_start=row["month_start"],
        used_kwh=row["used_kwh"],
        monthly_kwh=row["monthly_kwh"],
        notify_at_pct=row["notify_at_pct"],
        daily_allowance_kwh=row["daily_allowance_kwh"],
        updated_at=row["updated_at"],
    )


@router.get(
    "/api/sites/{site_id}/consumption-limit",
    response_model=ConsumptionLimit,
)
async def get_consumption_limit(
    conn: Conn,
    site_id: UUID,
    principal: Annotated[Principal, Depends(require_role("consumer"))],
) -> ConsumptionLimit:
    """The site's limit, or the same shape with nulls if none is set.

    200-with-nulls rather than 404: "this household has not set a budget" is a
    normal state of a real site, not a missing resource, and the usage figure is
    worth showing before they pick a number.
    """
    await visible_site_or_404(conn, principal, site_id)
    row = await conn.fetchrow(sql("get_consumption_limit"), site_id)
    return _limit(site_id, row)


@router.put(
    "/api/sites/{site_id}/consumption-limit",
    response_model=ConsumptionLimit,
)
async def set_consumption_limit(
    conn: Conn,
    site_id: UUID,
    payload: ConsumptionLimitUpdate,
    principal: Annotated[Principal, Depends(require_role("consumer"))],
) -> ConsumptionLimit:
    """Set or replace the monthly limit.

    PUT, not POST: there is exactly one limit per site and setting it again
    replaces it, so the call is idempotent and the URL names the thing.

    This is one of the few rows in the schema that is deliberately mutable. Rule
    1 makes money immutable because a bill must stay correct forever; a limit is
    a preference about future warnings, nothing was charged against it, and a
    version history of somebody adjusting a number in a settings box would be
    storage with no reader.
    """
    await visible_site_or_404(conn, principal, site_id)
    async with conn.transaction():
        await conn.fetchval(
            sql("set_consumption_limit"), site_id,
            payload.monthly_kwh, payload.notify_at_pct, principal.account_id,
        )
        row = await conn.fetchrow(sql("get_consumption_limit"), site_id)
    return _limit(site_id, row)


@router.delete("/api/sites/{site_id}/consumption-limit", status_code=204)
async def clear_consumption_limit(
    conn: Conn,
    site_id: UUID,
    principal: Annotated[Principal, Depends(require_role("consumer"))],
) -> None:
    """Turn the warning off.

    Deleting the row, not setting the limit absurdly high: the sweep's WHERE
    clause is what decides who gets told, and a household with no row is simply
    not considered. Answers 204 whether or not a row was there -- DELETE is
    idempotent, and reporting "there was nothing to delete" as a 404 would make
    a second click look like a failure.
    """
    await visible_site_or_404(conn, principal, site_id)
    await conn.execute(sql("clear_consumption_limit"), site_id)
