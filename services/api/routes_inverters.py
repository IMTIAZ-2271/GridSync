"""Inverters: the household's generating hardware, and whether any of it
produces enough to justify a net-metering application.

An inverter is not a meter and can never become one. It reports
`generation_kwh` and nothing else (rule 6) -- the import/export split is
knowable only at the bidirectional meter on the grid boundary. Nothing in this
module, and nothing anywhere else in the API, converts a meter into a solar
connection or a solar connection into a meter.

Since migration d4f8a2c61e95 an inverter is installed against a *site* and
names its own billing point, which stays NULL until net metering is granted.
That is what lets panels exist before a meter does, and what makes the
net-metering application a choice of two things -- an inverter, then a meter --
rather than one implying the other.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel, computed_field

from .auth import Principal, require_role, visible_site_or_404
from .db import Conn
from .queries import sql
from .types import Energy

router = APIRouter(tags=["inverters"])


# --------------------------------------------------------------------------
# The eligibility policy
#
# These four numbers are the whole of it, and they live here -- named, in one
# place -- rather than as literals inside the SQL. `inverters_for_account`
# takes them as parameters, so a test can drive the boundary from either side
# without editing a query, and so changing the policy is a one-line change in
# a file a reader would think to open.
# --------------------------------------------------------------------------

#: Generation must beat the household's own daily consumption by this factor.
#: An array that merely breaks even exports almost nothing, so the agreement
#: would earn no credit and the application would have been pointless.
SURPLUS_RATIO = Decimal("1.2")

#: kWh per kW of installed AC capacity per day that a healthy array yields in
#: Dhaka. Used only to judge whether an inverter is performing to its own
#: rating -- never to *estimate* production, which is always measured.
PEAK_SUN_HOURS = Decimal("4.5")

#: Fraction of that yield below which an array is treated as faulty rather
#: than merely small. A shaded, soiled or partly-failed array still reports;
#: it just reports less, and raw kWh cannot tell you that because a big array
#: always beats a small one.
PERFORMANCE_FLOOR = Decimal("0.65")

#: Days of readings -- from the inverter AND from the site's billing meter --
#: before a verdict is possible at all. Three sunny days would pass a system
#: that fails over a month, which is the failure this gate exists to catch.
MIN_OBSERVED_DAYS = 14


# --------------------------------------------------------------------------
# Models
# --------------------------------------------------------------------------

class Inverter(BaseModel):
    """One inverter, with the measurements behind its verdict.

    Every figure the verdict rests on is returned alongside it. A refusal that
    says only "not eligible" is one a household cannot act on; one that says
    "produces 12.4, needs 15.9" tells them whether to add panels or look at a
    shading problem.
    """

    device_id: UUID
    serial_no: str
    site_id: UUID
    site_label: str
    manufacturer: str | None
    model: str | None
    installed_at: datetime
    last_seen_at: datetime | None
    status: str
    ac_capacity_kw: Energy
    array_count: int

    #: The connection these panels generate behind, or None while they belong
    #: to no connection. None is the ordinary state of a fresh installation --
    #: it becomes a connection when net metering is granted, not before.
    billing_point_id: UUID | None

    # --- what was measured -------------------------------------------------
    #: Distinct days with readings, over the last 30 whole Asia/Dhaka days.
    gen_days: int
    meter_days: int
    generation_kwh: Energy
    #: None when there is nothing to average yet, rather than zero: "no data"
    #: and "produced nothing" are different facts.
    generation_daily_kwh: Energy | None
    consumption_daily_kwh: Energy | None

    # --- what was required -------------------------------------------------
    expected_daily_kwh: Energy
    required_daily_kwh: Energy | None
    performance_floor_kwh: Energy

    # --- the verdict, in three parts ---------------------------------------
    has_enough_history: bool
    meets_demand: bool | None
    meets_performance: bool | None
    eligible: bool

    @computed_field
    @property
    def blocking_reason(self) -> str | None:
        """Why this inverter cannot carry an application, in one sentence.

        A computed_field, not a bare property, so it is SERIALIZED. The page
        renders this string and the 409 from POST /net-metering-applications
        carries the same one, which is what stops the form and the enforcement
        explaining a refusal in two different sets of words.
        """
        return blocking_reason(self)


class SwappableMeter(BaseModel):
    """A billing meter that could be exchanged for a bidirectional one."""

    billing_point_id: UUID
    point_label: str
    point_reference: str | None
    meter_device_id: UUID
    meter_serial: str
    installed_at: datetime
    last_seen_at: datetime | None


def blocking_reason(inv: Inverter) -> str | None:
    """The one sentence a household needs, or None if nothing is blocking.

    Ordered by what they can do about it. "Wait" comes before "your array is
    underperforming", because until there is enough history the performance
    figure is not trustworthy enough to accuse anyone with.
    """
    if inv.eligible:
        return None
    if not inv.has_enough_history:
        short = max(MIN_OBSERVED_DAYS - inv.gen_days, MIN_OBSERVED_DAYS - inv.meter_days)
        if inv.gen_days == 0:
            return (
                f"{inv.serial_no} has not reported any generation yet. Net "
                f"metering needs {MIN_OBSERVED_DAYS} days of readings."
            )
        return (
            f"Needs {short} more day(s) of readings before it can be assessed "
            f"({MIN_OBSERVED_DAYS} required)."
        )
    if inv.meets_demand is False:
        return (
            f"Produces {inv.generation_daily_kwh} kWh a day; this property "
            f"needs {inv.required_daily_kwh} kWh a day to qualify "
            f"(its own use of {inv.consumption_daily_kwh} kWh plus the "
            f"{int((SURPLUS_RATIO - 1) * 100)}% surplus net metering requires)."
        )
    if inv.meets_performance is False:
        return (
            f"Produces {inv.generation_daily_kwh} kWh a day, below the "
            f"{inv.performance_floor_kwh} kWh expected of a {inv.ac_capacity_kw} kW "
            "array in working order. Check for shading, soiling or a failed string."
        )
    return "Not eligible for net metering."


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------

@router.get("/api/inverters", response_model=list[Inverter])
async def my_inverters(
    conn: Conn,
    principal: Annotated[Principal, Depends(require_role("consumer"))],
) -> list[Inverter]:
    """Every inverter this household owns, each with its net-metering verdict.

    This is the first dropdown of the net-metering application. It returns
    ineligible inverters too, carrying the reason -- a dropdown that silently
    omits the household's only inverter is indistinguishable from a broken
    page, and the reason is the actionable half.
    """
    rows = await conn.fetch(
        sql("inverters_for_account"),
        principal.account_id,
        SURPLUS_RATIO, PEAK_SUN_HOURS, PERFORMANCE_FLOOR, MIN_OBSERVED_DAYS,
    )
    return [Inverter(**dict(r)) for r in rows]


@router.get(
    "/api/sites/{site_id}/swappable-meters",
    response_model=list[SwappableMeter],
)
async def swappable_meters(
    conn: Conn,
    site_id: UUID,
    principal: Annotated[Principal, Depends(require_role("consumer"))],
) -> list[SwappableMeter]:
    """The second dropdown: which normal meter to exchange.

    Only unidirectional billing meters on connections with no live agreement.
    A household is never offered a choice the application endpoint would then
    refuse.
    """
    await visible_site_or_404(conn, principal, site_id)
    rows = await conn.fetch(sql("swappable_meters_for_site"), site_id)
    return [SwappableMeter(**dict(r)) for r in rows]
