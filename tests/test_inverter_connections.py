"""An inverter is its own connection, not something hanging off a meter.

Migration d4f8a2c61e95 moved the link that says which connection a generation
reading belongs to off `device.parent_device_id` and onto
`inverter_spec.billing_point_id`, so that panels can exist before a meter does
and so that a household choosing an inverter for net metering is choosing
something separate from the meter it will give up.

What is worth testing is not that an inverter can be inserted -- the live HTTP
checks cover that -- but the four things that make the new shape safe:

  * an inverter with no connection is legal, because that is what freshly
    installed panels are;
  * it cannot be pointed at a connection on somebody else's site;
  * retiring a connection releases the panels rather than deleting them or
    blocking the delete;
  * an agreement pins the inverter it was granted for, so the hardware behind
    a decision cannot be deleted out from under it.

Plus the two guards that stop the attach step being replayed into a lie.
"""
from __future__ import annotations

import asyncpg
import pytest

from services.api.queries import sql

from .factories import (
    make_account,
    make_inverter,
    make_meter,
    make_site,
    unique_suffix,
)

pytestmark = pytest.mark.asyncio


async def point_of(conn: asyncpg.Connection, site_id: str) -> str:
    """The 'Main' billing point make_site mints, as POST /api/sites does."""
    return await conn.fetchval(
        "SELECT point_id FROM billing_point WHERE site_id = $1", site_id
    )


async def make_agreement(conn, site_id, point_id, meter_id, inverter_id=None):
    return await conn.fetchval(
        """
        INSERT INTO net_metering_agreement (
            site_id, billing_point_id, billing_device_id, inverter_device_id,
            approval_ref, sanctioned_capacity_kw, effective_from, status
        )
        VALUES ($1, $2, $3, $4, $5, 5.000, CURRENT_DATE, 'pending')
        RETURNING agreement_id
        """,
        site_id, point_id, meter_id, inverter_id,
        f"TEST-NMA-{unique_suffix()}",
    )


# ---------------------------------------------------------------------------
# Panels before a meter
# ---------------------------------------------------------------------------

async def test_an_inverter_needs_no_connection(conn):
    """The ordinary state of a fresh installation.

    Panels are fitted by a private installer; a billing meter is issued by the
    distribution company. Requiring one before the other made the real-world
    sequence impossible to record.
    """
    site_id = await make_site(conn)
    inverter_id = await make_inverter(conn, site_id)

    point = await conn.fetchval(
        "SELECT billing_point_id FROM inverter_spec WHERE device_id = $1",
        inverter_id,
    )
    assert point is None


async def test_an_inverter_can_exist_on_a_site_with_no_meter_at_all(conn):
    """Not merely unattached -- there is nothing on the site to attach to."""
    site_id = await make_site(conn)
    await make_inverter(conn, site_id)

    meters = await conn.fetchval(
        "SELECT count(*) FROM meter_spec WHERE site_id = $1", site_id
    )
    assert meters == 0


# ---------------------------------------------------------------------------
# It cannot point at somebody else's connection
# ---------------------------------------------------------------------------

async def test_an_inverter_cannot_join_another_sites_connection(conn):
    """`inverter_spec_point_fk` is composite on (billing_point_id, site_id).

    That redundant-looking site_id is the whole mechanism: without it the FK
    would accept any point_id at all, and a household's panels could be
    credited against a connection at another address.
    """
    site_a = await make_site(conn)
    site_b = await make_site(conn)
    inverter_id = await make_inverter(conn, site_a)
    other_point = await point_of(conn, site_b)

    with pytest.raises(asyncpg.ForeignKeyViolationError):
        await conn.execute(
            "UPDATE inverter_spec SET billing_point_id = $2 WHERE device_id = $1",
            inverter_id, other_point,
        )


async def test_an_inverter_may_join_its_own_sites_connection(conn):
    """The same UPDATE, on the right site, is fine -- so the test above is
    testing the composite key and not merely that the UPDATE fails."""
    site_id = await make_site(conn)
    point_id = await point_of(conn, site_id)
    inverter_id = await make_inverter(conn, site_id)

    await conn.execute(
        "UPDATE inverter_spec SET billing_point_id = $2 WHERE device_id = $1",
        inverter_id, point_id,
    )
    assert await conn.fetchval(
        "SELECT billing_point_id FROM inverter_spec WHERE device_id = $1",
        inverter_id,
    ) == point_id


# ---------------------------------------------------------------------------
# Retiring a connection releases the panels
# ---------------------------------------------------------------------------

async def test_deleting_a_connection_releases_the_inverter(conn):
    """ON DELETE SET NULL, not RESTRICT and not CASCADE.

    RESTRICT would let an inverter block a connection from being removed;
    CASCADE would delete panels that are still on the roof and still
    generating. Belonging to no connection is a state this column already has
    to represent, so it is the right landing place.

    This test earned its place immediately: the first cut wrote a bare
    `ON DELETE SET NULL` on a COMPOSITE key, which nulls every column in it --
    including the NOT NULL `site_id` -- so deleting a connection failed with a
    not-null violation rather than releasing the inverter. The fix is
    `SET NULL (billing_point_id)`.
    """
    site_id = await make_site(conn)
    point_id = await point_of(conn, site_id)
    inverter_id = await make_inverter(conn, site_id, billing_point_id=point_id)

    await conn.execute("DELETE FROM billing_point WHERE point_id = $1", point_id)

    row = await conn.fetchrow(
        "SELECT billing_point_id FROM inverter_spec WHERE device_id = $1",
        inverter_id,
    )
    assert row is not None, "the inverter must survive its connection"
    assert row["billing_point_id"] is None


