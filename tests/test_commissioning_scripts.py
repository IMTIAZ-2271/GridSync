"""The three provisioning scripts around the head-end.

* `scripts/issue_source_keys.py` gives each utility a head-end credential.
* `scripts/commission_existing_meters.py` offers the meters installed before
  commissioning existed -- the seeded estate -- so a head-end can pick them up.
* `scripts/issue_device_keys.py` must now leave commissioned devices alone:
  re-keying one would cut a live meter off from the head-end holding its key.

Each script's work is a function taking a connection, so these tests run it
inside the rolled-back test transaction. That also means it sees the dev
database's own companies and seeded meters; every assertion is about rows the
test made.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from scripts.commission_existing_meters import offer_existing
from scripts.issue_source_keys import issue_source_keys
from services.api.auth import verify_password
from services.api.queries import sql

from .factories import (
    add_reading,
    make_commissioning,
    make_distribution_company,
    make_ingest_batch,
    make_inverter,
    make_meter,
    make_site,
    make_telemetry_source,
    retire_device,
)

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# issue_source_keys
# ---------------------------------------------------------------------------


async def test_a_utility_without_a_head_end_gets_one(conn):
    company_id = await make_distribution_company(conn)

    issued = await issue_source_keys(conn, only_missing=False)

    [mine] = [s for s in issued if s["company_id"] == company_id]
    assert mine["created"] is True
    stored = await conn.fetchval(
        "SELECT source_key_hash FROM telemetry_source WHERE source_id = $1",
        mine["source_id"],
    )
    assert mine["source_key"].startswith("gss_")
    assert verify_password(mine["source_key"], stored)


async def test_an_existing_head_end_is_rotated_in_place(conn):
    """Same source_id, new key: its commissioning history stays attached, and
    the old key stops working."""
    company_id = await make_distribution_company(conn)
    source_id = await make_telemetry_source(conn, company_id)

    issued = await issue_source_keys(conn, only_missing=False)

    [mine] = [s for s in issued if s["company_id"] == company_id]
    assert mine["created"] is False
    assert mine["source_id"] == source_id
    row = await conn.fetchrow(
        "SELECT source_key_hash, key_rotated_at FROM telemetry_source "
        "WHERE source_id = $1",
        source_id,
    )
    assert row["source_key_hash"] != "not-a-real-hash"
    assert row["key_rotated_at"] is not None


async def test_only_missing_leaves_existing_head_ends_alone(conn):
    company_id = await make_distribution_company(conn)
    await make_telemetry_source(conn, company_id)

    issued = await issue_source_keys(conn, only_missing=True)

    assert [s for s in issued if s["company_id"] == company_id] == []


# ---------------------------------------------------------------------------
# commission_existing_meters
# ---------------------------------------------------------------------------


async def utility_point(conn):
    """A site whose Main point belongs to a utility with a head-end."""
    company_id = await make_distribution_company(conn)
    source_id = await make_telemetry_source(conn, company_id)
    site_id = await make_site(conn)
    await conn.execute(
        "UPDATE billing_point SET distribution_company_id = $2 WHERE site_id = $1",
        site_id, company_id,
    )
    return site_id, source_id


async def offers(conn, device_id):
    return await conn.fetch(
        "SELECT source_id, status::text AS status, backfill_from, backfill_to, "
        "requested_by_account_id FROM device_commissioning WHERE device_id = $1",
        device_id,
    )


async def test_a_meter_with_history_is_offered_from_where_it_stops(conn):
    """Its readings run through yesterday, so the head-end continues from today
    -- the window is today..today, never a re-upload of what exists."""
    today = date(2026, 9, 13)
    site_id, source_id = await utility_point(conn)
    device_id = await make_meter(conn, site_id, meter_flow="unidirectional")
    yesterday_noon = datetime(2026, 9, 12, 6, 0, tzinfo=timezone.utc)
    await conn.execute("SELECT create_reading_partition($1)", date(2026, 9, 1))
    await add_reading(conn, device_id, await make_ingest_batch(conn, device_id),
                      yesterday_noon, export_kwh=None)

    await offer_existing(conn, today=today)

    [row] = await offers(conn, device_id)
    assert row["status"] == "offered"
    assert row["source_id"] == source_id
    assert row["backfill_from"] == today
    assert row["backfill_to"] == today
    assert row["requested_by_account_id"] is None


async def test_a_meter_with_no_history_gets_the_full_window(conn):
    today = date(2026, 9, 13)
    site_id, _ = await utility_point(conn)
    device_id = await make_meter(conn, site_id)

    await offer_existing(conn, today=today)

    [row] = await offers(conn, device_id)
    assert row["backfill_from"] == today - timedelta(days=90)
    assert row["backfill_to"] == today - timedelta(days=1)


async def test_meters_that_need_no_offer_are_skipped(conn):
    """Already open, retired, an inverter (meters only, phase 1) -- none of
    them gets a row."""
    site_id, source_id = await utility_point(conn)
    already = await make_meter(conn, site_id)
    db_now = await conn.fetchval("SELECT now()")
    await make_commissioning(conn, already, source_id,
                             offered_at=db_now, offer_expires_at=db_now + timedelta(hours=1))

    other_site, _ = await utility_point(conn)
    retired = await make_meter(conn, other_site)
    await retire_device(conn, retired)
    inverter = await make_inverter(conn, other_site)

    await offer_existing(conn, today=date(2026, 9, 13))

    assert len(await offers(conn, already)) == 1
    assert await offers(conn, retired) == []
    assert await offers(conn, inverter) == []


async def test_offering_twice_offers_once(conn):
    site_id, _ = await utility_point(conn)
    device_id = await make_meter(conn, site_id)

    await offer_existing(conn, today=date(2026, 9, 13))
    await offer_existing(conn, today=date(2026, 9, 13))

    assert len(await offers(conn, device_id)) == 1


async def test_a_failed_handshake_is_offered_again(conn):
    """A lapsed or refused handshake is exactly what this script is for."""
    site_id, source_id = await utility_point(conn)
    device_id = await make_meter(conn, site_id)
    db_now = await conn.fetchval("SELECT now()")
    await make_commissioning(
        conn, device_id, source_id, status="failed",
        offered_at=db_now, offer_expires_at=db_now + timedelta(hours=1),
        ended_at=db_now, failed_reason="offer_expired",
    )

    await offer_existing(conn, today=date(2026, 9, 13))

    assert sorted(r["status"] for r in await offers(conn, device_id)) == [
        "failed", "offered"
    ]


# ---------------------------------------------------------------------------
# issue_device_keys
# ---------------------------------------------------------------------------


async def test_device_keys_skip_commissioned_devices(conn):
    """The head-end holds that key. Replacing it from a script would silently
    cut the meter off, with nothing on either side saying why."""
    site_id, source_id = await utility_point(conn)
    commissioned = await make_meter(conn, site_id)
    db_now = await conn.fetchval("SELECT now()")
    await make_commissioning(conn, commissioned, source_id,
                             offered_at=db_now, offer_expires_at=db_now + timedelta(hours=1))
    other_site, _ = await utility_point(conn)
    failed = await make_meter(conn, other_site)
    await make_commissioning(
        conn, failed, source_id, status="failed",
        offered_at=db_now, offer_expires_at=db_now + timedelta(hours=1),
        ended_at=db_now, failed_reason="offer_expired",
    )

    keyed = {r["device_id"] for r in await conn.fetch(sql("devices_needing_keys"))}

    assert commissioned not in keyed
    assert failed in keyed
