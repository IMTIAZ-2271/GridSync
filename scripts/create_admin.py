"""Create an admin, or promote an existing household account to one.

    python -m scripts.create_admin admin@example.com --name "Ops Admin"
    python -m scripts.create_admin consumer1@demo.com          # promote
    printf '%s' "$PW" | python -m scripts.create_admin a@b.c --name X --password-stdin

**The only way an admin comes into existence.** Registration never produces the
role, and the admin panel can grant it only to an account that already exists
-- so the first admin of any database, local or hosted, is made here, by
someone with direct access to that database. Point `DATABASE_URL` at Supabase to
create the hosted one.

The password is prompted for (twice, not echoed) and never taken as an argument,
so it does not land in shell history. `--password-stdin` exists for scripted
use. Promoting an existing account keeps its password.

Refuses to promote a worker, an official or an installer's staff account: their
role rests on a profile holding real data, and the panel refuses the same swap
for the same reason. Every run writes an `audit_log` row with a NULL actor --
there is no admin yet to attribute it to -- and action `admin.bootstrap`.
"""
from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import sys

import asyncpg

from services.api.auth import hash_password
from services.api.db import database_url, init_connection
from services.api.queries import sql

MIN_PASSWORD_LENGTH = 10


async def bootstrap_admin(
    conn: asyncpg.Connection,
    email: str,
    full_name: str,
    password_hash: str | None,
) -> dict:
    """Create or promote. Returns {account_id, created, promoted}.

    `password_hash` is required only to create; a promotion keeps the account's
    password. Raises ValueError for anything it will not do.
    """
    email = email.strip().lower()
    async with conn.transaction():
        existing = await conn.fetchrow(sql("bootstrap_find_account"), email)
        if existing is None:
            if password_hash is None:
                raise ValueError("a password is required to create a new admin")
            account_id = await conn.fetchval(
                sql("bootstrap_create_admin"), email, password_hash, full_name.strip()
            )
            result = {"account_id": account_id, "created": True, "promoted": False}
        elif existing["role"] == "admin":
            result = {"account_id": existing["account_id"], "created": False, "promoted": False}
        elif existing["role"] == "consumer":
            await conn.fetchval(
                sql("admin_set_role"), existing["account_id"], "admin", "consumer"
            )
            result = {"account_id": existing["account_id"], "created": False, "promoted": True}
        else:
            raise ValueError(
                f"{email} is a {existing['role']} account; its role rests on profile "
                "data, so it cannot be made admin. Use a separate email."
            )

        if result["created"] or result["promoted"]:
            await conn.fetchval(
                sql("insert_audit_entry"),
                None,
                "admin.bootstrap",
                "account",
                str(result["account_id"]),
                None,
                json.dumps({"role": "admin", "created": result["created"],
                            "via": "scripts/create_admin.py"}),
                None,
            )
    return result


def _read_password(from_stdin: bool) -> str:
    if from_stdin:
        password = sys.stdin.read().rstrip("\n")
    else:
        password = getpass.getpass("Password for the new admin: ")
        if getpass.getpass("Again: ") != password:
            sys.exit("passwords did not match")
    if len(password) < MIN_PASSWORD_LENGTH:
        sys.exit(f"password must be at least {MIN_PASSWORD_LENGTH} characters")
    return password


async def run(email: str, name: str | None, password_stdin: bool) -> None:
    conn = await asyncpg.connect(database_url())
    await init_connection(conn)
    try:
        exists = await conn.fetchval(
            "SELECT 1 FROM account WHERE email = $1::citext", email.strip().lower()
        )
        password_hash = None
        if not exists:
            if not name:
                sys.exit("creating a new admin needs --name")
            password_hash = hash_password(_read_password(password_stdin))
        try:
            result = await bootstrap_admin(conn, email, name or "", password_hash)
        except ValueError as exc:
            sys.exit(str(exc))
    finally:
        await conn.close()

    if result["created"]:
        print(f"Created admin {email.lower()} ({result['account_id']})")
    elif result["promoted"]:
        print(f"Promoted {email.lower()} to admin ({result['account_id']}); "
              "their existing password is unchanged")
    else:
        print(f"{email.lower()} is already an admin; nothing changed")


def main() -> None:
    parser = argparse.ArgumentParser(prog="scripts.create_admin")
    parser.add_argument("email")
    parser.add_argument("--name", help="full name, required when creating")
    parser.add_argument(
        "--password-stdin", action="store_true",
        help="read the password from stdin instead of prompting",
    )
    args = parser.parse_args()
    asyncio.run(run(args.email, args.name, args.password_stdin))


if __name__ == "__main__":
    main()
