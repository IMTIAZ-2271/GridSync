"""An application is fulfilled by a visit, and the household says so.

Migration b7d3f5a92c14. Approving used to issue a meter on a click, with nobody
having been to the property; now the application runs through a work order, the
technician records the serial, and the household's confirmation is what unlocks
registration.

What is worth pinning here is not the flow -- the live HTTP checks walk that
end to end -- but the four constraints that stop it being faked: an order
cannot serve two masters, a verdict cannot precede the work, a household cannot
hold both verdicts at once, and a live visit cannot be duplicated while a
terminal one must not block the next.
"""
from __future__ import annotations

from datetime import datetime, timezone

import asyncpg
import pytest

from .factories import make_account, make_site, make_work_order, unique_suffix
from .test_meter_assets import make_application

pytestmark = pytest.mark.asyncio


async def make_issue(conn, site_id: str, reporter: str) -> str:
    return await conn.fetchval(
        """
        INSERT INTO issue (reported_by_account_id, site_id, category, severity,
                           title, priority)
        VALUES ($1, $2, 'outage', 'high', 'Power out', 2)
        RETURNING issue_id
        """,
        reporter, site_id,
    )


async def make_agreement(conn, site_id: str) -> str:
    """A pending agreement on the site's own billing point."""
    point_id = await conn.fetchval(
        "SELECT point_id FROM billing_point WHERE site_id = $1 LIMIT 1", site_id
    )
    device_id = await conn.fetchval(
        """
        INSERT INTO device (site_id, device_type, serial_no, device_key_hash)
        VALUES ($1, 'meter', $2, 'not-a-real-hash')
        RETURNING device_id
        """,
        site_id, f"TEST-NMA-METER-{unique_suffix()}",
    )
    await conn.execute(
        """
        INSERT INTO meter_spec (device_id, site_id, billing_point_id,
                                meter_flow, billing_role)
        VALUES ($1, $2, $3, 'bidirectional', 'billing')
        """,
        device_id, site_id, point_id,
    )
    return await conn.fetchval(
        """
        INSERT INTO net_metering_agreement (
            site_id, billing_point_id, billing_device_id, approval_ref,
            sanctioned_capacity_kw, effective_from, status
        )
        VALUES ($1, $2, $3, $4, 3.000, CURRENT_DATE, 'pending')
        RETURNING agreement_id
        """,
        site_id, point_id, device_id, f"TEST-NMA-{unique_suffix()}",
    )


async def complete(conn, order_id: str, serial: str = "TEST-FITTED-01") -> None:
    await conn.execute(
        "UPDATE work_order SET status = 'completed', completed_at = now(), "
        "installed_serial_no = $2 WHERE order_id = $1",
        order_id, serial,
    )


# ---------------------------------------------------------------------------
# order_single_origin
# ---------------------------------------------------------------------------


async def test_an_order_serves_one_master(conn, savepoint):
    """A visit answers a complaint, or fulfils an application. Never both.

    The pairing is the entire audit trail behind "this visit happened because
    of that", and a row carrying two of them cannot be read either way.
    """
    owner = await make_account(conn)
    site_id = await make_site(conn, owner)
    issue_id = await make_issue(conn, site_id, owner)
    app_id = await make_application(conn, owner, site_id)
    order_id = await make_work_order(conn, site_id, issue_id=issue_id)

    with pytest.raises(asyncpg.CheckViolationError):
        async with savepoint():
            await conn.execute(
                "UPDATE work_order SET meter_application_id = $2 "
                "WHERE order_id = $1",
                order_id, app_id,
            )


async def test_an_order_may_carry_no_origin_at_all(conn):
    """A visit raised straight against a site is still a visit.

    `POST /api/work-orders` takes either an issue or a site, so this is the
    ordinary shape of a scheduled inspection nobody complained about.
    """
    owner = await make_account(conn)
    site_id = await make_site(conn, owner)

    assert await make_work_order(conn, site_id) is not None


# ---------------------------------------------------------------------------
# The household's verdict
# ---------------------------------------------------------------------------


