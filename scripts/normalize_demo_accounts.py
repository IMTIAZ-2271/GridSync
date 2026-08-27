"""Give every account a numbered demo identity: email, name, National ID, password.

The demo estate had accumulated four naming conventions -- seeded residents on
`@gridsync.test`, hand-registered accounts on `@demo.com` and `@gmail.com`, and
eight throwaway probes on `@example.com` -- so no screen in the app identified
an account the same way twice. This renumbers all of them:

    consumer1..N @demo.com     worker1..N @demo.com
    gov1..N      @demo.com     supplier1..N @demo.com

and gives each the matching full name -- `Consumer 1`, `Worker 1`, `Gov 1`,
`Supplier 1` -- plus a National ID whose leading digit is its role:

    1_000_000_0NN  consumer      3_000_000_0NN  government
    2_000_000_0NN  worker        4_000_000_0NN  supplier

Ten digits, so it passes the same shape check `POST /api/auth/register/*`
applies, and unique across roles because the prefixes cannot collide.

The name is renumbered alongside the address because a name is the *other*
thing a screen identifies an account by: the supplier's fleet table, the
dispatcher's worker picker and the crew on a visit all print `full_name`, and
they were printing `Smoke Test2` and `Device Health Probe` next to a tidy
`consumer17@demo.com`. Deriving both from one index is what stops the two
identifiers naming different accounts.

Every account also gets the demo password, `demo1234`. The seven seeded
residents could not be signed into at all before this -- `seed_demo.sql` writes
a placeholder hash and only `seed_auth.sql` ever replaced it, for four accounts
-- so the richest households in the estate were the ones nobody could
demonstrate.

The mapping is written out by hand rather than derived from a ranking, because
the intended order (seeded residents first, then hand-registered accounts, then
the probes) is an editorial judgement about which account someone should reach
for first, and nothing stored on the row says that. Re-running is safe: an
account already at its target address is left alone.

    python -m scripts.normalize_demo_accounts --dry-run
    python -m scripts.normalize_demo_accounts

Renaming happens in two passes through a temporary address, because
`account_email_key` is a plain (non-deferrable) UNIQUE constraint and the
worker block is a chain -- worker2@demo.com becomes worker3@demo.com, which is
an address another account still holds until it moves too.
"""
from __future__ import annotations

import asyncio
import sys

import asyncpg
from argon2 import PasswordHasher

from services.api.db import database_url

DEMO_PASSWORD = "demo1234"

# (current email, role, index). The index drives both the new address and the
# National ID, so the two can never disagree.
PLAN: list[tuple[str, str, int]] = [
    # Seeded residents 1-8, in Seed Site order: consumer1 is on Seed Site 01
    # (solar, two bills, a credit balance, two issues).
    ("customer@demo.com", "consumer", 1),
    ("seed-resident-2@gridsync.test", "consumer", 2),
    ("seed-resident-3@gridsync.test", "consumer", 3),
    ("seed-resident-4@gridsync.test", "consumer", 4),
    ("seed-resident-5@gridsync.test", "consumer", 5),
    ("seed-resident-6@gridsync.test", "consumer", 6),
    ("seed-resident-7@gridsync.test", "consumer", 7),
    ("seed-resident-8@gridsync.test", "consumer", 8),
    # Hand-registered households.
    ("customer2@demo.com", "consumer", 9),
    ("customer3@demo.com", "consumer", 10),
    ("imtiaz123@gmail.com", "consumer", 11),
    ("imtiaz2@gmail.com", "consumer", 12),
    ("roki123@gmail.com", "consumer", 13),
    # Verification probes. Kept rather than deleted: every one of them owns a
    # site with committed bills, and rule 1's triggers refuse to delete those.
    ("browser-test-onboard@example.com", "consumer", 14),
    ("fix-verify-1787356693@example.com", "consumer", 15),
    ("onboard-smoke-1787355109@example.com", "consumer", 16),
    ("onboard-smoke2-1787355120@example.com", "consumer", 17),
    ("onboard-smoke3-1787355146@example.com", "consumer", 18),
    ("devhealth-165240@demo.com", "consumer", 19),
    ("devhealth-165452@demo.com", "consumer", 20),
    # Workers: the two seeded profiles first, so worker1/worker2 here are the
    # same two people a from-scratch `seed_demo.sql` produces.
    ("worker@demo.com", "worker", 1),          # SEED-EMP-001, six assignments
    ("rakib123@gmail.com", "worker", 2),       # SEED-EMP-002
    ("worker2@demo.com", "worker", 3),         # W-4E476FFF, Badda
    ("worker3@demo.com", "worker", 4),         # W-5ABAD209, Banani
    ("gov@demo.com", "government", 1),         # Badda
    ("gov2@demo.com", "government", 2),        # Gulshan
    ("supplier@demo.com", "supplier", 1),      # Noor Energy Systems
]

