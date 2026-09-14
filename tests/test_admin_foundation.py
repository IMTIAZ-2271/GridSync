"""Admin panel, phase 1: admins exist, and every admin action is audited.

Four things are held down here:

* **`audit_log` is append-only** (migration 19). The one UPDATE it permits is
  the `ON DELETE SET NULL` of its actor, because deleting an account must not be
  blocked by the trail that account left.
* **`account.sessions_valid_after` ends sessions.** `revoked_token` is per
  token and tokens are not stored, so suspending an account, resetting its
  password or signing it out everywhere could not reach a token already issued.
  `get_current_account` now refuses any token issued before the cut-off.
* **The admin account routes**: admin only, every write leaves exactly one
  audit row in the same transaction, and the guards that keep at least one
  admin able to sign in.
* **`scripts/create_admin.py`**, the only way an admin comes into existence.

Routes run in-process on the test's rolled-back connection (the pattern from
test_commissioning_admin_routes.py). The session tests use real tokens and the
real `get_current_account`, so the cut-off is exercised rather than mocked.
"""
from __future__ import annotations

import asyncpg
import httpx
import pytest
import pytest_asyncio

from scripts.create_admin import bootstrap_admin
from services.api.auth import (
    Principal,
    get_current_account,
    hash_password,
    issue_token,
    verify_password,
)
from services.api.db import get_conn
from services.api.main import app

from .factories import make_account, make_official, make_worker, unique_suffix

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


async def make_admin(conn, **overrides) -> str:
    account_id = await make_account(conn, **overrides)
    await conn.execute("UPDATE account SET role = 'admin' WHERE account_id = $1", account_id)
    return account_id


async def make_worker_account(conn, **overrides) -> str:
    """A worker as registration makes one. tests/factories.make_worker attaches
    a worker_profile but leaves account.role at its 'consumer' default, which is
    fine for dispatch tests and wrong for anything that reads the role."""
    account_id = await make_worker(conn, **overrides)
    await conn.execute("UPDATE account SET role = 'worker' WHERE account_id = $1", account_id)
    return account_id


async def audit_rows(conn, entity_id=None, action=None):
    return await conn.fetch(
        """
        SELECT actor_account_id, action, entity_type, entity_id,
               before_state, after_state
        FROM audit_log
        WHERE ($1::text IS NULL OR entity_id = $1)
          AND ($2::text IS NULL OR action = $2)
        ORDER BY audit_id
        """,
        None if entity_id is None else str(entity_id), action,
    )


@pytest_asyncio.fixture
async def as_role(conn):
    """A client acting as a given principal, bypassing token handling."""
    clients = []

    async def _test_conn():
        yield conn

    async def _make(role: str, account_id) -> httpx.AsyncClient:
        async def _principal():
            return Principal(account_id=account_id, role=role,
                             email="t@example.test", full_name="Test", jti=None)
        app.dependency_overrides[get_conn] = _test_conn
        app.dependency_overrides[get_current_account] = _principal
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://api.test"
        )
        clients.append(client)
        return client

    try:
        yield _make
    finally:
        for c in clients:
            await c.aclose()
        app.dependency_overrides.clear()


class _ConnAsPool:
    """get_current_account reads `request.app.state.pool.fetchrow`."""

    def __init__(self, conn):
        self._conn = conn

    async def fetchrow(self, *args):
        return await self._conn.fetchrow(*args)


@pytest_asyncio.fixture
async def with_tokens(conn):
    """A client whose requests go through the REAL get_current_account."""
    async def _test_conn():
        yield conn

    previous = getattr(app.state, "pool", None)
    app.state.pool = _ConnAsPool(conn)
    app.dependency_overrides[get_conn] = _test_conn
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://api.test"
        ) as client:
            yield client
    finally:
        app.dependency_overrides.clear()
        app.state.pool = previous


def bearer(account_id, role="consumer") -> dict:
    token, _ = issue_token(account_id, role, "t@example.test")
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# audit_log is append-only
# ---------------------------------------------------------------------------


