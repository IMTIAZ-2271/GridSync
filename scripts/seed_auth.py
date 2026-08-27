"""Generate and apply db/sql/seed_auth.sql -- the demo login credentials.

The hash cannot be committed as a literal: argon2 salts randomly, so a hash
written into the template once would be a fixed salt shared by every checkout.
This renders the template with a freshly derived hash and applies it.

    python scripts/seed_auth.py            # render and apply
    python scripts/seed_auth.py --render   # render only, print the path

Run after db/sql/seed_orgs.sql -- that is what creates the staff accounts
this file sets a password on.

Requires DATABASE_URL in .env and migration b3f1c9d4a7e2 applied.
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import asyncpg
from argon2 import PasswordHasher
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = PROJECT_ROOT / "db" / "sql" / "seed_auth.sql.template"
OUTPUT = PROJECT_ROOT / "db" / "sql" / "seed_auth.sql"

DEMO_PASSWORD = "demo1234"

# Every account the template touches, and the role each must land with. The
# eight households come from seed_demo.sql; the ten technicians, three
# officials and five installer staff come from db/sql/seed_orgs.sql, which is
# also where their district and firm are decided. See
# scripts/normalize_demo_accounts.py for the numbering.
EXPECTED = {
    **{f"consumer{n}@demo.com": "consumer" for n in range(1, 9)},
    **{f"worker{n}@demo.com": "worker" for n in range(1, 11)},
    **{f"gov{n}@demo.com": "government" for n in range(1, 4)},
    **{f"supplier{n}@demo.com": "supplier" for n in range(1, 6)},
}


def render() -> str:
    # One hash for every account: they share a password, and separate hashes of
    # the same string would only imply a distinction that isn't there.
    digest = PasswordHasher().hash(DEMO_PASSWORD)
    sql = TEMPLATE.read_text(encoding="utf-8").replace("__HASH__", digest)
    OUTPUT.write_text(sql, encoding="utf-8")
    return sql


async def apply(sql: str) -> int:
    load_dotenv(PROJECT_ROOT / ".env")
    url = os.environ["DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://", 1)
    conn = await asyncpg.connect(url)
    try:
        await conn.execute(sql)
        rows = await conn.fetch(
            """
            SELECT email::text AS email, full_name, role::text AS role
            FROM account
            WHERE email = ANY($1::citext[])
            ORDER BY role
            """,
            list(EXPECTED),
        )
    finally:
        await conn.close()

    found = {r["email"]: r["role"] for r in rows}
    for row in rows:
        print(f"  {row['email']:<22} {row['role']:<12} {row['full_name']}")

    missing = [e for e in EXPECTED if e not in found]
    wrong = [e for e, r in found.items() if r != EXPECTED[e]]
    if missing or wrong:
        for e in missing:
            print(f"  MISSING {e}", file=sys.stderr)
        for e in wrong:
            print(f"  WRONG ROLE {e}: {found[e]} != {EXPECTED[e]}", file=sys.stderr)
        return 1
    print(f"\n  password for all {len(EXPECTED)}: {DEMO_PASSWORD}")
    return 0


def main() -> int:
    sql = render()
    if "--render" in sys.argv:
        print(OUTPUT)
        return 0
    return asyncio.run(apply(sql))


if __name__ == "__main__":
    raise SystemExit(main())
