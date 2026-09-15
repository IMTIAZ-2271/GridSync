"""Admin panel, phase 4: correcting an issued bill by voiding and reissuing it.

Rule 1 forbids editing a bill. A correction is a new bill, and the original is
voided pointing at it (`voided_by_bill_id`). Until migration c8d4f2a61e37 the
schema made even that impossible: `bill_one_per_period` refused the second bill
and `bill_void_status` refused voiding without one. Now:

* **One LIVE bill per period** -- an exclusion constraint that ignores void
  bills, deferrable so a reissue can write the replacement before voiding the
  original. Immediate everywhere else.
* **One earned and one applied ledger entry per BILL**, not per period, so the
  replacement can post its own.
* `rebill_period(bill_id, merge_late)` reverses the original's ledger entries
  with `adjustment` rows, reruns `run_billing` for the same month and voids the
  original -- one transaction.

These are also the repo's first tests of the billing engine's arithmetic: every
expected figure below is worked out by hand in the docstring of the fixture
that produces it, so a change to the engine that moves a number fails here.

Only the LATEST bill on a connection can be reissued: every later bill's
opening credit balance was computed from this one, and rebilling underneath
them would leave their snapshots describing a ledger that no longer exists.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import asyncpg
import pytest

from .factories import (
    make_account,
    make_ingest_batch,
    make_meter,
    make_site,
    make_tariff_plan,
    make_tariff_rate,
)
from .test_admin_foundation import as_role, make_admin  # noqa: F401  (fixture)

DHAKA = ZoneInfo("Asia/Dhaka")
JUNE = date(2026, 6, 1)
JULY = date(2026, 7, 1)
D = Decimal


# ---------------------------------------------------------------------------
# a billable connection, with numbers you can check on paper
# ---------------------------------------------------------------------------


class Connection:
    def __init__(self, site_id, point_id, device_id, account_id):
        self.site_id = site_id
        self.point_id = point_id
        self.device_id = device_id
        self.account_id = account_id


async def billable_connection(conn) -> Connection:
    """A bidirectional meter on a flat tariff.

    Tariff: 8.500000 BDT/kWh import and 6.250000 BDT/kWh export credit, all day,
    every day type; fixed charge 100.0000; tax 5%.
    """
    account_id = await make_account(conn)
    plan_id = await make_tariff_plan(
        conn, fixed_monthly_charge=D("100.0000"), tax_rate=D("0.0500")
    )
    for day_type in ("weekday", "weekend", "holiday"):
        await make_tariff_rate(conn, plan_id, day_type=day_type,
                               import_rate=D("8.500000"), export_credit_rate=D("6.250000"))
    site_id = await make_site(conn, account_id, plan_id)
    device_id = await make_meter(conn, site_id, meter_flow="bidirectional")
    point_id = await conn.fetchval(
        "SELECT billing_point_id FROM meter_spec WHERE device_id = $1", device_id
    )
    # Installed well before the months billed, so the swap guard sees no change.
    await conn.execute(
        "UPDATE device SET installed_at = $2 WHERE device_id = $1",
        device_id, datetime(2026, 1, 1, tzinfo=DHAKA),
    )
    return Connection(site_id, point_id, device_id, account_id)


async def month_of_readings(conn, c: Connection, month: date,
                            import_kwh="0.1000", export_kwh="0.0500") -> int:
    """Every half hour of the Dhaka month, identical readings."""
    start = datetime.combine(month, time(0), tzinfo=DHAKA)
    end = datetime.combine((month + timedelta(days=32)).replace(day=1), time(0), tzinfo=DHAKA)
    await conn.execute("SELECT create_reading_partition($1)", month)
    batch = await make_ingest_batch(conn, c.device_id)
    return await conn.fetchval(
        """
        WITH ins AS (
            INSERT INTO device_reading (device_id, interval_start, interval_minutes,
                                        import_kwh, export_kwh, source, quality,
                                        ingest_batch_id)
            SELECT $1, g, 30, $4::numeric, $5::numeric, 'device', 'good', $6
            FROM generate_series($2::timestamptz, $3::timestamptz - interval '30 minutes',
                                 interval '30 minutes') g
            RETURNING 1
        )
        SELECT count(*) FROM ins
        """,
        c.device_id, start, end, D(import_kwh), D(export_kwh), batch,
    )


async def run_billing(conn, c: Connection, month: date):
    return await conn.fetchval("SELECT run_billing($1, $2)", c.point_id, month)


async def bill(conn, bill_id):
    return await conn.fetchrow("SELECT * FROM bill WHERE bill_id = $1", bill_id)


async def ledger(conn, c: Connection):
    return await conn.fetch(
        "SELECT entry_type::text AS entry_type, kwh_delta, amount_delta, balance_kwh_after, "
        "balance_amount_after, bill_id FROM credit_ledger WHERE billing_point_id = $1 "
        "ORDER BY entry_id",
        c.point_id,
    )


async def give_credit(conn, c: Connection, kwh: str, amount: str) -> None:
    await conn.execute(
        """
        INSERT INTO credit_ledger (billing_point_id, site_id, entry_type, kwh_delta, amount_delta,
                                   balance_kwh_after, balance_amount_after, note)
        VALUES ($1, $2, 'adjustment', $3, $4, $3, $4, 'opening credit for the test')
        """,
        c.point_id, c.site_id, D(kwh), D(amount),
    )


def assert_running_balance(entries) -> None:
    """Every entry's balance is the sum of every delta up to and including it."""
    kwh = amount = D("0")
    for e in entries:
        kwh += e["kwh_delta"]
        amount += e["amount_delta"]
        assert e["balance_kwh_after"] == kwh, e
        assert e["balance_amount_after"] == amount, e


