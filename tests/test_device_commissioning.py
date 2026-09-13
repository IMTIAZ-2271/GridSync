"""A meter is commissioned by a handshake with its utility's head-end.

Migration f2a9c4e1b7d6 added `telemetry_source` (one head-end per distribution
company) and `device_commissioning` (one handshake attempt per row). The flow:

    offered    -> the head-end has been told a meter exists
    activated  -> it proved it holds that meter and was handed a device key
    live       -> a batch signed with that key has actually been accepted
    failed     -> an offer or an activation lapsed, the head-end rejected it,
                  or no head-end serves the utility at all
    cancelled  -> the device was retired while the handshake was open or live

What is worth testing is the part a careful endpoint cannot guarantee on its
own: that two tabs or two retries cannot open two handshakes for one meter,
that a failed one can be retried, and that the status can never disagree with
the timestamps that are its evidence.
"""
from __future__ import annotations

from datetime import date, timedelta

import asyncpg
import pytest

from .factories import (
    COMMISSIONING_T0 as T0,
    make_commissioning,
    make_distribution_company as make_company,
    make_meter,
    make_site,
    make_telemetry_source as make_source,
    retire_device,
)

pytestmark = pytest.mark.asyncio


#: The columns that describe an activation, for tests that need one.
ACTIVATED = dict(
    status="activated",
    activated_at=T0 + timedelta(minutes=5),
    activation_expires_at=T0 + timedelta(hours=1, minutes=5),
    activation_count=1,
)
LIVE = dict(ACTIVATED, status="live", live_at=T0 + timedelta(minutes=10))


async def meter_and_source(conn) -> tuple[str, str]:
    site_id = await make_site(conn)
    return await make_meter(conn, site_id), await make_source(conn)


# ---------------------------------------------------------------------------
# telemetry_source
# ---------------------------------------------------------------------------


async def test_one_head_end_per_utility(conn, savepoint):
    """Two sources for one company would mean two systems each believing they
    own that utility's meters, and an offer could be claimed by either."""
    company_id = await make_company(conn)
    await make_source(conn, company_id)

    with pytest.raises(asyncpg.UniqueViolationError):
        async with savepoint():
            await make_source(conn, company_id)


async def test_a_source_needs_a_name(conn, savepoint):
    with pytest.raises(asyncpg.CheckViolationError):
        async with savepoint():
            await make_source(conn, name="   ")


async def test_a_source_with_history_cannot_be_deleted(conn, savepoint):
    """RESTRICT, not CASCADE: deleting a head-end must not erase the record of
    which meters it commissioned. Disable it instead.

    RESTRICT raises its own SQLSTATE (23001), not the foreign-key one."""
    device_id, source_id = await meter_and_source(conn)
    await make_commissioning(conn, device_id, source_id)

    with pytest.raises(asyncpg.RestrictViolationError):
        async with savepoint():
            await conn.execute(
                "DELETE FROM telemetry_source WHERE source_id = $1", source_id
            )


# ---------------------------------------------------------------------------
# one open handshake per device
# ---------------------------------------------------------------------------


async def test_an_offer_is_legal(conn):
    device_id, source_id = await meter_and_source(conn)
    commissioning_id = await make_commissioning(conn, device_id, source_id)

    row = await conn.fetchrow(
        "SELECT status::text, updated_at FROM device_commissioning "
        "WHERE commissioning_id = $1",
        commissioning_id,
    )
    assert row["status"] == "offered"
    assert row["updated_at"] is not None


@pytest.mark.parametrize("state", ["offered", "activated", "live"])
async def test_a_second_open_handshake_is_refused(conn, savepoint, state):
    """Rule 4. Two tabs installing one meter, or a retry racing the original,
    must produce one handshake -- otherwise the head-end could be handed two
    keys for one meter and only one of them would ever work."""
    device_id, source_id = await meter_and_source(conn)
    first = {"offered": {}, "activated": ACTIVATED, "live": LIVE}[state]
    await make_commissioning(conn, device_id, source_id, **first)

    with pytest.raises(asyncpg.UniqueViolationError):
        async with savepoint():
            await make_commissioning(conn, device_id, source_id)


async def test_a_failed_handshake_can_be_retried(conn):
    """The unique index covers OPEN handshakes only. A head-end that was down
    when the offer lapsed must not leave the meter uncommissionable forever."""
    device_id, source_id = await meter_and_source(conn)
    await make_commissioning(
        conn, device_id, source_id,
        status="failed", ended_at=T0 + timedelta(hours=24),
        failed_reason="offer_expired",
    )

    await make_commissioning(conn, device_id, source_id)

    assert await conn.fetchval(
        "SELECT count(*) FROM device_commissioning WHERE device_id = $1", device_id
    ) == 2


