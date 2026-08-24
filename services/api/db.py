"""Database connection: the DSN, the per-connection session setup, and the
pooled-connection dependency every router shares.

`get_conn` reads `request.app.state.pool` rather than a closed-over `app`
global: the pool is created once in main.py's lifespan, but routers live in
their own modules and must not import `app` from main.py to reach it -- that
would be a circular import the moment main.py imports the router back.

`database_url` and `init_connection` live here rather than in main.py because
`services/jobs` needs both and must not import main.py to get them: importing
main.py constructs the FastAPI app, wires nine routers and installs CORS
middleware, none of which a background process has any use for. Everything a
non-HTTP caller needs to open a correctly-configured pool is in this module.
"""
from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

import asyncpg
from dotenv import load_dotenv
from fastapi import Depends, Request

PROJECT_ROOT = Path(__file__).resolve().parents[2]

# The zone every session is pinned to. See init_connection.
SESSION_TIME_ZONE = "Asia/Dhaka"


def database_url() -> str:
    """The DSN asyncpg wants.

    load_dotenv gets an explicit path: called bare it resolves against the
    *calling* file and quietly picks up the wrong .env when the app is started
    from another directory. tests/conftest.py and db/migrations/env.py do the
    same thing.
    """
    load_dotenv(PROJECT_ROOT / ".env")
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL is not set; copy .env.example to .env")
    # Alembic's URL carries SQLAlchemy's dialect suffix; asyncpg wants it bare.
    return url.replace("postgresql+asyncpg://", "postgresql://", 1)


async def init_connection(conn: asyncpg.Connection) -> None:
    """Pin the session TimeZone for every pooled connection.

    Nothing in db/sql/dao/ depended on this when it was written: the queries
    compared against `now()`, which is an absolute instant, and every
    timestamptz is serialized to UTC on the way out. It is here so that a
    date_trunc, a ::date cast or a bare timestamptz literal added to one of
    those files -- all of which resolve against the *session* zone -- resolves
    against a zone this project named on purpose rather than whatever the
    server was configured with. That is the same discipline CLAUDE.md requires
    of DDL. Asia/Dhaka matches `site.timezone`.

    db/sql/dao/jobs_queries.sql now does depend on it: the rollup buckets
    readings into local days and the consumption sweep into local months, so a
    connection opened without this would summarise someone else's calendar.
    """
    await conn.execute(f"SET TIME ZONE '{SESSION_TIME_ZONE}'")


async def create_pool(min_size: int = 1, max_size: int = 10) -> asyncpg.Pool:
    """A pool with the session setup applied. The API and the jobs runner both
    open theirs through here so neither can drift from the other's session
    configuration."""
    return await asyncpg.create_pool(
        database_url(), min_size=min_size, max_size=max_size, init=init_connection
    )


@asynccontextmanager
async def pool_context(**kwargs) -> AsyncIterator[asyncpg.Pool]:
    """`create_pool` for a caller with no lifespan to hang it on."""
    pool = await create_pool(**kwargs)
    try:
        yield pool
    finally:
        await pool.close()


async def get_conn(request: Request) -> AsyncIterator[asyncpg.Connection]:
    """Yield a pooled connection, returned to the pool when the request ends."""
    async with request.app.state.pool.acquire() as conn:
        yield conn


Conn = Annotated[asyncpg.Connection, Depends(get_conn)]
