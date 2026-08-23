"""tariff_plan, tariff_rate, and the site FK (migration f8adcdc1e0ca).

The FK tests matter most: site.tariff_plan_id was NOT NULL but referentially
unchecked from migration 1 until this one, so a site could name a plan that
never existed. `test_site_cannot_reference_a_nonexistent_plan` is the proof
that gap is closed.
"""
from datetime import date
from decimal import Decimal

import asyncpg
import pytest

from tests.factories import (
    make_account,
    make_site,
    make_tariff_plan,
    make_tariff_rate,
)


# --------------------------------------------------------------------------
# The FK migration 1 promised
# --------------------------------------------------------------------------
async def test_site_cannot_reference_a_nonexistent_plan(conn):
    account = await make_account(conn)

    with pytest.raises(asyncpg.exceptions.ForeignKeyViolationError) as caught:
        await conn.execute(
            """
            INSERT INTO site (
                account_id, tariff_plan_id, label, address_line,
                city, district, latitude, longitude, sanctioned_load_kw
            )
            VALUES ($1, gen_random_uuid(), 'orphan', '1 Test Road',
                    'Dhaka', 'Dhanmondi', 23.746000, 90.376000, 5.000)
            """,
            account,
        )

    assert caught.value.constraint_name == "site_tariff_plan_fk"


async def test_site_tariff_plan_id_is_still_not_null(conn):
    account = await make_account(conn)

    with pytest.raises(asyncpg.exceptions.NotNullViolationError):
        await conn.execute(
            """
            INSERT INTO site (
                account_id, tariff_plan_id, label, address_line,
                city, district, latitude, longitude, sanctioned_load_kw
            )
            VALUES ($1, NULL, 'no-plan', '1 Test Road',
                    'Dhaka', 'Dhanmondi', 23.746000, 90.376000, 5.000)
            """,
            account,
        )


async def test_a_plan_in_use_cannot_be_deleted(conn):
    """Rule 1: a referenced plan must survive. RESTRICT, not CASCADE.

    RESTRICT raises 23001 (RestrictViolationError), which is a sibling of
    ForeignKeyViolationError rather than a subclass -- catching the latter
    would not catch this.
    """
    plan = await make_tariff_plan(conn)
    await make_site(conn, tariff_plan_id=plan)

    with pytest.raises(asyncpg.exceptions.RestrictViolationError) as caught:
        await conn.execute("DELETE FROM tariff_plan WHERE plan_id = $1", plan)

    assert caught.value.constraint_name == "site_tariff_plan_fk"


async def test_many_sites_can_share_one_plan(conn):
    plan = await make_tariff_plan(conn)

    await make_site(conn, tariff_plan_id=plan)
    await make_site(conn, tariff_plan_id=plan)

    assert await conn.fetchval(
        "SELECT count(*) FROM site WHERE tariff_plan_id = $1", plan
    ) == 2


# --------------------------------------------------------------------------
# Rule 1: rates are versioned, never edited
# --------------------------------------------------------------------------
async def test_two_open_ended_versions_of_one_code_are_refused(conn):
    await make_tariff_plan(conn, code="RES-TOU", effective_from=date(2026, 1, 1))

    with pytest.raises(asyncpg.exceptions.ExclusionViolationError) as caught:
        await make_tariff_plan(conn, code="RES-TOU",
                               effective_from=date(2026, 6, 1))

    assert caught.value.constraint_name == "plan_no_overlapping_versions"


async def test_a_closed_version_admits_its_successor(conn):
    """The intended workflow: close the current version, then open the next."""
    await make_tariff_plan(conn, code="RES-TOU",
                           effective_from=date(2026, 1, 1),
                           effective_to=date(2026, 7, 1))

    successor = await make_tariff_plan(conn, code="RES-TOU",
                                       effective_from=date(2026, 7, 1))

    assert successor is not None


async def test_versions_may_not_overlap_even_partially(conn):
    await make_tariff_plan(conn, code="RES-TOU",
                           effective_from=date(2026, 1, 1),
                           effective_to=date(2026, 7, 1))

    with pytest.raises(asyncpg.exceptions.ExclusionViolationError):
        await make_tariff_plan(conn, code="RES-TOU",
                               effective_from=date(2026, 6, 1),
                               effective_to=date(2026, 9, 1))


async def test_different_codes_may_overlap_freely(conn):
    await make_tariff_plan(conn, code="RES-TOU",
                           effective_from=date(2026, 1, 1))
    other = await make_tariff_plan(conn, code="COM-TOU",
                                   effective_from=date(2026, 1, 1))

    assert other is not None


@pytest.mark.parametrize("tax", [Decimal("-0.01"), Decimal("1.01")])
async def test_tax_rate_must_be_a_fraction(conn, tax):
    with pytest.raises(asyncpg.exceptions.CheckViolationError) as caught:
        await make_tariff_plan(conn, tax_rate=tax)

    assert caught.value.constraint_name == "plan_tax"


async def test_effective_to_must_follow_effective_from(conn):
    with pytest.raises(asyncpg.exceptions.CheckViolationError) as caught:
        await make_tariff_plan(conn, effective_from=date(2026, 6, 1),
                               effective_to=date(2026, 1, 1))

    assert caught.value.constraint_name == "plan_dates"


