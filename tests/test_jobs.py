"""services/jobs: the sweeps, the rollups and the partition maintenance.

These run the real job functions, not a re-implementation of their SQL, because
what is worth testing about a sweep is mostly what it *declines* to do -- an
offer accepted a moment before the sweep reached it, an order that has already
started, a household already told this month. Asserting on the statement alone
would miss all three.

`pool_of` is what makes that possible inside conftest's rolled-back
transaction: the jobs take an asyncpg pool, and this hands them one whose only
connection is the test's. Their inner `conn.transaction()` becomes a savepoint,
which is exactly the isolation they would get from a real pool anyway.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from services.jobs.consumption import sweep_consumption_limits
from services.jobs.deadlines import sweep_expired_offers, sweep_overdue_starts
from services.jobs.maintenance import ensure_partitions
from services.jobs.rollups import refresh_rollups

from .factories import (
    add_reading,
    make_account,
    make_assignment,
    make_ingest_batch,
    make_inverter,
    make_meter,
    make_site,
    make_tariff_rate,
    make_work_order,
    make_worker,
    set_consumption_limit,
)

DHAKA = ZoneInfo("Asia/Dhaka")


class pool_of:
    """An asyncpg.Pool-shaped object over one connection."""

    def __init__(self, conn):
        self._conn = conn

    def acquire(self):
        conn = self._conn

        @asynccontextmanager
        async def _acquire():
            yield conn

        return _acquire()


async def notifications_for(conn, account_id, kind=None):
    return await conn.fetch(
        "SELECT kind::text AS kind, title, body, severity::text AS severity, "
        "dedupe_key FROM notification "
        "WHERE account_id = $1 AND ($2::text IS NULL OR kind::text = $2) "
        "ORDER BY notification_id",
        account_id, kind,
    )


async def assignment_status(conn, order_id, account_id):
    return await conn.fetchval(
        "SELECT status::text FROM work_order_assignment "
        "WHERE order_id = $1 AND account_id = $2",
        order_id, account_id,
    )


async def order_status(conn, order_id):
    return await conn.fetchval(
        "SELECT status::text FROM work_order WHERE order_id = $1", order_id
    )


async def _dispatched_offer(conn, *, expires_in: timedelta, status="offered"):
    """A dispatched order with one assignment whose offer clock is set."""
    dispatcher = await make_account(conn)
    site_id = await make_site(conn)
    worker_id = await make_worker(conn)
    order_id = await make_work_order(conn, site_id, dispatcher, status="dispatched")
    await make_assignment(
        conn, order_id, worker_id,
        status=status,
        offer_expires_at=datetime.now(DHAKA) + expires_in,
    )
    return dispatcher, worker_id, order_id


# --------------------------------------------------------------------------
# Offer expiry -- supplier requirement 5
# --------------------------------------------------------------------------

async def test_a_lapsed_offer_expires_and_the_order_goes_back(conn):
    dispatcher, worker_id, order_id = await _dispatched_offer(
        conn, expires_in=timedelta(hours=-1)
    )

    result = await sweep_expired_offers(pool_of(conn), 100)

    assert result == {"found": 1, "expired": 1}
    assert await assignment_status(conn, order_id, worker_id) == "expired"
    # The state change is the point (CLAUDE.md decision 3), not a badge.
    assert await order_status(conn, order_id) == "draft"
    assert await conn.fetchval(
        "SELECT expired_at IS NOT NULL FROM work_order_assignment "
        "WHERE order_id = $1 AND account_id = $2", order_id, worker_id
    )


async def test_both_parties_are_told(conn):
    dispatcher, worker_id, order_id = await _dispatched_offer(
        conn, expires_in=timedelta(hours=-1)
    )
    await sweep_expired_offers(pool_of(conn), 100)

    to_worker = await notifications_for(conn, worker_id, "work_order_offer_expired")
    to_dispatcher = await notifications_for(
        conn, dispatcher, "work_order_offer_expired"
    )
    assert len(to_worker) == 1
    assert len(to_dispatcher) == 1
    # The dispatcher's is the one that needs acting on.
    assert to_dispatcher[0]["severity"] == "warning"
    assert to_worker[0]["severity"] == "info"


async def test_an_offer_still_inside_its_window_is_untouched(conn):
    _, worker_id, order_id = await _dispatched_offer(
        conn, expires_in=timedelta(hours=2)
    )

    assert await sweep_expired_offers(pool_of(conn), 100) == {
        "found": 0, "expired": 0
    }
    assert await assignment_status(conn, order_id, worker_id) == "offered"
    assert await order_status(conn, order_id) == "dispatched"


async def test_an_accepted_offer_is_not_expired_by_the_offer_sweep(conn):
    """The worker answered. A stale offer_expires_at must not undo that.

    This is the race the statement's status guard exists for -- but it is also
    simply true of any assignment that moved on, so it is asserted at the sweep
    level rather than only at the statement level.
    """
    _, worker_id, order_id = await _dispatched_offer(
        conn, expires_in=timedelta(hours=-1), status="accepted"
    )

    assert await sweep_expired_offers(pool_of(conn), 100) == {
        "found": 0, "expired": 0
    }
    assert await assignment_status(conn, order_id, worker_id) == "accepted"


async def test_the_order_stays_dispatched_while_anyone_is_still_on_it(conn):
    """A two-person job whose assistant lapsed is still dispatched to its lead.

    Telling the supplier it needs reassigning would be false, so it is not told.
    """
    dispatcher = await make_account(conn)
    site_id = await make_site(conn)
    lead = await make_worker(conn)
    assistant = await make_worker(conn)
    order_id = await make_work_order(conn, site_id, dispatcher, status="dispatched")
    await make_assignment(conn, order_id, lead, job_role="lead", status="accepted",
                          start_deadline_at=datetime.now(DHAKA) + timedelta(hours=20))
    await make_assignment(conn, order_id, assistant, job_role="assistant",
                          offer_expires_at=datetime.now(DHAKA) - timedelta(hours=1))

    await sweep_expired_offers(pool_of(conn), 100)

    assert await assignment_status(conn, order_id, assistant) == "expired"
    assert await assignment_status(conn, order_id, lead) == "accepted"
    assert await order_status(conn, order_id) == "dispatched"
    assert await notifications_for(conn, dispatcher) == []


async def test_a_re_offer_that_lapses_again_notifies_again(conn):
    """dedupe_key names the offer, not the order.

    An order can be offered, lapse, be re-offered and lapse again, and the
    second lapse is a real event. Keying the notification on the order id alone
    would swallow it; keying it on the deadline instant does not, while still
    making a re-run of the same sweep silent.
    """
    dispatcher, worker_id, order_id = await _dispatched_offer(
        conn, expires_in=timedelta(hours=-2)
    )
    await sweep_expired_offers(pool_of(conn), 100)

    # Re-offer with a different (also past) deadline, as the API's ON CONFLICT
    # re-offer would.
    await conn.execute(
        "UPDATE work_order_assignment SET status = 'offered', expired_at = NULL, "
        "offer_expires_at = $3 WHERE order_id = $1 AND account_id = $2",
        order_id, worker_id, datetime.now(DHAKA) - timedelta(hours=1),
    )
    await conn.execute(
        "UPDATE work_order SET status = 'dispatched' WHERE order_id = $1", order_id
    )

    await sweep_expired_offers(pool_of(conn), 100)

    assert len(await notifications_for(conn, worker_id, "work_order_offer_expired")) == 2


async def test_rerunning_the_sweep_changes_nothing(conn):
    dispatcher, worker_id, order_id = await _dispatched_offer(
        conn, expires_in=timedelta(hours=-1)
    )
    await sweep_expired_offers(pool_of(conn), 100)
    again = await sweep_expired_offers(pool_of(conn), 100)

    assert again == {"found": 0, "expired": 0}
    assert len(await notifications_for(conn, worker_id)) == 1
    assert len(await notifications_for(conn, dispatcher)) == 1


# --------------------------------------------------------------------------
# Start deadline -- worker requirement 5
# --------------------------------------------------------------------------

async def _accepted_but_late(conn, *, started: bool):
    dispatcher = await make_account(conn)
    site_id = await make_site(conn)
    worker_id = await make_worker(conn)
    order_id = await make_work_order(
        conn, site_id, dispatcher,
        status="in_progress" if started else "dispatched",
        started_at=datetime.now(DHAKA) - timedelta(hours=2) if started else None,
    )
    await make_assignment(
        conn, order_id, worker_id, status="accepted",
        start_deadline_at=datetime.now(DHAKA) - timedelta(hours=1),
    )
    return dispatcher, worker_id, order_id


async def test_accepted_and_never_started_goes_back_to_the_dispatcher(conn):
    dispatcher, worker_id, order_id = await _accepted_but_late(conn, started=False)

    assert await sweep_overdue_starts(pool_of(conn), 100) == {
        "found": 1, "expired": 1
    }
    assert await assignment_status(conn, order_id, worker_id) == "expired"
    assert await order_status(conn, order_id) == "draft"
    notes = await notifications_for(conn, worker_id, "work_order_start_overdue")
    assert len(notes) == 1
    # Losing a job you committed to is worth more than an FYI.
    assert notes[0]["severity"] == "warning"


async def test_a_worker_who_actually_started_keeps_the_job(conn):
    """The sweep reads started_at, which is the fact the worker's own status
    change wrote. A deadline is not a reason to take away work in progress."""
    _, worker_id, order_id = await _accepted_but_late(conn, started=True)

    assert await sweep_overdue_starts(pool_of(conn), 100) == {
        "found": 0, "expired": 0
    }
    assert await assignment_status(conn, order_id, worker_id) == "accepted"
    assert await order_status(conn, order_id) == "in_progress"


# --------------------------------------------------------------------------
# Consumption limit -- consumer requirement 5
# --------------------------------------------------------------------------

async def _site_using(conn, kwh_per_hour: str, hours: int):
    """A site whose billing meter has used a known amount this month."""
    owner = await make_account(conn)
    site_id = await make_site(conn, owner)
    meter_id = await make_meter(conn, site_id, interval_minutes=60)
    batch_id = await make_ingest_batch(conn, meter_id)
    start = datetime.now(DHAKA).replace(
        day=1, hour=0, minute=0, second=0, microsecond=0
    )
    for h in range(hours):
        await add_reading(
            conn, meter_id, batch_id, start + timedelta(hours=h),
            interval_minutes=60,
            import_kwh=Decimal(kwh_per_hour), export_kwh=Decimal("0.0000"),
        )
    return owner, site_id


async def test_a_household_over_its_limit_is_told_once(conn):
    owner, site_id = await _site_using(conn, "1.0000", 10)
    await set_consumption_limit(conn, site_id, Decimal("10.0000"))

    first = await sweep_consumption_limits(pool_of(conn), 100)
    assert first["over_limit"] == 1 and first["notified"] == 1

    notes = await notifications_for(conn, owner, "consumption_threshold")
    assert len(notes) == 1
    assert "10.0000 kWh" in notes[0]["body"]
    # The daily average is what makes the alert actionable, so it is in there.
    assert "averaging" in notes[0]["body"]

    second = await sweep_consumption_limits(pool_of(conn), 100)
    assert second == {"over_limit": 1, "notified": 0}
    assert len(await notifications_for(conn, owner, "consumption_threshold")) == 1


async def test_a_household_under_its_threshold_hears_nothing(conn):
    owner, site_id = await _site_using(conn, "1.0000", 5)
    await set_consumption_limit(conn, site_id, Decimal("10.0000"))

    assert await sweep_consumption_limits(pool_of(conn), 100) == {
        "over_limit": 0, "notified": 0
    }
    assert await notifications_for(conn, owner, "consumption_threshold") == []


async def test_the_threshold_is_the_households_own(conn):
    """50% of the same limit fires where the 80% default would not."""
    owner, site_id = await _site_using(conn, "1.0000", 6)
    await set_consumption_limit(conn, site_id, Decimal("10.0000"),
                                notify_at_pct=Decimal("50.00"))

    assert (await sweep_consumption_limits(pool_of(conn), 100))["notified"] == 1


# --------------------------------------------------------------------------
# Rollups
# --------------------------------------------------------------------------

PEAK_WINDOW = ("17:00", "22:00")


async def _rolled_up_site(conn):
    """One site, one whole local day of readings, on a plan with a peak window.

    24 hourly intervals: 1 kWh imported and 0.5 kWh exported each, 2 kWh
    generated each. Rates are declared for all three day types so the peak split
    is five hours whatever weekday the test happens to run on.
    """
    owner = await make_account(conn)
    site_id = await make_site(conn, owner)
    plan_id = await conn.fetchval(
        "SELECT tariff_plan_id FROM site WHERE site_id = $1", site_id
    )
    for day_type in ("weekday", "weekend", "holiday"):
        await make_tariff_rate(conn, plan_id, "peak", day_type, *PEAK_WINDOW)
        await make_tariff_rate(conn, plan_id, "off_peak", day_type, "00:00", "17:00")
        await make_tariff_rate(conn, plan_id, "off_peak", day_type, "22:00", "24:00")

    # Hourly readings on an hourly meter: peak_demand_kw is derived from the
    # device's declared interval, so the two have to agree.
    meter_id = await make_meter(conn, site_id, interval_minutes=60)
    inverter_id = await make_inverter(conn, site_id)
    meter_batch = await make_ingest_batch(conn, meter_id)
    inverter_batch = await make_ingest_batch(conn, inverter_id)

    day = date.today() - timedelta(days=2)
    for hour in range(24):
        at = datetime(day.year, day.month, day.day, hour, tzinfo=DHAKA)
        await add_reading(conn, meter_id, meter_batch, at, interval_minutes=60,
                          import_kwh=Decimal("1.0000"),
                          export_kwh=Decimal("0.5000"))
        await add_reading(conn, inverter_id, inverter_batch, at,
                          interval_minutes=60,
                          import_kwh=None, export_kwh=None,
                          generation_kwh=Decimal("2.0000"))
    return site_id, day


async def test_the_daily_rollup_matches_the_readings(conn):
    site_id, day = await _rolled_up_site(conn)

    await refresh_rollups(pool_of(conn), 1, site_id=site_id,
                          through=day + timedelta(days=1))

    row = await conn.fetchrow(
        "SELECT * FROM site_daily_summary WHERE site_id = $1 AND summary_date = $2",
        site_id, day,
    )
    assert row is not None
    assert row["import_kwh"] == Decimal("24.0000")
    assert row["export_kwh"] == Decimal("12.0000")
    assert row["generation_kwh"] == Decimal("48.0000")
    # GENERATED, never written by the job: generation - export (rule 6).
    assert row["self_consumption_kwh"] == Decimal("36.0000")
    assert row["peak_import_kwh"] == Decimal("1.0000")
    assert row["interval_count"] == 24
    # 17:00-22:00 is five hourly intervals; the rest is off-peak, and the two
    # must add back up to the day's import.
    assert row["import_peak_window_kwh"] == Decimal("5.0000")
    assert row["import_offpeak_window_kwh"] == Decimal("19.0000")


async def test_the_monthly_rollup_is_derived_from_the_daily_one(conn):
    site_id, day = await _rolled_up_site(conn)

    await refresh_rollups(pool_of(conn), 1, site_id=site_id,
                          through=day + timedelta(days=1))

    row = await conn.fetchrow(
        "SELECT * FROM site_monthly_summary "
        "WHERE site_id = $1 AND month_start = $2",
        site_id, day.replace(day=1),
    )
    assert row["import_kwh"] == Decimal("24.0000")
    assert row["export_kwh"] == Decimal("12.0000")
    assert row["generation_kwh"] == Decimal("48.0000")
    # 1 kWh in a 60-minute interval is 1 kW.
    assert row["peak_demand_kw"] == Decimal("1.000")
    # self_consumption 36 of 36 + 24 imported = 60%.
    assert row["self_sufficiency_pct"] == Decimal("60.00")


async def test_the_rollup_is_recomputed_not_accumulated(conn):
    """A late backfill must correct the summary, not double it.

    backfill_readings() upserts, so readings genuinely do arrive for a day that
    was summarised last night. A job that added instead of replacing would leave
    the rollup permanently wrong and nothing would look broken.
    """
    site_id, day = await _rolled_up_site(conn)
    through = day + timedelta(days=1)

    await refresh_rollups(pool_of(conn), 1, site_id=site_id, through=through)
    await refresh_rollups(pool_of(conn), 1, site_id=site_id, through=through)

    assert await conn.fetchval(
        "SELECT import_kwh FROM site_daily_summary "
        "WHERE site_id = $1 AND summary_date = $2", site_id, day
    ) == Decimal("24.0000")


@pytest.mark.parametrize("zone", ["UTC", "Pacific/Kiritimati", "America/Anchorage"])
async def test_the_rollup_ignores_the_session_timezone(conn, zone):
    """CLAUDE.md's timezone rule, applied to a query rather than to DDL.

    Every day boundary in the rollup is written AT TIME ZONE 'Asia/Dhaka'. A
    session in any other zone must therefore produce the identical row -- if it
    does not, someone has added a bare ::date or date_trunc that resolves
    against the session, and the summaries will silently disagree depending on
    who started the runner.
    """
    site_id, day = await _rolled_up_site(conn)
    through = day + timedelta(days=1)

    await conn.execute("SET TIME ZONE 'Asia/Dhaka'")
    await refresh_rollups(pool_of(conn), 1, site_id=site_id, through=through)
    baseline = dict(await conn.fetchrow(
        "SELECT import_kwh, export_kwh, generation_kwh, import_peak_window_kwh, "
        "interval_count FROM site_daily_summary "
        "WHERE site_id = $1 AND summary_date = $2", site_id, day
    ))

    await conn.execute(f"SET TIME ZONE '{zone}'")
    await refresh_rollups(pool_of(conn), 1, site_id=site_id, through=through)
    other = dict(await conn.fetchrow(
        "SELECT import_kwh, export_kwh, generation_kwh, import_peak_window_kwh, "
        "interval_count FROM site_daily_summary "
        "WHERE site_id = $1 AND summary_date = $2", site_id, day
    ))

    assert other == baseline


async def test_the_rollup_stops_at_yesterday(conn):
    """Today is still accumulating intervals; summarising it would read as a
    collapse in consumption. Same reason device_health excludes today."""
    site_id, _ = await _rolled_up_site(conn)

    await refresh_rollups(pool_of(conn), 7, site_id=site_id)

    assert await conn.fetchval(
        "SELECT count(*) FROM site_daily_summary "
        "WHERE site_id = $1 AND summary_date >= CURRENT_DATE", site_id
    ) == 0


# --------------------------------------------------------------------------
# Partition maintenance
# --------------------------------------------------------------------------

async def test_partition_creation_is_idempotent(conn):
    """The second pass creates nothing. This runs nightly, so the common case
    has to be free."""
    first = await ensure_partitions(pool_of(conn), 8)
    second = await ensure_partitions(pool_of(conn), 8)

    assert first["checked"] == 9
    assert second["created"] == 0
    assert second["checked"] == first["checked"]


async def test_every_month_in_the_window_exists_afterwards(conn):
    await ensure_partitions(pool_of(conn), 3)

    for months in range(4):
        total = (date.today().year * 12 + date.today().month - 1) + months
        name = f"device_reading_{total // 12:04d}_{total % 12 + 1:02d}"
        assert await conn.fetchval("SELECT to_regclass($1)", name) is not None


async def test_the_household_is_told_when_a_visit_is_not_started(conn):
    """Worker requirement 5 names the consumer as well as the supplier.

    They are the party who has been waiting in, so a job that silently goes back
    into the queue is exactly the thing they need telling about.
    """
    dispatcher, worker_id, order_id = await _accepted_but_late(conn, started=False)
    owner = await conn.fetchval(
        "SELECT s.account_id FROM site s JOIN work_order w USING (site_id) "
        "WHERE w.order_id = $1", order_id
    )

    await sweep_overdue_starts(pool_of(conn), 100)

    notes = await notifications_for(conn, owner, "work_order_start_overdue")
    assert len(notes) == 1
    assert notes[0]["severity"] == "warning"


async def test_the_household_is_not_told_an_offer_lapsed(conn):
    """The asymmetry is deliberate, not an oversight.

    Nobody tells a household a job was *offered*, so an offer lapsing is news
    about something they never heard had started. Supplier requirement 5 asks
    for the order to be reassigned, not for the household to be told about the
    dispatcher's churn -- and a panel that reports that is one people learn to
    ignore.
    """
    dispatcher, worker_id, order_id = await _dispatched_offer(
        conn, expires_in=timedelta(hours=-1)
    )
    owner = await conn.fetchval(
        "SELECT s.account_id FROM site s JOIN work_order w USING (site_id) "
        "WHERE w.order_id = $1", order_id
    )

    await sweep_expired_offers(pool_of(conn), 100)

    assert await notifications_for(conn, owner) == []


async def test_the_household_is_not_told_while_a_colleague_still_holds_it(conn):
    """A stronger version of the dispatcher's guard: the visit is still
    happening, so telling the household nobody is coming would be false."""
    dispatcher = await make_account(conn)
    site_id = await make_site(conn)
    owner = await conn.fetchval(
        "SELECT account_id FROM site WHERE site_id = $1", site_id
    )
    lead = await make_worker(conn)
    assistant = await make_worker(conn)
    order_id = await make_work_order(conn, site_id, dispatcher, status="dispatched")
    await make_assignment(conn, order_id, lead, job_role="lead", status="accepted",
                          start_deadline_at=datetime.now(DHAKA) + timedelta(hours=20))
    await make_assignment(conn, order_id, assistant, job_role="assistant",
                          status="accepted",
                          start_deadline_at=datetime.now(DHAKA) - timedelta(hours=1))

    await sweep_overdue_starts(pool_of(conn), 100)

    assert await assignment_status(conn, order_id, assistant) == "expired"
    assert await order_status(conn, order_id) == "dispatched"
    assert await notifications_for(conn, owner) == []
