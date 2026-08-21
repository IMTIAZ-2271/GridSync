"""Rule 6: a device may only report what it can actually measure.

Enforced by `reading_role_guard` / `assert_reading_matches_device_role()`
(migration 0f6109903981).

The dividing line is deliberate and easy to get backwards: **meter_flow**
decides whether export may be reported, **billing_role** decides only whether
a device is on the generation side. A `check_meter` sits at the grid boundary
exactly like the billing meter and reports identically -- billing_role governs
whether a reading counts toward a bill, not whether it may exist.
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import asyncpg
import pytest

from tests.factories import (
    add_reading,
    make_ingest_batch,
    make_inverter,
    make_meter,
    make_site,
)

DHAKA = timezone(timedelta(hours=6))
MOMENT = datetime(2026, 8, 10, 6, 30, tzinfo=DHAKA)

KWH = Decimal("1.0000")
ZERO = Decimal("0.0000")


@pytest.fixture
async def site(conn):
    return await make_site(conn)


async def reading(conn, device, **kwargs):
    await add_reading(conn, device, await make_ingest_batch(conn, device),
                      MOMENT, **kwargs)


async def stored(conn, device, column):
    return await conn.fetchval(
        f"SELECT {column} FROM device_reading WHERE device_id = $1", device
    )


# --------------------------------------------------------------------------
# Grid-boundary meters: billing and check_meter behave identically
# --------------------------------------------------------------------------
@pytest.mark.parametrize("role", ["billing", "check_meter"])
async def test_bidirectional_meter_reports_import_and_export(conn, site, role):
    device = await make_meter(conn, site, billing_role=role,
                              meter_flow="bidirectional")

    await reading(conn, device, import_kwh=KWH, export_kwh=Decimal("0.5000"))

    assert await stored(conn, device, "export_kwh") == Decimal("0.5000")


async def test_check_meter_is_not_restricted_by_its_billing_role(conn, site):
    """The whole point of a check meter: it measures, it just isn't billed on.

    A guard keyed on billing_role instead of meter_flow would reject this and
    leave check_meter devices unable to report anything at all.
    """
    await make_meter(conn, site, billing_role="billing")
    check = await make_meter(conn, site, billing_role="check_meter",
                             meter_flow="bidirectional")

    await reading(conn, check, import_kwh=KWH, export_kwh=KWH)

    assert await stored(conn, check, "import_kwh") == KWH


@pytest.mark.parametrize("role", ["billing", "check_meter"])
async def test_bidirectional_meter_must_send_export(conn, site, role):
    device = await make_meter(conn, site, billing_role=role,
                              meter_flow="bidirectional")

    with pytest.raises(asyncpg.exceptions.CheckViolationError) as caught:
        await reading(conn, device, import_kwh=KWH, export_kwh=None)

    assert "export_kwh required" in str(caught.value)


async def test_grid_meter_may_not_report_generation(conn, site):
    device = await make_meter(conn, site, billing_role="billing")

    with pytest.raises(asyncpg.exceptions.CheckViolationError) as caught:
        await reading(conn, device, import_kwh=KWH, export_kwh=ZERO,
                      generation_kwh=Decimal("2.0000"))

    assert "generation_kwh must be NULL" in str(caught.value)


# --------------------------------------------------------------------------
# Unidirectional meters: import only, export cannot be non-zero
# --------------------------------------------------------------------------
async def test_unidirectional_meter_reports_import_only(conn, site):
    device = await make_meter(conn, site, billing_role="billing",
                              meter_flow="unidirectional")

    await reading(conn, device, import_kwh=KWH, export_kwh=ZERO)

    assert await stored(conn, device, "import_kwh") == KWH


async def test_unidirectional_null_export_is_normalized_to_zero(conn, site):
    """'0 or NULL' is accepted, and stored as 0.

    For a unidirectional meter zero export is a measured fact, not a missing
    value. reading_shape also requires both halves of the import/export pair,
    so normalizing here is what makes NULL acceptable at all.
    """
    device = await make_meter(conn, site, billing_role="billing",
                              meter_flow="unidirectional")

    await reading(conn, device, import_kwh=KWH, export_kwh=None)

    assert await stored(conn, device, "export_kwh") == ZERO


async def test_unidirectional_meter_cannot_export(conn, site):
    device = await make_meter(conn, site, billing_role="billing",
                              meter_flow="unidirectional")

    with pytest.raises(asyncpg.exceptions.CheckViolationError) as caught:
        await reading(conn, device, import_kwh=KWH, export_kwh=Decimal("0.5000"))

    assert "unidirectional and cannot export" in str(caught.value)


# --------------------------------------------------------------------------
# Generation side: inverters and generation_only meters
# --------------------------------------------------------------------------
async def test_inverter_reports_generation(conn, site):
    device = await make_inverter(conn, site)

    await reading(conn, device, import_kwh=None, export_kwh=None,
                  generation_kwh=Decimal("2.5000"))

    assert await stored(conn, device, "generation_kwh") == Decimal("2.5000")


async def test_generation_only_meter_reports_generation(conn, site):
    await make_meter(conn, site, billing_role="billing")
    device = await make_meter(conn, site, billing_role="generation_only",
                              meter_flow="unidirectional")

    await reading(conn, device, import_kwh=None, export_kwh=None,
                  generation_kwh=Decimal("2.5000"))

    assert await stored(conn, device, "generation_kwh") == Decimal("2.5000")


@pytest.mark.parametrize("maker", ["inverter", "generation_only"])
async def test_generation_side_may_not_report_import_or_export(conn, site,
                                                               maker):
    """Rule 6's core claim: only the grid meter knows the import/export split."""
    if maker == "inverter":
        device = await make_inverter(conn, site)
    else:
        await make_meter(conn, site, billing_role="billing")
        device = await make_meter(conn, site, billing_role="generation_only",
                                  meter_flow="unidirectional")

    with pytest.raises(asyncpg.exceptions.CheckViolationError) as caught:
        await reading(conn, device, import_kwh=KWH, export_kwh=ZERO,
                      generation_kwh=Decimal("2.5000"))

    assert "import_kwh and export_kwh must be NULL" in str(caught.value)


async def test_generation_side_must_send_generation(conn, site):
    device = await make_inverter(conn, site)

    with pytest.raises(asyncpg.exceptions.CheckViolationError) as caught:
        await reading(conn, device, import_kwh=None, export_kwh=None,
                      generation_kwh=None)

    assert "generation_kwh required" in str(caught.value)


# --------------------------------------------------------------------------
# Incomplete hardware
# --------------------------------------------------------------------------
async def test_meter_without_a_meter_spec_row_cannot_report(conn, site):
    """Subtype completeness is not declaratively enforceable, so it is caught
    here rather than producing a confusing NULL-flow comparison."""
    device = await conn.fetchval(
        """
        INSERT INTO device (site_id, device_type, serial_no, device_key_hash)
        VALUES ($1, 'meter', 'ORPHAN-METER', 'x') RETURNING device_id
        """,
        site,
    )

    with pytest.raises(asyncpg.exceptions.CheckViolationError) as caught:
        await reading(conn, device, import_kwh=KWH, export_kwh=ZERO)

    assert "no meter_spec row" in str(caught.value)
