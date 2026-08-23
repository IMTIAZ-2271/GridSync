"""Organisations: districts, distribution companies, supplier companies.

Three things live here because more than one router needs them and none of
them belongs to a single resource:

* **National ID normalization.** Consumer requirement 1 and worker
  requirement 1 both collect one, and `account.national_id` is UNIQUE — so
  two people writing the same number with different spacing must collide, not
  co-exist.
* **District resolution.** `district` became a real table in migration
  `e7c4b19a2d83`; before that it was free text canonicalised by a dict in
  `routes_sites.py`, which had already leaked `Dhaka`, `dhaka` and `g` into
  the regulator's rollup. The list now comes from the database, so the form,
  the validator and the foreign key cannot disagree.
* **The reference endpoints** every registration and issue-filing form reads
  its dropdowns from.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Annotated
from uuid import UUID

import asyncpg
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from .auth import CurrentAccount
from .db import Conn
from .queries import sql

router = APIRouter(tags=["organisations"])


# --------------------------------------------------------------------------
# National ID
# --------------------------------------------------------------------------

# Bangladesh NIDs come in three lengths: the 10-digit smart card, the older
# 13-digit form, and the 17-digit form that carries the birth year. Anything
# else is a typo, and accepting it would put an unusable number on an account
# that can never be corrected without an admin.
_VALID_NID_LENGTHS = (10, 13, 17)

NATIONAL_ID_HELP = (
    "a National ID is 10, 13 or 17 digits"
)


def normalized_national_id(raw: str) -> str:
    """Digits only, or 422.

    Spaces and dashes are stripped rather than rejected — people write their
    NID both ways and neither is wrong — but the stored form is canonical, so
    the UNIQUE constraint on account.national_id actually catches a second
    registration by the same person.
    """
    digits = "".join(ch for ch in raw if ch.isdigit())
    if len(digits) not in _VALID_NID_LENGTHS:
        raise HTTPException(status_code=422, detail=NATIONAL_ID_HELP)
    return digits


# --------------------------------------------------------------------------
# Districts
# --------------------------------------------------------------------------

async def resolve_district(
    conn: asyncpg.Connection, district: str
) -> tuple[str, Decimal, Decimal]:
    """Canonical name plus centroid, or 422.

    Rejecting an unknown district rather than defaulting it is the point. The
    original fallback accepted anything and quietly assigned a centroid, so a
    typo — or the city name typed into the district field — became a real
    coordinate that the simulator's solar geometry would later read as fact.

    Non-selectable districts (the legacy free-text values the migration
    preserved) are refused here too: they stay joinable so the government
    rollup keeps reporting them honestly, but nothing new may be filed under
    one.
    """
    row = await conn.fetchrow(sql("district_centroid"), district)
    if row is None:
        names = [r["name"] for r in await conn.fetch(sql("list_districts"))]
        raise HTTPException(
            status_code=422,
            detail=(
                f"unknown district '{district.strip()}'. "
                "Expected one of: " + ", ".join(names)
            ),
        )
    return row["name"], row["latitude"], row["longitude"]


# --------------------------------------------------------------------------
# Models
# --------------------------------------------------------------------------

class District(BaseModel):
    name: str
    latitude: Decimal
    longitude: Decimal


class DistributionCompany(BaseModel):
    company_id: UUID
    code: str
    name: str
    contact_email: str | None
    contact_phone: str | None
    districts: list[str]


class SupplierCompany(BaseModel):
    supplier_id: UUID
    code: str
    name: str
    license_no: str | None
    contact_email: str | None
    contact_phone: str | None
    districts: list[str]
    # Null, not zero, when nobody has rated them yet. "Unrated" and "rated
    # badly" must not look the same in a dropdown that sorts by this
    # (supplier requirement 4).
    rating_avg: Decimal | None
    rating_count: int


DistrictFilter = Annotated[
    str | None,
    Query(description="Only organisations serving this district."),
]


# --------------------------------------------------------------------------
# Endpoints
#
# Reference data: which districts exist, which utilities serve them, which
# installers cover which area. Nothing here is scoped to a site, so there is
# no row-level question to ask — only whether the caller must be signed in.
#
# **The first two are deliberately unauthenticated.** The registration form
# needs them before anyone has a token: a worker picks their region and, if
# they are a government worker, their employer, and neither dropdown can be
# populated by a request that requires the account being created. Gating them
# would have left that dropdown permanently empty and government-worker
# registration impossible from the UI.
#
# What they expose is the list of districts and the public fact of which
# utility serves each — printed on every electricity bill in the city. No
# account, site or meter is reachable through either.
#
# /api/suppliers stays authenticated: it carries ratings and contact details,
# it is only wanted after sign-in (applying for solar, filing an issue against
# an installer), and an open supplier directory with scores attached is a
# scraping target with no offsetting reason to be public.
# --------------------------------------------------------------------------

@router.get("/api/districts", response_model=list[District])
async def list_districts(conn: Conn) -> list[District]:
    """The districts a site, worker or official may be filed under.

    Served rather than hardcoded in the client so the registration form, the
    onboarding wizard, `resolve_district` and the foreign key cannot disagree
    about the list.
    """
    return [District(**dict(r)) for r in await conn.fetch(sql("list_districts"))]


@router.get(
    "/api/distribution-companies", response_model=list[DistributionCompany]
)
async def list_distribution_companies(
    conn: Conn, district: DistrictFilter = None
) -> list[DistributionCompany]:
    """The utilities that own billing meters.

    Consumer requirement 6: filing a meter fault asks which distribution
    company handles that meter. Filtering by district usually narrows it to
    one — but not always, and Badda is the case in point, which is why this is
    a choice and not a lookup.
    """
    rows = await conn.fetch(sql("list_distribution_companies"), district)
    return [DistributionCompany(**dict(r)) for r in rows]


@router.get("/api/suppliers", response_model=list[SupplierCompany])
async def list_suppliers(
    conn: Conn, _: CurrentAccount, district: DistrictFilter = None
) -> list[SupplierCompany]:
    """The solar installers, with the rating they have earned so far.

    Consumer requirement 7 asks for suppliers "in the consumer's nearby
    region", which is the `district` filter; supplier requirement 4 sorts by
    consumer rating, which is `rating_avg`. Ordering is left to the caller —
    the regulator's and the household's reasons for ranking these differ.
    """
    rows = await conn.fetch(sql("list_supplier_companies"), district)
    return [SupplierCompany(**dict(r)) for r in rows]
