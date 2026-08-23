"""GridSync dashboard API.

Read-mostly HTTP surface over the GridSync database for the React client.

This module only bootstraps the app: the connection pool, CORS, and wiring
every router in. The endpoints themselves live one file per resource --
routes_auth.py, routes_sites.py, routes_devices.py, routes_issues.py,
routes_work_orders.py, routes_agreements.py, routes_analytics.py -- mirroring
one controller per screen. orgs.py is the exception to that rule: districts,
distribution companies and supplier companies are reference data that every
portal's forms read, so they live together rather than being split across the
routers that happen to need them. Three conventions govern all of them.

**Every endpoint is authenticated, and authorization has two layers.**
`require_role(...)` decides whether a role may call an endpoint at all;
`visible_site_or_404` decides whether this particular caller may see this
particular row. Both are needed -- a customer may legitimately call
/summary, but only for a site they own. Row scoping is done by selecting a
narrower statement, not by filtering a full result set, so a row the caller
may not see is never fetched. See services/api/auth.py.

**Raw SQL, no ORM.** Statements live in `db/sql/dao/*.sql` and are reached by
name through `queries.sql()`. Handlers translate rows into response models and
do nothing else -- no query construction in Python.

**Money and energy cross the wire as strings.** Postgres NUMERIC arrives as
Decimal, and rule 5 forbids FLOAT for money and energy. Serializing a Decimal
as a JSON number would hand it to a JavaScript double, which is precisely the
lossy step the rule exists to prevent, so the `Money`/`Energy`/`Rate` aliases
in services/api/types.py pin serialization to str. The client parses them
where it needs to plot them, and keeps the exact string wherever it displays a
number a customer is being asked to pay.
"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path

import asyncpg
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .routes_agreements import router as agreements_router
from .routes_analytics import router as analytics_router
from .routes_auth import router as auth_router
from .orgs import router as orgs_router
from .routes_devices import router as devices_router
from .routes_issues import router as issues_router
from .routes_notifications import router as notifications_router
from .routes_sites import router as sites_router
from .routes_work_orders import router as work_orders_router

PROJECT_ROOT = Path(__file__).resolve().parents[2]


# --------------------------------------------------------------------------
# Pool
# --------------------------------------------------------------------------

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


async def _init_connection(conn: asyncpg.Connection) -> None:
    """Pin the session TimeZone for every pooled connection.

    Nothing in db/sql/dao/ depends on this today: the queries compare against
    `now()`, which is an absolute instant, and every timestamptz is serialized
    to UTC on the way out. It is here so that the day someone adds a
    date_trunc, a ::date cast or a bare timestamptz literal to one of those
    files -- all of which resolve against the *session* zone -- they resolve
    against a zone this project named on purpose rather than whatever the
    server was configured with. That is the same discipline CLAUDE.md
    requires of DDL. Asia/Dhaka matches `site.timezone`.
    """
    await conn.execute("SET TIME ZONE 'Asia/Dhaka'")


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.pool = await asyncpg.create_pool(
        database_url(),
        min_size=1,
        max_size=10,
        init=_init_connection,
    )
    try:
        yield
    finally:
        await app.state.pool.close()


app = FastAPI(
    title="GridSync API",
    description="Read-mostly API over the GridSync net-metering database.",
    version="0.1.0",
    lifespan=lifespan,
)

# The Vite dev server. Credentials are carried in an Authorization header
# rather than a cookie, so this list is what stops another origin's script from
# reading responses on a logged-in user's behalf -- keep it exact.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# /api/auth/* is the only unauthenticated surface: register, login, and the
# /me probe (which authenticates itself).
app.include_router(auth_router)
app.include_router(orgs_router)
app.include_router(sites_router)
app.include_router(devices_router)
app.include_router(issues_router)
app.include_router(notifications_router)
app.include_router(work_orders_router)
app.include_router(agreements_router)
app.include_router(analytics_router)
