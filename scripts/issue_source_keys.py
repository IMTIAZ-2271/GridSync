"""Give each utility a head-end credential, so it can take part in commissioning.

    python -m scripts.issue_source_keys                 # every active utility
    python -m scripts.issue_source_keys --only-missing  # leave existing ones
    python -m scripts.issue_source_keys --out keys.json

One `telemetry_source` per distribution company. A company without one gets
one; a company with one has its key **rotated in place** -- the source_id every
commissioning row points at stays, the old key stops working, and no device is
touched (device keys are separate). Only the argon2 hash is stored.

The plaintext goes to `source_keys.json` (gitignored), which
`python -m simulator --mode headend` reads. Like `device_keys.json` it is a
development artifact: a real utility would be handed its key once and GridSync
could never recover it. Run this locally, never on the hosted API's ephemeral
disk.
"""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

import asyncpg

from services.api.auth import hash_password
from services.api.db import PROJECT_ROOT, database_url, init_connection
from services.api.queries import sql
from services.ingest.keys import mint_source_key

DEFAULT_KEYFILE = PROJECT_ROOT / "source_keys.json"


async def issue_source_keys(conn: asyncpg.Connection, *, only_missing: bool) -> list[dict]:
    """Create or rotate a head-end per active utility. Returns what was issued,
    plaintext keys included -- the caller decides where they go."""
    issued = []
    for utility in await conn.fetch(sql("utilities_for_source_keys")):
        if only_missing and utility["source_id"] is not None:
            continue
        key = mint_source_key()
        row = await conn.fetchrow(
            sql("upsert_telemetry_source"),
            utility["company_id"], f"{utility['code']} head-end", hash_password(key),
        )
        issued.append({
            "company_id": utility["company_id"],
            "code": utility["code"],
            "name": f"{utility['code']} head-end",
            "source_id": row["source_id"],
            "source_key": key,
            "created": row["created"],
        })
    return issued


async def run(out: Path, only_missing: bool) -> None:
    existing: dict[str, dict] = {}
    if out.exists():
        existing = json.loads(out.read_text(encoding="utf-8")).get("sources", {})

    conn = await asyncpg.connect(database_url())
    await init_connection(conn)
    try:
        async with conn.transaction():
            issued = await issue_source_keys(conn, only_missing=only_missing)
    finally:
        await conn.close()

    # --only-missing keeps the keys already in the file; a full run replaces
    # them, because every one of them was just rotated.
    sources = dict(existing) if only_missing else {}
    for s in issued:
        sources[s["code"]] = {
            "source_id": str(s["source_id"]),
            "source_key": s["source_key"],
            "name": s["name"],
        }
        print(f"  {s['code']:<8} {'created' if s['created'] else 'rotated'}  {s['source_id']}")

    out.write_text(json.dumps({"sources": sources}, indent=2, sort_keys=True) + "\n",
                   encoding="utf-8")
    print(f"\nIssued {len(issued)} head-end key(s); {len(sources)} in {out}")


def main() -> None:
    parser = argparse.ArgumentParser(prog="scripts.issue_source_keys")
    parser.add_argument("--out", type=Path, default=DEFAULT_KEYFILE)
    parser.add_argument(
        "--only-missing",
        action="store_true",
        help="leave utilities that already have a head-end (their keys keep working)",
    )
    args = parser.parse_args()
    asyncio.run(run(args.out, args.only_missing))


if __name__ == "__main__":
    main()
