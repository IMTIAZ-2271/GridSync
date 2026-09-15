"""Admin panel, phase 3: acting across the system, on the record.

Three things:

* **The routes an admin already shared with officials and dispatchers now
  audit an admin's use of them.** They accepted `admin` long before the panel
  existed and wrote nothing when one called. A static test holds the rule for
  every write route that accepts the role, so a new one cannot quietly skip it.
* **Work-order intervention** (`PATCH /api/admin/work-orders/{id}`): release a
  stuck job back to the dispatcher, or cancel it, with every live assignment
  released and the people involved told.
* **Credit adjustments** (`POST /api/admin/billing-points/{id}/credit-adjustments`):
  rule 1's way of changing money -- a new ledger row whose running balance
  continues the connection's own, never below zero.

Plus the unscoped queues: an admin sees every district's registrations.
"""
from __future__ import annotations

import inspect
from datetime import timedelta
from decimal import Decimal

import pytest
from fastapi.routing import APIRoute

from services.api.main import app

from .factories import (
    make_account,
    make_assignment,
    make_billing_point,
    make_meter,
    make_site,
    make_telemetry_source,
    make_work_order,
    make_worker,
)
from .test_admin_foundation import as_role, make_admin  # noqa: F401  (fixture)
from .test_registration_approvals import make_official, make_supplier_staff

# Async tests run under asyncio_mode = auto (pyproject.toml); the one static
# test below is synchronous, so no module-wide asyncio mark.


async def audit_actions(conn, entity_id):
    return [
        r["action"]
        for r in await conn.fetch(
            "SELECT action FROM audit_log WHERE entity_id = $1 ORDER BY audit_id",
            str(entity_id),
        )
    ]


async def notifications(conn, account_id):
    return await conn.fetch(
        "SELECT kind::text AS kind, title, body FROM notification WHERE account_id = $1 "
        "ORDER BY notification_id",
        account_id,
    )


# ---------------------------------------------------------------------------
# every admin-reachable write is audited
# ---------------------------------------------------------------------------

#: Write routes that accept admin but change nothing anyone would audit.
NOT_AN_ACTION = {
    # Marks a list as seen for the caller's own unread dots.
    ("POST", "/api/views/{view_key}/seen"),
    # The admin's own inbox.
    ("POST", "/api/notifications/{notification_id}/read"),
    ("POST", "/api/notifications/read-all"),
    # Logging yourself out.
    ("POST", "/api/auth/logout"),
}


def _api_routes(routes):
    """Every APIRoute, however it was mounted.

    FastAPI 0.141 no longer flattens `include_router` into `app.routes`: each
    included router stays one `_IncludedRouter` holding `original_router`. The
    first version of this guard iterated `app.routes` directly, found no
    APIRoute at all, and passed while checking nothing -- caught only by
    deleting an audit call and watching it still pass.
    """
    for route in routes:
        if isinstance(route, APIRoute):
            yield route
        inner = getattr(route, "original_router", None)
        if inner is not None:
            yield from _api_routes(inner.routes)


def test_the_route_walk_actually_finds_routes():
    """So the guard below can never pass by finding nothing again."""
    paths = {(m, r.path) for r in _api_routes(app.routes) for m in r.methods}
    assert ("POST", "/api/devices/{device_id}/commissioning") in paths
    assert ("PATCH", "/api/admin/work-orders/{order_id}") in paths
    assert len(paths) > 80


def test_every_write_route_an_admin_can_call_is_audited():
    """A static guard, deliberately crude: a handler that accepts `admin` and
    changes state must reference the audit module. Crude is the point -- it
    fails the build the day someone adds `"admin"` to a route and forgets."""
    missing = []
    for route in _api_routes(app.routes):
        writes = route.methods - {"GET", "HEAD", "OPTIONS"}
        if not writes:
            continue
        source = inspect.getsource(route.endpoint)
        if '"admin"' not in source and "Admin" not in source:
            continue
        for method in writes:
            if (method, route.path) in NOT_AN_ACTION:
                continue
            if "audit." not in source:
                missing.append(f"{method} {route.path}")
    assert missing == []


