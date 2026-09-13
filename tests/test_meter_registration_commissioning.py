"""Registering a meter offers it to its utility's head-end, behind a switch.

Step 3 of the commissioning work. `POST /api/sites/{id}/meter` either:

* **COMMISSIONING_ENABLED on** -- creates the device and a commissioning offer
  in one transaction, and writes no readings: the head-end uploads the history
  window the offer names; or
* **off** (the default, and the hosted estate) -- backfills 90 days in SQL
  exactly as before, and offers nothing.

Retiring a meter cancels its open handshake in both modes, because a handshake
created while the switch was on must not outlive the device it was for.

Run in-process against services/api with `get_conn` overridden to the test's
connection (the pattern from test_commissioning_routes.py), and the principal
supplied directly -- token handling is not what is under test here.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import httpx
import pytest
import pytest_asyncio

from services.api.auth import Principal, get_current_account
from services.api.db import get_conn
from services.api.main import app as api_app
from services.ingest.main import app as ingest_app

from .factories import (
    add_reading,
    make_account,
    make_commissioning,
    make_distribution_company,
    make_ingest_batch,
    make_site,
    make_telemetry_source,
    unique_suffix,
)

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


class Household:
    def __init__(self, account_id, site_id, point_id):
        self.account_id = account_id
        self.site_id = site_id
        self.point_id = point_id


@pytest_asyncio.fixture
async def household(conn):
    account_id = await make_account(conn)
    site_id = await make_site(conn, account_id)
    point_id = await conn.fetchval(
        "SELECT point_id FROM billing_point WHERE site_id = $1", site_id
    )
    return Household(account_id, site_id, point_id)


@pytest_asyncio.fixture
async def api(conn, household):
    async def _test_conn():
        yield conn

    async def _consumer():
        return Principal(
            account_id=household.account_id, role="consumer",
            email="test@example.test", full_name="Test Consumer", jti=None,
        )

    api_app.dependency_overrides[get_conn] = _test_conn
    api_app.dependency_overrides[get_current_account] = _consumer
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=api_app), base_url="http://api.test"
        ) as client:
            yield client
    finally:
        api_app.dependency_overrides.clear()


@pytest.fixture
def commissioning(monkeypatch):
    """Set the switch explicitly. conftest loads .env into the environment, so
    'unset' is not a state a test can rely on."""
    def _set(value: str) -> None:
        monkeypatch.setenv("COMMISSIONING_ENABLED", value)
    return _set


async def make_asset(conn, account_id, issued_by=None) -> str:
    return await conn.fetchval(
        """
        INSERT INTO meter_asset (account_id, serial_no, manufacturer, model,
                                 issued_by_company_id)
        VALUES ($1, $2, 'Hexing', 'HXE310-BD', $3)
        RETURNING meter_asset_id
        """,
        account_id, f"TEST-ASSET-{unique_suffix()}", issued_by,
    )


async def utility_with_head_end(conn) -> tuple[str, str]:
    company_id = await make_distribution_company(conn)
    return company_id, await make_telemetry_source(conn, company_id)


async def set_point_utility(conn, point_id, company_id) -> None:
    await conn.execute(
        "UPDATE billing_point SET distribution_company_id = $2 WHERE point_id = $1",
        point_id, company_id,
    )


async def register(api, household, asset_id, **body):
    return await api.post(
        f"/api/sites/{household.site_id}/meter",
        json={"meter_asset_id": str(asset_id), **body},
    )


async def offers_for(conn, device_id):
    return await conn.fetch(
        """
        SELECT commissioning_id, source_id, status::text AS status,
               failed_reason::text AS failed_reason, requested_by_account_id,
               offered_at, offer_expires_at, ended_at, backfill_from, backfill_to
        FROM device_commissioning WHERE device_id = $1
        ORDER BY offered_at
        """,
        device_id,
    )


async def reading_count(conn, device_id) -> int:
    return await conn.fetchval(
        "SELECT count(*) FROM device_reading WHERE device_id = $1", device_id
    )


# ---------------------------------------------------------------------------
# switch on
# ---------------------------------------------------------------------------


async def test_registration_offers_the_meter_instead_of_backfilling(
    conn, api, household, commissioning
):
    commissioning("on")
    company_id, source_id = await utility_with_head_end(conn)
    await set_point_utility(conn, household.point_id, company_id)
    asset_id = await make_asset(conn, household.account_id)

    response = await register(api, household, asset_id)

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["readings_backfilled"] == 0
    assert body["readings_pending"] is True

    device_id = body["device_id"]
    assert await reading_count(conn, device_id) == 0

    [offer] = await offers_for(conn, device_id)
    assert offer["status"] == "offered"
    assert offer["source_id"] == source_id
    assert offer["requested_by_account_id"] == household.account_id
    assert offer["offer_expires_at"] - offer["offered_at"] == timedelta(hours=24)
    # The window the head-end should upload is the one the SQL backfill would
    # have written: 90 days, ending yesterday.
    assert offer["backfill_from"] == date.today() - timedelta(days=90)
    assert offer["backfill_to"] == date.today() - timedelta(days=1)
    assert body["backfill_from"] == offer["backfill_from"].isoformat()


async def test_the_offer_reaches_the_head_end(conn, api, household, commissioning):
    """The whole of steps 2 and 3 meeting: a meter registered through the
    consumer API is on its utility's feed at ingest."""
    commissioning("on")
    from services.api.auth import hash_password

    company_id = await make_distribution_company(conn)
    key = f"gss_test_{unique_suffix()}"
    source_id = await make_telemetry_source(
        conn, company_id, source_key_hash=hash_password(key)
    )
    await set_point_utility(conn, household.point_id, company_id)
    device_id = (await register(
        api, household, await make_asset(conn, household.account_id)
    )).json()["device_id"]

    async def _test_conn():
        yield conn

    ingest_app.dependency_overrides[get_conn] = _test_conn
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=ingest_app), base_url="http://ingest.test"
        ) as ingest:
            feed = (await ingest.get(
                "/v1/source/commissions",
                headers={"X-Source-Id": str(source_id), "X-Source-Key": key},
            )).json()
    finally:
        ingest_app.dependency_overrides.pop(get_conn, None)

    assert [c["device_id"] for c in feed["commissions"]] == [device_id]
    assert feed["commissions"][0]["meter_flow"] == "unidirectional"