# ---------------------------------------------------------------------------
# the engine's arithmetic, pinned
# ---------------------------------------------------------------------------


async def test_june_bills_to_the_hand_computed_figures(conn):
    """June 2026 has 30 days = 1440 half-hour intervals.

        import  1440 x 0.1000 = 144.0000 kWh   x 8.500000 = 1224.0000 energy
        export  1440 x 0.0500 =  72.0000 kWh   x 6.250000 =  450.0000 credit earned
        fixed                                              100.0000
        tax     (1224.0000 + 100.0000) x 0.05            =   66.2000
        gross   1224.0000 + 100.0000 + 66.2000           = 1390.2000
        opening credit 0, so nothing applied; amount due  = 1390.2000
        closing credit 0 + 72.0000 - 0                    =   72.0000 kWh
    """
    c = await billable_connection(conn)
    assert await month_of_readings(conn, c, JUNE) == 1440

    b = await bill(conn, await run_billing(conn, c, JUNE))

    assert b["energy_charge"] == D("1224.0000")
    assert b["export_credit_earned"] == D("450.0000")
    assert b["fixed_charge"] == D("100.0000")
    assert b["tax_amount"] == D("66.2000")
    assert b["gross_amount"] == D("1390.2000")
    assert b["credit_applied_kwh"] == D("0")
    assert b["amount_due"] == D("1390.2000")
    assert b["credit_closing_kwh"] == D("72.0000")
    [earned] = await ledger(conn, c)
    assert (earned["entry_type"], earned["kwh_delta"], earned["amount_delta"]) == (
        "earned", D("72.0000"), D("450.0000"))