async def test_a_cancelled_handshake_frees_the_device(conn):
    device_id, source_id = await meter_and_source(conn)
    await make_commissioning(
        conn, device_id, source_id, **dict(LIVE, status="cancelled",
                                           ended_at=T0 + timedelta(days=3)),
    )

    await make_commissioning(conn, device_id, source_id)


async def test_deleting_a_device_removes_its_handshakes(conn):
    """CASCADE: a handshake is a fact about a device and means nothing without
    one. Devices are retired, not deleted, in normal operation -- this is the
    reset script's path."""
    device_id, source_id = await meter_and_source(conn)
    await make_commissioning(conn, device_id, source_id)

    await conn.execute("DELETE FROM meter_spec WHERE device_id = $1", device_id)
    await conn.execute("DELETE FROM device WHERE device_id = $1", device_id)

    assert await conn.fetchval(
        "SELECT count(*) FROM device_commissioning WHERE device_id = $1", device_id
    ) == 0


async def test_retiring_a_device_does_not_touch_its_handshake(conn):
    """Cancelling on retirement is the API's job (it knows why the meter left).
    The schema only has to keep the row, which it does."""
    device_id, source_id = await meter_and_source(conn)
    commissioning_id = await make_commissioning(conn, device_id, source_id, **LIVE)

    await retire_device(conn, device_id)

    assert await conn.fetchval(
        "SELECT status::text FROM device_commissioning WHERE commissioning_id = $1",
        commissioning_id,
    ) == "live"


# ---------------------------------------------------------------------------
# no head-end serves this utility
# ---------------------------------------------------------------------------


async def test_no_source_is_recorded_as_a_visible_failure(conn):
    """A meter whose utility runs no head-end is not silently skipped: the row
    exists, so the equipment page can say why it will never report."""
    device_id = await make_meter(conn, await make_site(conn))

    await make_commissioning(
        conn, device_id, None,
        status="failed", ended_at=T0, failed_reason="no_source",
    )


async def test_an_open_handshake_needs_a_source(conn, savepoint):
    device_id = await make_meter(conn, await make_site(conn))

    with pytest.raises(asyncpg.CheckViolationError):
        async with savepoint():
            await make_commissioning(conn, device_id, None)


async def test_no_source_cannot_name_a_source(conn, savepoint):
    """The reason and the column must agree, or the equipment page would say
    "no head-end serves this utility" about a meter that has one."""
    device_id, source_id = await meter_and_source(conn)

    with pytest.raises(asyncpg.CheckViolationError):
        async with savepoint():
            await make_commissioning(
                conn, device_id, source_id,
                status="failed", ended_at=T0, failed_reason="no_source",
            )


# ---------------------------------------------------------------------------
# status agrees with its evidence
# ---------------------------------------------------------------------------


async def test_activated_needs_its_timestamps(conn, savepoint):
    device_id, source_id = await meter_and_source(conn)

    with pytest.raises(asyncpg.CheckViolationError):
        async with savepoint():
            await make_commissioning(
                conn, device_id, source_id, status="activated"
            )


async def test_an_activation_carries_its_deadline(conn, savepoint):
    """activated_at and activation_expires_at travel together: an activation
    with no deadline is one the sweep can never fail."""
    device_id, source_id = await meter_and_source(conn)

    with pytest.raises(asyncpg.CheckViolationError):
        async with savepoint():
            await make_commissioning(
                conn, device_id, source_id,
                **dict(ACTIVATED, activation_expires_at=None),
            )


async def test_activation_count_matches_activation(conn, savepoint):
    """A key was minted exactly when activated_at is set. A count of zero with
    an activation stamped is a key nobody can account for."""
    device_id, source_id = await meter_and_source(conn)

    with pytest.raises(asyncpg.CheckViolationError):
        async with savepoint():
            await make_commissioning(
                conn, device_id, source_id, **dict(ACTIVATED, activation_count=0)
            )


async def test_retried_activations_are_counted(conn):
    device_id, source_id = await meter_and_source(conn)
    await make_commissioning(
        conn, device_id, source_id, **dict(ACTIVATED, activation_count=3)
    )


async def test_live_needs_live_at(conn, savepoint):
    device_id, source_id = await meter_and_source(conn)

    with pytest.raises(asyncpg.CheckViolationError):
        async with savepoint():
            await make_commissioning(
                conn, device_id, source_id, **dict(LIVE, live_at=None)
            )