async def test_an_admin_decision_on_a_worker_is_audited_and_an_officials_is_not(conn, as_role):
    admin = await make_admin(conn)
    first = await make_worker(conn, approval_status="pending")
    second = await make_worker(conn, approval_status="pending")
    official = await make_official(conn, "Dhanmondi")

    as_admin = await as_role("admin", admin)
    response = await as_admin.patch(f"/api/workers/{first}/approval", json={"decision": "approve"})
    assert response.status_code == 200, response.text
    assert await audit_actions(conn, first) == ["worker.approval"]

    as_official = await as_role("government", official)
    response = await as_official.patch(f"/api/workers/{second}/approval", json={"decision": "approve"})
    assert response.status_code == 200, response.text
    assert await audit_actions(conn, second) == []


async def test_a_refused_admin_call_leaves_no_audit_row(conn, as_role):
    """The audit row is written inside the route's transaction, so a decision
    that is refused rolls it back with everything else."""
    admin = await make_admin(conn)
    worker = await make_worker(conn)  # already approved
    client = await as_role("admin", admin)

    response = await client.patch(f"/api/workers/{worker}/approval", json={"decision": "reject"})

    assert response.status_code == 409
    assert await audit_actions(conn, worker) == []


async def test_an_admin_retry_of_a_connection_is_audited(conn, as_role, monkeypatch):
    monkeypatch.setenv("COMMISSIONING_ENABLED", "on")
    admin = await make_admin(conn)
    site_id = await make_site(conn)
    company = await conn.fetchval(
        "INSERT INTO distribution_company (code, name) VALUES ('TEST-ADM-DC', 'Test DC') "
        "RETURNING company_id"
    )
    await make_telemetry_source(conn, company)
    await conn.execute(
        "UPDATE billing_point SET distribution_company_id = $2 WHERE site_id = $1", site_id, company
    )
    device_id = await make_meter(conn, site_id)
    client = await as_role("admin", admin)

    response = await client.post(f"/api/devices/{device_id}/commissioning")

    assert response.status_code == 201, response.text
    assert await audit_actions(conn, device_id) == ["device.commissioning_retry"]


# ---------------------------------------------------------------------------
# unscoped queues
# ---------------------------------------------------------------------------


async def test_an_admin_sees_every_districts_registrations(conn, as_role):
    admin = await make_admin(conn)
    dhanmondi = await make_worker(conn, approval_status="pending")
    badda = await make_worker(conn, approval_status="pending")
    await conn.execute(
        "UPDATE worker_profile SET service_district = 'Badda' WHERE account_id = $1", badda
    )
    staff_dhanmondi = await make_supplier_staff(conn, district="Dhanmondi")
    staff_uttara = await make_supplier_staff(conn, district="Uttara")
    official = await make_official(conn, "Dhanmondi")

    as_admin = await as_role("admin", admin)
    workers = {w["account_id"] for w in (await as_admin.get("/api/workers/pending")).json()}
    staff = {s["account_id"] for s in (await as_admin.get("/api/supplier-registrations/pending")).json()}
    assert {str(dhanmondi), str(badda)} <= workers
    assert {str(staff_dhanmondi), str(staff_uttara)} <= staff

    as_official = await as_role("government", official)
    workers = {w["account_id"] for w in (await as_official.get("/api/workers/pending")).json()}
    assert str(dhanmondi) in workers
    assert str(badda) not in workers


# ---------------------------------------------------------------------------
# work-order intervention
# ---------------------------------------------------------------------------


async def dispatched_order(conn):
    """A dispatched order with a lead who accepted and an assistant still offered."""
    dispatcher = await make_account(conn)
    site_id = await make_site(conn)
    order_id = await make_work_order(conn, site_id, dispatcher, status="dispatched")
    lead, assistant = await make_worker(conn), await make_worker(conn)
    now = await conn.fetchval("SELECT now()")
    await make_assignment(conn, order_id, lead, job_role="lead", status="accepted",
                          start_deadline_at=now + timedelta(days=1))
    await make_assignment(conn, order_id, assistant, job_role="assistant", status="offered",
                          offer_expires_at=now + timedelta(hours=3))
    return order_id, dispatcher, site_id, lead, assistant


async def assignment_states(conn, order_id):
    return {
        r["account_id"]: r["status"]
        for r in await conn.fetch(
            "SELECT account_id, status::text AS status FROM work_order_assignment "
            "WHERE order_id = $1",
            order_id,
        )
    }


