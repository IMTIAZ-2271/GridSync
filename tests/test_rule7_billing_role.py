"""Rule 7: exactly one device per site has meter_spec.billing_role = 'billing'.

Enforced solely by the deferred constraint triggers
`meter_spec_one_active_billing` and `device_one_active_billing` (migration
0b24bc6b5a1f). The partial unique index that used to share the job was dropped
there because it could not see `device.removed_at` and, being an index rather
than a deferrable constraint, rejected legal mid-transaction states.

`test_meter_swap_*` and `test_duplicate_is_caught_at_commit_not_on_insert` are
the regression guards for that: they fail loudly if immediate enforcement is
reintroduced. See the module-level note above each.
"""
import asyncpg
import pytest

from tests.factories import make_meter, make_site, retire_device

# Raised by a unique index or a non-deferred unique constraint, i.e. exactly
# what rule 7 must NOT be enforced by.
IMMEDIATE_ENFORCEMENT = asyncpg.exceptions.UniqueViolationError

RULE_7_ERROR = "active billing meters"


async def active_billing_count(conn, site_id) -> int:
    return await conn.fetchval(
        """
        SELECT count(*) FROM meter_spec ms
        JOIN device d ON d.device_id = ms.device_id
        WHERE ms.site_id = $1 AND ms.billing_role = 'billing'
          AND d.removed_at IS NULL
        """,
        site_id,
    )


# --------------------------------------------------------------------------
# A. the shape that must be accepted
# --------------------------------------------------------------------------
async def test_one_active_billing_meter_is_accepted(conn, commit_check):
    site = await make_site(conn)
    await make_meter(conn, site, billing_role="billing")

    await commit_check()

    assert await active_billing_count(conn, site) == 1


# --------------------------------------------------------------------------
# B. too few
# --------------------------------------------------------------------------
async def test_site_without_a_billing_meter_is_refused(conn, commit_check):
    site = await make_site(conn)
    await make_meter(conn, site, billing_role="generation_only",
                     meter_flow="unidirectional")

    with pytest.raises(asyncpg.exceptions.CheckViolationError) as caught:
        await commit_check()

    assert "0 " + RULE_7_ERROR in str(caught.value)


# --------------------------------------------------------------------------
# C. too many -- and, critically, *when* that is noticed
# --------------------------------------------------------------------------
async def test_duplicate_is_caught_at_commit_not_on_insert(
    conn, commit_check, savepoint
):
    """Two billing meters must be refused, by the trigger, at commit time.

    The timing is the assertion. If a unique index or non-deferred constraint
    is reintroduced, the INSERT itself raises and this test fails -- which is
    the point. Rule 7 has to tolerate illegal intermediate states so that
    `test_meter_swap_in_one_transaction_is_allowed` can pass.
    """
    site = await make_site(conn)
    await make_meter(conn, site, billing_role="billing")

    try:
        async with savepoint():
            await make_meter(conn, site, billing_role="billing")
    except IMMEDIATE_ENFORCEMENT as exc:
        pytest.fail(
            "a second billing meter was rejected at statement time by "
            f"{exc.constraint_name or 'a unique constraint'}. Rule 7 must be "
            "enforced only by the deferred trigger -- immediate enforcement "
            "breaks the legal mid-transaction states of a meter swap. See "
            "migration 0b24bc6b5a1f."
        )

    # The duplicate is real and must still be refused -- just later.
    with pytest.raises(asyncpg.exceptions.CheckViolationError) as caught:
        await commit_check()

    assert "2 " + RULE_7_ERROR in str(caught.value)


# --------------------------------------------------------------------------
# D. the case the deferral exists for
# --------------------------------------------------------------------------
async def test_meter_swap_in_one_transaction_is_allowed(conn, commit_check):
    """Retire the old billing meter, install its replacement, one transaction.

    This is the regression guard. Between the two statements the site has two
    meter_spec rows with billing_role = 'billing', which is illegal at COMMIT
    but perfectly legal in between. Any enforcement that fires mid-statement
    rejects this, and a real meter swap becomes impossible to record.
    """
    site = await make_site(conn)
    old_meter = await make_meter(conn, site, billing_role="billing")
    await commit_check()

    await retire_device(conn, old_meter)
    try:
        new_meter = await make_meter(conn, site, billing_role="billing")
    except IMMEDIATE_ENFORCEMENT as exc:
        pytest.fail(
            "a meter swap was rejected at statement time by "
            f"{exc.constraint_name or 'a unique constraint'}. The retired "
            "meter still has billing_role = 'billing', and immediate "
            "enforcement cannot see device.removed_at. Rule 7 must stay on "
            "the deferred trigger alone -- see migration 0b24bc6b5a1f."
        )

    await commit_check()

    assert await active_billing_count(conn, site) == 1
    assert new_meter != old_meter


# --------------------------------------------------------------------------
# E. the same swap, old meter stood down first
# --------------------------------------------------------------------------
async def test_meter_swap_with_role_cleared_first_is_allowed(conn, commit_check):
    site = await make_site(conn)
    old_meter = await make_meter(conn, site, billing_role="billing")
    await commit_check()

    await retire_device(conn, old_meter)
    await conn.execute(
        "UPDATE meter_spec SET billing_role = 'check_meter' WHERE device_id = $1",
        old_meter,
    )
    await make_meter(conn, site, billing_role="billing")

    await commit_check()

    assert await active_billing_count(conn, site) == 1


# --------------------------------------------------------------------------
# F. retiring the last billing meter leaves the site unbillable
# --------------------------------------------------------------------------
async def test_retiring_the_only_billing_meter_is_refused(conn, commit_check):
    site = await make_site(conn)
    meter = await make_meter(conn, site, billing_role="billing")
    await commit_check()

    await retire_device(conn, meter)

    with pytest.raises(asyncpg.exceptions.CheckViolationError) as caught:
        await commit_check()

    assert "0 " + RULE_7_ERROR in str(caught.value)


# --------------------------------------------------------------------------
# G. the trigger must not block a legitimate cascade
# --------------------------------------------------------------------------
async def test_deleting_a_site_cascades_without_tripping_rule_7(
    conn, commit_check
):
    """Deleting a site removes its meters, momentarily leaving zero.

    The trigger skips sites that no longer exist. Without that branch this
    raises, and no site could ever be deleted.
    """
    site = await make_site(conn)
    await make_meter(conn, site, billing_role="billing")
    await commit_check()

    await conn.execute("DELETE FROM site WHERE site_id = $1", site)

    await commit_check()

    assert await conn.fetchval(
        "SELECT count(*) FROM site WHERE site_id = $1", site
    ) == 0
