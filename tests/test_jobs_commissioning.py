"""The `commissioning` sweep: a handshake past its stored deadline fails.

Decision 3 again: deadlines change state. An offer no head-end claimed within
its window, or an activation whose key never signed an accepted batch, becomes
`failed` -- not merely overdue on a page -- and the district office is told.

**A lapsed activation also loses its key.** Until this sweep existed, the key
minted at activation kept authenticating after its window closed: ingest
accepted its readings and only the handshake stayed short of live
(test_commissioning_routes.py::test_a_lapsed_activation_does_not_go_live).
Failing the handshake without revoking the key would leave a working credential
attached to an attempt the system has declared dead.

Like the other sweeps, what matters most is what it declines to touch: an offer
still inside its window, a handshake that went live, one already ended.
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from services.api.auth import hash_password, verify_password
from services.jobs.commissioning import sweep_commissioning

from .factories import (
    make_commissioning,
    make_meter,
    make_official,
    make_site,
    make_telemetry_source,
)
from .test_jobs import notifications_for, pool_of

pytestmark = pytest.mark.asyncio


async def lapsed_offer(conn, device_id, source_id):
    now = await conn.fetchval("SELECT now()")
    return await make_commissioning(
        conn, device_id, source_id,
        offered_at=now - timedelta(hours=25), offer_expires_at=now - timedelta(hours=1),
    )


async def handshake(conn, commissioning_id):
    return await conn.fetchrow(
        "SELECT status::text AS status, failed_reason::text AS failed_reason, "
        "ended_at FROM device_commissioning WHERE commissioning_id = $1",
        commissioning_id,
    )


async def test_a_lapsed_offer_fails_and_the_office_is_told(conn):
    official = await make_official(conn, "Dhanmondi")
    source_id = await make_telemetry_source(conn)
    device_id = await make_meter(conn, await make_site(conn))
    commissioning_id = await lapsed_offer(conn, device_id, source_id)

    result = await sweep_commissioning(pool_of(conn), limit=100)

    assert result["expired"] >= 1
    row = await handshake(conn, commissioning_id)
    assert row["status"] == "failed"
    assert row["failed_reason"] == "offer_expired"
    assert row["ended_at"] is not None
    [note] = await notifications_for(conn, official, "device_commissioning")
    assert note["severity"] == "warning"
    assert note["dedupe_key"] == f"commissioning:{commissioning_id}:offer_expired"


async def test_an_offer_inside_its_window_is_untouched(conn):
    source_id = await make_telemetry_source(conn)
    device_id = await make_meter(conn, await make_site(conn))
    now = await conn.fetchval("SELECT now()")
    commissioning_id = await make_commissioning(
        conn, device_id, source_id,
        offered_at=now - timedelta(hours=1), offer_expires_at=now + timedelta(hours=23),
    )

    await sweep_commissioning(pool_of(conn), limit=100)

    assert (await handshake(conn, commissioning_id))["status"] == "offered"


async def test_a_lapsed_activation_fails_and_its_key_stops_working(conn):
    source_id = await make_telemetry_source(conn)
    device_id = await make_meter(conn, await make_site(conn))
    key = "gsk_test_lapsed"
    await conn.execute(
        "UPDATE device SET device_key_hash = $2 WHERE device_id = $1",
        device_id, hash_password(key),
    )
    now = await conn.fetchval("SELECT now()")
    commissioning_id = await make_commissioning(
        conn, device_id, source_id, status="activated",
        offered_at=now - timedelta(hours=3), offer_expires_at=now + timedelta(hours=21),
        activated_at=now - timedelta(hours=2),
        activation_expires_at=now - timedelta(hours=1), activation_count=1,
    )

    await sweep_commissioning(pool_of(conn), limit=100)

    row = await handshake(conn, commissioning_id)
    assert row["status"] == "failed"
    assert row["failed_reason"] == "activation_expired"
    stored = await conn.fetchval(
        "SELECT device_key_hash FROM device WHERE device_id = $1", device_id
    )
    assert not verify_password(key, stored)


async def test_a_live_meter_is_never_touched(conn):
    """Its activation deadline is in the past -- that is normal once live. The
    deadline only means something while the handshake is waiting for data."""
    source_id = await make_telemetry_source(conn)
    device_id = await make_meter(conn, await make_site(conn))
    key = "gsk_test_live"
    await conn.execute(
        "UPDATE device SET device_key_hash = $2 WHERE device_id = $1",
        device_id, hash_password(key),
    )
    now = await conn.fetchval("SELECT now()")
    commissioning_id = await make_commissioning(
        conn, device_id, source_id, status="live",
        offered_at=now - timedelta(days=3), offer_expires_at=now - timedelta(days=2),
        activated_at=now - timedelta(days=3),
        activation_expires_at=now - timedelta(days=2, hours=23),
        activation_count=1, live_at=now - timedelta(days=3),
    )

    await sweep_commissioning(pool_of(conn), limit=100)

    assert (await handshake(conn, commissioning_id))["status"] == "live"
    stored = await conn.fetchval(
        "SELECT device_key_hash FROM device WHERE device_id = $1", device_id
    )
    assert verify_password(key, stored)


async def test_an_ended_handshake_is_never_touched(conn):
    source_id = await make_telemetry_source(conn)
    device_id = await make_meter(conn, await make_site(conn))
    now = await conn.fetchval("SELECT now()")
    commissioning_id = await make_commissioning(
        conn, device_id, source_id, status="failed",
        offered_at=now - timedelta(hours=25), offer_expires_at=now - timedelta(hours=1),
        ended_at=now - timedelta(minutes=30), failed_reason="rejected_by_source",
    )

    await sweep_commissioning(pool_of(conn), limit=100)

    assert (await handshake(conn, commissioning_id))["failed_reason"] == "rejected_by_source"


async def test_rerunning_the_sweep_changes_nothing(conn):
    official = await make_official(conn, "Dhanmondi")
    source_id = await make_telemetry_source(conn)
    device_id = await make_meter(conn, await make_site(conn))
    await lapsed_offer(conn, device_id, source_id)
    await sweep_commissioning(pool_of(conn), limit=100)

    await sweep_commissioning(pool_of(conn), limit=100)

    assert len(await notifications_for(conn, official, "device_commissioning")) == 1


async def test_only_the_meters_district_is_told(conn):
    here = await make_official(conn, "Dhanmondi")
    elsewhere = await make_official(conn, "Badda")
    source_id = await make_telemetry_source(conn)
    device_id = await make_meter(conn, await make_site(conn))  # Dhanmondi
    await lapsed_offer(conn, device_id, source_id)

    await sweep_commissioning(pool_of(conn), limit=100)

    assert len(await notifications_for(conn, here, "device_commissioning")) == 1
    assert await notifications_for(conn, elsewhere, "device_commissioning") == []