async def test_a_utility_with_no_head_end_is_recorded(conn, api, household, commissioning):
    """Not skipped silently: the failure is a row the equipment page can show."""
    commissioning("on")
    company_id = await make_distribution_company(conn)  # no telemetry_source
    await set_point_utility(conn, household.point_id, company_id)

    response = await register(api, household, await make_asset(conn, household.account_id))

    assert response.status_code == 201, response.text
    [offer] = await offers_for(conn, response.json()["device_id"])
    assert offer["status"] == "failed"
    assert offer["failed_reason"] == "no_source"
    assert offer["source_id"] is None
    assert offer["ended_at"] is not None


async def test_a_disabled_head_end_counts_as_none(conn, api, household, commissioning):
    commissioning("on")
    company_id = await make_distribution_company(conn)
    # now(), not a Python instant: telemetry_source_disabled_after_created
    # compares against created_at, which is the transaction's now().
    await make_telemetry_source(
        conn, company_id, disabled_at=await conn.fetchval("SELECT now()")
    )
    await set_point_utility(conn, household.point_id, company_id)

    response = await register(api, household, await make_asset(conn, household.account_id))

    [offer] = await offers_for(conn, response.json()["device_id"])
    assert offer["failed_reason"] == "no_source"


async def test_a_new_connection_routes_by_the_meters_issuer(
    conn, api, household, commissioning
):
    """A connection opened through the API carries no utility of its own
    (create_billing_point never sets one), so the meter's issuer decides --
    otherwise every household's second meter would read as no_source."""
    commissioning("on")
    company_id, source_id = await utility_with_head_end(conn)
    asset_id = await make_asset(conn, household.account_id, issued_by=company_id)

    response = await register(api, household, asset_id, point_label="Upstairs")

    assert response.status_code == 201, response.text
    [offer] = await offers_for(conn, response.json()["device_id"])
    assert offer["source_id"] == source_id


