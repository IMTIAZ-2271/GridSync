"""The notification panel's read and acknowledge surface.

Every role reads its own inbox from here; nothing writes to it (see
services/api/notify.py). Scoping is by `account_id` from the token in every
statement, so there is no row a caller can reach by guessing an id -- the
mark-read statements are scoped the same way and answer 404 rather than
silently doing nothing, so a guessed id cannot be used to probe for existence.
"""
from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from .auth import CurrentAccount
from .db import Conn
from .queries import sql

router = APIRouter(prefix="/api/notifications", tags=["notifications"])

# The panel is a panel, not an archive. Older notifications stay in the table
# for the audit trail; nobody scrolls a bell dropdown past this.
DEFAULT_LIMIT = 50
MAX_LIMIT = 200


class Notification(BaseModel):
    notification_id: int
    kind: str
    severity: str
    title: str
    body: str | None
    # Loose pointer at whatever this is about -- deliberately not a foreign
    # key, so a notification outlives the row that caused it. The client links
    # only the entity_types it recognises and renders the rest as plain text.
    entity_type: str | None
    entity_id: str | None
    created_at: datetime
    read_at: datetime | None


class NotificationPage(BaseModel):
    items: list[Notification]
    # Served with the list rather than from a separate endpoint, so the badge
    # and the panel can never disagree about how many are unread.
    unread_count: int


class ReadResult(BaseModel):
    marked_read: int
    unread_count: int


async def _unread(conn, account_id) -> int:
    return await conn.fetchval(sql("unread_notification_count"), account_id)


@router.get("", response_model=NotificationPage)
async def list_notifications(
    conn: Conn,
    principal: CurrentAccount,
    unread_only: Annotated[
        bool, Query(description="Only notifications not yet read.")
    ] = False,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
) -> NotificationPage:
    rows = await conn.fetch(
        sql("list_notifications"), principal.account_id, unread_only, limit
    )
    return NotificationPage(
        items=[Notification(**dict(r)) for r in rows],
        unread_count=await _unread(conn, principal.account_id),
    )


@router.post("/{notification_id}/read", response_model=ReadResult)
async def mark_read(
    conn: Conn, notification_id: int, principal: CurrentAccount
) -> ReadResult:
    """Mark one notification read.

    404 covers three cases -- no such notification, one belonging to someone
    else, and one already read. Only the first two are worth distinguishing
    and doing so would leak whether an id exists, so they are not.
    """
    async with conn.transaction():
        marked = await conn.fetchval(
            sql("mark_notification_read"), notification_id, principal.account_id
        )
        if marked is None:
            raise HTTPException(
                status_code=404, detail="no unread notification with that id"
            )
        return ReadResult(
            marked_read=1, unread_count=await _unread(conn, principal.account_id)
        )


@router.post("/read-all", response_model=ReadResult)
async def mark_all_read(conn: Conn, principal: CurrentAccount) -> ReadResult:
    """Mark everything unread as read. Idempotent: a second call marks zero."""
    async with conn.transaction():
        rows = await conn.fetch(
            sql("mark_all_notifications_read"), principal.account_id
        )
        return ReadResult(
            marked_read=len(rows),
            unread_count=await _unread(conn, principal.account_id),
        )
