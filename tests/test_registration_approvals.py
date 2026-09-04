"""Registrations are decided by a government official, in one district.

Two roles are gated: a field worker (both kinds — a private worker used to be
approved by the act of registering) and an installer's staff account (which
used to be gated by a shared string every firm in the city knew).

What these tests hold down is the part that is not a handler: the constraints
on `supplier_profile` and the two decision statements' own guards. The
statements are exercised directly, exactly as `db/sql/dao/` files are reached
at runtime, so a change to one of them fails here rather than at the next
demo.

Not covered, because there are no route tests in this repo at all: that
`get_current_account` refuses a pending account. That is checked over HTTP by
the throwaway smoke script the change was built with; see CLAUDE.md's known
weaknesses.
"""
import asyncpg
import pytest

from services.api.queries import sql

from .factories import make_account, unique_suffix

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Helpers -- an installer and a staff account attached to it
# ---------------------------------------------------------------------------

async def make_supplier_company(conn, districts=("Dhanmondi",), **overrides):
    tag = overrides.pop("tag", unique_suffix())
    supplier_id = await conn.fetchval(
        """
        INSERT INTO supplier_company (code, name, license_no)
        VALUES ($1, $2, $3)
        RETURNING supplier_id
        """,
        overrides.pop("code", f"TEST-SUP-{tag}"),
        overrides.pop("name", f"Test Installer {tag}"),
        overrides.pop("license_no", f"LIC-{tag}"),
    )
    for district in districts:
        await conn.execute(
            "INSERT INTO supplier_service_area (supplier_id, district) "
            "VALUES ($1, $2)",
            supplier_id, district,
        )
    return supplier_id


async def make_supplier_staff(
    conn, supplier_id=None, *, district="Dhanmondi", status="pending",
    account_id=None,
):
    """One staff account, through the statement registration actually uses."""
    if supplier_id is None:
        supplier_id = await make_supplier_company(conn, districts=(district,))
    if account_id is None:
        account_id = await make_account(conn)
    await conn.execute(
        sql("create_supplier_profile"),
        account_id, supplier_id, "Dispatcher", district, status,
    )
    return account_id, supplier_id


async def make_official(conn, district="Dhanmondi"):
    tag = unique_suffix()
    account_id = await make_account(conn, tag=f"gov-{tag}")
    code = f"TEST-GOV-{tag}"
    await conn.execute(
        "INSERT INTO government_official_code (code, district, issued_to, "
        "claimed_by_account_id, claimed_at) VALUES ($1, $2, 'Test', $3, now())",
        code, district, account_id,
    )
    await conn.execute(
        sql("create_government_profile"), account_id, district, code
    )
    return account_id


# ---------------------------------------------------------------------------
# The columns migration b6f2d90ac417 added
# ---------------------------------------------------------------------------

async def test_a_new_staff_account_is_pending_and_undated(conn):
    """Registration writes 'pending', and nothing is stamped until a decision.

    The pairing is a CHECK (supplier_approval_timestamps), not a convention:
    approved_at existing is exactly the same fact as the status not being
    pending.
    """
    account_id, _ = await make_supplier_staff(conn)
    row = await conn.fetchrow(
        "SELECT approval_status::text AS s, approved_at, "
        "approved_by_account_id, rejection_reason "
        "FROM supplier_profile WHERE account_id = $1",
        account_id,
    )
    assert row["s"] == "pending"
    assert row["approved_at"] is None
    assert row["approved_by_account_id"] is None
    assert row["rejection_reason"] is None


async def test_a_decided_registration_must_carry_a_timestamp(conn, savepoint):
    account_id, _ = await make_supplier_staff(conn)
    with pytest.raises(asyncpg.CheckViolationError):
        async with savepoint():
            await conn.execute(
                "UPDATE supplier_profile SET approval_status = 'approved' "
                "WHERE account_id = $1",
                account_id,
            )


async def test_a_pending_registration_must_not_carry_one(conn, savepoint):
    """The check runs both ways: a timestamp on a pending row is equally wrong.

    That direction matters more than it looks. It is what stops a decision
    being half-unwound -- clearing the status while leaving the date it was
    taken.
    """
    account_id, _ = await make_supplier_staff(conn)
    with pytest.raises(asyncpg.CheckViolationError):
        async with savepoint():
            await conn.execute(
                "UPDATE supplier_profile SET approved_at = now() "
                "WHERE account_id = $1",
                account_id,
            )


