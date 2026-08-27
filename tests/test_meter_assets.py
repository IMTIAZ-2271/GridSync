"""A meter belongs to a person before it belongs to a site.

Migration c9e2f4a71b83 split "hardware the utility issued to a customer"
(`meter_asset`) from "hardware installed somewhere" (`device`), because a
household registering a meter by typing a serial number could conjure hardware
nobody owns.

What is worth testing here is not the happy path -- the live HTTP checks cover
that -- but the constraints that make the happy path safe: that one physical
meter cannot serve two positions, that a household cannot hold two live
requests for one site, and that an approval which issued nothing cannot be
recorded as having issued something.
"""
from __future__ import annotations

from datetime import datetime, timezone

import asyncpg
import pytest

from .factories import make_account, make_meter, make_site, unique_suffix

pytestmark = pytest.mark.asyncio


async def make_asset(conn, account_id: str, **overrides) -> str:
    return await conn.fetchval(
        """
        INSERT INTO meter_asset (account_id, serial_no, manufacturer, model)
        VALUES ($1, $2, $3, $4)
        RETURNING meter_asset_id
        """,
        account_id,
        overrides.pop("serial_no", f"TEST-ASSET-{unique_suffix()}"),
        overrides.pop("manufacturer", "Hexing"),
        overrides.pop("model", "HXE310-BD"),
    )


async def make_application(conn, account_id: str, site_id: str, **overrides) -> str:
    return await conn.fetchval(
        """
        INSERT INTO meter_application (account_id, site_id, reason, status,
                                       decided_at)
        VALUES ($1, $2, $3, $4::application_status, $5)
        RETURNING application_id
        """,
        account_id, site_id,
        overrides.pop("reason", "needs a connection"),
        overrides.pop("status", "submitted"),
        overrides.pop("decided_at", None),
    )


def decided_now() -> datetime:
    """A decision time the CHECK will accept.

    `meter_application_decided_after` requires decided_at >= submitted_at, and
    submitted_at defaults to now() -- which inside a transaction is the
    *transaction's* start. A module-level constant captured at import time is
    therefore in the past by the time the row is written.
    """
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# meter_asset
# ---------------------------------------------------------------------------


async def test_a_new_asset_is_available(conn):
    """`available` is derived, not stored: device_id IS NULL and nothing else."""
    owner = await make_account(conn)
    asset_id = await make_asset(conn, owner)

    assert await conn.fetchval(
        "SELECT device_id IS NULL FROM meter_asset WHERE meter_asset_id = $1",
        asset_id,
    )


async def test_one_meter_cannot_serve_two_positions(conn, savepoint):
    """The UNIQUE on device_id is the whole point of the column.

    Two assets pointing at one device would mean two households each believing
    they own the meter measuring one connection.
    """
    owner = await make_account(conn)
    site_id = await make_site(conn, owner)
    device_id = await make_meter(conn, site_id)

    await conn.execute(
        "UPDATE meter_asset SET device_id = $1 WHERE meter_asset_id = $2",
        device_id, await make_asset(conn, owner),
    )
    other = await make_asset(conn, owner)

    with pytest.raises(asyncpg.UniqueViolationError):
        async with savepoint():
            await conn.execute(
                "UPDATE meter_asset SET device_id = $1 WHERE meter_asset_id = $2",
                device_id, other,
            )


async def test_a_serial_is_claimed_once(conn, savepoint):
    owner = await make_account(conn)
    serial = f"TEST-DUP-{unique_suffix()}"
    await make_asset(conn, owner, serial_no=serial)

    with pytest.raises(asyncpg.UniqueViolationError):
        async with savepoint():
            await make_asset(conn, owner, serial_no=serial)


async def test_a_blank_serial_is_refused(conn, savepoint):
    """meter_asset_serial_present. A meter with no number is not a meter."""
    owner = await make_account(conn)

    with pytest.raises(asyncpg.CheckViolationError):
        async with savepoint():
            await make_asset(conn, owner, serial_no="   ")


async def test_removing_the_device_frees_the_asset(conn):
    """ON DELETE SET NULL, and no second column to fall out of step with it.

    This is why there is no `assigned_at`: a biconditional CHECK against one
    would be violated the moment the device row went, and deleting a site
    would start failing.
    """
    owner = await make_account(conn)
    site_id = await make_site(conn, owner)
    device_id = await make_meter(conn, site_id)
    asset_id = await make_asset(conn, owner)
    await conn.execute(
        "UPDATE meter_asset SET device_id = $1 WHERE meter_asset_id = $2",
        device_id, asset_id,
    )

    await conn.execute("DELETE FROM meter_spec WHERE device_id = $1", device_id)
    await conn.execute("DELETE FROM device WHERE device_id = $1", device_id)

    assert await conn.fetchval(
        "SELECT device_id IS NULL FROM meter_asset WHERE meter_asset_id = $1",
        asset_id,
    )


# ---------------------------------------------------------------------------
# meter_application
# ---------------------------------------------------------------------------


