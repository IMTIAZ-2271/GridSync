"""The simulated utility head-end, driven one cycle at a time against ingest.

`simulator/headend.py` is given an httpx client rather than a URL, so these
tests hand it the real ingest app in-process on the test's connection: every
activation, upload and live transition below really happens, and rolls back.
Its local state is a SQLite file under pytest's tmp_path.

What is pinned here is the head-end's contract with GridSync, not its logging:
it claims what it is offered, delivers exactly the intervals owed, never re-sends
what it already delivered, survives a restart, stops for a meter that leaves its
feed, and refuses a serial it does not recognise.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

import httpx
import pytest
import pytest_asyncio

from services.api.auth import hash_password
from services.api.db import get_conn
from services.ingest.main import MAX_BACKDATE, app
from simulator.headend import HeadEnd

from .factories import (
    make_commissioning,
    make_inverter,
    make_meter,
    make_site,
    make_telemetry_source,
    retire_device,
    unique_suffix,
)

pytestmark = pytest.mark.asyncio

DHAKA = ZoneInfo("Asia/Dhaka")
STEP = timedelta(minutes=30)


@pytest_asyncio.fixture
async def ingest(conn):
    async def _test_conn():
        yield conn

    app.dependency_overrides[get_conn] = _test_conn
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://ingest.test"
        ) as client:
            yield client
    finally:
        app.dependency_overrides.pop(get_conn, None)


@pytest_asyncio.fixture
async def source(conn):
    key = f"gss_test_{unique_suffix()}"
    source_id = await make_telemetry_source(conn, source_key_hash=hash_password(key))
    return source_id, key


@pytest.fixture
def headend(ingest, source, tmp_path):
    """A factory, so a test can start a second head-end on the same state file
    -- which is what a restart is."""
    def _make(**kwargs) -> HeadEnd:
        source_id, key = source
        return HeadEnd(
            ingest,
            source_id=str(source_id),
            source_key=key,
            state_path=tmp_path / "state.sqlite",
            log=lambda *_: None,
            **kwargs,
        )
    return _make


#: A fixed "now" per test. Real wall-clock time minus a margin, so every
#: interval the head-end computes is in the past for ingest's skew check too.
@pytest.fixture
def now():
    return datetime.now(timezone.utc).replace(second=0, microsecond=0) - timedelta(minutes=1)


def last_elapsed(now: datetime) -> datetime:
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    return epoch + ((now - epoch) // STEP) * STEP - STEP


def owed(start: datetime, now: datetime) -> int:
    """Intervals from `start` through the last one fully elapsed at `now`."""
    end = last_elapsed(now)
    return 0 if end < start else (end - start) // STEP + 1


def dhaka_midnight(day: date) -> datetime:
    return datetime.combine(day, time(0), tzinfo=DHAKA).astimezone(timezone.utc)


async def offer(conn, device_id, source_id, **overrides):
    db_now = await conn.fetchval("SELECT now()")
    overrides.setdefault("offered_at", db_now - timedelta(minutes=1))
    overrides.setdefault("offer_expires_at", db_now + timedelta(hours=24))
    return await make_commissioning(conn, device_id, source_id, **overrides)


async def handshake(conn, commissioning_id):
    return await conn.fetchrow(
        "SELECT status::text AS status, activation_count, failed_reason::text AS "
        "failed_reason FROM device_commissioning WHERE commissioning_id = $1",
        commissioning_id,
    )


async def readings(conn, device_id):
    return await conn.fetch(
        "SELECT interval_start, import_kwh, export_kwh FROM device_reading "
        "WHERE device_id = $1 ORDER BY interval_start",
        device_id,
    )


# ---------------------------------------------------------------------------
# the handshake, end to end
# ---------------------------------------------------------------------------


async def test_one_cycle_claims_uploads_and_goes_live(conn, source, headend, now):
    source_id, _ = source
    device_id = await make_meter(conn, await make_site(conn), meter_flow="unidirectional")
    yesterday = (now.astimezone(DHAKA) - timedelta(days=1)).date()
    commissioning_id = await offer(
        conn, device_id, source_id, backfill_from=yesterday, backfill_to=yesterday
    )

    report = await headend().cycle(now=now)

    assert report.activated == 1
    assert report.went_live == 1
    assert (await handshake(conn, commissioning_id))["status"] == "live"
    rows = await readings(conn, device_id)
    # History starts at the window's first Dhaka midnight and runs, unbroken,
    # through the last interval that has finished.
    start = dhaka_midnight(yesterday)
    assert rows[0]["interval_start"] == start
    assert rows[-1]["interval_start"] == last_elapsed(now)
    assert len(rows) == owed(start, now)
    assert report.accepted == len(rows)
    # Unidirectional: the head-end sends no export half, and reading_role_guard
    # stores that as a measured zero (rule 6) -- never a positive figure.
    assert all(r["export_kwh"] == 0 for r in rows)


async def test_a_second_cycle_resends_nothing(conn, source, headend, now):
    source_id, _ = source
    device_id = await make_meter(conn, await make_site(conn), meter_flow="unidirectional")
    yesterday = (now.astimezone(DHAKA) - timedelta(days=1)).date()
    await offer(conn, device_id, source_id, backfill_from=yesterday, backfill_to=yesterday)
    he = headend()
    await he.cycle(now=now)

    report = await he.cycle(now=now)

    assert report.activated == 0
    assert report.accepted == 0
    assert report.duplicates == 0


async def test_later_cycles_deliver_only_what_has_since_elapsed(conn, source, headend, now):
    source_id, _ = source
    device_id = await make_meter(conn, await make_site(conn), meter_flow="unidirectional")
    yesterday = (now.astimezone(DHAKA) - timedelta(days=1)).date()
    await offer(conn, device_id, source_id, backfill_from=yesterday, backfill_to=yesterday)
    he = headend()
    earlier = now - timedelta(hours=2)
    await he.cycle(now=earlier)

    report = await he.cycle(now=now)

    assert report.accepted == owed(last_elapsed(earlier) + STEP, now)
    assert report.duplicates == 0


async def test_a_restart_resumes_from_its_state(conn, source, headend, now):
    """The key and the watermark survive the process: a new head-end on the same
    state file neither activates again (which would replace a working key) nor
    re-sends history."""
    source_id, _ = source
    device_id = await make_meter(conn, await make_site(conn), meter_flow="unidirectional")
    yesterday = (now.astimezone(DHAKA) - timedelta(days=1)).date()
    commissioning_id = await offer(
        conn, device_id, source_id, backfill_from=yesterday, backfill_to=yesterday
    )
    await headend().cycle(now=now)

    report = await headend().cycle(now=now)

    assert report.activated == 0
    assert report.accepted == 0
    assert (await handshake(conn, commissioning_id))["activation_count"] == 1


async def test_with_no_window_delivery_starts_in_the_offers_interval(
    conn, source, headend, now
):
    """A swap whose predecessor already covers the connection offers no window.
    Earlier intervals are the retired meter's (site_readings sums across
    devices); the interval the swap happened in is nobody's unless the new
    meter takes it, because the retired one left the feed at that moment."""
    source_id, _ = source
    device_id = await make_meter(conn, await make_site(conn), meter_flow="unidirectional")
    db_now = await conn.fetchval("SELECT now()")
    offered_at = db_now - timedelta(hours=2, minutes=10)
    await offer(conn, device_id, source_id, offered_at=offered_at)

    await headend().cycle(now=now)

    rows = await readings(conn, device_id)
    first = rows[0]["interval_start"]
    assert first <= offered_at
    assert offered_at - first < STEP
    assert len(rows) == owed(first, now)


async def test_history_never_reaches_past_what_ingest_accepts(conn, source, headend, now):
    """The offer's window is 90 whole days, and ingest refuses anything older
    than 90 days to the minute -- so the first morning of the window would be
    rejected. The head-end starts where ingest's limit allows instead."""
    source_id, _ = source
    device_id = await make_meter(conn, await make_site(conn), meter_flow="unidirectional")
    today = now.astimezone(DHAKA).date()
    await offer(
        conn, device_id, source_id,
        backfill_from=today - timedelta(days=90), backfill_to=today - timedelta(days=1),
    )

    report = await headend().cycle(now=now)

    assert report.rejected == 0
    assert report.accepted > 0
    rows = await readings(conn, device_id)
    assert rows[0]["interval_start"] > now - MAX_BACKDATE