async def insert_audit(conn, actor=None) -> int:
    return await conn.fetchval(
        "INSERT INTO audit_log (actor_account_id, action, entity_type, entity_id) "
        "VALUES ($1, 'test.action', 'account', 'x') RETURNING audit_id",
        actor,
    )


async def test_an_audit_row_cannot_be_edited(conn, savepoint):
    audit_id = await insert_audit(conn)

    with pytest.raises(asyncpg.CheckViolationError):
        async with savepoint():
            await conn.execute(
                "UPDATE audit_log SET action = 'rewritten' WHERE audit_id = $1", audit_id
            )


async def test_an_audit_row_cannot_be_deleted(conn, savepoint):
    audit_id = await insert_audit(conn)

    with pytest.raises(asyncpg.CheckViolationError):
        async with savepoint():
            await conn.execute("DELETE FROM audit_log WHERE audit_id = $1", audit_id)


async def test_deleting_an_actor_keeps_their_trail(conn):
    """ON DELETE SET NULL is the one update allowed: an account's deletion must
    not be blocked by the audit rows it left, and the rows must survive it."""
    actor = await make_account(conn)
    audit_id = await insert_audit(conn, actor)

    await conn.execute("DELETE FROM account WHERE account_id = $1", actor)

    row = await conn.fetchrow(
        "SELECT actor_account_id, action FROM audit_log WHERE audit_id = $1", audit_id
    )
    assert row["actor_account_id"] is None
    assert row["action"] == "test.action"


async def test_nulling_the_actor_cannot_smuggle_another_change(conn, savepoint):
    actor = await make_account(conn)
    audit_id = await insert_audit(conn, actor)

    with pytest.raises(asyncpg.CheckViolationError):
        async with savepoint():
            await conn.execute(
                "UPDATE audit_log SET actor_account_id = NULL, action = 'hidden' "
                "WHERE audit_id = $1",
                audit_id,
            )


# ---------------------------------------------------------------------------
# sessions_valid_after
# ---------------------------------------------------------------------------


async def test_a_token_issued_before_the_cutoff_is_refused(conn, with_tokens):
    account_id = await make_account(conn)
    headers = bearer(account_id)
    assert (await with_tokens.get("/api/auth/me", headers=headers)).status_code == 200

    # A cut-off a full second after issue: iat is whole seconds, so this is the
    # smallest step the check can see.
    await conn.execute(
        "UPDATE account SET sessions_valid_after = now() + interval '2 seconds' "
        "WHERE account_id = $1",
        account_id,
    )

    response = await with_tokens.get("/api/auth/me", headers=headers)
    assert response.status_code == 401
    assert response.json()["detail"] == "session ended"


async def test_a_token_issued_after_the_cutoff_works(conn, with_tokens):
    account_id = await make_account(conn)
    await conn.execute(
        "UPDATE account SET sessions_valid_after = now() - interval '1 minute' "
        "WHERE account_id = $1",
        account_id,
    )

    response = await with_tokens.get("/api/auth/me", headers=bearer(account_id))

    assert response.status_code == 200


async def test_no_cutoff_means_every_token_works(conn, with_tokens):
    account_id = await make_account(conn)
    assert await conn.fetchval(
        "SELECT sessions_valid_after FROM account WHERE account_id = $1", account_id
    ) is None

    assert (await with_tokens.get("/api/auth/me", headers=bearer(account_id))).status_code == 200


# ---------------------------------------------------------------------------
# who may call the admin routes
# ---------------------------------------------------------------------------

ADMIN_READS = ["/api/admin/overview", "/api/admin/accounts", "/api/admin/audit"]


