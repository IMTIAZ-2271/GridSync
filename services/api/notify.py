"""Writing notifications.

One function, used by the handlers that observe something worth telling
someone about. Kept out of the routers because the same event notifies
different people from different places -- a work order completing is written
by the worker's router but read by the household -- and because the jobs
runner will call it from outside the HTTP layer entirely.

**Notifications are never posted by a client.** There is no create endpoint:
a notification is something the system observed, and a forgeable panel is
worse than no panel.

**Failure to notify never fails the operation.** A household's work order was
still completed even if the row telling them about it could not be written,
so `notify` swallows its own errors and reports what happened in the return
value. The caller is inside the same transaction as the thing that actually
mattered; taking that down to save a notification would be the wrong trade.
"""
from __future__ import annotations

import logging
from typing import Literal
from uuid import UUID

import asyncpg

from .queries import sql

log = logging.getLogger(__name__)

Severity = Literal["info", "warning", "critical"]


async def notify(
    conn: asyncpg.Connection,
    account_id: UUID,
    kind: str,
    title: str,
    *,
    body: str | None = None,
    severity: Severity = "info",
    entity_type: str | None = None,
    entity_id: str | None = None,
    dedupe_key: str | None = None,
) -> int | None:
    """Write one notification. Returns its id, or None if nothing was written.

    None means one of three things, and the caller almost never needs to tell
    them apart: the dedupe key had already been used (at-most-once delivery
    working as intended), there was no such account, or the insert failed and
    was logged.

    `dedupe_key` is scoped per account and should name the *event*, not the
    moment -- "wo:{order_id}:completed", not something carrying a timestamp.
    That is what makes a re-run of the jobs sweep silent instead of noisy.
    """
    try:
        return await conn.fetchval(
            sql("create_notification"),
            account_id, kind, severity, title, body,
            entity_type, entity_id, dedupe_key,
        )
    except asyncpg.PostgresError:
        # Deliberately swallowed -- see the module docstring. Logged with the
        # kind and target so a silently undelivered notification is still
        # diagnosable.
        log.exception(
            "could not write notification kind=%s account=%s", kind, account_id
        )
        return None


async def notify_site_owner(
    conn: asyncpg.Connection,
    site_id: UUID,
    kind: str,
    title: str,
    **kwargs,
) -> int | None:
    """Notify whoever owns a site, if anyone does.

    A site always has an account_id, but the seeded estate holds sites whose
    owner is an unclaimed placeholder account; writing to those is harmless
    and they simply never sign in to read it.
    """
    row = await conn.fetchrow(sql("site_owner_account"), site_id)
    if row is None:
        return None
    kwargs.setdefault("entity_type", "site")
    kwargs.setdefault("entity_id", str(site_id))
    return await notify(conn, row["account_id"], kind, title, **kwargs)
