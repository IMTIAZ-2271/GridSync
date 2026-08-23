"""device_reading: partition routing, rule 4 idempotency, and the CHECKs.

Migration 0f6109903981. These cover the parts of the telemetry schema that are
easy to break silently -- a reading landing in the wrong partition, or a
duplicate retry being accepted instead of refused.
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


async def partition_of(conn, device_id, interval_start) -> str:
    """Which partition the row actually landed in."""
    return await conn.fetchval(
        "SELECT tableoid::regclass::text FROM device_reading "
        "WHERE device_id = $1 AND interval_start = $2",
        device_id, interval_start,
    )


@pytest.fixture
async def meter_and_batch(conn):
    site = await make_site(conn)
    device = await make_meter(conn, site, billing_role="billing")
    batch = await make_ingest_batch(conn, device)
    return device, batch


# --------------------------------------------------------------------------
# Partition routing
# --------------------------------------------------------------------------
@pytest.mark.parametrize("moment, expected", [
    (datetime(2026, 8, 1, 0, 0, tzinfo=DHAKA), "device_reading_2026_08"),
    (datetime(2026, 8, 31, 23, 30, tzinfo=DHAKA), "device_reading_2026_08"),
    (datetime(2026, 9, 1, 0, 0, tzinfo=DHAKA), "device_reading_2026_09"),
    (datetime(2026, 6, 15, 12, 0, tzinfo=DHAKA), "device_reading_2026_06"),
])
async def test_reading_routes_to_its_month(meter_and_batch, conn, moment,
                                           expected):
    device, batch = meter_and_batch
    await add_reading(conn, device, batch, moment)

    assert await partition_of(conn, device, moment) == expected


async def test_month_boundary_is_dhaka_midnight_not_utc(meter_and_batch, conn):
    """The last half-hour of a Dhaka month must not fall into the next one.

    18:00 UTC on 31 Aug is 00:00 on 1 Sep in Dhaka. A UTC-bounded partition set
    would put the 23:30 Dhaka reading in September.
    """
    device, batch = meter_and_batch
    last = datetime(2026, 8, 31, 23, 30, tzinfo=DHAKA)
    first = datetime(2026, 9, 1, 0, 0, tzinfo=DHAKA)

    await add_reading(conn, device, batch, last)
    await add_reading(conn, device, batch, first)

    assert await partition_of(conn, device, last) == "device_reading_2026_08"
    assert await partition_of(conn, device, first) == "device_reading_2026_09"


async def test_out_of_range_reading_lands_in_default_not_rejected(
    meter_and_batch, conn
):
    """Rule: a clock-skewed device must not fail the insert outright."""
    device, batch = meter_and_batch
    skewed = datetime(2035, 1, 1, 0, 0, tzinfo=DHAKA)

    await add_reading(conn, device, batch, skewed)

    assert await partition_of(conn, device, skewed) == "device_reading_default"


# --------------------------------------------------------------------------
# Rule 4: idempotency by constraint
# --------------------------------------------------------------------------
async def test_duplicate_reading_is_refused_by_the_primary_key(
    meter_and_batch, conn, savepoint
):
    device, batch = meter_and_batch
    moment = datetime(2026, 8, 10, 6, 30, tzinfo=DHAKA)
    await add_reading(conn, device, batch, moment)

    with pytest.raises(asyncpg.exceptions.UniqueViolationError):
        async with savepoint():
            await add_reading(conn, device, batch, moment,
                              import_kwh=Decimal("99.0000"))

    # The retry left the original untouched -- no upsert, no clobber.
    assert await conn.fetchval(
        "SELECT import_kwh FROM device_reading "
        "WHERE device_id = $1 AND interval_start = $2",
        device, moment,
    ) == Decimal("1.0000")


async def test_same_interval_on_a_different_device_is_fine(conn):
    site = await make_site(conn)
    meter = await make_meter(conn, site, billing_role="billing")
    inverter = await make_inverter(conn, site)
    moment = datetime(2026, 8, 10, 6, 30, tzinfo=DHAKA)

    await add_reading(conn, meter, await make_ingest_batch(conn, meter), moment)
    await add_reading(conn, inverter,
                      await make_ingest_batch(conn, inverter), moment,
                      import_kwh=None, export_kwh=None,
                      generation_kwh=Decimal("2.5000"))

    # Scoped to the two devices this test created. An unscoped COUNT(*) on the
    # interval passes only on an empty database: rule 4's uniqueness is per
    # (device_id, interval_start), so any other device that happens to hold
    # 06:30 on 2026-08-10 -- a backfill, a seed run -- is a legitimate row that
    # would inflate the count and fail a test about something else entirely.
    assert await conn.fetchval(
        "SELECT count(*) FROM device_reading "
        "WHERE interval_start = $1 AND device_id = ANY($2::uuid[])",
        moment, [meter, inverter],
    ) == 2


# --------------------------------------------------------------------------
# CHECK constraints
# --------------------------------------------------------------------------
async def test_misaligned_interval_start_is_refused(meter_and_batch, conn):
    device, batch = meter_and_batch
    # 06:07 is not on a 30-minute boundary.
    misaligned = datetime(2026, 8, 10, 6, 7, tzinfo=DHAKA)

    with pytest.raises(asyncpg.exceptions.CheckViolationError) as caught:
        await add_reading(conn, device, batch, misaligned)

    assert caught.value.constraint_name == "reading_aligned"


@pytest.mark.parametrize("minutes, moment", [
    (15, datetime(2026, 8, 10, 6, 45, tzinfo=DHAKA)),
    (30, datetime(2026, 8, 10, 6, 30, tzinfo=DHAKA)),
    (60, datetime(2026, 8, 10, 6, 0, tzinfo=DHAKA)),
])
async def test_aligned_intervals_are_accepted(meter_and_batch, conn, minutes,
                                              moment):
    device, batch = meter_and_batch
    await add_reading(conn, device, batch, moment, interval_minutes=minutes)

    assert await partition_of(conn, device, moment) == "device_reading_2026_08"


async def test_unsupported_interval_length_is_refused(meter_and_batch, conn):
    device, batch = meter_and_batch

    with pytest.raises(asyncpg.exceptions.CheckViolationError) as caught:
        await add_reading(conn, device, batch,
                          datetime(2026, 8, 10, 6, 5, tzinfo=DHAKA),
                          interval_minutes=5)

    assert caught.value.constraint_name == "reading_interval_valid"


async def test_negative_energy_is_refused(meter_and_batch, conn):
    device, batch = meter_and_batch

    with pytest.raises(asyncpg.exceptions.CheckViolationError) as caught:
        await add_reading(conn, device, batch,
                          datetime(2026, 8, 10, 6, 30, tzinfo=DHAKA),
                          import_kwh=Decimal("-1.0000"))

    assert caught.value.constraint_name == "reading_non_negative"


async def test_reading_with_no_measurement_family_is_refused(
    meter_and_batch, conn
):
    """reading_role_guard gets there before reading_shape, on purpose.

    Both would reject this. The BEFORE trigger runs ahead of the table CHECKs
    and can say which family *this* device owes, so it is the one that fires.
    """
    device, batch = meter_and_batch

    with pytest.raises(asyncpg.exceptions.CheckViolationError) as caught:
        await add_reading(conn, device, batch,
                          datetime(2026, 8, 10, 6, 30, tzinfo=DHAKA),
                          import_kwh=None, export_kwh=None,
                          generation_kwh=None)

    assert "import_kwh required" in str(caught.value)


async def test_generation_only_reading_is_accepted(conn):
    """An inverter sends generation with no import/export half."""
    site = await make_site(conn)
    await make_meter(conn, site, billing_role="billing")
    inverter = await make_inverter(conn, site)
    batch = await make_ingest_batch(conn, inverter)
    moment = datetime(2026, 8, 10, 6, 30, tzinfo=DHAKA)

    await add_reading(conn, inverter, batch, moment,
                      import_kwh=None, export_kwh=None,
                      generation_kwh=Decimal("2.5000"))

    assert await partition_of(conn, inverter, moment) == "device_reading_2026_08"


# --------------------------------------------------------------------------
# ingest_batch
# --------------------------------------------------------------------------
async def test_idempotency_key_is_unique(conn, savepoint):
    site = await make_site(conn)
    device = await make_meter(conn, site, billing_role="billing")
    await make_ingest_batch(conn, device, idempotency_key="repeated-key")

    with pytest.raises(asyncpg.exceptions.UniqueViolationError):
        async with savepoint():
            await make_ingest_batch(conn, device,
                                    idempotency_key="repeated-key")


async def test_outcome_counts_may_not_exceed_the_batch_total(conn):
    site = await make_site(conn)
    device = await make_meter(conn, site, billing_role="billing")
    batch = await make_ingest_batch(conn, device, reading_count=10)

    # Partial progress is allowed; overshooting the total is not.
    await conn.execute(
        "UPDATE ingest_batch SET accepted_count = 6 WHERE batch_id = $1", batch
    )
    with pytest.raises(asyncpg.exceptions.CheckViolationError) as caught:
        await conn.execute(
            "UPDATE ingest_batch SET rejected_count = 7 WHERE batch_id = $1",
            batch,
        )

    assert caught.value.constraint_name == "batch_counts_within_total"


# --------------------------------------------------------------------------
# Partition management
# --------------------------------------------------------------------------
async def test_create_reading_partition_is_idempotent(conn):
    first = await conn.fetchval(
        "SELECT create_reading_partition(DATE '2026-08-14')"
    )
    second = await conn.fetchval(
        "SELECT create_reading_partition(DATE '2026-08-01')"
    )

    assert first == second == "device_reading_2026_08"


async def test_partition_bounds_ignore_the_session_timezone(conn):
    """The same month must produce the same instant from any session zone.

    Bounds are rendered in the session's zone, so compare instants, not text.
    """
    bounds = {}
    for zone in ("UTC", "America/New_York", "Asia/Dhaka"):
        await conn.execute(f"SET TimeZone = '{zone}'")
        name = await conn.fetchval(
            "SELECT create_reading_partition(DATE '2029-03-10')"
        )
        bounds[zone] = await conn.fetchval(
            """
            SELECT (regexp_match(pg_get_expr(relpartbound, oid),
                                 'FROM \\(''([^'']+)''\\)'))[1]::timestamptz
            FROM pg_class WHERE relname = $1
            """,
            name,
        )

    assert len(set(bounds.values())) == 1, bounds
    assert bounds["Asia/Dhaka"] == datetime(2029, 3, 1, 0, 0, tzinfo=DHAKA)
