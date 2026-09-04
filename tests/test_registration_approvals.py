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
    conn, *, district="Dhanmondi", organisation="Noor Solar Ltd",
    status="pending", account_id=None,
):
    """One registration, through the statement registration actually uses.

    Note what is NOT passed: a firm. A registration is a typed claim and
    nothing more until an official resolves it.
    """
    if account_id is None:
        account_id = await make_account(conn)
    await conn.execute(
        sql("create_supplier_profile"),
        account_id, organisation, district, "Dispatcher", status,
    )
    return account_id


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
    account_id = await make_supplier_staff(conn, organisation="  Noor Solar  ")
    row = await conn.fetchrow(
        "SELECT approval_status::text AS s, approved_at, "
        "approved_by_account_id, rejection_reason, supplier_id, "
        "claimed_organisation "
        "FROM supplier_profile WHERE account_id = $1",
        account_id,
    )
    assert row["s"] == "pending"
    assert row["approved_at"] is None
    assert row["approved_by_account_id"] is None
    assert row["rejection_reason"] is None
    # The claim is kept, trimmed, and resolves to nothing yet.
    assert row["claimed_organisation"] == "Noor Solar"
    assert row["supplier_id"] is None


async def test_a_decided_registration_must_carry_a_timestamp(conn, savepoint):
    account_id = await make_supplier_staff(conn)
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
    account_id = await make_supplier_staff(conn)
    with pytest.raises(asyncpg.CheckViolationError):
        async with savepoint():
            await conn.execute(
                "UPDATE supplier_profile SET approved_at = now() "
                "WHERE account_id = $1",
                account_id,
            )