async def test_a_verdict_needs_work_to_have_finished(conn, savepoint):
    """order_verdict_after_completion.

    Confirming a visit nobody has completed is not a statement about the
    world -- and it is what `register` is guarded on, so it has to mean
    something.
    """
    owner = await make_account(conn)
    site_id = await make_site(conn, owner)
    order_id = await make_work_order(conn, site_id, status="in_progress")

    with pytest.raises(asyncpg.CheckViolationError):
        async with savepoint():
            await conn.execute(
                "UPDATE work_order SET consumer_confirmed_at = now() "
                "WHERE order_id = $1",
                order_id,
            )


async def test_a_completed_visit_can_be_confirmed(conn):
    owner = await make_account(conn)
    site_id = await make_site(conn, owner)
    order_id = await make_work_order(conn, site_id)
    await complete(conn, order_id)

    await conn.execute(
        "UPDATE work_order SET consumer_confirmed_at = now() WHERE order_id = $1",
        order_id,
    )

    assert await conn.fetchval(
        "SELECT consumer_confirmed_at IS NOT NULL FROM work_order "
        "WHERE order_id = $1",
        order_id,
    )


async def test_a_household_holds_one_verdict(conn, savepoint):
    """order_one_verdict. "It worked and it did not" is not a state."""
    owner = await make_account(conn)
    site_id = await make_site(conn, owner)
    order_id = await make_work_order(conn, site_id)
    await complete(conn, order_id)

    with pytest.raises(asyncpg.CheckViolationError):
        async with savepoint():
            await conn.execute(
                "UPDATE work_order SET consumer_confirmed_at = now(), "
                "consumer_disputed_at = now() WHERE order_id = $1",
                order_id,
            )


async def test_a_verdict_can_be_changed_from_one_to_the_other(conn):
    """set_order_verdict writes both columns together, so a household that
    confirms by mistake and then disputes lands in a legal state rather than
    tripping order_one_verdict."""
    owner = await make_account(conn)
    site_id = await make_site(conn, owner)
    order_id = await make_work_order(conn, site_id)
    await complete(conn, order_id)

    for confirmed in (True, False, True):
        await conn.execute(
            "UPDATE work_order SET "
            "  consumer_confirmed_at = CASE WHEN $2 THEN now() ELSE NULL END, "
            "  consumer_disputed_at  = CASE WHEN $2 THEN NULL ELSE now() END "
            "WHERE order_id = $1",
            order_id, confirmed,
        )

    assert await conn.fetchval(
        "SELECT consumer_confirmed_at IS NOT NULL AND consumer_disputed_at IS NULL "
        "FROM work_order WHERE order_id = $1",
        order_id,
    )


async def test_a_blank_serial_is_refused(conn, savepoint):
    """order_serial_present. An install completed under an empty string would
    pass the API's own check and give the official nothing to register."""
    owner = await make_account(conn)
    site_id = await make_site(conn, owner)
    order_id = await make_work_order(conn, site_id)

    with pytest.raises(asyncpg.CheckViolationError):
        async with savepoint():
            await conn.execute(
                "UPDATE work_order SET installed_serial_no = '   ' "
                "WHERE order_id = $1",
                order_id,
            )


# ---------------------------------------------------------------------------
# One LIVE visit per application
# ---------------------------------------------------------------------------


async def test_one_live_visit_per_meter_application(conn, savepoint):
    """The race two officials working one district can produce."""
    owner = await make_account(conn)
    site_id = await make_site(conn, owner)
    app_id = await make_application(conn, owner, site_id)
    await make_work_order(conn, site_id, meter_application_id=app_id)

    with pytest.raises(asyncpg.UniqueViolationError):
        async with savepoint():
            await make_work_order(conn, site_id, meter_application_id=app_id)


