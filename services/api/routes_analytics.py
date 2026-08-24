"""Fleet-wide rollups for government and supplier."""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from .auth import Principal, require_role
from .db import Conn
from .queries import sql
from .types import Energy, Money, Rate

router = APIRouter(tags=["analytics"])


class AreaStats(BaseModel):
    district: str
    site_count: int
    solar_site_count: int
    total_import_kwh: Energy
    total_export_kwh: Energy
    total_generation_kwh: Energy


@router.get("/api/analytics/by-area", response_model=list[AreaStats])
async def analytics_by_area(
    conn: Conn,
    _: Annotated[
        Principal, Depends(require_role("government", "supplier", "admin"))
    ],
    district: str | None = None,
) -> list[AreaStats]:
    """District rollup, optionally narrowed to one district.

    Government requirements 2 and 4 are this one endpoint asking different
    questions -- "within their own region" and "total overall" -- so the scope
    is a parameter the caller chooses rather than a filter forced on the role.
    Hard-scoping it to the official's own district would satisfy 2 by breaking
    4, and an official who cannot see the national picture cannot tell whether
    their district is doing well or badly.

    The official's own district is already on `/api/auth/me`, so the client
    knows which value to pass without another request.
    """
    rows = await conn.fetch(sql("analytics_by_area"), district)
    return [AreaStats(**dict(r)) for r in rows]


class NetMeteringOutcome(BaseModel):
    """What the credit scheme did in one district.

    Balance is not earned-minus-applied: it is the sum of each connection's
    latest running balance, which is what the next bill can actually spend. The
    two agree today and diverge the moment an `expired` or `adjustment` entry is
    written — and when they diverge, the running balance is the true one.
    """

    district: str
    site_count: int
    sites_in_credit: int
    earned_kwh: Energy
    earned_amount: Money
    applied_kwh: Energy
    applied_amount: Money
    balance_kwh: Energy
    balance_amount: Money
    # Of everything ever earned, the share spent against a bill. The figure the
    # scheme is judged on: credit nobody can use is a policy that looks
    # generous and is not. NULL where nothing has been earned yet.
    applied_pct: Rate | None


class AgreementSummary(BaseModel):
    status: str
    agreement_count: int
    sanctioned_capacity_kw: Energy


class NetMeteringReport(BaseModel):
    by_area: list[NetMeteringOutcome]
    agreements: list[AgreementSummary]


@router.get("/api/analytics/net-metering", response_model=NetMeteringReport)
async def net_metering_outcomes(
    conn: Conn,
    _: Annotated[
        Principal, Depends(require_role("government", "supplier", "admin"))
    ],
    district: str | None = None,
) -> NetMeteringReport:
    """Government requirement 5: what net metering actually produced.

    The energy rollup shows power flowing both ways; this shows the consequence
    — credit earned for exporting, credit spent against a bill, and the balance
    rolling forward. It reads `credit_ledger` rather than recomputing from
    readings: the ledger is append-only and carries a running balance on every
    entry, so it *is* the record, and deriving these a second way would only
    create an opportunity to disagree with the bills.

    Scoped the same way as `by-area`, so the regulator's own-region toggle
    behaves identically on both pages.

    Districts with no net-metering agreement at all are omitted. They are not
    hidden — they appear, correctly, on the energy rollup — but a page about
    the credit scheme listing places the scheme has never reached is noise.
    """
    return NetMeteringReport(
        by_area=[
            NetMeteringOutcome(**dict(r))
            for r in await conn.fetch(sql("net_metering_outcomes_by_area"), district)
        ],
        agreements=[
            AgreementSummary(**dict(r))
            for r in await conn.fetch(sql("net_metering_agreement_summary"), district)
        ],
    )
