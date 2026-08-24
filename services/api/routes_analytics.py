"""Fleet-wide rollups for government and supplier."""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from .auth import Principal, require_role
from .db import Conn
from .queries import sql
from .types import Energy

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