@pytest.mark.parametrize("role", ["consumer", "worker", "government", "supplier"])
async def test_admin_routes_refuse_every_other_role(conn, as_role, role):
    client = await as_role(role, await make_account(conn))
    target = await make_account(conn)

    for path in ADMIN_READS:
        assert (await client.get(path)).status_code == 403, path
    for method, path, body in [
        ("PATCH", f"/api/admin/accounts/{target}/status", {"status": "suspended", "reason": "x x x"}),
        ("POST", f"/api/admin/accounts/{target}/sessions/revoke", {"reason": "x x x"}),
        ("POST", f"/api/admin/accounts/{target}/password", {"password": "longenough1", "reason": "x x x"}),
        ("PUT", f"/api/admin/accounts/{target}/admin", {"granted": True, "reason": "x x x"}),
    ]:
        response = await client.request(method, path, json=body)
        assert response.status_code == 403, (method, path)
    assert await audit_rows(conn, target) == []


async def test_a_pending_worker_cannot_reach_admin_routes(conn, with_tokens):
    """The approval gate in get_current_account runs before require_role."""
    worker = await make_worker_account(conn, approval_status="pending")

    response = await with_tokens.get("/api/admin/overview", headers=bearer(worker, "worker"))

    assert response.status_code == 403


# ---------------------------------------------------------------------------
# reads
# ---------------------------------------------------------------------------


async def test_the_account_list_finds_by_email_and_filters(conn, as_role):
    admin = await make_admin(conn)
    tag = unique_suffix()
    wanted = await make_account(conn, email=f"findme-{tag}@example.test")
    client = await as_role("admin", admin)

    body = (await client.get("/api/admin/accounts", params={"q": f"findme-{tag}"})).json()

    assert body["total"] == 1
    assert body["items"][0]["account_id"] == str(wanted)
    assert body["items"][0]["role"] == "consumer"

    admins = (await client.get("/api/admin/accounts", params={"role": "admin", "limit": 200})).json()
    assert str(admin) in {a["account_id"] for a in admins["items"]}
    assert all(a["role"] == "admin" for a in admins["items"])


async def test_the_account_detail_includes_profile_context(conn, as_role):
    admin = await make_admin(conn)
    official = await make_official(conn, "Badda")
    client = await as_role("admin", admin)

    body = (await client.get(f"/api/admin/accounts/{official}")).json()

    assert body["role"] == "government"
    assert body["district"] == "Badda"
    assert "password_hash" not in body


async def test_an_unknown_account_is_404(conn, as_role):
    client = await as_role("admin", await make_admin(conn))
    missing = "00000000-0000-0000-0000-000000000000"

    assert (await client.get(f"/api/admin/accounts/{missing}")).status_code == 404
    response = await client.patch(
        f"/api/admin/accounts/{missing}/status", json={"status": "suspended", "reason": "gone away"}
    )
    assert response.status_code == 404


async def test_the_overview_counts_accounts_and_pending_work(conn, as_role):
    admin = await make_admin(conn)
    await make_worker(conn, approval_status="pending")
    client = await as_role("admin", admin)

    body = (await client.get("/api/admin/overview")).json()

    assert body["accounts_by_role"]["admin"] >= 1
    assert body["pending_workers"] >= 1
    assert "pending_suppliers" in body
    assert isinstance(body["recent_audit"], list)


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


async def test_suspending_an_account_audits_it_and_ends_its_sessions(conn, as_role):
    admin = await make_admin(conn)
    target = await make_account(conn)
    client = await as_role("admin", admin)

    response = await client.patch(
        f"/api/admin/accounts/{target}/status",
        json={"status": "suspended", "reason": "reported for abuse"},
    )

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "suspended"
    row = await conn.fetchrow(
        "SELECT status::text, sessions_valid_after FROM account WHERE account_id = $1", target
    )
    assert row["status"] == "suspended"
    assert row["sessions_valid_after"] is not None
    [entry] = await audit_rows(conn, target)
    assert entry["actor_account_id"] == admin
    assert entry["action"] == "account.status"
    assert '"active"' in entry["before_state"]
    assert '"suspended"' in entry["after_state"]
    assert "reported for abuse" in entry["after_state"]


async def test_setting_the_same_status_changes_nothing(conn, as_role):
    admin = await make_admin(conn)
    target = await make_account(conn)
    client = await as_role("admin", admin)

    response = await client.patch(
        f"/api/admin/accounts/{target}/status", json={"status": "active", "reason": "no change"}
    )

    assert response.status_code == 409
    assert await audit_rows(conn, target) == []


