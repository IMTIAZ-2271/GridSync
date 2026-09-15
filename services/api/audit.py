"""Writing the audit trail.

`audit_log` has existed since the first billing migration and, until the admin
panel, nothing wrote to it. Every admin action now writes exactly one row, in
the same transaction as the change it describes.

**Unlike `notify()`, this does not swallow errors.** A notification that fails
to write costs a person a heads-up; an admin action that happened without its
audit row is an action nobody can account for. If the row cannot be written,
the action rolls back with it.

What goes in `before_state` / `after_state` is chosen by the caller and is the
*decision*, not the row: the fields that changed, plus the admin's stated
reason. Secrets never go in -- a password reset records that it happened, not
what the password became or what hash replaced which. The table is append-only
(migration a7c3e9f15b20), so anything written here is written forever.
"""
from __future__ import annotations

import json
from typing import Any
from uuid import UUID

import asyncpg
from fastapi import Request

from .queries import sql


def _client_ip(request: Request | None) -> str | None:
    if request is None or request.client is None:
        return None
    return request.client.host


async def record(
    conn: asyncpg.Connection,
    *,
    actor_account_id: UUID | None,
    action: str,
    entity_type: str,
    entity_id: str | UUID | None,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
    request: Request | None = None,
) -> int:
    """Append one audit row and return its id. Call inside the action's
    transaction. `default=str` renders UUIDs, datetimes and Decimals as text,
    which is how the portal displays them anyway (rule 5: never a float)."""
    return await conn.fetchval(
        sql("insert_audit_entry"),
        actor_account_id,
        action,
        entity_type,
        None if entity_id is None else str(entity_id),
        None if before is None else json.dumps(before, default=str, sort_keys=True),
        None if after is None else json.dumps(after, default=str, sort_keys=True),
        _client_ip(request),
    )


async def admin_action(
    conn: asyncpg.Connection,
    principal: Any,
    action: str,
    entity_type: str,
    entity_id: str | UUID | None,
    detail: Any = None,
) -> None:
    """Record an admin's use of a route officials and dispatchers share.

    Many routes accepted `admin` long before the admin panel existed -- approving
    a registration, ordering a visit, dispatching it -- and wrote nothing when
    an admin called them. Call this as the first statement inside the route's
    own transaction: it records only when the caller is an admin, and a refusal
    later in the route rolls the row back with everything else, so the trail
    holds only actions that happened.

    `detail` is the request body (a pydantic model or dict). No client IP: these
    routes do not take the request, and the actor and reason are what matter.
    """
    if getattr(principal, "role", None) != "admin":
        return
    if detail is not None and hasattr(detail, "model_dump"):
        detail = detail.model_dump(mode="json")
    await record(
        conn,
        actor_account_id=principal.account_id,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        after=detail,
    )
