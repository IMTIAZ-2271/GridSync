"""Repeated failed sign-ins are refused (migration e5b7a3c19d42, login_limits.py).

The real login route, in-process on the test's rolled-back connection. Each
test gets its own client address from TEST-NET ranges so committed rows from
signing in to the local dev server can never count against it.
"""
from __future__ import annotations

from datetime import timedelta
from itertools import count

import httpx
import pytest
import pytest_asyncio
from starlette.requests import Request

from services.api import login_limits
from services.api.auth import hash_password
from services.api.db import get_conn
from services.api.main import app
from services.jobs.maintenance import prune_login_attempts

from .test_jobs import pool_of
from .factories import make_account, unique_suffix

pytestmark = pytest.mark.asyncio

PASSWORD = "the-right-password"
_hosts = count(1)


def fresh_ip() -> str:
    n = next(_hosts)
    return f"198.51.{100 + n // 250}.{n % 250 + 1}"


@pytest_asyncio.fixture
async def login(conn):
    """login(email, password, ip=None) -> response, through the real route."""
    clients: dict[str, httpx.AsyncClient] = {}

    async def _test_conn():
        yield conn

    app.dependency_overrides[get_conn] = _test_conn
    default_ip = fresh_ip()

    async def _login(email, password, ip=None, headers=None):
        ip = ip or default_ip
        if ip not in clients:
            clients[ip] = httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app, client=(ip, 40000)),
                base_url="http://api.test",
            )
        return await clients[ip].post(
            "/api/auth/login",
            json={"email": email, "password": password},
            headers=headers,
        )

    try:
        yield _login
    finally:
        for c in clients.values():
            await c.aclose()
        app.dependency_overrides.clear()


async def account_with_password(conn, status="active") -> str:
    email = f"limits-{unique_suffix()}@example.test"
    account_id = await make_account(conn, email=email)
    await conn.execute(
        "UPDATE account SET password_hash = $2, status = $3::account_status "
        "WHERE account_id = $1",
        account_id, hash_password(PASSWORD), status,
    )
    return email


async def old_failures(conn, *, email, ip=None, n, minutes_ago):
    await conn.executemany(
        "INSERT INTO login_attempt (email, client_ip, attempted_at) "
        "VALUES ($1, $2::inet, clock_timestamp() - make_interval(mins => $3))",
        [(email, ip, minutes_ago)] * n,
    )


async def outcomes(conn, email):
    rows = await conn.fetch(
        "SELECT outcome FROM login_attempt WHERE email = $1 ORDER BY attempt_id", email
    )
    return [r["outcome"] for r in rows]


# ---------------------------------------------------------------------------
# per email
# ---------------------------------------------------------------------------


async def test_five_wrong_passwords_lock_the_sixth_attempt(conn, login):
    email = await account_with_password(conn)
    for _ in range(5):
        assert (await login(email, "wrong")).status_code == 401

    r = await login(email, "wrong")
    assert r.status_code == 429
    assert "too many failed sign-in attempts" in r.json()["detail"]
    assert 890 <= int(r.headers["Retry-After"]) <= 900


async def test_the_right_password_is_refused_while_locked(conn, login):
    email = await account_with_password(conn)
    for _ in range(5):
        await login(email, "wrong")

    assert (await login(email, PASSWORD)).status_code == 429


async def test_refused_attempts_do_not_extend_the_lock(conn, login):
    email = await account_with_password(conn)
    for _ in range(5):
        await login(email, "wrong")
    for _ in range(4):
        assert (await login(email, "wrong")).status_code == 429

    assert await outcomes(conn, email) == ["failed"] * 5 + ["refused"] * 4


async def test_an_unknown_email_locks_exactly_like_a_real_one(conn, login):
    """Otherwise the lock would say which emails are registered."""
    real = await account_with_password(conn)
    ghost = f"nobody-{unique_suffix()}@example.test"
    for email in (real, ghost):
        for _ in range(5):
            assert (await login(email, "wrong")).status_code == 401

    a, b = await login(real, "wrong"), await login(ghost, "wrong")
    assert a.status_code == b.status_code == 429
    assert a.json() == b.json()


async def test_the_email_is_counted_case_insensitively(conn, login):
    email = await account_with_password(conn)
    for i in range(5):
        await login(email.upper() if i % 2 else email, "wrong")

    assert (await login(email.title(), "wrong")).status_code == 429


async def test_a_successful_sign_in_starts_the_count_again(conn, login):
    email = await account_with_password(conn)
    for _ in range(4):
        await login(email, "wrong")
    assert (await login(email, PASSWORD)).status_code == 200
    for _ in range(4):
        assert (await login(email, "wrong")).status_code == 401

    assert (await login(email, PASSWORD)).status_code == 200