async def test_releasing_an_order_frees_everyone_and_returns_it_to_draft(conn, as_role):
    admin = await make_admin(conn)
    order_id, dispatcher, _, lead, assistant = await dispatched_order(conn)
    client = await as_role("admin", admin)

    response = await client.patch(
        f"/api/admin/work-orders/{order_id}",
        json={"action": "release", "reason": "crew unreachable since Tuesday"},
    )

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "draft"
    assert await assignment_states(conn, order_id) == {lead: "released", assistant: "released"}
    assert await audit_actions(conn, order_id) == ["work_order.released"]
    assert len(await notifications(conn, lead)) == 1
    assert len(await notifications(conn, assistant)) == 1
    assert len(await notifications(conn, dispatcher)) == 1


async def test_cancelling_an_order_tells_the_household_too(conn, as_role):
    admin = await make_admin(conn)
    order_id, _, site_id, lead, _ = await dispatched_order(conn)
    owner = await conn.fetchval("SELECT account_id FROM site WHERE site_id = $1", site_id)
    client = await as_role("admin", admin)

    response = await client.patch(
        f"/api/admin/work-orders/{order_id}",
        json={"action": "cancel", "reason": "duplicate of another visit"},
    )

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "cancelled"
    assert set((await assignment_states(conn, order_id)).values()) == {"released"}
    assert len(await notifications(conn, owner)) == 1
    assert len(await notifications(conn, lead)) == 1


async def test_releasing_a_draft_order_with_nobody_on_it_is_409(conn, as_role):
    admin = await make_admin(conn)
    order_id = await make_work_order(conn, await make_site(conn), status="draft")
    client = await as_role("admin", admin)

    response = await client.patch(
        f"/api/admin/work-orders/{order_id}", json={"action": "release", "reason": "nothing to do"}
    )

    assert response.status_code == 409
    assert await audit_actions(conn, order_id) == []


@pytest.mark.parametrize("status", ["completed", "failed", "cancelled"])
async def test_a_finished_order_cannot_be_released_or_cancelled(conn, as_role, status):
    admin = await make_admin(conn)
    order_id = await make_work_order(conn, await make_site(conn), status=status)
    client = await as_role("admin", admin)

    for action in ("release", "cancel"):
        response = await client.patch(
            f"/api/admin/work-orders/{order_id}", json={"action": action, "reason": "too late now"}
        )
        assert response.status_code == 409, action
    assert await audit_actions(conn, order_id) == []


async def test_intervention_is_admin_only(conn, as_role):
    order_id, dispatcher, *_ = await dispatched_order(conn)
    client = await as_role("supplier", dispatcher)

    response = await client.patch(
        f"/api/admin/work-orders/{order_id}", json={"action": "cancel", "reason": "not mine to do"}
    )

    assert response.status_code == 403


# ---------------------------------------------------------------------------
# credit adjustments
# ---------------------------------------------------------------------------


async def ledger_entry(conn, point_id, site_id, kwh_after, amount_after, entry_type="earned",
                       kwh_delta=None, amount_delta=None):
    await conn.execute(
        """
        INSERT INTO credit_ledger (billing_point_id, site_id, entry_type, kwh_delta,
                                   amount_delta, balance_kwh_after, balance_amount_after)
        VALUES ($1, $2, $3::ledger_entry_type, $4, $5, $6, $7)
        """,
        point_id, site_id, entry_type,
        kwh_delta if kwh_delta is not None else kwh_after,
        amount_delta if amount_delta is not None else amount_after,
        kwh_after, amount_after,
    )


async def connection_with_credit(conn, kwh="40.0000", amount="250.0000"):
    site_id = await make_site(conn)
    point_id = await conn.fetchval("SELECT point_id FROM billing_point WHERE site_id = $1", site_id)
    await ledger_entry(conn, point_id, site_id, Decimal(kwh), Decimal(amount))
    return site_id, point_id


async def adjust(client, point_id, **body):
    body.setdefault("reason", "meter misread in August, confirmed on site")
    return await client.post(f"/api/admin/billing-points/{point_id}/credit-adjustments", json=body)