# ---------------------------------------------------------------------------
# what a meter reports
# ---------------------------------------------------------------------------


async def test_a_bidirectional_meter_nets_against_its_connections_solar(
    conn, source, headend, now
):
    source_id, _ = source
    site_id = await make_site(conn)
    device_id = await make_meter(conn, site_id, meter_flow="bidirectional")
    point_id = await conn.fetchval(
        "SELECT billing_point_id FROM meter_spec WHERE device_id = $1", device_id
    )
    await make_inverter(conn, site_id, billing_point_id=point_id,
                        ac_capacity_kw=Decimal("5.000"))
    yesterday = (now.astimezone(DHAKA) - timedelta(days=1)).date()
    await offer(conn, device_id, source_id, backfill_from=yesterday, backfill_to=yesterday)

    await headend().cycle(now=now)

    day = [r for r in await readings(conn, device_id)
           if r["interval_start"].astimezone(DHAKA).date() == yesterday]
    assert all(r["export_kwh"] is not None for r in day)
    # A 5 kW array out-produces a household at midday.
    assert sum(r["export_kwh"] for r in day) > 0


# ---------------------------------------------------------------------------
# leaving, and refusing
# ---------------------------------------------------------------------------


async def test_a_meter_that_leaves_the_feed_is_forgotten(conn, source, headend, now):
    source_id, _ = source
    device_id = await make_meter(conn, await make_site(conn), meter_flow="unidirectional")
    yesterday = (now.astimezone(DHAKA) - timedelta(days=1)).date()
    commissioning_id = await offer(
        conn, device_id, source_id, backfill_from=yesterday, backfill_to=yesterday
    )
    he = headend()
    await he.cycle(now=now)
    await retire_device(conn, device_id)
    await conn.execute(
        "UPDATE device_commissioning SET status = 'cancelled', ended_at = now() "
        "WHERE commissioning_id = $1",
        commissioning_id,
    )

    report = await he.cycle(now=now + timedelta(hours=1))

    assert report.stopped == 1
    assert he.held_device_ids() == set()


