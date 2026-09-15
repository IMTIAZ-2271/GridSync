"""Refusing repeated failed sign-ins.

Two limits over one sliding window, checked in `POST /api/auth/login` before
the password is verified:

* **per email** -- EMAIL_FAILURES failed attempts, counted since the later of
  the window's start, that email's last successful sign-in, and the account's
  `sessions_valid_after` (so an admin's password reset clears it). Applied to
  emails that match no account exactly as to real ones: a lock that exists only
  for real accounts would say which emails are registered.
* **per client address** -- IP_FAILURES failed attempts from one address across
  every email, which is what catches one password tried against many accounts.

A refused attempt answers 429 with `Retry-After` and never reaches argon2, and
it is not counted, so retrying against a lock does not extend it. The attempt
row is written *before* the check (migration e5b7a3c19d42 says why: a burst of
concurrent guesses must see each other).

The honest cost: anyone who knows an email can keep that account locked by
failing five times every fifteen minutes. Its owner is locked for the window,
not forever, and an admin's password reset or a successful sign-in from before
the failures does not help the attacker. Trading that for unlimited guessing
against an admin account is the right trade here.

**Client address behind a proxy.** Render (and anything like it) terminates the
connection, so `request.client.host` is the proxy's address and every visitor
would share one IP budget. `LOGIN_CLIENT_IP_HEADER` names the header the proxy
puts the real address in; the rightmost comma-separated entry is used, because
that is the one the nearest proxy appended and a client can only forge entries
to its left. Unset, the socket's address is used -- right for local dev, where
there is no proxy. Check what the proxy actually sends before setting it.
"""
from __future__ import annotations

import ipaddress
import math
import os
from datetime import datetime, timedelta

import asyncpg
from fastapi import HTTPException, Request, status

from .queries import sql

WINDOW = timedelta(minutes=15)
EMAIL_FAILURES = 5
IP_FAILURES = 30


def client_ip(request: Request) -> str | None:
    """The caller's address as text PostgreSQL's inet accepts, or None."""
    header = os.environ.get("LOGIN_CLIENT_IP_HEADER", "").strip()
    raw: str | None
    if header:
        value = request.headers.get(header)
        raw = value.split(",")[-1].strip() if value else None
    else:
        raw = request.client.host if request.client else None
    if not raw:
        return None
    try:
        return str(ipaddress.ip_address(raw))
    except ValueError:
        return None


async def begin_attempt(
    conn: asyncpg.Connection, email: str, ip: str | None
) -> int:
    """Record the attempt as failed, then refuse it if either limit is spent.

    Returns the attempt's id for `mark_succeeded`. Raises 429 after marking the
    row `refused`.
    """
    row = await conn.fetchrow(sql("login_attempt_begin"), email, ip)
    attempt_id, now = row["attempt_id"], row["attempted_at"]

    counts = await conn.fetchrow(
        sql("login_failures"), email, ip, WINDOW, attempt_id
    )
    unlock_at: datetime | None = None
    if counts["email_failures"] >= EMAIL_FAILURES:
        unlock_at = counts["email_oldest"] + WINDOW
    if ip is not None and counts["ip_failures"] >= IP_FAILURES:
        ip_unlock = counts["ip_oldest"] + WINDOW
        unlock_at = ip_unlock if unlock_at is None else max(unlock_at, ip_unlock)

    if unlock_at is None:
        return attempt_id

    await conn.execute(sql("login_attempt_finish"), attempt_id, "refused")
    wait = max(1, math.ceil((unlock_at - now).total_seconds()))
    minutes = max(1, math.ceil(wait / 60))
    raise HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        detail=(
            "too many failed sign-in attempts; try again in "
            f"{minutes} minute{'s' if minutes != 1 else ''}"
        ),
        headers={"Retry-After": str(wait)},
    )


async def mark_succeeded(conn: asyncpg.Connection, attempt_id: int) -> None:
    await conn.execute(sql("login_attempt_finish"), attempt_id, "succeeded")
