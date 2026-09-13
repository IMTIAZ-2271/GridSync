"""Staff views of meter commissioning: the overview, and retrying a connection.

`GET /api/commissioning` -- every live billing meter with the state of its
latest handshake. A district official sees their own district; a supplier (the
fleet telemetry view) sees every meter, read-only.

`POST /api/devices/{device_id}/commissioning` -- offer the meter to its
head-end again. District officials only (and admins): they register meters and
are the ones notified when a connection fails. A meter whose head-end lost a
live key is the case this exists for -- activation cannot re-issue a live key,
so the retry cancels that handshake, revokes the key and offers afresh.

Nothing here is available to the household (Consumer 9).
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import httpx
import pytest
import pytest_asyncio

from services.api.auth import Principal, get_current_account, hash_password, verify_password
from services.api.db import get_conn
from services.api.main import app

from .factories import (
    add_reading,
    make_account,
    make_commissioning,
    make_distribution_company,
    make_ingest_batch,
    make_inverter,
    make_meter,
    make_official,
    make_site,
    make_telemetry_source,
    retire_device,
)

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def switch_on(monkeypatch):
    monkeypatch.setenv("COMMISSIONING_ENABLED", "on")


@pytest_asyncio.fixture
async def as_role(conn):
    """A client acting as a given principal. Call as `await as_role(role, id)`."""
    clients = []

    async def _test_conn():
        yield conn

    async def _make(role: str, account_id) -> httpx.AsyncClient:
        async def _principal():
            return Principal(account_id=account_id, role=role,
                             email="t@example.test", full_name="Test", jti=None)
        app.dependency_overrides[get_conn] = _test_conn
        app.dependency_overrides[get_current_account] = _principal
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://api.test"
        )
        clients.append(client)
        return client

    try:
        yield _make
    finally:
        for c in clients:
            await c.aclose()
        app.dependency_overrides.clear()


async def utility_site(conn, district: str = "Dhanmondi"):
    """A site in `district` whose Main point is served by a utility with a
    head-end. make_site always uses Dhanmondi; other districts are set after."""
    company_id = await make_distribution_company(conn)
    source_id = await make_telemetry_source(conn, company_id)
    site_id = await make_site(conn)
    await conn.execute("UPDATE site SET district = $2 WHERE site_id = $1", site_id, district)
    await conn.execute(
        "UPDATE billing_point SET distribution_company_id = $2 WHERE site_id = $1",
        site_id, company_id,
    )
    return site_id, source_id


async def rows_for(conn, device_id):
    return await conn.fetch(
        "SELECT commissioning_id, status::text AS status, backfill_from, backfill_to, "
        "requested_by_account_id FROM device_commissioning WHERE device_id = $1 "
        "ORDER BY offered_at, status",
        device_id,
    )


def mine(body: dict, device_id) -> dict:
    [row] = [m for m in body["meters"] if m["device_id"] == str(device_id)]
    return row


# ---------------------------------------------------------------------------
# overview
# ---------------------------------------------------------------------------


async def test_the_overview_names_each_meters_state(conn, as_role):
    official = await make_official(conn, "Dhanmondi")
    site_id, source_id = await utility_site(conn)
    never = await make_meter(conn, site_id)
    other_site, _ = await utility_site(conn)
    lapsed = await make_meter(conn, other_site)
    now = await conn.fetchval("SELECT now()")
    await make_commissioning(
        conn, lapsed, source_id,
        offered_at=now - timedelta(hours=25), offer_expires_at=now - timedelta(hours=1),
    )
    live_site, _ = await utility_site(conn)
    live = await make_meter(conn, live_site)
    await make_commissioning(
        conn, live, source_id, status="live",
        offered_at=now - timedelta(hours=2), offer_expires_at=now + timedelta(hours=22),
        activated_at=now - timedelta(hours=2), activation_expires_at=now - timedelta(hours=1),
        activation_count=1, live_at=now - timedelta(hours=2),
    )

    client = await as_role("government", official)
    body = (await client.get("/api/commissioning")).json()

    assert body["enabled"] is True
    # Between sweeps the stored deadline already decides (decision 3).
    assert mine(body, never)["state"] == "not_commissioned"
    assert mine(body, never)["needs_attention"] is True
    assert mine(body, lapsed)["state"] == "offer_lapsed"
    assert mine(body, lapsed)["needs_attention"] is True
    assert mine(body, live)["state"] == "live"
    assert mine(body, live)["needs_attention"] is False


async def test_an_official_sees_only_their_district(conn, as_role):
    official = await make_official(conn, "Dhanmondi")
    here_site, _ = await utility_site(conn, "Dhanmondi")
    here = await make_meter(conn, here_site)
    there_site, _ = await utility_site(conn, "Badda")
    there = await make_meter(conn, there_site)

    client = await as_role("government", official)
    ids = {m["device_id"] for m in (await client.get("/api/commissioning")).json()["meters"]}

    assert str(here) in ids
    assert str(there) not in ids


async def test_a_supplier_sees_the_whole_fleet(conn, as_role):
    site_a, _ = await utility_site(conn, "Dhanmondi")
    site_b, _ = await utility_site(conn, "Badda")
    a, b = await make_meter(conn, site_a), await make_meter(conn, site_b)

    client = await as_role("supplier", await make_account(conn))
    ids = {m["device_id"] for m in (await client.get("/api/commissioning")).json()["meters"]}

    assert {str(a), str(b)} <= ids


async def test_only_billing_meters_are_listed(conn, as_role):
    """Inverters are not commissioned in this phase, and a retired meter has
    nothing to connect."""
    official = await make_official(conn, "Dhanmondi")
    site_id, _ = await utility_site(conn)
    inverter = await make_inverter(conn, site_id)
    other, _ = await utility_site(conn)
    retired = await make_meter(conn, other)
    await retire_device(conn, retired)

    client = await as_role("government", official)
    ids = {m["device_id"] for m in (await client.get("/api/commissioning")).json()["meters"]}

    assert str(inverter) not in ids
    assert str(retired) not in ids


async def test_the_overview_says_when_commissioning_is_off(conn, as_role, monkeypatch):
    monkeypatch.setenv("COMMISSIONING_ENABLED", "off")
    client = await as_role("government", await make_official(conn, "Dhanmondi"))

    assert (await client.get("/api/commissioning")).json()["enabled"] is False


@pytest.mark.parametrize("role", ["consumer", "worker"])
async def test_the_household_and_workers_cannot_read_it(conn, as_role, role):
    client = await as_role(role, await make_account(conn))
    assert (await client.get("/api/commissioning")).status_code == 403


# ---------------------------------------------------------------------------
# retry
# ---------------------------------------------------------------------------


async def retry(client, device_id):
    return await client.post(f"/api/devices/{device_id}/commissioning")


async def test_a_failed_connection_is_offered_again(conn, as_role):
    official = await make_official(conn, "Dhanmondi")
    site_id, source_id = await utility_site(conn)
    device_id = await make_meter(conn, site_id)
    now = await conn.fetchval("SELECT now()")
    await make_commissioning(
        conn, device_id, source_id, status="failed",
        offered_at=now - timedelta(hours=25), offer_expires_at=now - timedelta(hours=1),
        ended_at=now - timedelta(minutes=5), failed_reason="offer_expired",
    )

    response = await retry(await as_role("government", official), device_id)

    assert response.status_code == 201, response.text
    assert response.json()["status"] == "offered"
    rows = await rows_for(conn, device_id)
    assert [r["status"] for r in rows] == ["failed", "offered"]
    assert rows[-1]["requested_by_account_id"] == official


async def test_a_retry_continues_after_the_connections_last_reading(conn, as_role):
    official = await make_official(conn, "Dhanmondi")
    site_id, _ = await utility_site(conn)
    device_id = await make_meter(conn, site_id, meter_flow="unidirectional")
    yesterday = date.today() - timedelta(days=1)
    await conn.execute("SELECT create_reading_partition($1)", yesterday.replace(day=1))
    await add_reading(
        conn, device_id, await make_ingest_batch(conn, device_id),
        datetime.combine(yesterday, datetime.min.time(), tzinfo=timezone.utc)
        + timedelta(hours=6),
        export_kwh=None,
    )

    await retry(await as_role("government", official), device_id)

    [row] = await rows_for(conn, device_id)
    assert row["backfill_from"] == date.today()
    assert row["backfill_to"] == date.today()


async def test_a_retry_after_readings_today_still_starts_today(conn, as_role):
    """Found by the end-to-end run. A live meter whose head-end lost its key has
    readings from this morning; a window starting tomorrow left the head-end
    owing nothing, and the retried handshake sat in `activated` until it lapsed.
    Same device, so today's intervals come back as harmless duplicates."""
    official = await make_official(conn, "Dhanmondi")
    site_id, _ = await utility_site(conn)
    device_id = await make_meter(conn, site_id, meter_flow="unidirectional")
    today = date.today()
    await conn.execute("SELECT create_reading_partition($1)", today.replace(day=1))
    await add_reading(
        conn, device_id, await make_ingest_batch(conn, device_id),
        # 00:30 Dhaka today, as UTC.
        datetime.combine(today, datetime.min.time(), tzinfo=timezone.utc)
        - timedelta(hours=5, minutes=30),
        export_kwh=None,
    )

    await retry(await as_role("government", official), device_id)

    [row] = await rows_for(conn, device_id)
    assert row["backfill_from"] == today
    assert row["backfill_to"] == today