async def test_only_a_rejection_may_carry_a_reason(conn, savepoint):
    """An approval holding a leftover reason is a lie the UI would render."""
    account_id = await make_supplier_staff(conn)
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

    The organisation beside it is deliberately the opposite -- free text, no
    key, no lookup -- because that one is an assertion for a human to check.
    """
    account_id = await make_account(conn)
    with pytest.raises(asyncpg.ForeignKeyViolationError):
        async with savepoint():
            await conn.execute(
                sql("create_supplier_profile"),
                account_id, "Noor Solar", "Nowhere-upon-Sea", None, "pending",
            )


async def test_an_approval_must_name_a_firm(conn, savepoint):
    """supplier_approved_has_firm: approved implies resolved.

    The nullable supplier_id is a window that closes at the decision, not a
    permanent maybe. Without this CHECK an approval could leave somebody a
    supplier of nothing -- able to open the portal, and belonging to no firm
    that a household could rate or complain about.
    """
    account_id = await make_supplier_staff(conn)
    with pytest.raises(asyncpg.CheckViolationError):
        async with savepoint():
            await conn.execute(
                "UPDATE supplier_profile SET approval_status = 'approved', "
                "approved_at = now() WHERE account_id = $1",
                account_id,
            )


async def test_a_rejection_needs_no_firm(conn):
    """The other side of that CHECK: nobody ever linked a refused claim."""
    account_id = await make_supplier_staff(conn)
    official = await make_official(conn)
    await conn.fetchval(
        sql("decide_supplier_registration"),
        account_id, "rejected", official, "Not our staff", "Dhanmondi", None,
    )
    row = await conn.fetchrow(
        "SELECT approval_status::text AS s, supplier_id, claimed_organisation "
        "FROM supplier_profile WHERE account_id = $1",
        account_id,
    )
    assert row["s"] == "rejected"
    assert row["supplier_id"] is None
    # And the claim survives the rejection -- it is the evidence the decision
    # was made against.
    assert row["claimed_organisation"] == "Noor Solar Ltd"


# ---------------------------------------------------------------------------
# The queue and the decision
# ---------------------------------------------------------------------------

async def test_the_queue_is_scoped_to_the_officials_own_district(conn):
    here = await make_supplier_staff(conn, district="Dhanmondi")
    away = await make_supplier_staff(conn, district="Uttara")

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
    """Name, National ID and the typed organisation: the whole check."""
    account_id = await make_account(conn)
    await conn.execute(
        "UPDATE account SET national_id = $2, full_name = 'Rina Haque' "
        "WHERE account_id = $1",
        account_id, f"99{unique_suffix()}00",
    )
    await make_supplier_staff(
        conn, district="Dhanmondi", organisation="Totally New Solar",
        account_id=account_id,
    )

    row = next(
        r for r in await conn.fetch(sql("pending_supplier_registrations"), "Dhanmondi")
        if r["account_id"] == account_id
    )
    assert row["full_name"] == "Rina Haque"
    assert row["national_id"] is not None
    assert row["claimed_organisation"] == "Totally New Solar"
    # Nothing resolved, and nothing pretended to resolve.
    assert row["supplier_id"] is None
    assert row["supplier_name"] is None
    assert row["suggested_supplier_id"] is None


async def test_the_queue_suggests_an_exact_name_match(conn):
    """A shortcut for the ordinary case, and only for the ordinary case.

    The match is exact but case-insensitive. Fuzzy matching would be worse than
    none: the official is the only check there is, and a plausible-looking
    wrong suggestion is the one thing that could get waved through.
    """
    name = f"Noor Solar {unique_suffix()}"
    supplier_id = await make_supplier_company(
        conn, districts=("Dhanmondi",), name=name
    )
    matched = await make_supplier_staff(conn, organisation=name.lower())
    unmatched = await make_supplier_staff(conn, organisation=f"{name} Limited")

    rows = {
        r["account_id"]: r
        for r in await conn.fetch(sql("pending_supplier_registrations"), "Dhanmondi")
    }
    assert rows[matched]["suggested_supplier_id"] == supplier_id
    assert rows[matched]["suggested_supplier_name"] == name
    # Still NULL: "no obvious match" is not "new firm", and the official
    # decides which of the two this is.
    assert rows[unmatched]["suggested_supplier_id"] is None


async def test_a_decision_is_made_once(conn):
    """The second official updates zero rows, which the handler answers 409 to."""
    account_id = await make_supplier_staff(conn)
    official = await make_official(conn)
    firm = await make_supplier_company(conn)

    first = await conn.fetchval(
        sql("decide_supplier_registration"),
        account_id, "approved", official, None, "Dhanmondi", firm,
    )
    assert first == account_id

    second = await conn.fetchval(
        sql("decide_supplier_registration"),
        account_id, "rejected", official, "changed my mind", "Dhanmondi", None,
    )
    assert second is None

    row = await conn.fetchrow(
        "SELECT approval_status::text AS s, rejection_reason, supplier_id "
        "FROM supplier_profile WHERE account_id = $1",
        account_id,
    )
    assert row["s"] == "approved"
    assert row["rejection_reason"] is None
    # The link and the status were written together -- there is no ordering
    # here for a failure to land between.
    assert row["supplier_id"] == firm


async def test_an_official_cannot_decide_the_next_district(conn):
    """The scope predicate is repeated in the UPDATE, not merely in the SELECT.

    Trusting the listing statement would leave the decision reachable by
    account id alone -- and the handler answers 404 to a row it cannot see,
    which would then be a lie about what it just failed to do.
    """
    account_id = await make_supplier_staff(conn, district="Uttara")
    official = await make_official(conn, district="Dhanmondi")
    firm = await make_supplier_company(conn)

    decided = await conn.fetchval(
        sql("decide_supplier_registration"),
        account_id, "approved", official, None, "Dhanmondi", firm,
    )
    assert decided is None
    assert await conn.fetchval(
        "SELECT approval_status = 'pending' FROM supplier_profile "
        "WHERE account_id = $1",
        account_id,
    )


async def test_a_rejection_keeps_its_reason_and_still_gets_a_date(conn):
    """approved_at records when the decision was made, not whether it was yes."""
    account_id = await make_supplier_staff(conn)
    official = await make_official(conn)

    await conn.fetchval(
        sql("decide_supplier_registration"),
        account_id, "rejected", official, "Not on our staff list", "Dhanmondi",
        None,
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
    account_id = await make_supplier_staff(conn)
    official = await make_official(conn)

    state = await conn.fetchrow(sql("supplier_registration_state"), account_id)
    # LEFT JOIN, so a pending applicant still gets a row -- an inner join here
    # would blank the sign-in of exactly the person who most needs telling
    # where they stand.
    assert state is not None
    assert state["approval_status"] == "pending"
    assert state["service_district"] == "Dhanmondi"
    assert state["claimed_organisation"] == "Noor Solar Ltd"
    assert state["supplier_name"] is None

    await conn.fetchval(
        sql("decide_supplier_registration"),
        account_id, "rejected", official, "Wrong firm", "Dhanmondi", None,
    )
    state = await conn.fetchrow(sql("supplier_registration_state"), account_id)
    assert state["approval_status"] == "rejected"
    assert state["rejection_reason"] == "Wrong firm"


async def test_approval_records_that_the_firm_works_that_district(conn):
    """The official has just asserted it, so it is written down.

    Without this a firm created from a claim would be invisible to every
    household: requirement 7's installer list is filtered by district, so a
    firm with no service area is a firm nobody can choose. Idempotent, because
    the second approval in the same district is the normal case.
    """
    firm = await make_supplier_company(conn, districts=())
    assert not await conn.fetchval(
        "SELECT EXISTS (SELECT 1 FROM supplier_service_area "
        "WHERE supplier_id = $1 AND district = 'Uttara')", firm,
    )

    await conn.execute(sql("add_supplier_service_area"), firm, "Uttara")
    await conn.execute(sql("add_supplier_service_area"), firm, "Uttara")

    assert await conn.fetchval(
        "SELECT count(*) FROM supplier_service_area "
        "WHERE supplier_id = $1 AND district = 'Uttara'", firm,
    ) == 1


async def test_only_an_active_firm_can_be_linked(conn):
    """A suspended or closed installer must not gain new staff."""
    firm = await make_supplier_company(conn)
    assert await conn.fetchrow(sql("supplier_company_for_linking"), firm)

    await conn.execute(
        "UPDATE supplier_company SET status = 'suspended' WHERE supplier_id = $1",
        firm,
    )
    assert await conn.fetchrow(sql("supplier_company_for_linking"), firm) is None


async def test_a_firm_created_from_a_claim_is_one_row(conn):
    """create_supplier_company, and the licence that stops a duplicate.

    license_no is UNIQUE, which is the one guard against the same firm being
    created twice under two spellings -- the risk that made a typed name a
    claim rather than a key in the first place.
    """
    tag = unique_suffix()
    created = await conn.fetchrow(
        sql("create_supplier_company"),
        f"NOOR-SOLAR-{tag}", "  Noor Solar Ltd  ", f"LIC-{tag}",
    )
    assert created["name"] == "Noor Solar Ltd"

    with pytest.raises(asyncpg.UniqueViolationError):
        await conn.execute(
            sql("create_supplier_company"),
            f"NOOR-SOLARS-{tag}", "Noor Solars Limited", f"LIC-{tag}",
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
