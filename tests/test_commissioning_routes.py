"""The commissioning handshake, over HTTP, against the real ingest app.

These are the repo's first route tests. The app runs in-process through
httpx's ASGI transport, on the same event loop as the test, and `get_conn` is
overridden to hand every request the test's own connection -- so each handler's
`conn.transaction()` becomes a savepoint inside the test transaction, and
everything a request writes is rolled back with the test, exactly as the schema
tests' rows are. The app's lifespan (its pool) never starts.

One consequence worth knowing when reading an assertion about time: inside one
transaction `now()` is constant, so an activation, and the batch that makes it
live, all happen at the same database instant.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import httpx
import pytest
import pytest_asyncio

from services.api.auth import hash_password, verify_password
from services.api.db import get_conn
from services.ingest.main import app

from .factories import (
    make_commissioning,
    make_inverter,
    make_meter,
    make_official,
    make_site,
    make_telemetry_source,
    retire_device,
    unique_suffix,
)

pytestmark = pytest.mark.asyncio

DHAKA = ZoneInfo("Asia/Dhaka")


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def client(conn):
    async def _test_conn():
        yield conn

    app.dependency_overrides[get_conn] = _test_conn
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(
            transport=transport, base_url="http://ingest.test"
        ) as c:
            yield c
    finally:
        app.dependency_overrides.pop(get_conn, None)


class Source:
    def __init__(self, source_id, key: str):
        self.source_id = source_id
        self.key = key

    @property
    def headers(self) -> dict[str, str]:
        return {"X-Source-Id": str(self.source_id), "X-Source-Key": self.key}


async def make_source(conn, **overrides) -> Source:
    key = f"gss_test_{unique_suffix()}"
    source_id = await make_telemetry_source(
        conn, source_key_hash=hash_password(key), **overrides
    )
    return Source(source_id, key)


async def fresh_offer(conn, device_id, source: Source, **overrides):
    """An offer made "just now" -- the transaction's now(), which is what the
    handlers compare deadlines against."""
    now = await conn.fetchval("SELECT now()")
    overrides.setdefault("offered_at", now - timedelta(minutes=1))
    overrides.setdefault("offer_expires_at", now + timedelta(hours=24))
    return await make_commissioning(conn, device_id, source.source_id, **overrides)


async def status_of(conn, commissioning_id) -> str:
    return await conn.fetchval(
        "SELECT status::text FROM device_commissioning WHERE commissioning_id = $1",
        commissioning_id,
    )


def a_reading(**fields) -> dict:
    """Yesterday 10:00 in Dhaka: aligned, not in the future, well inside the
    backdate limit, and in a month no test has billed."""
    start = (datetime.now(DHAKA) - timedelta(days=1)).replace(
        hour=10, minute=0, second=0, microsecond=0
    )
    body = {
        "interval_start": start.isoformat(),
        "interval_minutes": 30,
        "import_kwh": "0.4200",
        "export_kwh": "0.1000",
    }
    body.update(fields)
    return body


async def post_readings(client, device_id, key: str, readings: list[dict]):
    return await client.post(
        "/v1/ingest/readings",
        headers={"X-Device-Key": key, "Idempotency-Key": str(uuid.uuid4())},
        json={"device_id": str(device_id), "readings": readings},
    )


async def activate(client, source: Source, commissioning_id, serial_no: str):
    return await client.post(
        f"/v1/source/commissions/{commissioning_id}/activate",
        headers=source.headers,
        json={"serial_no": serial_no},
    )


async def serial_of(conn, device_id) -> str:
    return await conn.fetchval(
        "SELECT serial_no FROM device WHERE device_id = $1", device_id
    )


# ---------------------------------------------------------------------------
# source authentication
# ---------------------------------------------------------------------------


async def test_the_feed_needs_a_source(conn, client):
    response = await client.get("/v1/source/commissions")
    assert response.status_code == 401


@pytest.mark.parametrize("failure", ["wrong_key", "unknown_source", "disabled"])
async def test_every_auth_failure_looks_the_same(conn, client, failure):
    """An unknown source, a wrong key and a disabled source are one answer, so
    the endpoint is not an oracle for which source ids exist."""
    now = await conn.fetchval("SELECT now()")
    source = await make_source(
        conn, disabled_at=now if failure == "disabled" else None
    )
    headers = source.headers
    if failure == "wrong_key":
        headers["X-Source-Key"] = "gss_not_the_key"
    elif failure == "unknown_source":
        headers["X-Source-Id"] = str(uuid.uuid4())

    response = await client.get("/v1/source/commissions", headers=headers)

    assert response.status_code == 401
    assert response.json()["detail"] == "source authentication failed"


# ---------------------------------------------------------------------------
# the feed
# ---------------------------------------------------------------------------


async def test_the_feed_holds_only_this_sources_open_handshakes(conn, client):
    """Scoped to the caller, and correct between sweeps: a lapsed offer is not
    listed even though no sweep has marked it failed yet (decision 3)."""
    mine, theirs = await make_source(conn), await make_source(conn)
    now = await conn.fetchval("SELECT now()")

    listed = await fresh_offer(conn, await make_meter(conn, await make_site(conn)), mine)
    await fresh_offer(conn, await make_meter(conn, await make_site(conn)), theirs)
    await fresh_offer(
        conn, await make_meter(conn, await make_site(conn)), mine,
        offered_at=now - timedelta(days=2), offer_expires_at=now - timedelta(days=1),
    )
    await fresh_offer(
        conn, await make_meter(conn, await make_site(conn)), mine,
        status="failed", ended_at=now, failed_reason="rejected_by_source",
    )
    retired = await make_meter(conn, await make_site(conn))
    await fresh_offer(conn, retired, mine)
    await retire_device(conn, retired)

    response = await client.get("/v1/source/commissions", headers=mine.headers)

    assert response.status_code == 200
    ids = [c["commissioning_id"] for c in response.json()["commissions"]]
    assert ids == [str(listed)]


async def test_the_feed_describes_the_meter(conn, client):
    """What a head-end needs to talk to the meter, plus the one labelled
    simulation hint: the solar behind this connection, which a real head-end
    would not know but a synthetic one needs to produce believable export."""
    source = await make_source(conn)
    site_id = await make_site(conn)
    device_id = await make_meter(conn, site_id, meter_flow="bidirectional")
    point_id = await conn.fetchval(
        "SELECT billing_point_id FROM meter_spec WHERE device_id = $1", device_id
    )
    await make_inverter(conn, site_id, billing_point_id=point_id,
                        ac_capacity_kw=Decimal("3.500"))
    await fresh_offer(conn, device_id, source,
                      backfill_from=date(2026, 6, 1),
                      backfill_to=date(2026, 8, 31))

    [offer] = (await client.get(
        "/v1/source/commissions", headers=source.headers
    )).json()["commissions"]

    assert offer["device_id"] == str(device_id)
    assert offer["serial_no"] == await serial_of(conn, device_id)
    assert offer["status"] == "offered"
    assert offer["meter_flow"] == "bidirectional"
    assert offer["interval_minutes"] == 30
    assert offer["backfill_from"] == "2026-06-01"
    assert offer["backfill_to"] == "2026-08-31"
    assert offer["simulation"]["point_solar_capacity_kw"] == "3.500"


# ---------------------------------------------------------------------------
# activation
# ---------------------------------------------------------------------------


async def test_activation_hands_over_a_working_key(conn, client):
    source = await make_source(conn)
    device_id = await make_meter(conn, await make_site(conn))
    commissioning_id = await fresh_offer(conn, device_id, source)

    response = await activate(client, source, commissioning_id,
                              await serial_of(conn, device_id))

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "activated"
    assert body["activation_count"] == 1
    assert body["device_key"].startswith("gsk_")
    assert body["ingest_path"] == "/v1/ingest/readings"
    stored = await conn.fetchval(
        "SELECT device_key_hash FROM device WHERE device_id = $1", device_id
    )
    assert verify_password(body["device_key"], stored)
    assert await status_of(conn, commissioning_id) == "activated"


async def test_a_retried_activation_replaces_the_key(conn, client):
    """A head-end that lost the response retries. The first key is dead -- safe,
    because no reading has been signed with it -- and the count says two."""
    source = await make_source(conn)
    device_id = await make_meter(conn, await make_site(conn))
    commissioning_id = await fresh_offer(conn, device_id, source)
    serial = await serial_of(conn, device_id)

    first = (await activate(client, source, commissioning_id, serial)).json()
    second = (await activate(client, source, commissioning_id, serial)).json()

    stored = await conn.fetchval(
        "SELECT device_key_hash FROM device WHERE device_id = $1", device_id
    )
    assert second["activation_count"] == 2
    assert not verify_password(first["device_key"], stored)
    assert verify_password(second["device_key"], stored)


async def test_another_sources_offer_is_not_found(conn, client):
    mine, theirs = await make_source(conn), await make_source(conn)
    device_id = await make_meter(conn, await make_site(conn))
    commissioning_id = await fresh_offer(conn, device_id, theirs)

    response = await activate(client, mine, commissioning_id,
                              await serial_of(conn, device_id))

    assert response.status_code == 404
    assert await status_of(conn, commissioning_id) == "offered"


async def test_a_serial_that_does_not_match_is_refused(conn, client):
    """The head-end is claiming a different meter from the one offered."""
    source = await make_source(conn)
    device_id = await make_meter(conn, await make_site(conn))
    commissioning_id = await fresh_offer(conn, device_id, source)

    response = await activate(client, source, commissioning_id, "NOT-THIS-METER")

    assert response.status_code == 422
    assert await status_of(conn, commissioning_id) == "offered"


async def test_a_lapsed_offer_cannot_be_activated(conn, client):
    source = await make_source(conn)
    device_id = await make_meter(conn, await make_site(conn))
    now = await conn.fetchval("SELECT now()")
    commissioning_id = await fresh_offer(
        conn, device_id, source,
        offered_at=now - timedelta(days=2), offer_expires_at=now - timedelta(days=1),
    )

    response = await activate(client, source, commissioning_id,
                              await serial_of(conn, device_id))

    assert response.status_code == 409
    assert "expired" in response.json()["detail"]


async def test_a_live_meter_is_not_reactivated(conn, client):
    """Once a key has signed accepted readings, replacing it is a rotation --
    doing it through activation would silently cut off a working meter."""
    source = await make_source(conn)
    device_id = await make_meter(conn, await make_site(conn))
    now = await conn.fetchval("SELECT now()")
    commissioning_id = await fresh_offer(
        conn, device_id, source, status="live",
        activated_at=now, activation_expires_at=now + timedelta(hours=1),
        activation_count=1, live_at=now,
    )

    response = await activate(client, source, commissioning_id,
                              await serial_of(conn, device_id))

    assert response.status_code == 409
    assert "live" in response.json()["detail"]


async def test_activation_retries_are_capped(conn, client):
    """Every retry restarts the activation window. Without a cap, a head-end
    stuck in a loop would keep a handshake open forever and the sweep could
    never fail it."""
    from services.ingest.commissioning import MAX_ACTIVATIONS

    source = await make_source(conn)
    device_id = await make_meter(conn, await make_site(conn))
    now = await conn.fetchval("SELECT now()")
    commissioning_id = await fresh_offer(
        conn, device_id, source, status="activated",
        activated_at=now, activation_expires_at=now + timedelta(hours=1),
        activation_count=MAX_ACTIVATIONS,
    )

    response = await activate(client, source, commissioning_id,
                              await serial_of(conn, device_id))

    assert response.status_code == 409
    assert "too many" in response.json()["detail"]


async def test_a_retired_meter_cannot_be_activated(conn, client):
    source = await make_source(conn)
    device_id = await make_meter(conn, await make_site(conn))
    commissioning_id = await fresh_offer(conn, device_id, source)
    await retire_device(conn, device_id)

    response = await activate(client, source, commissioning_id,
                              await serial_of(conn, device_id))

    assert response.status_code == 409
    assert "retired" in response.json()["detail"]


# ---------------------------------------------------------------------------
# live is proved by data
# ---------------------------------------------------------------------------


async def test_the_first_accepted_batch_makes_it_live(conn, client):
    source = await make_source(conn)
    device_id = await make_meter(conn, await make_site(conn))
    commissioning_id = await fresh_offer(conn, device_id, source)
    key = (await activate(client, source, commissioning_id,
                          await serial_of(conn, device_id))).json()["device_key"]

    first = await post_readings(client, device_id, key, [a_reading()])
    assert first.status_code == 200, first.text
    assert first.json()["accepted"] == 1
    assert first.json()["went_live"] is True
    assert await status_of(conn, commissioning_id) == "live"

    later = await post_readings(
        client, device_id, key,
        [a_reading(interval_start=(datetime.now(DHAKA) - timedelta(days=1)).replace(
            hour=11, minute=0, second=0, microsecond=0).isoformat())],
    )
    assert later.json()["went_live"] is False


async def test_a_batch_of_rejects_proves_nothing(conn, client):
    """Authenticating is not delivering. A meter whose every reading is refused
    has not shown it works."""
    source = await make_source(conn)
    device_id = await make_meter(conn, await make_site(conn))
    commissioning_id = await fresh_offer(conn, device_id, source)
    key = (await activate(client, source, commissioning_id,
                          await serial_of(conn, device_id))).json()["device_key"]

    response = await post_readings(
        client, device_id, key, [a_reading(generation_kwh="1.0000")]
    )

    assert response.json()["rejected"] == 1
    assert response.json()["went_live"] is False
    assert await status_of(conn, commissioning_id) == "activated"


async def test_a_lapsed_activation_does_not_go_live(conn, client):
    """The stored deadline decides, not the sweep's timing."""
    source = await make_source(conn)
    device_id = await make_meter(conn, await make_site(conn))
    key = f"gsk_test_{unique_suffix()}"
    await conn.execute(
        "UPDATE device SET device_key_hash = $2 WHERE device_id = $1",
        device_id, hash_password(key),
    )
    now = await conn.fetchval("SELECT now()")
    commissioning_id = await fresh_offer(
        conn, device_id, source, status="activated",
        offered_at=now - timedelta(hours=3),
        activated_at=now - timedelta(hours=2),
        activation_expires_at=now - timedelta(hours=1),
        activation_count=1,
    )

    response = await post_readings(client, device_id, key, [a_reading()])

    assert response.json()["accepted"] == 1
    assert response.json()["went_live"] is False
    assert await status_of(conn, commissioning_id) == "activated"