async def test_an_admin_cannot_change_their_own_status(conn, as_role):
    admin = await make_admin(conn)
    client = await as_role("admin", admin)

    response = await client.patch(
        f"/api/admin/accounts/{admin}/status", json={"status": "closed", "reason": "leaving now"}
    )

    assert response.status_code == 409
    assert await audit_rows(conn, admin) == []


async def test_a_reason_is_required(conn, as_role):
    client = await as_role("admin", await make_admin(conn))
    target = await make_account(conn)

    response = await client.patch(
        f"/api/admin/accounts/{target}/status", json={"status": "suspended", "reason": " "}
    )

    assert response.status_code == 422


# ---------------------------------------------------------------------------
# sessions and passwords
# ---------------------------------------------------------------------------


async def test_revoking_sessions_moves_the_cutoff_and_audits(conn, as_role):
    admin = await make_admin(conn)
    target = await make_account(conn)
    client = await as_role("admin", admin)

    response = await client.post(
        f"/api/admin/accounts/{target}/sessions/revoke", json={"reason": "lost phone"}
    )

    assert response.status_code == 200, response.text
    assert await conn.fetchval(
        "SELECT sessions_valid_after IS NOT NULL FROM account WHERE account_id = $1", target
    )
    [entry] = await audit_rows(conn, target)
    assert entry["action"] == "account.sessions_revoked"


async def test_a_password_reset_works_and_never_reaches_the_trail(conn, as_role):
    admin = await make_admin(conn)
    target = await make_account(conn)
    client = await as_role("admin", admin)

    response = await client.post(
        f"/api/admin/accounts/{target}/password",
        json={"password": "Temporary-pass-42", "reason": "locked out, verified by phone"},
    )

    assert response.status_code == 200, response.text
    row = await conn.fetchrow(
        "SELECT password_hash, sessions_valid_after FROM account WHERE account_id = $1", target
    )
    assert verify_password("Temporary-pass-42", row["password_hash"])
    assert row["sessions_valid_after"] is not None
    [entry] = await audit_rows(conn, target)
    assert entry["action"] == "account.password_reset"
    for state in (entry["before_state"] or "", entry["after_state"] or ""):
        assert "Temporary-pass-42" not in state
        assert "argon2" not in state


async def test_a_short_password_is_refused(conn, as_role):
    client = await as_role("admin", await make_admin(conn))
    target = await make_account(conn)

    response = await client.post(
        f"/api/admin/accounts/{target}/password", json={"password": "short", "reason": "too short"}
    )

    assert response.status_code == 422


# ---------------------------------------------------------------------------
# granting and revoking admin
# ---------------------------------------------------------------------------


async def test_a_consumer_can_be_made_admin_and_back(conn, as_role):
    admin = await make_admin(conn)
    target = await make_account(conn)
    client = await as_role("admin", admin)

    granted = await client.put(
        f"/api/admin/accounts/{target}/admin", json={"granted": True, "reason": "new operator"}
    )
    assert granted.status_code == 200, granted.text
    assert granted.json()["role"] == "admin"

    revoked = await client.put(
        f"/api/admin/accounts/{target}/admin", json={"granted": False, "reason": "left the team"}
    )
    assert revoked.status_code == 200, revoked.text
    assert revoked.json()["role"] == "consumer"

    actions = [r["action"] for r in await audit_rows(conn, target)]
    assert actions == ["account.admin_granted", "account.admin_revoked"]
    # A role change ends existing sessions: a token carries its role.
    assert await conn.fetchval(
        "SELECT sessions_valid_after IS NOT NULL FROM account WHERE account_id = $1", target
    )