# ---------------------------------------------------------------------------
# An agreement pins its hardware
# ---------------------------------------------------------------------------

async def test_an_agreement_blocks_deleting_the_inverter_it_named(conn):
    """RESTRICT on `net_metering_agreement.inverter_device_id`.

    The agreement is a decision about that specific hardware -- 30 days of its
    generation against the property's consumption -- and the figures behind it
    are only re-checkable while it exists.
    """
    site_id = await make_site(conn)
    point_id = await point_of(conn, site_id)
    meter_id = await make_meter(conn, site_id, billing_point_id=point_id)
    inverter_id = await make_inverter(conn, site_id, billing_point_id=point_id)
    await make_agreement(conn, site_id, point_id, meter_id, inverter_id)

    # RestrictViolationError (SQLSTATE 23001), not ForeignKeyViolationError
    # (23503): RESTRICT and a plain FK failure are different states, and
    # asyncpg models them as sibling classes rather than one inheriting the
    # other.
    with pytest.raises(asyncpg.RestrictViolationError):
        await conn.execute(
            "DELETE FROM inverter_spec WHERE device_id = $1", inverter_id
        )


async def test_an_agreement_may_name_no_inverter(conn):
    """Nullable, because every agreement predating the migration was granted
    without a production test, and backfilling one would be a lie in the
    data."""
    site_id = await make_site(conn)
    point_id = await point_of(conn, site_id)
    meter_id = await make_meter(conn, site_id, billing_point_id=point_id)

    agreement_id = await make_agreement(conn, site_id, point_id, meter_id, None)
    assert agreement_id is not None


# ---------------------------------------------------------------------------
# attach_inverter_to_point: guarded, so a replay cannot move panels
# ---------------------------------------------------------------------------

async def test_attaching_an_inverter_is_guarded_on_it_being_free(conn):
    """The UPDATE matches only `billing_point_id IS NULL`.

    Registering a swapped meter runs this. Without the guard, re-running a
    registration -- or running one on a household's second connection -- would
    silently move panels off the connection they already serve, and the credit
    for their export with them.
    """
    site_id = await make_site(conn)
    first = await point_of(conn, site_id)
    second = await conn.fetchval(
        "INSERT INTO billing_point (site_id, label) VALUES ($1, 'Shop') "
        "RETURNING point_id",
        site_id,
    )
    inverter_id = await make_inverter(conn, site_id)

    claimed = await conn.fetchval(
        sql("attach_inverter_to_point"), inverter_id, first
    )
    assert claimed == inverter_id

    # A second attempt, at a different connection, must do nothing.
    again = await conn.fetchval(
        sql("attach_inverter_to_point"), inverter_id, second
    )
    assert again is None
    assert await conn.fetchval(
        "SELECT billing_point_id FROM inverter_spec WHERE device_id = $1",
        inverter_id,
    ) == first


# ---------------------------------------------------------------------------
# The swap candidates
# ---------------------------------------------------------------------------

async def test_only_unidirectional_meters_are_offered_for_swapping(conn):
    """A bidirectional meter has already been swapped, or was built that way.

    Offering it would hand the household a choice the application endpoint
    then refuses, which reads as a broken button.
    """
    site_id = await make_site(conn)
    point_id = await point_of(conn, site_id)
    await make_meter(
        conn, site_id, meter_flow="bidirectional", billing_point_id=point_id
    )

    rows = await conn.fetch(sql("swappable_meters_for_site"), site_id)
    assert rows == []

    other_site = await make_site(conn)
    other_point = await point_of(conn, other_site)
    await make_meter(
        conn, other_site, meter_flow="unidirectional", billing_point_id=other_point
    )
    rows = await conn.fetch(sql("swappable_meters_for_site"), other_site)
    assert [r["billing_point_id"] for r in rows] == [other_point]


async def test_a_connection_with_a_live_agreement_is_not_offered(conn):
    """nma_no_overlap would refuse a second application anyway; filtering here
    is what keeps the household from being offered it in the first place."""
    site_id = await make_site(conn)
    point_id = await point_of(conn, site_id)
    meter_id = await make_meter(
        conn, site_id, meter_flow="unidirectional", billing_point_id=point_id
    )

    assert len(await conn.fetch(sql("swappable_meters_for_site"), site_id)) == 1

    await make_agreement(conn, site_id, point_id, meter_id)
    assert await conn.fetch(sql("swappable_meters_for_site"), site_id) == []


# ---------------------------------------------------------------------------
# Read/unread watermarks
# ---------------------------------------------------------------------------

async def test_marking_a_view_seen_returns_the_watermark_it_replaced(conn):
    """The whole mechanism of the highlight.

    A page marks itself seen on open and lights the rows newer than the value
    it gets back. A first-ever visit gets NULL, which the client reads as
    "every row is new" -- correct, and why a first visit lights up.
    """
    account_id = await make_account(conn)

    first = await conn.fetchrow(
        sql("mark_view_seen"), account_id, "consumer:applications"
    )
    assert first["previous_viewed_at"] is None
    assert first["last_viewed_at"] is not None

    second = await conn.fetchrow(
        sql("mark_view_seen"), account_id, "consumer:applications"
    )
    assert second["previous_viewed_at"] == first["last_viewed_at"], (
        "the second visit must be handed the first visit's timestamp, not its own"
    )


async def test_view_watermarks_are_per_list(conn):
    """One row per (account, list). Opening the bills page must not clear the
    indicator on applications."""
    account_id = await make_account(conn)
    await conn.fetchrow(sql("mark_view_seen"), account_id, "consumer:bills")

    rows = await conn.fetch(sql("list_view_states"), account_id)
    assert [r["view_key"] for r in rows] == ["consumer:bills"]