# ---------------------------------------------------------------------------
# rejection
# ---------------------------------------------------------------------------


async def reject(client, source: Source, commissioning_id, detail="unknown serial"):
    return await client.post(
        f"/v1/source/commissions/{commissioning_id}/reject",
        headers=source.headers,
        json={"detail": detail},
    )


async def test_a_rejection_fails_the_handshake_and_tells_the_office(conn, client):
    """Usually a technician's typo. The district office is who can send someone
    back to read the serial off the meter."""
    source = await make_source(conn)
    official = await make_official(conn, "Dhanmondi")
    device_id = await make_meter(conn, await make_site(conn))
    commissioning_id = await fresh_offer(conn, device_id, source)

    response = await reject(client, source, commissioning_id,
                            "serial not in DESCO inventory")

    assert response.status_code == 200, response.text
    row = await conn.fetchrow(
        "SELECT status::text, failed_reason::text, failure_detail, ended_at "
        "FROM device_commissioning WHERE commissioning_id = $1",
        commissioning_id,
    )
    assert row["status"] == "failed"
    assert row["failed_reason"] == "rejected_by_source"
    assert row["failure_detail"] == "serial not in DESCO inventory"
    assert row["ended_at"] is not None
    assert await conn.fetchval(
        "SELECT count(*) FROM notification WHERE account_id = $1 "
        "AND kind = 'device_commissioning' AND entity_id = $2",
        official, str(device_id),
    ) == 1