PREFIX = {"consumer": "consumer", "worker": "worker",
          "government": "gov", "supplier": "supplier"}
# The display name mirrors the email's local part exactly, abbreviation and
# all, so `Gov 1` and gov1@demo.com are visibly the same account.
NAME = {"consumer": "Consumer", "worker": "Worker",
        "government": "Gov", "supplier": "Supplier"}
NID_BASE = {"consumer": 1_000_000_000, "worker": 2_000_000_000,
            "government": 3_000_000_000, "supplier": 4_000_000_000}


def target(role: str, n: int) -> tuple[str, str, str]:
    return (f"{PREFIX[role]}{n}@demo.com",
            f"{NAME[role]} {n}",
            str(NID_BASE[role] + n))


async def main() -> int:
    dry_run = "--dry-run" in sys.argv
    conn = await asyncpg.connect(database_url())
    try:
        async with conn.transaction():
            rows = await conn.fetch(
                "SELECT account_id, email::text AS email, role::text AS role,"
                "       full_name, national_id FROM account"
            )
            by_email = {r["email"].lower(): r for r in rows}
            resolved: list[tuple[asyncpg.Record, str, str, str, str]] = []
            missing: list[str] = []

            # Which column identifies an account is decided once, for the whole
            # estate, rather than per row -- because the worker block is a
            # rename chain and its two halves overlap. `worker2@demo.com` is
            # entry 2's *target* and entry 3's *old* address at the same time,
            # so a per-row "old address, else target" fallback hands one row to
            # two entries the moment the estate has already been renumbered.
            #
            # The whole-estate signal is unambiguous: an old address that is
            # not itself somebody's target only exists before the first run.
            targets = {target(role, n)[0] for _, role, n in PLAN}
            legacy = [o.lower() for o, _, _ in PLAN if o.lower() not in targets]
            renumbered = not any(o in by_email for o in legacy)

            for old, role, n in PLAN:
                new_email, name, nid = target(role, n)
                row = (by_email.get(new_email) if renumbered
                       else by_email.get(old.lower()))
                if row is None:
                    missing.append(new_email if renumbered else old)
                    continue
                if row["role"] != role:
                    print(f"  ROLE MISMATCH {old}: {row['role']} != {role}",
                          file=sys.stderr)
                    return 1
                resolved.append((row, row["email"], new_email, name, nid))

            if missing:
                for m in missing:
                    print(f"  MISSING {m}", file=sys.stderr)
                return 1

            planned = {r["account_id"] for r, *_ in resolved}
            if len(planned) != len(resolved):
                # Two PLAN entries resolved to one row -- an old address is
                # gone and its fallback landed on a neighbour's target.
                print("  PLAN resolves two entries to the same account",
                      file=sys.stderr)
                return 1
            for r in rows:
                if r["account_id"] not in planned:
                    print(f"  UNPLANNED ACCOUNT {r['email']} ({r['role']}) --"
                          " add it to PLAN", file=sys.stderr)
                    return 1

            for row, old, new, name, nid in resolved:
                unchanged = old == new and row["full_name"] == name
                mark = "  " if unchanged else "->"
                print(f"  {mark} {old:<38} {new:<22} {name:<14} {nid}")

            if dry_run:
                print("\n  --dry-run: nothing written")
                return 0

            digest = PasswordHasher().hash(DEMO_PASSWORD)

            # Pass 1: park every account that is moving at an address nothing
            # else can hold, so a chain (worker2 -> worker3 -> worker4) cannot
            # collide with itself mid-rename.
            for row, old, new, _, _nid in resolved:
                if old != new:
                    await conn.execute(
                        "UPDATE account SET email = $2 WHERE account_id = $1",
                        row["account_id"],
                        f"moving-{row['account_id']}@invalid",
                    )
            # Pass 2: final address, name, National ID and password, together.
            for row, old, new, name, nid in resolved:
                await conn.execute(
                    "UPDATE account"
                    "   SET email = $2, full_name = $3, national_id = $4,"
                    "       password_hash = $5, updated_at = now()"
                    " WHERE account_id = $1",
                    row["account_id"], new, name, nid, digest,
                )

            left = await conn.fetchval(
                "SELECT count(*) FROM account WHERE email LIKE '%@invalid'"
            )
            assert left == 0, f"{left} accounts stranded at a temporary address"

        print(f"\n  {len(resolved)} accounts renumbered."
              f" Password for all of them: {DEMO_PASSWORD}")
        return 0
    finally:
        await conn.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
