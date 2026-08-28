"""Read/unread watermarks for lists.

One row per account per list, holding when that account last opened it. A row
in the list is unread when it is newer than the watermark.

**The server never counts unread rows.** It stores and returns timestamps; the
page compares them against rows it has already fetched. That is deliberate:
counting server-side would mean re-implementing every list's scoping rules --
which sites a consumer owns, which complaints a supplier is near, which
district an official governs -- as a second query beside the first, and the two
would drift. The list is the authority on what is in the list.

The mark-seen call returns the watermark it *replaced*, which is what makes the
highlight work: a page marks itself seen on open, highlights rows newer than
the returned value, and on the next load the watermark has moved past them so
nothing highlights. The arrivals are visible on exactly the visit that clears
them.
"""
from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path
from pydantic import BaseModel

from .auth import Principal, require_role
from .db import Conn
from .queries import sql

router = APIRouter(tags=["views"])

#: Every list that carries an unread indicator, by portal. A closed set rather
#: than free text: `view_key` is a text column so adding a page is not a
#: migration, but an unknown key arriving from a client is a bug -- most likely
#: a typo -- and accepting it would silently create a watermark nothing ever
#: reads, and an indicator that never clears.
VIEW_KEYS: frozenset[str] = frozenset({
    # Consumer
    "consumer:applications",
    "consumer:meters",
    "consumer:bills",
    "consumer:issues",
    "consumer:visits",
    # Worker
    "worker:orders",
    "worker:issues",
    # Government
    "government:agreements",
    "government:meter-applications",
    "government:workers",
    # Supplier
    "supplier:applications",
    "supplier:issues",
    "supplier:dispatch",
})


class ViewState(BaseModel):
    view_key: str
    last_viewed_at: datetime


class ViewSeen(BaseModel):
    view_key: str
    #: Where the watermark was before this call. None when this account has
    #: never opened the list, which means every row in it is new.
    previous_viewed_at: datetime | None
    last_viewed_at: datetime


@router.get("/api/views", response_model=list[ViewState])
async def my_view_states(
    conn: Conn,
    principal: Annotated[
        Principal,
        Depends(require_role("consumer", "worker", "government", "supplier", "admin")),
    ],
) -> list[ViewState]:
    """Every watermark this account holds, in one request.

    Scoped by `account_id` from the token and nothing else, so there is no
    ownership check to get wrong -- an account can only ever read its own.
    """
    rows = await conn.fetch(sql("list_view_states"), principal.account_id)
    return [ViewState(**dict(r)) for r in rows]


@router.post("/api/views/{view_key}/seen", response_model=ViewSeen)
async def mark_view_seen(
    conn: Conn,
    principal: Annotated[
        Principal,
        Depends(require_role("consumer", "worker", "government", "supplier", "admin")),
    ],
    view_key: Annotated[str, Path(max_length=64)],
) -> ViewSeen:
    """Record that this account has just opened one list.

    Idempotent in the only sense that matters: calling it twice moves the
    watermark twice, and the second call reports a `previous_viewed_at` a
    moment earlier -- so a double-render highlights nothing the second time
    rather than re-lighting rows the user has now seen.
    """
    if view_key not in VIEW_KEYS:
        raise HTTPException(status_code=422, detail=f"unknown view '{view_key}'")

    row = await conn.fetchrow(sql("mark_view_seen"), principal.account_id, view_key)
    return ViewSeen(
        view_key=view_key,
        previous_viewed_at=row["previous_viewed_at"],
        last_viewed_at=row["last_viewed_at"],
    )