async def test_opening_credit_is_spent_before_the_bill_is_due(conn):
    """Same June, with 40.0000 kWh / 250.0000 BDT of credit already held.

        credit rate      450.0000 / 72.0000             = 6.250000
        applied amount   least(1390.2000, 40 x 6.25)    =  250.0000
        applied kWh      least(40.0000, 250 / 6.25)     =   40.0000
        amount due       1390.2000 - 250.0000           = 1140.2000
        closing kWh      40 + 72 - 40                   =   72.0000
    """
    c = await billable_connection(conn)
    await give_credit(conn, c, "40.0000", "250.0000")
    await month_of_readings(conn, c, JUNE)

    b = await bill(conn, await run_billing(conn, c, JUNE))

    assert b["credit_opening_kwh"] == D("40.0000")
    assert b["credit_applied_kwh"] == D("40.0000")
    assert b["credit_applied_amount"] == D("250.0000")
    assert b["amount_due"] == D("1140.2000")
    assert b["credit_closing_kwh"] == D("72.0000")
    assert_running_balance(await ledger(conn, c))


# ---------------------------------------------------------------------------
# the schema that makes a reissue possible
# ---------------------------------------------------------------------------


async def test_two_live_bills_for_one_period_are_still_refused(conn, savepoint):
    """Immediate by default: the ordinary billing path gains no slack."""
    c = await billable_connection(conn)
    await month_of_readings(conn, c, JUNE)
    original = await bill(conn, await run_billing(conn, c, JUNE))

    with pytest.raises(asyncpg.ExclusionViolationError):
        async with savepoint():
            await conn.execute(
                """
                INSERT INTO bill (period_id, billing_point_id, site_id, account_id,
                                  tariff_plan_id, amount_due)
                SELECT period_id, billing_point_id, site_id, account_id, tariff_plan_id, 1
                FROM bill WHERE bill_id = $1
                """,
                original["bill_id"],
            )


async def test_a_second_earned_entry_for_one_bill_is_refused(conn, savepoint):
    c = await billable_connection(conn)
    await month_of_readings(conn, c, JUNE)
    bill_id = await run_billing(conn, c, JUNE)

    with pytest.raises(asyncpg.UniqueViolationError):
        async with savepoint():
            await conn.execute(
                """
                INSERT INTO credit_ledger (billing_point_id, site_id, bill_id, entry_type,
                                           kwh_delta, balance_kwh_after)
                VALUES ($1, $2, $3, 'earned', 1, 73)
                """,
                c.point_id, c.site_id, bill_id,
            )


async def test_billing_the_same_month_again_is_still_idempotent(conn):
    c = await billable_connection(conn)
    await month_of_readings(conn, c, JUNE)
    first = await run_billing(conn, c, JUNE)

    assert await run_billing(conn, c, JUNE) == first


# ---------------------------------------------------------------------------
# rebill_period
# ---------------------------------------------------------------------------


async def test_a_reissue_after_corrected_readings(conn):
    """The meter was misread: every June import was really 0.2000.

        import  1440 x 0.2000 = 288.0000 kWh  x 8.5 = 2448.0000
        tax     (2448.0000 + 100.0000) x 0.05        =  127.4000
        gross   2448.0000 + 100.0000 + 127.4000      = 2675.4000
        export unchanged: 72.0000 kWh / 450.0000 earned, nothing applied.

    Ledger: the original's earned entry is reversed by an adjustment, and the
    replacement posts its own -- ending where it started, at 72.0000 kWh.
    """
    c = await billable_connection(conn)
    await month_of_readings(conn, c, JUNE)
    original_id = await run_billing(conn, c, JUNE)
    await conn.execute(
        "UPDATE device_reading SET import_kwh = 0.2000 WHERE device_id = $1", c.device_id
    )

    new_id = await conn.fetchval("SELECT rebill_period($1, false)", original_id)

    original, replacement = await bill(conn, original_id), await bill(conn, new_id)
    assert original["status"] == "void"
    assert original["voided_by_bill_id"] == new_id
    assert replacement["status"] == "issued"
    assert replacement["period_id"] == original["period_id"]
    assert replacement["energy_charge"] == D("2448.0000")
    assert replacement["tax_amount"] == D("127.4000")
    assert replacement["amount_due"] == D("2675.4000")
    assert replacement["credit_closing_kwh"] == D("72.0000")
    entries = await ledger(conn, c)
    assert [(e["entry_type"], e["kwh_delta"], e["bill_id"]) for e in entries] == [
        ("earned", D("72.0000"), original_id),
        ("adjustment", D("-72.0000"), original_id),
        ("earned", D("72.0000"), new_id),
    ]
    assert_running_balance(entries)
    assert await conn.fetchval(
        "SELECT status::text FROM billing_period WHERE period_id = $1", original["period_id"]
    ) == "billed"
    assert await conn.fetchval(
        "SELECT count(*) FROM bill WHERE period_id = $1 AND status <> 'void'", original["period_id"]
    ) == 1