async def test_the_connections_own_utility_outranks_the_issuer(
    conn, api, household, commissioning
):
    """Whoever bills the connection runs the network the meter is on."""
    commissioning("on")
    billing_company, billing_source = await utility_with_head_end(conn)
    issuing_company, _ = await utility_with_head_end(conn)
    await set_point_utility(conn, household.point_id, billing_company)
    asset_id = await make_asset(conn, household.account_id, issued_by=issuing_company)

    response = await register(api, household, asset_id)

    [offer] = await offers_for(conn, response.json()["device_id"])
    assert offer["source_id"] == billing_source


# ---------------------------------------------------------------------------
# swaps
# ---------------------------------------------------------------------------


async def meter_with_live_handshake(conn, api, household, source_id):
    """Register a meter, then put its handshake in the live state directly and
    give it a reading yesterday -- what a working head-end would have left."""
    device_id = (await register(
        api, household, await make_asset(conn, household.account_id)
    )).json()["device_id"]
    now = await conn.fetchval("SELECT now()")
    await conn.execute("DELETE FROM device_commissioning WHERE device_id = $1", device_id)
    commissioning_id = await make_commissioning(
        conn, device_id, source_id, status="live",
        offered_at=now - timedelta(hours=2), offer_expires_at=now + timedelta(hours=22),
        activated_at=now - timedelta(hours=1), activation_expires_at=now,
        activation_count=1, live_at=now - timedelta(minutes=30),
    )
    yesterday = datetime.combine(
        date.today() - timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc
    )
    await conn.execute(
        "SELECT create_reading_partition($1)", yesterday.date().replace(day=1)
    )
    batch_id = await make_ingest_batch(conn, device_id)
    await add_reading(conn, device_id, batch_id, yesterday + timedelta(hours=12),
                      export_kwh=None)
    return device_id, commissioning_id


async def test_a_swap_cancels_the_old_handshake_and_offers_the_new_meter(
    conn, api, household, commissioning
):
    commissioning("on")
    company_id, source_id = await utility_with_head_end(conn)
    await set_point_utility(conn, household.point_id, company_id)
    old_device, old_handshake = await meter_with_live_handshake(
        conn, api, household, source_id
    )

    response = await register(
        api, household, await make_asset(conn, household.account_id),
        point_id=str(household.point_id), replace_existing=True,
    )

    assert response.status_code == 201, response.text
    old = await conn.fetchrow(
        "SELECT status::text AS status, ended_at FROM device_commissioning "
        "WHERE commissioning_id = $1",
        old_handshake,
    )
    assert old["status"] == "cancelled"
    assert old["ended_at"] is not None

    [new] = await offers_for(conn, response.json()["device_id"])
    assert new["status"] == "offered"
    # The retired meter covered the point through yesterday, so there is no
    # history left for the new one to upload -- never re-cover its ground.
    assert new["backfill_from"] is None
    assert new["backfill_to"] is None


async def test_a_swap_with_the_switch_off_still_cancels(
    conn, api, household, commissioning
):
    """A handshake opened while the switch was on must not outlive its device
    after someone turns it off."""
    commissioning("on")
    company_id, source_id = await utility_with_head_end(conn)
    await set_point_utility(conn, household.point_id, company_id)
    _, old_handshake = await meter_with_live_handshake(conn, api, household, source_id)

    commissioning("off")
    response = await register(
        api, household, await make_asset(conn, household.account_id),
        point_id=str(household.point_id), replace_existing=True,
    )

    assert response.status_code == 201, response.text
    assert await conn.fetchval(
        "SELECT status::text FROM device_commissioning WHERE commissioning_id = $1",
        old_handshake,
    ) == "cancelled"
    assert await offers_for(conn, response.json()["device_id"]) == []


# ---------------------------------------------------------------------------
# switch off
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", ["off", ""])
async def test_switch_off_backfills_as_before(conn, api, household, commissioning, value):
    """Empty counts as off: the hosted estate sets nothing and must keep its
    SQL backfill."""
    commissioning(value)
    company_id, _ = await utility_with_head_end(conn)
    await set_point_utility(conn, household.point_id, company_id)

    response = await register(api, household, await make_asset(conn, household.account_id))

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["readings_pending"] is False
    assert body["readings_backfilled"] > 0
    assert await reading_count(conn, body["device_id"]) == body["readings_backfilled"]
    assert await offers_for(conn, body["device_id"]) == []