async def test_one_live_request_per_site(conn, savepoint):
    """meter_application_one_open.

    A household waiting on a decision must not be able to file the same request
    twice, and two officials working one district must not both issue against
    it.
    """
    owner = await make_account(conn)
    site_id = await make_site(conn, owner)
    await make_application(conn, owner, site_id)

    with pytest.raises(asyncpg.UniqueViolationError):
        async with savepoint():
            await make_application(conn, owner, site_id)


@pytest.mark.parametrize("decided", ["accepted", "rejected", "withdrawn"])
async def test_a_decided_request_lets_the_site_apply_again(conn, decided):
    """The index is partial for a reason: a refusal is not a life sentence.

    A household turned down last year, or one that withdrew and changed its
    mind, files again -- and the second visit through this table must not be
    the schema's business to forbid.
    """
    owner = await make_account(conn)
    site_id = await make_site(conn, owner)
    await make_application(conn, owner, site_id, status=decided, decided_at=decided_now())

    second = await make_application(conn, owner, site_id)

    assert second is not None


async def test_under_review_is_still_open(conn, savepoint):
    """It is a step inside the queue, not a decision, so it still blocks."""
    owner = await make_account(conn)
    site_id = await make_site(conn, owner)
    await make_application(conn, owner, site_id, status="under_review")

    with pytest.raises(asyncpg.UniqueViolationError):
        async with savepoint():
            await make_application(conn, owner, site_id)


async def test_an_open_request_cannot_carry_a_decision_time(conn, savepoint):
    """meter_application_decision_timestamps, one direction."""
    owner = await make_account(conn)
    site_id = await make_site(conn, owner)

    with pytest.raises(asyncpg.CheckViolationError):
        async with savepoint():
            await make_application(
                conn, owner, site_id, status="submitted", decided_at=decided_now()
            )


async def test_a_decided_request_must_carry_one(conn, savepoint):
    """The other direction. 'Rejected, at no particular time' is not a record."""
    owner = await make_account(conn)
    site_id = await make_site(conn, owner)

    with pytest.raises(asyncpg.CheckViolationError):
        async with savepoint():
            await make_application(
                conn, owner, site_id, status="rejected", decided_at=None
            )


async def test_only_an_acceptance_may_have_issued_a_meter(conn, savepoint):
    """meter_application_issue_on_accept.

    A rejected application holding an asset id would read as hardware handed
    out by a refusal.
    """
    owner = await make_account(conn)
    site_id = await make_site(conn, owner)
    asset_id = await make_asset(conn, owner)
    app_id = await make_application(
        conn, owner, site_id, status="rejected", decided_at=decided_now()
    )

    with pytest.raises(asyncpg.CheckViolationError):
        async with savepoint():
            await conn.execute(
                "UPDATE meter_application SET issued_meter_asset_id = $1 "
                "WHERE application_id = $2",
                asset_id, app_id,
            )


async def test_an_acceptance_may(conn):
    owner = await make_account(conn)
    site_id = await make_site(conn, owner)
    asset_id = await make_asset(conn, owner)
    app_id = await make_application(
        conn, owner, site_id, status="accepted", decided_at=decided_now()
    )

    await conn.execute(
        "UPDATE meter_application SET issued_meter_asset_id = $1 "
        "WHERE application_id = $2",
        asset_id, app_id,
    )

    assert await conn.fetchval(
        "SELECT issued_meter_asset_id FROM meter_application "
        "WHERE application_id = $1",
        app_id,
    ) == asset_id


async def test_one_asset_is_issued_by_one_application(conn, savepoint):
    """UNIQUE on issued_meter_asset_id.

    Two approvals crediting themselves with the same physical meter would make
    the audit trail from application to hardware ambiguous in exactly the case
    it exists for.
    """
    owner = await make_account(conn)
    asset_id = await make_asset(conn, owner)
    first_site = await make_site(conn, owner)
    # Two sites: meter_application_one_open would otherwise refuse the second
    # application before the UNIQUE under test could be reached.
    second_site = await make_site(conn, owner)

    app_a = await make_application(
        conn, owner, first_site, status="accepted", decided_at=decided_now()
    )
    app_b = await make_application(
        conn, owner, second_site, status="accepted", decided_at=decided_now()
    )
    await conn.execute(
        "UPDATE meter_application SET issued_meter_asset_id = $1 "
        "WHERE application_id = $2",
        asset_id, app_a,
    )

    with pytest.raises(asyncpg.UniqueViolationError):
        async with savepoint():
            await conn.execute(
                "UPDATE meter_application SET issued_meter_asset_id = $1 "
                "WHERE application_id = $2",
                asset_id, app_b,
            )


async def test_completed_is_unreachable(conn, savepoint):
    """meter_application_no_completion.

    `application_status` is shared with solar_application, where 'completed'
    means the panels are on the roof. Issuing a meter has no second act --
    acceptance IS the delivery -- so the value is excluded structurally rather
    than only by the API's Literal.
    """
    owner = await make_account(conn)
    site_id = await make_site(conn, owner)

    with pytest.raises(asyncpg.CheckViolationError):
        async with savepoint():
            await make_application(
                conn, owner, site_id, status="completed", decided_at=decided_now()
            )