async def test_rejecting_after_activation_kills_the_key(conn, client):
    """A head-end that took a key and then refused the meter must not leave a
    working credential behind."""
    source = await make_source(conn)
    device_id = await make_meter(conn, await make_site(conn))
    commissioning_id = await fresh_offer(conn, device_id, source)
    key = (await activate(client, source, commissioning_id,
                          await serial_of(conn, device_id))).json()["device_key"]

    await reject(client, source, commissioning_id)

    stored = await conn.fetchval(
        "SELECT device_key_hash FROM device WHERE device_id = $1", device_id
    )
    assert not verify_password(key, stored)
    assert (await post_readings(client, device_id, key, [a_reading()])).status_code == 401


async def test_a_live_meter_cannot_be_rejected(conn, client):
    """A meter delivering readings is retired by GridSync, not refused by the
    head-end after the fact."""
    source = await make_source(conn)
    device_id = await make_meter(conn, await make_site(conn))
    now = await conn.fetchval("SELECT now()")
    commissioning_id = await fresh_offer(
        conn, device_id, source, status="live",
        activated_at=now, activation_expires_at=now + timedelta(hours=1),
        activation_count=1, live_at=now,
    )

    response = await reject(client, source, commissioning_id)

    assert response.status_code == 409
    assert await status_of(conn, commissioning_id) == "live"