async def test_only_a_rejection_may_carry_a_reason(conn, savepoint):
    """An approval holding a leftover reason is a lie the UI would render."""
    account_id, _ = await make_supplier_staff(conn)
    with pytest.raises(asyncpg.CheckViolationError):
        async with savepoint():
            await conn.execute(
                "UPDATE supplier_profile SET approval_status = 'approved', "
                "approved_at = now(), rejection_reason = 'nope' "
                "WHERE account_id = $1",
                account_id,
            )


async def test_the_district_is_a_real_district(conn, savepoint):
    """service_district is a foreign key, not free text.

    Same reason it is one on worker_profile and site: the district decides
    which official sees the application, and a typo would file it where nobody
    is looking rather than failing.
    """
    supplier_id = await make_supplier_company(conn)
    account_id = await make_account(conn)
    with pytest.raises(asyncpg.ForeignKeyViolationError):
        async with savepoint():
            await conn.execute(
                sql("create_supplier_profile"),
                account_id, supplier_id, None, "Nowhere-upon-Sea", "pending",
            )


# ---------------------------------------------------------------------------
# The queue and the decision
# ---------------------------------------------------------------------------

async def test_the_queue_is_scoped_to_the_officials_own_district(conn):
    here, _ = await make_supplier_staff(conn, district="Dhanmondi")
    away, _ = await make_supplier_staff(conn, district="Uttara")

    listed = {
        r["account_id"]
        for r in await conn.fetch(sql("pending_supplier_registrations"), "Dhanmondi")
    }
    assert here in listed
    assert away not in listed

    # NULL is admin: every district, not none.
    everything = {
        r["account_id"]
        for r in await conn.fetch(sql("pending_supplier_registrations"), None)
    }
    assert {here, away} <= everything


async def test_the_queue_carries_what_the_decision_turns_on(conn):
    """Name, National ID and organisation, because that is the whole check."""
    account_id = await make_account(conn)
    await conn.execute(
        "UPDATE account SET national_id = $2, full_name = 'Rina Haque' "
        "WHERE account_id = $1",
        account_id, f"99{unique_suffix()}00",
    )
    supplier_id = await make_supplier_company(
        conn, districts=("Dhanmondi",), name="Noor Solar", license_no="LIC-XYZ"
    )
    await make_supplier_staff(
        conn, supplier_id, district="Dhanmondi", account_id=account_id
    )

    row = next(
        r for r in await conn.fetch(sql("pending_supplier_registrations"), "Dhanmondi")
        if r["account_id"] == account_id
    )
    assert row["full_name"] == "Rina Haque"
    assert row["national_id"] is not None
    assert row["supplier_name"] == "Noor Solar"
    assert row["license_no"] == "LIC-XYZ"


async def test_a_decision_is_made_once(conn):
    """The second official updates zero rows, which the handler answers 409 to."""
    account_id, _ = await make_supplier_staff(conn)
    official = await make_official(conn)

    first = await conn.fetchval(
        sql("decide_supplier_registration"),
        account_id, "approved", official, None, "Dhanmondi",
    )
    assert first == account_id

    second = await conn.fetchval(
        sql("decide_supplier_registration"),
        account_id, "rejected", official, "changed my mind", "Dhanmondi",
    )
    assert second is None

    row = await conn.fetchrow(
        "SELECT approval_status::text AS s, rejection_reason "
        "FROM supplier_profile WHERE account_id = $1",
        account_id,
    )
    assert row["s"] == "approved"
    assert row["rejection_reason"] is None


async def test_an_official_cannot_decide_the_next_district(conn):
    """The scope predicate is repeated in the UPDATE, not merely in the SELECT.

    Trusting the listing statement would leave the decision reachable by
    account id alone -- and the handler answers 404 to a row it cannot see,
    which would then be a lie about what it just failed to do.
    """
    account_id, _ = await make_supplier_staff(conn, district="Uttara")
    official = await make_official(conn, district="Dhanmondi")

    decided = await conn.fetchval(
        sql("decide_supplier_registration"),
        account_id, "approved", official, None, "Dhanmondi",
    )
    assert decided is None
    assert await conn.fetchval(
        "SELECT approval_status = 'pending' FROM supplier_profile "
        "WHERE account_id = $1",
        account_id,
    )


