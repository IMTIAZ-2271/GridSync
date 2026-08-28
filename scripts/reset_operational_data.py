"""Wipe every transactional and operational record, keep who everyone is.

After this runs, every consumer owns **zero meters** and the worker, government
and supplier dashboards are empty. What survives is identity and configuration:
accounts and their passwords, the three staff profile tables, districts,
distribution and supplier companies with their service areas, official codes,
tariff plans and their rates, and every site with its billing points.

    python -m scripts.reset_operational_data --dry-run   # counts, writes nothing
    python -m scripts.reset_operational_data --yes       # do it

**This is irreversible and it is not a migration.** Take a dump first if the
data matters:

    pg_dump -U postgres -d gridsync -Fc -f before-reset.dump

Sites and their billing points are kept on purpose. A billing point is the
*connection* -- the position on the wall the utility bills -- and a household
whose meter has been removed still lives at the same address on the same
connection. Keeping them is also what makes the reset useful: a consumer can
immediately apply for a meter against a real address instead of re-running
onboarding to invent one.

RULE 1 IS SUSPENDED FOR THIS SCRIPT, DELIBERATELY AND NARROWLY.
`bill`, `bill_line_item` and `credit_ledger` carry `forbid_mutation()`
triggers that refuse DELETE outright, because money is immutable and a
correction is a new row. That rule exists to protect a *running* system's
records; it is not a claim that a development database can never be emptied.
The three triggers are disabled, the rows deleted, and the triggers re-enabled
inside one transaction -- so a failure anywhere leaves both the data and the
protection exactly as they were. `tariff_rate` carries the same trigger and is
NOT touched: pricing is configuration, and the billing engine has nothing to
run against without it.
"""
from __future__ import annotations

import argparse
import asyncio

import asyncpg

from services.api.db import database_url, init_connection

#: Deleted in this order. FKs would mostly cascade, but naming the order makes
#: the dependency structure readable and means a new table added upstream fails
#: loudly here rather than being silently cascaded away.
TABLES: tuple[str, ...] = (
    # --- what people were told, and what they had seen -------------------
    "list_view_state",
    "notification",
    # --- field operations -------------------------------------------------
    "service_rating",
    "work_order_assignment",
    "work_order",
    "issue_comment",
    "issue",
    # --- telemetry --------------------------------------------------------
    "late_reading",
    "device_reading",
    "ingest_batch",
    # --- money (see the rule 1 note above) --------------------------------
    "credit_ledger",
    "payment",
    "bill_line_item",
    "bill",
    "billing_period",
    "billing_run",
    # --- derived analytics ------------------------------------------------
    "site_daily_summary",
    "site_monthly_summary",
    # --- agreements and applications --------------------------------------
    "net_metering_agreement",
    "solar_application",
    "meter_application",
    "meter_asset",
    # --- hardware ---------------------------------------------------------
    "solar_array",
    "inverter_spec",
    "meter_spec",
    "device",
    # --- per-site preferences ---------------------------------------------
    "site_consumption_limit",
    "audit_log",
)

#: The append-only tables whose `forbid_mutation()` trigger has to stand down.
#: `tariff_rate` has one too and is deliberately absent -- it is kept.
IMMUTABLE = ("bill", "bill_line_item", "credit_ledger")

#: Rule 7's deferred constraint triggers, which also have to stand down --
#: (table, trigger).
#:
#: Rule 7 requires exactly one active billing meter per billing point, and it
#: is checked at COMMIT. Deleting every meter while KEEPING the billing points
#: leaves each point with zero, so the commit is refused.
#:
#: That state is not actually illegal, which is the point. `POST /api/sites`
#: mints a site's "Main" point with no meter on it, and `site_points` renders
#: exactly that as "No meter yet" -- a connection can sit unmetered, and one
#: whose meter has been removed is in the same state as one that never had
#: one. The trigger never sees a bare point because it only fires on
#: meter_spec and device rows; a bulk delete of those rows is the one moment
#: it can, and it reads the empty point as a violation rather than as a
#: connection awaiting a meter. The next install fires it again and counts 1,
#: so nothing is left un-enforced.
RULE_7 = (
    ("meter_spec", "meter_spec_one_active_billing"),
    ("device", "device_one_active_billing"),
)