async def test_an_unrecognised_serial_is_rejected(conn, source, headend, now):
    source_id, _ = source
    device_id = await make_meter(conn, await make_site(conn),
                                 serial_no=f"TYPO-{unique_suffix()}")
    commissioning_id = await offer(conn, device_id, source_id)

    report = await headend(reject_serials=["TYPO-*"]).cycle(now=now)

    assert report.rejected_offers == 1
    row = await handshake(conn, commissioning_id)
    assert row["status"] == "failed"
    assert row["failed_reason"] == "rejected_by_source"
    assert await readings(conn, device_id) == []


async def test_a_lost_key_for_an_activated_meter_is_reissued(conn, source, headend, now):
    """The state file went away between activating and going live. Activation
    is still allowed then, so the head-end simply asks again."""
    source_id, _ = source
    device_id = await make_meter(conn, await make_site(conn), meter_flow="unidirectional")
    db_now = await conn.fetchval("SELECT now()")
    yesterday = (now.astimezone(DHAKA) - timedelta(days=1)).date()
    commissioning_id = await offer(
        conn, device_id, source_id, status="activated",
        activated_at=db_now, activation_expires_at=db_now + timedelta(hours=1),
        activation_count=1, backfill_from=yesterday, backfill_to=yesterday,
    )

    report = await headend().cycle(now=now)

    assert report.activated == 1
    row = await handshake(conn, commissioning_id)
    assert row["activation_count"] == 2
    assert row["status"] == "live"


async def test_a_live_meter_with_no_key_is_rekeyed_and_delivery_resumes(
    conn, source, headend, now, tmp_path
):
    """The host restarted with an empty disk -- a free container, say. The
    head-end re-keys every live meter it no longer holds, and delivers again
    from the offer's own starting point: anything already held comes back as a
    duplicate, so nothing is lost and nothing is double-counted."""
    source_id, _ = source
    device_id = await make_meter(conn, await make_site(conn), meter_flow="unidirectional")
    db_now = await conn.fetchval("SELECT now()")
    commissioning_id = await offer(
        conn, device_id, source_id, status="live",
        activated_at=db_now, activation_expires_at=db_now + timedelta(hours=1),
        activation_count=1, live_at=db_now,
        offered_at=last_elapsed(now) - 3 * STEP,
    )

    first = headend()
    report = await first.cycle(now=now)

    assert report.rekeyed == 1
    row = await handshake(conn, commissioning_id)
    assert (row["status"], row["activation_count"]) == ("live", 1)
    assert len(await readings(conn, device_id)) == 4

    # Wiped again: the same batches go out under the same idempotency keys, so
    # ingest replays its answer and writes nothing new.
    first.state.close()
    (tmp_path / "state.sqlite").unlink()
    again = await headend().cycle(now=now)

    assert again.rekeyed == 1
    assert not again.errors
    assert len(await readings(conn, device_id)) == 4


async def test_a_new_handshake_is_a_new_delivery(conn, source, headend, now):
    """Found by the end-to-end run. The same device re-offered after its
    handshake ended re-sends intervals it already delivered. Those batches must
    carry new Idempotency-Keys -- scoped to the handshake -- so ingest records
    them as duplicates of held readings rather than replaying the old batch."""
    source_id, _ = source
    device_id = await make_meter(conn, await make_site(conn), meter_flow="unidirectional")
    yesterday = (now.astimezone(DHAKA) - timedelta(days=1)).date()
    first = await offer(conn, device_id, source_id, backfill_from=yesterday, backfill_to=yesterday)
    he = headend()
    await he.cycle(now=now)
    await conn.execute(
        "UPDATE device_commissioning SET status = 'cancelled', ended_at = now() "
        "WHERE commissioning_id = $1", first,
    )
    second = await offer(conn, device_id, source_id, backfill_from=yesterday, backfill_to=yesterday)

    report = await he.cycle(now=now)

    assert report.activated == 1
    assert report.accepted == 0
    assert report.duplicates == owed(dhaka_midnight(yesterday), now)
    assert report.went_live == 1
    assert (await handshake(conn, second))["status"] == "live"
