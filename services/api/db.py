"""The pooled-connection dependency every router shares.

`request.app.state.pool` rather than a closed-over `app` global: the pool is
created once in main.py's lifespan, but routers live in their own modules and
must not import `app` from main.py to reach it -- that would be a circular
import the moment main.py imports the router back.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated

import asyncpg
from fastapi import Depends, Request


async def get_conn(request: Request) -> AsyncIterator[asyncpg.Connection]:
    """Yield a pooled connection, returned to the pool when the request ends."""
    async with request.app.state.pool.acquire() as conn:
        yield conn


Conn = Annotated[asyncpg.Connection, Depends(get_conn)]