#: Never emptied, and listed so the intent is explicit rather than implied by
#: absence. If one of these ever needs clearing it should be a deliberate edit
#: here, with a reason.
KEPT = (
    "account", "worker_profile", "supplier_profile", "government_profile",
    "government_official_code", "district", "distribution_company",
    "distribution_company_area", "supplier_company", "supplier_service_area",
    "tariff_plan", "tariff_rate", "holiday_calendar", "site", "billing_point",
)


async def counts(conn: asyncpg.Connection, tables: tuple[str, ...]) -> dict[str, int]:
    out: dict[str, int] = {}
    for t in tables:
        try:
            out[t] = await conn.fetchval(f"SELECT count(*) FROM {t}")
        except asyncpg.UndefinedTableError:
            out[t] = -1
    return out


def report(title: str, rows: dict[str, int]) -> int:
    print(f"\n{title}")
    total = 0
    for table, n in rows.items():
        if n < 0:
            print(f"  {table:<28} (no such table)")
            continue
        total += n
        print(f"  {table:<28} {n:>9,}")
    print(f"  {'':<28} {'-' * 9}\n  {'total':<28} {total:>9,}")
    return total


async def run(apply: bool) -> None:
    conn = await asyncpg.connect(database_url())
    await init_connection(conn)
    try:
        before = await counts(conn, TABLES)
        report("WILL DELETE", before)
        report("WILL KEEP", await counts(conn, KEPT))

        if not apply:
            print("\n--dry-run: nothing was written.")
            return

        async with conn.transaction():
            # Narrow and temporary: re-enabled below, inside this same
            # transaction, so an error anywhere restores them by rollback.
            for t in IMMUTABLE:
                await conn.execute(f"ALTER TABLE {t} DISABLE TRIGGER {t}_immutable")
            for table, trigger in RULE_7:
                await conn.execute(f"ALTER TABLE {table} DISABLE TRIGGER {trigger}")

            for t in TABLES:
                if before.get(t, -1) < 0:
                    continue
                if t == "device_reading":
                    # Partitioned and by far the largest table. TRUNCATE beats
                    # a row-by-row DELETE by orders of magnitude and drops the
                    # partitions' contents with it. late_reading and
                    # ingest_batch reference it only by batch, and both are
                    # cleared in this same transaction.
                    await conn.execute("TRUNCATE TABLE device_reading")
                else:
                    await conn.execute(f"DELETE FROM {t}")

            for table, trigger in RULE_7:
                await conn.execute(f"ALTER TABLE {table} ENABLE TRIGGER {trigger}")
            for t in IMMUTABLE:
                await conn.execute(f"ALTER TABLE {t} ENABLE TRIGGER {t}_immutable")

        after = await counts(conn, TABLES)
        left = sum(n for n in after.values() if n > 0)
        report("AFTER", after)

        still_armed = await conn.fetch(
            """
            SELECT c.relname AS table_name, t.tgname, t.tgenabled
            FROM pg_trigger t
            JOIN pg_class c ON c.oid = t.tgrelid
            WHERE t.tgname LIKE '%_immutable'
               OR t.tgname LIKE '%_one_active_billing'
            ORDER BY c.relname, t.tgname
            """
        )
        # pg_trigger.tgenabled is Postgres's "char" type, which asyncpg hands
        # back as BYTES -- so a bare == "O" is False for a perfectly enabled
        # trigger and this report cried wolf on every run. Normalized rather
        # than compared raw: a reset that misreports whether rule 1 is armed
        # is worse than one that says nothing at all.
        print("\nConstraint triggers (all must read ENABLED):")
        all_enabled = True
        for r in still_armed:
            raw = r["tgenabled"]
            flag = raw.decode() if isinstance(raw, (bytes, bytearray)) else str(raw)
            enabled = flag == "O"
            all_enabled &= enabled
            state = "ENABLED" if enabled else f"DISABLED (tgenabled={flag!r})"
            print(f"  {r['table_name']:<20} {r['tgname']:<30} {state}")
        if not all_enabled:
            raise SystemExit(
                "REFUSING TO REPORT SUCCESS: a constraint trigger is still "
                "disabled. Re-enable it before using this database."
            )

        print(
            f"\nDone. {left:,} row(s) left across the cleared tables "
            "(0 is the expected result)."
        )
    finally:
        await conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(prog="scripts.reset_operational_data")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--dry-run", action="store_true", help="report, write nothing")
    group.add_argument(
        "--yes", action="store_true",
        help="actually delete. Irreversible -- take a pg_dump first.",
    )
    args = parser.parse_args()
    asyncio.run(run(apply=args.yes))


if __name__ == "__main__":
    main()
