"""The commissioning sweep: a handshake past its stored deadline fails.

Decision 3 -- deadlines change state. An offer no head-end claimed inside its
24 hours, or an activation whose key never signed an accepted batch inside its
hour, becomes `failed`, and the district office is told. The deadlines were
written onto the row by the API and by ingest; this sweep only makes the
consequence happen, which is why no duration lives here.

**A lapsed activation also loses its key.** Nothing else would take it away:
ingest authenticates a key regardless of the handshake's state, so a failed
handshake with a working key would be a credential attached to an attempt the
system has declared dead. Revoked in the same transaction that fails the row.

Each row is its own transaction, guarded on the status and deadline the sweep
read. A head-end whose first batch landed a moment ago made the row live; the
guard matches nothing and the sweep leaves it alone.
"""
from __future__ import annotations

import asyncpg

from ..api.notify import notify
from ..api.queries import sql

_REASON = {"offered": "offer_expired", "activated": "activation_expired"}


async def sweep_commissioning(pool: asyncpg.Pool, limit: int) -> dict[str, int]:
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql("expiring_commissionings"), limit)

    expired = revoked = 0
    for row in rows:
        async with pool.acquire() as conn, conn.transaction():
            ended = await conn.fetchrow(
                sql("expire_commissioning"), row["commissioning_id"], row["status"]
            )
            if ended is None:
                continue
            expired += 1

            if ended["activation_count"] > 0:
                await conn.fetchval(sql("revoke_device_key"), row["device_id"])
                revoked += 1

            reason = _REASON[row["status"]]
            head_end = row["head_end"] or "The utility's head-end"
            body = (
                f"{head_end} never claimed meter {row['serial_no']} at "
                f"{row['site_label']}, so it is not reporting. Retry the "
                "connection once the head-end is running."
                if row["status"] == "offered" else
                f"{head_end} took the key for meter {row['serial_no']} at "
                f"{row['site_label']} but delivered no readings in time. The key "
                "has been revoked; retry the connection."
            )
            for official in await conn.fetch(
                sql("officials_for_district"), row["district"]
            ):
                await notify(
                    conn,
                    official["account_id"],
                    "device_commissioning",
                    "A meter could not be connected",
                    body=body,
                    severity="warning",
                    entity_type="device",
                    entity_id=str(row["device_id"]),
                    dedupe_key=f"commissioning:{row['commissioning_id']}:{reason}",
                )

    return {"found": len(rows), "expired": expired, "keys_revoked": revoked}
