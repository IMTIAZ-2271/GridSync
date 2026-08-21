"""Minimal row builders for schema tests.

Only what a table's NOT NULL columns actually demand -- these exist so a test
about one constraint is not buried under twenty columns of irrelevant setup.
Every value is deterministic; nothing here is random.
"""
import itertools
from datetime import date
from decimal import Decimal

import asyncpg

_counter = itertools.count(1)


def unique_suffix() -> str:
    """Distinct within a transaction, which is as far as any of it survives."""
    return f"{next(_counter):06d}"


async def make_account(conn: asyncpg.Connection, **overrides) -> str:
    tag = overrides.pop("tag", unique_suffix())
    return await conn.fetchval(
        """
        INSERT INTO account (email, password_hash, full_name)
        VALUES ($1, 'not-a-real-hash', $2)
        RETURNING account_id
        """,
        overrides.pop("email", f"test-{tag}@example.test"),
        overrides.pop("full_name", "Test Account"),
    )


async def make_tariff_plan(conn: asyncpg.Connection, **overrides) -> str:
    """A minimal current plan. One version, open-ended.

    Note the code is unique per call: plan_no_overlapping_versions rejects a
    second open-ended version of the same code, so tests that want two plans
    must not share one.
    """
    tag = overrides.pop("tag", unique_suffix())
    return await conn.fetchval(
        """
        INSERT INTO tariff_plan (
            code, name, customer_class, fixed_monthly_charge,
            tax_rate, effective_from, effective_to
        )
        VALUES ($1, $2, $3::site_connection_type, $4, $5, $6, $7)
        RETURNING plan_id
        """,
        overrides.pop("code", f"TEST-PLAN-{tag}"),
        overrides.pop("name", "Test Residential TOU"),
        overrides.pop("customer_class", "residential"),
        overrides.pop("fixed_monthly_charge", Decimal("100.0000")),
        overrides.pop("tax_rate", Decimal("0.0500")),
        overrides.pop("effective_from", date(2026, 1, 1)),
        overrides.pop("effective_to", None),
    )


async def make_tariff_rate(conn: asyncpg.Connection, plan_id: str,
                           period_name: str = "flat",
                           day_type: str = "weekday",
                           start_time: str = "00:00",
                           end_time: str = "24:00",
                           **overrides) -> str:
    """Times are passed as strings and cast in SQL, not as datetime.time.

    '24:00:00' is a valid PostgreSQL time and is how the ERD models a window
    running to end of day -- but Python's datetime.time tops out at 23:59:59,
    so it cannot be sent as a parameter (and asyncpg cannot decode one either;
    see test_pricing.py::test_end_of_day_window_is_write_only).
    """
    return await conn.fetchval(
        """
        INSERT INTO tariff_rate (
            plan_id, period_name, day_type, start_time, end_time,
            import_rate, export_credit_rate
        )
        VALUES ($1, $2::tou_period, $3::rate_day_type,
                $4::text::time, $5::text::time, $6, $7)
        RETURNING rate_id
        """,
        plan_id, period_name, day_type, start_time, end_time,
        overrides.pop("import_rate", Decimal("8.500000")),
        overrides.pop("export_credit_rate", Decimal("6.250000")),
    )


async def make_site(conn: asyncpg.Connection, account_id: str = None,
                    tariff_plan_id: str = None, **overrides) -> str:
    """A site with every NOT NULL column that has no default.

    tariff_plan_id is NOT NULL and, since migration f8adcdc1e0ca, a real FK to
    tariff_plan -- so this mints a plan unless given one. Pass an existing
    plan_id when a test needs several sites on the same tariff.
    """
    if account_id is None:
        account_id = await make_account(conn)
    if tariff_plan_id is None:
        tariff_plan_id = await make_tariff_plan(conn)
    tag = overrides.pop("tag", unique_suffix())
    return await conn.fetchval(
        """
        INSERT INTO site (
            account_id, tariff_plan_id, label, address_line,
            city, district, latitude, longitude, sanctioned_load_kw
        )
        VALUES ($1, $2, $3, '1 Test Road',
                'Dhaka', 'Dhaka', 23.780000, 90.279000, 5.000)
        RETURNING site_id
        """,
        account_id,
        tariff_plan_id,
        overrides.pop("label", f"test-site-{tag}"),
    )


