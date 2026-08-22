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
) -> list[AreaStats]:
    rows = await conn.fetch(sql("analytics_by_area"))
    return [AreaStats(**dict(r)) for r in rows]