async def test_a_live_meter_whose_key_was_lost_is_reconnected(conn, as_role):
    official = await make_official(conn, "Dhanmondi")
    site_id, source_id = await utility_site(conn)
    device_id = await make_meter(conn, site_id)
    key = "gsk_test_lost"
    await conn.execute(
        "UPDATE device SET device_key_hash = $2 WHERE device_id = $1",
        device_id, hash_password(key),
    )
    now = await conn.fetchval("SELECT now()")
    await make_commissioning(
        conn, device_id, source_id, status="live",
        offered_at=now - timedelta(days=2), offer_expires_at=now - timedelta(days=1),
        activated_at=now - timedelta(days=2), activation_expires_at=now - timedelta(days=1),
        activation_count=1, live_at=now - timedelta(days=2),
    )

    response = await retry(await as_role("government", official), device_id)

    assert response.status_code == 201, response.text
    assert sorted(r["status"] for r in await rows_for(conn, device_id)) == [
        "cancelled", "offered"
    ]
    stored = await conn.fetchval(
        "SELECT device_key_hash FROM device WHERE device_id = $1", device_id
    )
    assert not verify_password(key, stored)


async def test_an_offer_still_waiting_is_not_retried(conn, as_role):
    """Nothing to retry: the head-end simply has not asked yet."""
    official = await make_official(conn, "Dhanmondi")
    site_id, source_id = await utility_site(conn)
    device_id = await make_meter(conn, site_id)
    now = await conn.fetchval("SELECT now()")
    await make_commissioning(
        conn, device_id, source_id,
        offered_at=now - timedelta(hours=1), offer_expires_at=now + timedelta(hours=23),
    )

    response = await retry(await as_role("government", official), device_id)

    assert response.status_code == 409
    assert len(await rows_for(conn, device_id)) == 1