async def make_meter(conn: asyncpg.Connection, site_id: str,
                     billing_role: str = "billing",
                     meter_flow: str = "bidirectional",
                     **overrides) -> str:
    """A meter device plus its meter_spec subtype row, as one unit."""
    tag = overrides.pop("tag", unique_suffix())
    device_id = await conn.fetchval(
        """
        INSERT INTO device (site_id, device_type, serial_no, device_key_hash)
        VALUES ($1, 'meter', $2, 'not-a-real-hash')
        RETURNING device_id
        """,
        site_id,
        overrides.pop("serial_no", f"TEST-METER-{tag}"),
    )
    await conn.execute(
        """
        INSERT INTO meter_spec (device_id, site_id, meter_flow, billing_role)
        VALUES ($1, $2, $3::meter_flow, $4::meter_billing_role)
        """,
        device_id, site_id, meter_flow, billing_role,
    )
    return device_id


async def retire_device(conn: asyncpg.Connection, device_id: str) -> None:
    await conn.execute(
        "UPDATE device SET removed_at = now(), status = 'removed' "
        "WHERE device_id = $1",
        device_id,
    )


async def make_ingest_batch(conn: asyncpg.Connection, device_id: str,
                            **overrides) -> str:
    tag = overrides.pop("tag", unique_suffix())
    return await conn.fetchval(
        """
        INSERT INTO ingest_batch (device_id, idempotency_key, reading_count)
        VALUES ($1, $2, $3)
        RETURNING batch_id
        """,
        device_id,
        overrides.pop("idempotency_key", f"test-batch-{tag}"),
        overrides.pop("reading_count", 1),
    )


async def add_reading(conn: asyncpg.Connection, device_id: str, batch_id: str,
                      interval_start, interval_minutes: int = 30,
                      import_kwh=Decimal("1.0000"),
                      export_kwh=Decimal("0.0000"),
                      generation_kwh=None, **overrides) -> None:
    """One device_reading row. interval_start must be a tz-aware datetime."""
    await conn.execute(
        """
        INSERT INTO device_reading (
            device_id, interval_start, interval_minutes,
            import_kwh, export_kwh, generation_kwh,
            source, quality, ingest_batch_id
        )
        VALUES ($1, $2, $3, $4, $5, $6,
                $7::reading_source, $8::reading_quality, $9)
        """,
        device_id, interval_start, interval_minutes,
        import_kwh, export_kwh, generation_kwh,
        overrides.pop("source", "device"),
        overrides.pop("quality", "good"),
        batch_id,
    )


async def make_inverter(conn: asyncpg.Connection, site_id: str,
                        **overrides) -> str:
    """An inverter device plus its inverter_spec subtype row."""
    tag = overrides.pop("tag", unique_suffix())
    device_id = await conn.fetchval(
        """
        INSERT INTO device (site_id, device_type, serial_no, device_key_hash)
        VALUES ($1, 'inverter', $2, 'not-a-real-hash')
        RETURNING device_id
        """,
        site_id,
        overrides.pop("serial_no", f"TEST-INV-{tag}"),
    )
    await conn.execute(
        """
        INSERT INTO inverter_spec (device_id, ac_capacity_kw, dc_capacity_kw)
        VALUES ($1, $2, $3)
        """,
        device_id,
        overrides.pop("ac_capacity_kw", Decimal("5.000")),
        overrides.pop("dc_capacity_kw", Decimal("6.000")),
    )
    return device_id