async def test_cannot_go_live_without_activating(conn, savepoint):
    """Live means a batch signed with the minted key was accepted. With no
    activation there was no key to sign it with."""
    device_id, source_id = await meter_and_source(conn)

    with pytest.raises(asyncpg.CheckViolationError):
        async with savepoint():
            await make_commissioning(
                conn, device_id, source_id,
                status="live", live_at=T0 + timedelta(minutes=10),
            )


async def test_cannot_go_live_before_activating(conn, savepoint):
    device_id, source_id = await meter_and_source(conn)

    with pytest.raises(asyncpg.CheckViolationError):
        async with savepoint():
            await make_commissioning(
                conn, device_id, source_id,
                **dict(LIVE, live_at=T0 + timedelta(minutes=1)),
            )


async def test_cannot_activate_before_the_offer(conn, savepoint):
    device_id, source_id = await meter_and_source(conn)

    with pytest.raises(asyncpg.CheckViolationError):
        async with savepoint():
            await make_commissioning(
                conn, device_id, source_id,
                **dict(ACTIVATED, activated_at=T0 - timedelta(minutes=1)),
            )


async def test_an_offer_expires_after_it_is_made(conn, savepoint):
    device_id, source_id = await meter_and_source(conn)

    with pytest.raises(asyncpg.CheckViolationError):
        async with savepoint():
            await make_commissioning(
                conn, device_id, source_id, offer_expires_at=T0
            )


async def test_failed_needs_a_reason(conn, savepoint):
    device_id, source_id = await meter_and_source(conn)

    with pytest.raises(asyncpg.CheckViolationError):
        async with savepoint():
            await make_commissioning(
                conn, device_id, source_id, status="failed", ended_at=T0
            )


async def test_only_a_failure_has_a_reason(conn, savepoint):
    device_id, source_id = await meter_and_source(conn)

    with pytest.raises(asyncpg.CheckViolationError):
        async with savepoint():
            await make_commissioning(
                conn, device_id, source_id, failed_reason="offer_expired"
            )


@pytest.mark.parametrize("state", ["failed", "cancelled"])
async def test_an_ended_handshake_says_when(conn, savepoint, state):
    device_id, source_id = await meter_and_source(conn)
    reason = "offer_expired" if state == "failed" else None

    with pytest.raises(asyncpg.CheckViolationError):
        async with savepoint():
            await make_commissioning(
                conn, device_id, source_id, status=state, failed_reason=reason
            )


async def test_an_open_handshake_has_not_ended(conn, savepoint):
    device_id, source_id = await meter_and_source(conn)

    with pytest.raises(asyncpg.CheckViolationError):
        async with savepoint():
            await make_commissioning(conn, device_id, source_id, ended_at=T0)


# ---------------------------------------------------------------------------
# the history window
# ---------------------------------------------------------------------------


async def test_backfill_window_is_both_or_neither(conn, savepoint):
    """NULL on both means "upload nothing" -- a swapped-in meter whose
    predecessor already covers every day. One without the other means nothing."""
    device_id, source_id = await meter_and_source(conn)

    with pytest.raises(asyncpg.CheckViolationError):
        async with savepoint():
            await make_commissioning(
                conn, device_id, source_id, backfill_from=date(2026, 6, 1)
            )


async def test_backfill_window_is_ordered(conn, savepoint):
    device_id, source_id = await meter_and_source(conn)

    with pytest.raises(asyncpg.CheckViolationError):
        async with savepoint():
            await make_commissioning(
                conn, device_id, source_id,
                backfill_from=date(2026, 8, 31), backfill_to=date(2026, 6, 1),
            )


async def test_a_one_day_window_is_legal(conn):
    device_id, source_id = await meter_and_source(conn)
    await make_commissioning(
        conn, device_id, source_id,
        backfill_from=date(2026, 8, 31), backfill_to=date(2026, 8, 31),
    )


# ---------------------------------------------------------------------------
# rows say when they changed
# ---------------------------------------------------------------------------


async def test_a_status_change_moves_updated_at(conn):
    """The shared touch_updated_at() trigger from migration e8b1d3f70a26, so
    the equipment list can light a handshake that changed, not only a new one."""
    device_id, source_id = await meter_and_source(conn)
    commissioning_id = await make_commissioning(conn, device_id, source_id)
    before = await conn.fetchval(
        "SELECT updated_at FROM device_commissioning WHERE commissioning_id = $1",
        commissioning_id,
    )

    after = await conn.fetchval(
        """
        UPDATE device_commissioning
        SET status = 'activated', activated_at = $2,
            activation_expires_at = $3, activation_count = 1
        WHERE commissioning_id = $1
        RETURNING updated_at
        """,
        commissioning_id, T0 + timedelta(minutes=5), T0 + timedelta(hours=1),
    )
    assert after > before
