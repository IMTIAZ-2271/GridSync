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

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .db import create_pool

from .routes_agreements import router as agreements_router
from .routes_analytics import router as analytics_router
from .routes_applications import router as applications_router
from .routes_auth import router as auth_router
from .orgs import router as orgs_router
from .routes_devices import router as devices_router
from .routes_issues import router as issues_router
from .routes_meters import router as meters_router
from .routes_notifications import router as notifications_router
from .routes_ratings import router as ratings_router
from .routes_sites import router as sites_router
from .routes_work_orders import router as work_orders_router
from .routes_workers import router as workers_router

# --------------------------------------------------------------------------
# Pool
#
# The DSN and the per-connection session setup live in db.py, not here:
# services/jobs opens its own pool from the same two functions and must not
# import this module to reach them.
# --------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.pool = await create_pool()
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
app.include_router(meters_router)
app.include_router(issues_router)
app.include_router(notifications_router)
app.include_router(ratings_router)
app.include_router(work_orders_router)
app.include_router(workers_router)
app.include_router(agreements_router)
app.include_router(analytics_router)
app.include_router(applications_router)