# --------------------------------------------------------------------------
# TOU windows
# --------------------------------------------------------------------------
async def test_overlapping_windows_in_one_day_type_are_refused(conn):
    """An interval that fell in two windows could be billed at two rates."""
    plan = await make_tariff_plan(conn)
    await make_tariff_rate(conn, plan, period_name="peak", day_type="weekday",
                           start_time="17:00", end_time="22:00")

    with pytest.raises(asyncpg.exceptions.ExclusionViolationError) as caught:
        await make_tariff_rate(conn, plan, period_name="shoulder",
                               day_type="weekday",
                               start_time="21:00", end_time="23:00")

    assert caught.value.constraint_name == "rate_no_overlapping_windows"


async def test_the_same_window_on_another_day_type_is_fine(conn):
    plan = await make_tariff_plan(conn)
    await make_tariff_rate(conn, plan, period_name="peak", day_type="weekday",
                           start_time="17:00", end_time="22:00")

    rate = await make_tariff_rate(conn, plan, period_name="peak",
                                  day_type="weekend",
                                  start_time="17:00", end_time="22:00")

    assert rate is not None


async def test_abutting_windows_do_not_overlap(conn):
    """Bounds are '[)', so 17:00-22:00 and 22:00-24:00 are adjacent, not
    overlapping -- which is what makes a full day tileable."""
    plan = await make_tariff_plan(conn)
    await make_tariff_rate(conn, plan, period_name="peak", day_type="weekday",
                           start_time="17:00", end_time="22:00")

    rate = await make_tariff_rate(conn, plan, period_name="off_peak",
                                  day_type="weekday",
                                  start_time="22:00", end_time="24:00")

    assert rate is not None


async def test_end_time_must_follow_start_time(conn):
    plan = await make_tariff_plan(conn)

    with pytest.raises(asyncpg.exceptions.CheckViolationError) as caught:
        await make_tariff_rate(conn, plan, start_time="22:00",
                               end_time="06:00")

    assert caught.value.constraint_name == "rate_window"


async def test_overnight_window_is_modelled_as_two_rows(conn):
    """22:00 -> 06:00 is not a range. It is two rows, which is why start_time
    is part of the natural key."""
    plan = await make_tariff_plan(conn)

    await make_tariff_rate(conn, plan, period_name="off_peak",
                           day_type="weekday",
                           start_time="22:00", end_time="24:00")
    await make_tariff_rate(conn, plan, period_name="off_peak",
                           day_type="weekday",
                           start_time="00:00", end_time="06:00")

    assert await conn.fetchval(
        "SELECT count(*) FROM tariff_rate WHERE plan_id = $1 "
        "AND period_name = 'off_peak'", plan
    ) == 2


async def test_the_natural_key_is_unique(conn):
    plan = await make_tariff_plan(conn)
    await make_tariff_rate(conn, plan, period_name="peak", day_type="weekday",
                           start_time="17:00", end_time="22:00")

    # Same (plan, period_name, day_type, start_time), different window: caught
    # by the natural key rather than the overlap constraint.
    with pytest.raises(asyncpg.exceptions.UniqueViolationError) as caught:
        await conn.execute(
            """
            INSERT INTO tariff_rate (plan_id, period_name, day_type,
                                     start_time, end_time,
                                     import_rate, export_credit_rate)
            VALUES ($1, 'peak', 'weekday', time '17:00', time '18:00', 1, 1)
            """,
            plan,
        )

    assert caught.value.constraint_name == "rate_natural_key"


@pytest.mark.parametrize("column", ["import_rate", "export_credit_rate"])
async def test_negative_rates_are_refused(conn, column):
    plan = await make_tariff_plan(conn)

    with pytest.raises(asyncpg.exceptions.CheckViolationError) as caught:
        await make_tariff_rate(conn, plan, **{column: Decimal("-1.000000")})

    assert caught.value.constraint_name == "rate_non_negative"


async def test_rates_keep_six_decimals(conn):
    """Rule 5: rates are NUMERIC(10,6), not FLOAT."""
    plan = await make_tariff_plan(conn)
    await make_tariff_rate(conn, plan, import_rate=Decimal("8.123456"))

    assert await conn.fetchval(
        "SELECT import_rate FROM tariff_rate WHERE plan_id = $1", plan
    ) == Decimal("8.123456")


# --------------------------------------------------------------------------
# Known limitation, pinned so it cannot become a silent surprise
# --------------------------------------------------------------------------
async def test_end_of_day_window_is_write_only(conn):
    """`end_time = '24:00'` stores and constrains correctly but cannot be READ.

    PostgreSQL accepts time '24:00:00'; Python's datetime.time stops at
    23:59:59, so asyncpg raises while decoding the column. The row is fine --
    the driver cannot represent it. Any query that selects end_time for a
    day-ending window must cast it, e.g. `end_time::text`.

    This is the ERD's recommended way to model an overnight window, so it
    affects real plans, not just tests. See the note in test_pricing.py's
    module docstring and in factories.make_tariff_rate.
    """
    plan = await make_tariff_plan(conn)
    await make_tariff_rate(conn, plan, start_time="22:00", end_time="24:00")

    with pytest.raises(ValueError, match="hour must be in 0..23"):
        await conn.fetchval(
            "SELECT end_time FROM tariff_rate WHERE plan_id = $1", plan
        )

    assert await conn.fetchval(
        "SELECT end_time::text FROM tariff_rate WHERE plan_id = $1", plan
    ) == "24:00:00"