@pytest.mark.parametrize("terminal", ["completed", "cancelled", "failed"])
async def test_a_terminal_visit_does_not_block_the_next(conn, terminal):
    """The failure branch the flow depends on.

    A technician who could not get access leaves the household waiting on the
    office to send someone else. An index forbidding the second visit would
    make that impossible -- the same lesson a1c4e8b70d3f learned about issues.
    """
    owner = await make_account(conn)
    site_id = await make_site(conn, owner)
    app_id = await make_application(conn, owner, site_id)
    await make_work_order(
        conn, site_id, status=terminal, meter_application_id=app_id
    )

    second = await make_work_order(conn, site_id, meter_application_id=app_id)

    assert second is not None


async def test_one_live_inspection_per_agreement(conn, savepoint):
    owner = await make_account(conn)
    site_id = await make_site(conn, owner)
    agreement_id = await make_agreement(conn, site_id)
    await make_work_order(conn, site_id, agreement_id=agreement_id)

    with pytest.raises(asyncpg.UniqueViolationError):
        async with savepoint():
            await make_work_order(conn, site_id, agreement_id=agreement_id)


async def test_a_failed_inspection_does_not_block_the_next(conn):
    owner = await make_account(conn)
    site_id = await make_site(conn, owner)
    agreement_id = await make_agreement(conn, site_id)
    await make_work_order(
        conn, site_id, status="failed", agreement_id=agreement_id
    )

    assert await make_work_order(conn, site_id, agreement_id=agreement_id)


async def test_orders_without_an_application_do_not_collide(conn):
    """The partial indexes are keyed on a nullable column.

    Every ordinary work order carries NULL in both, and NULLs do not collide in
    a unique index -- but the predicate says so explicitly too, because relying
    on that is the kind of thing that is true until somebody adds NULLS NOT
    DISTINCT.
    """
    owner = await make_account(conn)
    site_id = await make_site(conn, owner)

    first = await make_work_order(conn, site_id)
    second = await make_work_order(conn, site_id)

    assert first != second


# ---------------------------------------------------------------------------
# The swap the net-metering flow ends in
# ---------------------------------------------------------------------------


async def test_a_meter_swap_keeps_rule_7(conn, commit_check):
    """Retire the old billing meter, attach the new one, and COMMIT is happy.

    Rule 7 allows exactly one ACTIVE billing meter per point and is enforced by
    DEFERRED triggers -- which is the whole reason a swap is expressible. The
    point holds two meter_spec rows afterwards; only one belongs to a device
    that has not been removed.
    """
    from .factories import make_meter

    owner = await make_account(conn)
    site_id = await make_site(conn, owner)
    point_id = await conn.fetchval(
        "SELECT point_id FROM billing_point WHERE site_id = $1 LIMIT 1", site_id
    )
    old = await make_meter(conn, site_id, billing_point_id=point_id)

    await conn.execute(
        "UPDATE device SET removed_at = now(), status = 'removed' "
        "WHERE device_id = $1",
        old,
    )
    new = await make_meter(conn, site_id, billing_point_id=point_id)

    await commit_check()

    assert await conn.fetchval(
        "SELECT count(*) FROM meter_spec WHERE billing_point_id = $1", point_id
    ) == 2
    assert await conn.fetchval(
        "SELECT ms.device_id FROM meter_spec ms JOIN device d "
        "  ON d.device_id = ms.device_id "
        "WHERE ms.billing_point_id = $1 AND d.removed_at IS NULL",
        point_id,
    ) == new


async def test_swapping_without_retiring_the_old_one_is_refused(
    conn, commit_check, savepoint
):
    """The half of the swap that must not be skippable.

    Two live billing meters on one point is exactly what rule 7 forbids, and
    because the triggers are deferred it is only caught at COMMIT -- which is
    what `commit_check` forces here.
    """
    from .factories import make_meter

    owner = await make_account(conn)
    site_id = await make_site(conn, owner)
    point_id = await conn.fetchval(
        "SELECT point_id FROM billing_point WHERE site_id = $1 LIMIT 1", site_id
    )
    await make_meter(conn, site_id, billing_point_id=point_id)
    await make_meter(conn, site_id, billing_point_id=point_id)

    with pytest.raises(asyncpg.PostgresError):
        async with savepoint():
            await commit_check()
