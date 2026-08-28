"""Issue device keys, so hardware can actually authenticate to the ingest API.

`device.device_key_hash` has been written since the very first migration from a
token that was then **discarded**. Every device in the database therefore had a
credential nobody held, and no device could have authenticated to anything --
which is exactly why CLAUDE.md listed "ingest needs a key-issuance path" as a
blocker under NOT DONE.

This is that path, in the form a dev estate needs: it mints a fresh key per
device, stores only the argon2 hash (the same treatment an account password
gets) and writes the plaintext to a local keyfile that the simulator reads.

    python -m scripts.issue_device_keys                 # all live devices
    python -m scripts.issue_device_keys --only-missing  # skip ones in the file
    python -m scripts.issue_device_keys --out keys.json

**The keyfile is a development artifact and must not be committed.** Real
provisioning hands the key to the device once, at manufacture or commissioning,
and never writes it anywhere else; there is no way to recover one afterwards,
which is the point. Rotating is always safe -- it invalidates the old key and
the device is given the new one -- so a lost keyfile costs a re-run, not a
device.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import secrets
from pathlib import Path

import asyncpg

from services.api.auth import hash_password
from services.api.db import PROJECT_ROOT, database_url, init_connection
from services.api.queries import sql

DEFAULT_KEYFILE = PROJECT_ROOT / "device_keys.json"

#: Long enough that guessing is hopeless, short enough to paste. The prefix is
#: there so a leaked key is recognisable in a log as a GridSync device key and
#: can be revoked, rather than looking like any other opaque string.
KEY_PREFIX = "gsk_"


def mint() -> str:
    return KEY_PREFIX + secrets.token_urlsafe(32)


async def run(out: Path, only_missing: bool, dry_run: bool) -> int:
    existing: dict[str, dict] = {}
    if out.exists():
        existing = json.loads(out.read_text(encoding="utf-8")).get("devices", {})

    conn = await asyncpg.connect(database_url())
    await init_connection(conn)
    try:
        devices = await conn.fetch(sql("devices_needing_keys"))
        issued = 0
        keys: dict[str, dict] = dict(existing) if only_missing else {}

        for d in devices:
            device_id = str(d["device_id"])
            if only_missing and device_id in existing:
                continue

            key = mint()
            if not dry_run:
                async with conn.transaction():
                    await conn.fetchrow(
                        sql("rotate_device_key"), d["device_id"], hash_password(key)
                    )
            keys[device_id] = {
                "device_key": key,
                "serial_no": d["serial_no"],
                "device_type": d["device_type"],
                "interval_minutes": d["interval_minutes"],
                "site_id": str(d["site_id"]),
                "site_label": d["site_label"],
                "ac_capacity_kw": str(d["ac_capacity_kw"]),
                "meter_flow": d["meter_flow"],
                "billing_point_id": (
                    str(d["billing_point_id"]) if d["billing_point_id"] else None
                ),
            }
            issued += 1
            print(f"  {d['device_type']:<9} {d['serial_no']:<22} {d['site_label']}")

        if dry_run:
            print(f"\n--dry-run: would issue {issued} key(s), wrote nothing")
            return issued

        out.write_text(
            json.dumps({"devices": keys}, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        print(f"\nIssued {issued} key(s); {len(keys)} device(s) in {out}")
        return issued
    finally:
        await conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(prog="scripts.issue_device_keys")
    parser.add_argument("--out", type=Path, default=DEFAULT_KEYFILE)
    parser.add_argument(
        "--only-missing",
        action="store_true",
        help="leave devices already in the keyfile alone (their keys keep working)",
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    asyncio.run(run(args.out, args.only_missing, args.dry_run))


if __name__ == "__main__":
    main()