@pytest.mark.parametrize("maker", ["worker", "government"])
async def test_a_staff_account_cannot_be_made_admin(conn, as_role, maker):
    """Their role rests on a profile row with real data. Swapping `role` alone
    would leave an admin carrying a worker's employer, or an official's code."""
    admin = await make_admin(conn)
    target = await (make_worker_account(conn) if maker == "worker" else make_official(conn))
    client = await as_role("admin", admin)

    response = await client.put(
        f"/api/admin/accounts/{target}/admin", json={"granted": True, "reason": "promote them"}
    )

    assert response.status_code == 409
    assert await audit_rows(conn, target) == []


async def test_an_admin_cannot_revoke_their_own_admin(conn, as_role):
    admin = await make_admin(conn)
    await make_admin(conn)
    client = await as_role("admin", admin)

    response = await client.put(
        f"/api/admin/accounts/{admin}/admin", json={"granted": False, "reason": "stepping down"}
    )

    assert response.status_code == 409


async def test_the_last_active_admin_is_protected(conn, as_role):
    """Reached only when the caller is not themselves an active admin row --
    e.g. a token for an admin suspended a moment ago by someone else. The guard
    counts under lock, so two admins demoting each other cannot both succeed."""
    for_sure_only = await make_admin(conn)
    await conn.execute(
        "UPDATE account SET status = 'suspended' WHERE role = 'admin' AND account_id <> $1",
        for_sure_only,
    )
    caller = await make_account(conn)  # not an admin row; only the principal says so
    client = await as_role("admin", caller)

    response = await client.patch(
        f"/api/admin/accounts/{for_sure_only}/status",
        json={"status": "suspended", "reason": "try to lock everyone out"},
    )

    assert response.status_code == 409
    assert "last active admin" in response.json()["detail"]


# ---------------------------------------------------------------------------
# the audit log
# ---------------------------------------------------------------------------


async def test_the_audit_log_filters_by_entity(conn, as_role):
    admin = await make_admin(conn)
    a, b = await make_account(conn), await make_account(conn)
    client = await as_role("admin", admin)
    for target in (a, b):
        await client.post(f"/api/admin/accounts/{target}/sessions/revoke", json={"reason": "audit me"})

    body = (await client.get("/api/admin/audit", params={"entity_id": str(a)})).json()

    assert body["total"] == 1
    [entry] = body["items"]
    assert entry["entity_id"] == str(a)
    assert entry["actor_account_id"] == str(admin)
    assert entry["actor_email"] is not None


# ---------------------------------------------------------------------------
# scripts/create_admin.py
# ---------------------------------------------------------------------------


async def test_bootstrap_creates_an_admin(conn):
    email = f"Boss-{unique_suffix()}@Example.test"

    result = await bootstrap_admin(conn, email, "The Boss", hash_password("correct-horse-9"))

    assert result["created"] is True
    row = await conn.fetchrow(
        "SELECT email::text, role::text, status::text, password_hash FROM account "
        "WHERE account_id = $1",
        result["account_id"],
    )
    assert row["email"] == email.lower()
    assert row["role"] == "admin"
    assert row["status"] == "active"
    assert verify_password("correct-horse-9", row["password_hash"])
    [entry] = await audit_rows(conn, result["account_id"], "admin.bootstrap")
    assert entry["actor_account_id"] is None


async def test_bootstrap_promotes_an_existing_consumer(conn):
    tag = unique_suffix()
    account_id = await make_account(conn, email=f"promote-{tag}@example.test")

    result = await bootstrap_admin(conn, f"promote-{tag}@example.test", "ignored", None)

    assert result == {"account_id": account_id, "created": False, "promoted": True}
    assert await conn.fetchval("SELECT role::text FROM account WHERE account_id = $1", account_id) == "admin"


async def test_bootstrap_refuses_a_staff_account(conn):
    worker = await make_worker_account(conn)
    email = await conn.fetchval("SELECT email::text FROM account WHERE account_id = $1", worker)

    with pytest.raises(ValueError, match="worker"):
        await bootstrap_admin(conn, email, "ignored", None)


async def test_bootstrap_needs_a_password_to_create(conn):
    with pytest.raises(ValueError, match="password"):
        await bootstrap_admin(conn, f"nobody-{unique_suffix()}@example.test", "Nobody", None)