async def test_another_sources_handshake_cannot_be_rejected(conn, client):
    mine, theirs = await make_source(conn), await make_source(conn)
    device_id = await make_meter(conn, await make_site(conn))
    commissioning_id = await fresh_offer(conn, device_id, theirs)

    response = await reject(client, mine, commissioning_id)

    assert response.status_code == 404
    assert await status_of(conn, commissioning_id) == "offered"



async def test_a_replayed_batch_completes_a_new_handshake(conn, client):
    """Found by the end-to-end run. A retry re-offers the same device; its
    head-end re-sends a batch it already delivered under the previous handshake,
    with the same Idempotency-Key, and ingest answers with the original outcome.
    The replay authenticated with the NEW key and its readings are held, which
    is exactly what live means -- before this, the replay path skipped the
    check and the handshake sat in `activated` until it lapsed."""
    source = await make_source(conn)
    device_id = await make_meter(conn, await make_site(conn))
    serial = await serial_of(conn, device_id)
    first = await fresh_offer(conn, device_id, source)
    key1 = (await activate(client, source, first, serial)).json()["device_key"]
    idem = str(uuid.uuid4())
    body = {"device_id": str(device_id), "readings": [a_reading()]}
    assert (await client.post("/v1/ingest/readings", json=body,
            headers={"X-Device-Key": key1, "Idempotency-Key": idem})).json()["went_live"]

    # The retry: the live handshake ends, a new one is offered and claimed.
    await conn.execute(
        "UPDATE device_commissioning SET status = 'cancelled', ended_at = now() "
        "WHERE commissioning_id = $1", first,
    )
    second = await fresh_offer(conn, device_id, source)
    key2 = (await activate(client, source, second, serial)).json()["device_key"]

    replay = (await client.post("/v1/ingest/readings", json=body,
              headers={"X-Device-Key": key2, "Idempotency-Key": idem})).json()

    assert replay["replayed"] is True
    assert replay["went_live"] is True
    assert await status_of(conn, second) == "live"