async def test_another_districts_meter_is_not_found(conn, as_role):
    official = await make_official(conn, "Dhanmondi")
    site_id, _ = await utility_site(conn, "Badda")
    device_id = await make_meter(conn, site_id)

    response = await retry(await as_role("government", official), device_id)

    assert response.status_code == 404
    assert await rows_for(conn, device_id) == []


async def test_a_retired_meter_or_an_inverter_is_not_found(conn, as_role):
    official = await make_official(conn, "Dhanmondi")
    site_id, _ = await utility_site(conn)
    retired = await make_meter(conn, site_id)
    await retire_device(conn, retired)
    inverter = await make_inverter(conn, site_id)
    client = await as_role("government", official)

    assert (await retry(client, retired)).status_code == 404
    assert (await retry(client, inverter)).status_code == 404


async def test_retry_is_refused_while_commissioning_is_off(conn, as_role, monkeypatch):
    """Offering a meter nothing will claim would only manufacture a failure."""
    monkeypatch.setenv("COMMISSIONING_ENABLED", "off")
    official = await make_official(conn, "Dhanmondi")
    site_id, _ = await utility_site(conn)
    device_id = await make_meter(conn, site_id)

    response = await retry(await as_role("government", official), device_id)

    assert response.status_code == 409
    assert await rows_for(conn, device_id) == []


@pytest.mark.parametrize("role", ["consumer", "worker", "supplier"])
async def test_only_officials_retry(conn, as_role, role):
    site_id, _ = await utility_site(conn)
    device_id = await make_meter(conn, site_id)

    response = await retry(await as_role(role, await make_account(conn)), device_id)

    assert response.status_code == 403