async def test_a_rejection_keeps_its_reason_and_still_gets_a_date(conn):
    """approved_at records when the decision was made, not whether it was yes."""
    account_id, _ = await make_supplier_staff(conn)
    official = await make_official(conn)

    await conn.fetchval(
        sql("decide_supplier_registration"),
        account_id, "rejected", official, "Not on our staff list", "Dhanmondi",
    )
    row = await conn.fetchrow(
        "SELECT approval_status::text AS s, approved_at, rejection_reason, "
        "approved_by_account_id FROM supplier_profile WHERE account_id = $1",
        account_id,
    )
    assert row["s"] == "rejected"
    assert row["approved_at"] is not None
    assert row["rejection_reason"] == "Not on our staff list"
    assert row["approved_by_account_id"] == official


async def test_sign_in_reads_the_verdict_off_the_row(conn):
    """supplier_registration_state is what /auth/me answers with.

    The portal asks the database whether the registration was approved; it
    never infers it from the role, and it never remembers it.
    """
    account_id, _ = await make_supplier_staff(conn)
    official = await make_official(conn)

    state = await conn.fetchrow(sql("supplier_registration_state"), account_id)
    assert state["approval_status"] == "pending"
    assert state["service_district"] == "Dhanmondi"
    assert state["supplier_name"]

    await conn.fetchval(
        sql("decide_supplier_registration"),
        account_id, "rejected", official, "Wrong firm", "Dhanmondi",
    )
    state = await conn.fetchrow(sql("supplier_registration_state"), account_id)
    assert state["approval_status"] == "rejected"
    assert state["rejection_reason"] == "Wrong firm"


async def test_the_firm_must_actually_work_the_district(conn):
    """supplier_company_serves is what registration checks before writing.

    An application filed where the firm has no presence would land in a queue
    whose official has no way to verify it -- the same reasoning that refuses a
    government worker employed by a utility with no presence in their region.
    """
    supplier_id = await make_supplier_company(conn, districts=("Dhanmondi",))
    assert await conn.fetchval(
        sql("supplier_company_serves"), supplier_id, "Dhanmondi"
    )
    assert not await conn.fetchval(
        sql("supplier_company_serves"), supplier_id, "Uttara"
    )


# ---------------------------------------------------------------------------
# The worker half: private is an application too now
# ---------------------------------------------------------------------------

async def test_a_private_worker_registration_lands_pending(conn):
    """create_worker_profile takes the status from the caller, and registration
    passes 'pending' for both kinds.

    This is the whole worker-side change: the column and the statement were
    always able to express it, and only the call site said 'approved'.
    """
    account_id = await make_account(conn)
    await conn.execute(
        sql("create_worker_profile"),
        account_id, f"TEST-EMP-{unique_suffix()}", "Dhanmondi", "private",
        None, "pending",
    )
    row = await conn.fetchrow(
        "SELECT approval_status::text AS s, approved_at FROM worker_profile "
        "WHERE account_id = $1",
        account_id,
    )
    assert row["s"] == "pending"
    assert row["approved_at"] is None


async def test_a_pending_private_worker_is_in_the_officials_queue(conn):
    """It used to be impossible for one to appear here at all."""
    account_id = await make_account(conn)
    await conn.execute(
        sql("create_worker_profile"),
        account_id, f"TEST-EMP-{unique_suffix()}", "Dhanmondi", "private",
        None, "pending",
    )
    row = next(
        (r for r in await conn.fetch(sql("pending_workers"), "Dhanmondi")
         if r["account_id"] == account_id),
        None,
    )
    assert row is not None
    assert row["worker_kind"] == "private"
    # No employer to name, and worker_kind_employer makes that a constraint
    # rather than a convention -- so the page must say the kind, not leave a
    # blank where a company would be.
    assert row["distribution_company_name"] is None


async def test_a_pending_private_worker_is_not_dispatchable(conn):
    """The queue blocks work, not paperwork.

    `assignable_workers` is the dispatcher's list and it is gated on
    `approval_status = 'approved'`, so an undecided private worker is not
    offerable — where before this change every private worker was, the moment
    they finished the form.
    """
    account_id = await make_account(conn)
    await conn.execute(
        sql("create_worker_profile"),
        account_id, f"TEST-EMP-{unique_suffix()}", "Dhanmondi", "private",
        None, "pending",
    )
    listed = {
        r["account_id"]
        for r in await conn.fetch(sql("assignable_workers"), "Dhanmondi", False)
    }
    assert account_id not in listed

    # And the row that gates the offer itself says why.
    row = await conn.fetchrow(sql("offerable_worker"), account_id)
    assert row["approval_status"] == "pending"
