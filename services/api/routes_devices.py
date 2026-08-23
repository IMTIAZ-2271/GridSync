"""Devices: telemetry health for the equipment on one site.

The read side of a question the portal could not previously ask. Every other
router here answers "what did this site consume, owe, or report"; this one
answers "is the equipment that produces those numbers actually reporting".

`health` is computed in SQL from interval coverage rather than read off
`device.status`, because `status` is a manual flag and `last_seen_at` is only
stamped by backfill_readings() -- neither is a heartbeat today. See the header
of db/sql/dao/device_queries.sql for the window and the thresholds.

Scoped like every other site-keyed endpoint: `require_role` decides who may
call it, `visible_site_or_404` decides whether this caller may see this site.
Worker is included because a dispatched field engineer needs exactly this
screen, and `visible_site_or_404` already narrows them to the sites their
assignments cover.
"""
from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from .auth import Principal, require_role, visible_site_or_404
from .db import Conn
from .queries import sql
from .types import Energy

router = APIRouter(tags=["devices"])


# --------------------------------------------------------------------------
# Models
# --------------------------------------------------------------------------

DeviceHealth = Literal["healthy", "degraded", "silent", "no_data", "faulty", "unknown"]


class SiteDevice(BaseModel):
    """One telemetry-reporting device, with its coverage over the window."""

    device_id: UUID
    device_type: Literal["meter", "inverter"]
    serial_no: str
    manufacturer: str | None
    model: str | None
    firmware_version: str | None
    interval_minutes: int
    installed_at: datetime
    status: Literal["active", "faulty", "removed"]

    # Meter only. Rule 7: exactly one device per site is the 'billing' meter,
    # and it is the one whose silence costs the customer money.
    billing_role: Literal["billing", "generation_only", "check_meter"] | None
    meter_flow: Literal["unidirectional", "bidirectional"] | None

    # Inverter only.
    ac_capacity_kw: Energy | None
    array_count: int
    dc_capacity_kw: Energy | None
    array_status: str | None

    # Coverage. The window is the last 7 whole Asia/Dhaka days, clipped at
    # installed_at, and deliberately excludes today -- see the DAO.
    window_from: datetime
    window_to: datetime
    last_reading_at: datetime | None
    intervals_expected: int
    intervals_received: int
    intervals_suspect: int
    coverage_pct: Energy | None

    health: DeviceHealth


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------

@router.get("/api/sites/{site_id}/devices", response_model=list[SiteDevice])
async def site_devices(
    conn: Conn,
    site_id: UUID,
    principal: Annotated[
        Principal,
        Depends(require_role("consumer", "worker", "government", "supplier", "admin")),
    ],
) -> list[SiteDevice]:
    """The site's reporting devices, billing meter first."""
    await visible_site_or_404(conn, principal, site_id)
    rows = await conn.fetch(sql("devices_for_site"), site_id)
    return [SiteDevice(**dict(r)) for r in rows]