async def test_a_reissue_reverses_applied_credit_so_it_can_be_spent_again(conn):
    """The original spent 40.0000 kWh of opening credit. Reversed first, the
    replacement sees the same 40.0000 opening and spends it the same way --
    rather than finding it already gone and charging the full 1390.2000."""
    c = await billable_connection(conn)
    await give_credit(conn, c, "40.0000", "250.0000")
    await month_of_readings(conn, c, JUNE)
    original_id = await run_billing(conn, c, JUNE)

    new_id = await conn.fetchval("SELECT rebill_period($1, false)", original_id)

    replacement = await bill(conn, new_id)
    assert replacement["credit_opening_kwh"] == D("40.0000")
    assert replacement["amount_due"] == D("1140.2000")
    entries = await ledger(conn, c)
    assert_running_balance(entries)
    assert entries[-1]["balance_kwh_after"] == D("72.0000")


async def test_late_readings_can_be_merged_into_the_reissue(conn):
    """Two June intervals were missing and arrived after billing, so ingest kept
    them in late_reading (rule 8). Merged, they are billed and marked resolved.

    1438 readings x 0.1000 + 2 late x 1.0000 = 145.8000 kWh import.
    Coverage 1438/1440 = 99.86 pct: the original clears rule 8's 95 pct.
    """
    c = await billable_connection(conn)
    await month_of_readings(conn, c, JUNE)
    gap = [datetime(2026, 6, 10, 19, 0, tzinfo=DHAKA), datetime(2026, 6, 10, 19, 30, tzinfo=DHAKA)]
    await conn.execute(
        "DELETE FROM device_reading WHERE device_id = $1 AND interval_start = ANY($2)",
        c.device_id, gap,
    )
    original_id = await run_billing(conn, c, JUNE)
    batch = await make_ingest_batch(conn, c.device_id)
    for ts in gap:
        await conn.execute(
            "INSERT INTO late_reading (device_id, interval_start, import_kwh, export_kwh, reason, "
            "ingest_batch_id) VALUES ($1, $2, 1.0000, 0.0000, 'period_billed', $3)",
            c.device_id, ts, batch,
        )

    new_id = await conn.fetchval("SELECT rebill_period($1, true)", original_id)

    assert (await bill(conn, original_id))["energy_charge"] == D("1222.3000")  # 143.8 x 8.5
    assert (await bill(conn, new_id))["energy_charge"] == D("1239.3000")      # 145.8 x 8.5
    assert await conn.fetchval(
        "SELECT bool_and(resolved) FROM late_reading WHERE device_id = $1", c.device_id
    )


async def test_only_the_latest_bill_can_be_reissued(conn, savepoint):
    c = await billable_connection(conn)
    await month_of_readings(conn, c, JUNE)
    await month_of_readings(conn, c, JULY)
    june = await run_billing(conn, c, JUNE)
    await run_billing(conn, c, JULY)

    with pytest.raises(asyncpg.ObjectNotInPrerequisiteStateError, match="later bill"):
        async with savepoint():
            await conn.fetchval("SELECT rebill_period($1, false)", june)