async def test_failures_older_than_the_window_do_not_count(conn, login):
    email = await account_with_password(conn)
    await old_failures(conn, email=email, n=5, minutes_ago=16)

    assert (await login(email, PASSWORD)).status_code == 200


async def test_retry_after_is_when_the_oldest_failure_ages_out(conn, login):
    email = await account_with_password(conn)
    await old_failures(conn, email=email, n=1, minutes_ago=10)
    await old_failures(conn, email=email, n=4, minutes_ago=2)

    r = await login(email, PASSWORD)
    assert r.status_code == 429
    assert 290 <= int(r.headers["Retry-After"]) <= 300
    assert r.json()["detail"].endswith("in 5 minutes")


async def test_an_admin_password_reset_lifts_the_lock(conn, login):
    """sessions_valid_after is what every admin account action sets."""
    email = await account_with_password(conn)
    for _ in range(5):
        await login(email, "wrong")
    await conn.execute(
        "UPDATE account SET sessions_valid_after = clock_timestamp() "
        "WHERE email = $1",
        email,
    )

    assert (await login(email, PASSWORD)).status_code == 200


async def test_the_right_password_on_a_suspended_account_is_not_a_guess(conn, login):
    email = await account_with_password(conn, status="suspended")

    r = await login(email, PASSWORD)
    assert r.status_code == 403
    assert await outcomes(conn, email) == ["succeeded"]


async def test_a_giant_email_is_refused_before_anything_is_written(conn, login):
    email = "x" * 400 + "@example.test"
    assert (await login(email, "wrong")).status_code == 422
    assert await outcomes(conn, email) == []


# ---------------------------------------------------------------------------
# per client address
# ---------------------------------------------------------------------------


async def test_one_address_spraying_many_emails_is_locked(conn, login):
    ip = fresh_ip()
    for _ in range(30):
        await old_failures(conn, email=f"spray-{unique_suffix()}@example.test",
                           ip=ip, n=1, minutes_ago=1)
    email = await account_with_password(conn)

    assert (await login(email, PASSWORD, ip=ip)).status_code == 429
    assert (await login(email, PASSWORD, ip=fresh_ip())).status_code == 200


async def test_twenty_nine_failures_from_an_address_are_not_yet_a_lock(conn, login):
    ip = fresh_ip()
    for _ in range(29):
        await old_failures(conn, email=f"spray-{unique_suffix()}@example.test",
                           ip=ip, n=1, minutes_ago=1)
    email = await account_with_password(conn)

    assert (await login(email, PASSWORD, ip=ip)).status_code == 200


# ---------------------------------------------------------------------------
# client address resolution
# ---------------------------------------------------------------------------


def _request(headers=None, host="10.0.0.5") -> Request:
    return Request({
        "type": "http",
        "headers": [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()],
        "client": (host, 1234) if host else None,
    })


async def test_without_a_configured_header_the_socket_address_is_used(monkeypatch):
    monkeypatch.delenv("LOGIN_CLIENT_IP_HEADER", raising=False)
    req = _request({"X-Forwarded-For": "203.0.113.9"})
    assert login_limits.client_ip(req) == "10.0.0.5"


async def test_a_configured_header_takes_its_rightmost_entry(monkeypatch):
    """A client can prepend anything; only the proxy's own entry is trusted."""
    monkeypatch.setenv("LOGIN_CLIENT_IP_HEADER", "X-Forwarded-For")
    req = _request({"X-Forwarded-For": "1.2.3.4, 203.0.113.9"})
    assert login_limits.client_ip(req) == "203.0.113.9"


async def test_an_unparseable_address_is_no_address(monkeypatch):
    monkeypatch.setenv("LOGIN_CLIENT_IP_HEADER", "X-Forwarded-For")
    assert login_limits.client_ip(_request({"X-Forwarded-For": "not-an-ip"})) is None
    assert login_limits.client_ip(_request()) is None
    monkeypatch.delenv("LOGIN_CLIENT_IP_HEADER")
    assert login_limits.client_ip(_request(host="testclient")) is None


# ---------------------------------------------------------------------------
# the prune job
# ---------------------------------------------------------------------------


async def test_the_prune_job_deletes_only_old_attempts(conn):
    email = f"prune-{unique_suffix()}@example.test"
    await old_failures(conn, email=email, n=2, minutes_ago=25 * 60)
    await old_failures(conn, email=email, n=3, minutes_ago=5)

    result = await prune_login_attempts(pool_of(conn), timedelta(days=1))

    assert result["deleted"] >= 2
    assert await outcomes(conn, email) == ["failed"] * 3