async def test_an_adjustment_continues_the_running_balance(conn, as_role):
    admin = await make_admin(conn)
    _, point_id = await connection_with_credit(conn)
    client = await as_role("admin", admin)

    response = await adjust(client, point_id, kwh_delta="-12.5000", amount_delta="-78.1250")

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["balance_kwh"] == "27.5000"
    assert body["balance_amount"] == "171.8750"
    row = await conn.fetchrow(
        "SELECT entry_type::text AS entry_type, kwh_delta, amount_delta, balance_kwh_after, "
        "note, period_id, bill_id FROM credit_ledger WHERE billing_point_id = $1 "
        "ORDER BY entry_id DESC LIMIT 1",
        point_id,
    )
    assert row["entry_type"] == "adjustment"
    assert row["kwh_delta"] == Decimal("-12.5000")
    assert row["balance_kwh_after"] == Decimal("27.5000")
    assert row["period_id"] is None and row["bill_id"] is None
    assert "meter misread" in row["note"]
    # The running balance still agrees with the sum of the deltas.
    assert await conn.fetchval(
        "SELECT sum(kwh_delta) FROM credit_ledger WHERE billing_point_id = $1", point_id
    ) == Decimal("27.5000")
    assert await audit_actions(conn, point_id) == ["credit.adjustment"]


async def test_a_connection_with_no_ledger_starts_at_zero(conn, as_role):
    admin = await make_admin(conn)
    site_id = await make_site(conn)
    point_id = await make_billing_point(conn, site_id, "Fresh")
    client = await as_role("admin", admin)

    response = await adjust(client, point_id, kwh_delta="5.0000", amount_delta="31.2500")

    assert response.status_code == 201, response.text
    assert response.json()["balance_kwh"] == "5.0000"


async def test_an_adjustment_cannot_take_a_balance_below_zero(conn, as_role):
    """A negative credit balance would be a debt, and a debt is a bill."""
    admin = await make_admin(conn)
    _, point_id = await connection_with_credit(conn, kwh="10.0000", amount="62.5000")
    client = await as_role("admin", admin)

    response = await adjust(client, point_id, kwh_delta="-10.0001", amount_delta="0")

    assert response.status_code == 409
    assert await audit_actions(conn, point_id) == []


@pytest.mark.parametrize("body", [
    {"kwh_delta": "0", "amount_delta": "0"},
    {"kwh_delta": "1.00001", "amount_delta": "0"},       # finer than NUMERIC(12,4)
    {"kwh_delta": "1", "amount_delta": "0", "reason": " "},
])
async def test_a_meaningless_adjustment_is_422(conn, as_role, body):
    admin = await make_admin(conn)
    _, point_id = await connection_with_credit(conn)
    client = await as_role("admin", admin)

    response = await adjust(client, point_id, **body)

    assert response.status_code == 422


async def test_an_unknown_connection_is_404(conn, as_role):
    client = await as_role("admin", await make_admin(conn))

    response = await adjust(
        client, "00000000-0000-0000-0000-000000000000", kwh_delta="1", amount_delta="0"
    )

    assert response.status_code == 404


async def test_the_ledger_reads_newest_first_with_its_balance(conn, as_role):
    admin = await make_admin(conn)
    _, point_id = await connection_with_credit(conn)
    client = await as_role("admin", admin)
    await adjust(client, point_id, kwh_delta="2.0000", amount_delta="12.5000")

    body = (await client.get(f"/api/admin/billing-points/{point_id}/ledger")).json()

    assert body["balance_kwh"] == "42.0000"
    assert [e["entry_type"] for e in body["entries"]] == ["adjustment", "earned"]
    assert body["entries"][0]["note"].startswith("meter misread")


@pytest.mark.parametrize("role", ["consumer", "government", "supplier"])
async def test_adjustments_are_admin_only(conn, as_role, role):
    _, point_id = await connection_with_credit(conn)
    client = await as_role(role, await make_account(conn))

    assert (await adjust(client, point_id, kwh_delta="1", amount_delta="0")).status_code == 403
    assert (await client.get(f"/api/admin/billing-points/{point_id}/ledger")).status_code == 403


async def test_the_account_detail_lists_connections_with_balances(conn, as_role):
    admin = await make_admin(conn)
    site_id, point_id = await connection_with_credit(conn, kwh="7.2500", amount="45.3125")
    owner = await conn.fetchval("SELECT account_id FROM site WHERE site_id = $1", site_id)
    client = await as_role("admin", admin)

    body = (await client.get(f"/api/admin/accounts/{owner}")).json()

    [site] = body["sites"]
    [point] = site["connections"]
    assert point["point_id"] == str(point_id)
    assert point["balance_kwh"] == "7.2500"
    assert point["balance_amount"] == "45.3125"