async def test_a_void_bill_cannot_be_reissued(conn, savepoint):
    c = await billable_connection(conn)
    await month_of_readings(conn, c, JUNE)
    original = await run_billing(conn, c, JUNE)
    await conn.fetchval("SELECT rebill_period($1, false)", original)

    with pytest.raises(asyncpg.ObjectNotInPrerequisiteStateError, match="void"):
        async with savepoint():
            await conn.fetchval("SELECT rebill_period($1, false)", original)


async def test_a_bill_with_payments_is_not_reissued(conn, savepoint):
    """Nothing in GridSync takes payments yet; when something does, moving money
    already received onto a replacement bill is its own decision."""
    c = await billable_connection(conn)
    await month_of_readings(conn, c, JUNE)
    bill_id = await run_billing(conn, c, JUNE)
    await conn.execute(
        "INSERT INTO payment (bill_id, account_id, amount, method, status) "
        "VALUES ($1, $2, 100, 'bkash', 'succeeded')",
        bill_id, c.account_id,
    )

    with pytest.raises(asyncpg.ObjectNotInPrerequisiteStateError, match="payment"):
        async with savepoint():
            await conn.fetchval("SELECT rebill_period($1, false)", bill_id)


async def test_a_reissue_after_an_ownership_change_is_refused(conn, savepoint):
    """run_billing snapshots the site's CURRENT owner; reissuing after a transfer
    would move last month's charge onto the new household (rule 2)."""
    c = await billable_connection(conn)
    await month_of_readings(conn, c, JUNE)
    bill_id = await run_billing(conn, c, JUNE)
    await conn.execute(
        "UPDATE site SET account_id = $2 WHERE site_id = $1", c.site_id, await make_account(conn)
    )

    with pytest.raises(asyncpg.ObjectNotInPrerequisiteStateError, match="owner"):
        async with savepoint():
            await conn.fetchval("SELECT rebill_period($1, false)", bill_id)


async def test_a_reissue_after_a_meter_swap_is_refused(conn, savepoint):
    """run_billing reads the CURRENT billing meter; the original was cut from
    the one it replaced, whose readings a rerun would not see."""
    c = await billable_connection(conn)
    await month_of_readings(conn, c, JUNE)
    bill_id = await run_billing(conn, c, JUNE)
    await conn.execute(
        "UPDATE device SET removed_at = now(), status = 'removed' WHERE device_id = $1", c.device_id
    )
    replacement = await make_meter(conn, c.site_id, meter_flow="bidirectional",
                                   billing_point_id=c.point_id)
    # now() is constant inside a test transaction, so the swap would otherwise
    # appear to happen at the very instant the bill was issued.
    await conn.execute(
        "UPDATE device SET installed_at = (SELECT issued_at FROM bill WHERE bill_id = $2) "
        "+ interval '1 day' WHERE device_id = $1",
        replacement, bill_id,
    )

    with pytest.raises(asyncpg.ObjectNotInPrerequisiteStateError, match="meter"):
        async with savepoint():
            await conn.fetchval("SELECT rebill_period($1, false)", bill_id)


async def test_a_reissue_that_fails_rule_8_leaves_the_original_alone(conn, savepoint):
    """The correction removed readings: coverage drops below 95 pct, run_billing
    refuses, and the whole reissue rolls back -- reversals included."""
    c = await billable_connection(conn)
    await month_of_readings(conn, c, JUNE)
    bill_id = await run_billing(conn, c, JUNE)
    await conn.execute(
        "DELETE FROM device_reading WHERE device_id = $1 AND interval_start < $2",
        c.device_id, datetime(2026, 6, 4, tzinfo=DHAKA),
    )

    with pytest.raises(asyncpg.CheckViolationError, match="coverage"):
        async with savepoint():
            await conn.fetchval("SELECT rebill_period($1, false)", bill_id)

    assert (await bill(conn, bill_id))["status"] == "issued"
    assert len(await ledger(conn, c)) == 1


# ---------------------------------------------------------------------------
# the route
# ---------------------------------------------------------------------------


async def test_an_admin_reissues_a_bill_and_it_is_on_the_record(conn, as_role):
    admin = await make_admin(conn)
    c = await billable_connection(conn)
    await month_of_readings(conn, c, JUNE)
    original_id = await run_billing(conn, c, JUNE)
    await conn.execute("UPDATE device_reading SET import_kwh = 0.2000 WHERE device_id = $1", c.device_id)
    client = await as_role("admin", admin)

    response = await client.post(
        f"/api/admin/bills/{original_id}/reissue",
        json={"reason": "meter misread; corrected readings confirmed on site"},
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["voided_bill_id"] == str(original_id)
    assert body["previous_amount_due"] == "1390.2000"
    assert body["amount_due"] == "2675.4000"
    assert body["previous_gross_amount"] == "1390.2000"
    assert body["gross_amount"] == "2675.4000"
    [entry] = await conn.fetch(
        "SELECT action, before_state::text AS before, after_state::text AS after FROM audit_log "
        "WHERE entity_id = $1",
        str(original_id),
    )
    assert entry["action"] == "bill.reissued"
    assert "1390.2000" in entry["before"] and "2675.4000" in entry["after"]
    assert await conn.fetchval(
        "SELECT count(*) FROM notification WHERE account_id = $1", c.account_id
    ) == 1


async def test_the_route_turns_guards_into_409_and_422(conn, as_role):
    admin = await make_admin(conn)
    c = await billable_connection(conn)
    await month_of_readings(conn, c, JUNE)
    await month_of_readings(conn, c, JULY)
    june = await run_billing(conn, c, JUNE)
    july = await run_billing(conn, c, JULY)
    client = await as_role("admin", admin)

    later = await client.post(f"/api/admin/bills/{june}/reissue", json={"reason": "try june"})
    assert later.status_code == 409
    assert "later bill" in later.json()["detail"]

    await conn.execute(
        "DELETE FROM device_reading WHERE device_id = $1 AND interval_start >= $2 AND interval_start < $3",
        c.device_id, datetime(2026, 7, 1, tzinfo=DHAKA), datetime(2026, 7, 5, tzinfo=DHAKA),
    )
    coverage = await client.post(f"/api/admin/bills/{july}/reissue", json={"reason": "try july"})
    assert coverage.status_code == 422
    assert "coverage" in coverage.json()["detail"]

    missing = await client.post(
        "/api/admin/bills/00000000-0000-0000-0000-000000000000/reissue", json={"reason": "nobody"}
    )
    assert missing.status_code == 404
    assert await conn.fetchval("SELECT count(*) FROM audit_log WHERE action = 'bill.reissued'") == 0


@pytest.mark.parametrize("role", ["consumer", "government", "supplier"])
async def test_only_an_admin_reissues(conn, as_role, role):
    c = await billable_connection(conn)
    await month_of_readings(conn, c, JUNE)
    bill_id = await run_billing(conn, c, JUNE)
    client = await as_role(role, c.account_id if role == "consumer" else await make_account(conn))

    response = await client.post(f"/api/admin/bills/{bill_id}/reissue", json={"reason": "not mine"})

    assert response.status_code == 403


async def test_a_connections_bills_are_listed_with_which_can_be_reissued(conn, as_role):
    admin = await make_admin(conn)
    c = await billable_connection(conn)
    await month_of_readings(conn, c, JUNE)
    await month_of_readings(conn, c, JULY)
    await run_billing(conn, c, JUNE)
    july = await run_billing(conn, c, JULY)
    client = await as_role("admin", admin)

    body = (await client.get(f"/api/admin/billing-points/{c.point_id}/bills")).json()

    assert [b["period_start"] for b in body] == ["2026-07-01", "2026-06-01"]
    assert [b["reissuable"] for b in body] == [True, False]
    assert body[0]["bill_id"] == str(july)
