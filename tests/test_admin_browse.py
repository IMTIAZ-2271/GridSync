"""Admin panel, phase 2: a read-only view of every table.

`services/api/admin_browse.py` is the one place the API builds SQL from names
that arrive in a request, so what is held down here is mostly what it refuses:

* only tables that exist in `public` (not partitions, not a crafted name);
* only columns that exist on that table, and values that parse as its type;
* credential hashes are never selected, whoever asks;
* `device_reading` only with a device filter;
* nobody but an admin.

And what it must get right for everything else: NUMERIC arrives as its exact
string (rule 5), counts and pages agree, and a filter uses the column's own
type so an index can serve it.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from .factories import (
    add_reading,
    make_account,
    make_ingest_batch,
    make_meter,
    make_site,
    make_tariff_plan,
    make_tariff_rate,
    unique_suffix,
)
from .test_admin_foundation import as_role, make_admin  # noqa: F401  (fixture)

pytestmark = pytest.mark.asyncio


async def admin_client(conn, as_role):
    return await as_role("admin", await make_admin(conn))


# ---------------------------------------------------------------------------
# the table list
# ---------------------------------------------------------------------------


async def test_the_table_list_covers_the_schema_and_skips_partitions(conn, as_role):
    client = await admin_client(conn, as_role)

    body = (await client.get("/api/admin/tables")).json()

    names = {t["name"] for t in body}
    assert {"account", "audit_log", "bill", "credit_ledger", "device_reading",
            "device_commissioning"} <= names
    # Partitions are device_reading, seen from inside; listing them would be
    # twelve copies of one table.
    assert not any(n.startswith("device_reading_") for n in names)


async def test_the_table_list_describes_columns_and_flags_secrets(conn, as_role):
    client = await admin_client(conn, as_role)

    [account] = [t for t in (await client.get("/api/admin/tables")).json() if t["name"] == "account"]

    columns = {c["name"]: c for c in account["columns"]}
    assert columns["account_id"]["primary_key"] is True
    assert columns["email"]["type"] == "citext"
    assert columns["password_hash"]["masked"] is True
    assert columns["email"]["masked"] is False
    [reading] = [t for t in (await client.get("/api/admin/tables")).json()
                 if t["name"] == "device_reading"]
    assert reading["required_filter"] == "device_id"


# ---------------------------------------------------------------------------
# rows
# ---------------------------------------------------------------------------


async def test_rows_never_carry_a_credential_hash(conn, as_role):
    client = await admin_client(conn, as_role)
    tag = unique_suffix()
    await make_account(conn, email=f"browse-{tag}@example.test")

    body = (await client.get(
        "/api/admin/tables/account/rows",
        params={"filter_col": "email", "filter_val": f"browse-{tag}@example.test"},
    )).json()

    assert body["total"] == 1
    [row] = body["rows"]
    assert row["email"] == f"browse-{tag}@example.test"
    assert row["password_hash"] is None
    assert "password_hash" in body["masked"]


@pytest.mark.parametrize("table, column", [
    ("device", "device_key_hash"),
    ("telemetry_source", "source_key_hash"),
])
async def test_every_credential_column_is_masked(conn, as_role, table, column):
    client = await admin_client(conn, as_role)

    body = (await client.get(f"/api/admin/tables/{table}/rows", params={"limit": 5})).json()

    assert column in body["masked"]
    assert all(r[column] is None for r in body["rows"])


async def test_numeric_arrives_as_its_exact_string(conn, as_role):
    """Rule 5: a rate through a JSON number would pass through a double.

    Also the regression for tariff_rate itself: its default end_time is '24:00',
    which asyncpg cannot decode into a Python time, so the browser selects time
    columns as text -- before that, this table was a 500."""
    client = await admin_client(conn, as_role)
    plan_id = await make_tariff_plan(conn)
    await make_tariff_rate(conn, plan_id, import_rate=Decimal("8.123456"))

    body = (await client.get(
        "/api/admin/tables/tariff_rate/rows",
        params={"filter_col": "plan_id", "filter_val": str(plan_id)},
    )).json()

    [row] = body["rows"]
    assert row["import_rate"] == "8.123456"
    assert row["end_time"] == "24:00:00"


async def test_pages_and_counts_agree(conn, as_role):
    client = await admin_client(conn, as_role)
    site_id = await make_site(conn)
    for label in ("A", "B", "C"):
        await conn.execute(
            "INSERT INTO billing_point (site_id, label) VALUES ($1, $2)", site_id, f"Extra {label}"
        )

    first = (await client.get("/api/admin/tables/billing_point/rows", params={
        "filter_col": "site_id", "filter_val": str(site_id), "limit": 2, "offset": 0,
    })).json()
    second = (await client.get("/api/admin/tables/billing_point/rows", params={
        "filter_col": "site_id", "filter_val": str(site_id), "limit": 2, "offset": 2,
    })).json()

    assert first["total"] == second["total"] == 4  # Main + three extras
    ids = [r["point_id"] for r in first["rows"] + second["rows"]]
    assert len(ids) == len(set(ids)) == 4


async def test_device_reading_needs_its_device(conn, as_role, savepoint):
    client = await admin_client(conn, as_role)
    device_id = await make_meter(conn, await make_site(conn))
    when = datetime(2026, 9, 1, 6, 0, tzinfo=timezone.utc)
    await conn.execute("SELECT create_reading_partition($1)", when.date().replace(day=1))
    batch = await make_ingest_batch(conn, device_id)
    for i in range(3):
        await add_reading(conn, device_id, batch, when + timedelta(minutes=30 * i))

    refused = await client.get("/api/admin/tables/device_reading/rows")
    assert refused.status_code == 422
    assert "device_id" in refused.json()["detail"]

    wrong_filter = await client.get(
        "/api/admin/tables/device_reading/rows",
        params={"filter_col": "import_kwh", "filter_val": "1.0000"},
    )
    assert wrong_filter.status_code == 422

    body = (await client.get(
        "/api/admin/tables/device_reading/rows",
        params={"filter_col": "device_id", "filter_val": str(device_id)},
    )).json()
    assert body["total"] == 3


# ---------------------------------------------------------------------------
# what it refuses
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", [
    "no_such_table",
    "account;DROP TABLE account",
    'account"',
    "device_reading_2026_09",   # a partition, not a table in the list
    "pg_authid",
    "ACCOUNT",                  # names are matched exactly, never folded
])
async def test_only_listed_tables_can_be_read(conn, as_role, name):
    client = await admin_client(conn, as_role)

    response = await client.get(f"/api/admin/tables/{name}/rows")

    assert response.status_code == 404
    assert await conn.fetchval("SELECT to_regclass('public.account') IS NOT NULL")


@pytest.mark.parametrize("column", ["no_such_column", "email; --", "password_hash"])
async def test_only_real_unmasked_columns_can_filter(conn, as_role, column):
    """Filtering on a hash would let someone test guesses against it."""
    client = await admin_client(conn, as_role)

    response = await client.get(
        "/api/admin/tables/account/rows", params={"filter_col": column, "filter_val": "x"}
    )

    assert response.status_code == 422


async def test_a_value_that_is_not_the_columns_type_is_422(conn, as_role):
    client = await admin_client(conn, as_role)

    response = await client.get(
        "/api/admin/tables/account/rows",
        params={"filter_col": "account_id", "filter_val": "not-a-uuid"},
    )

    assert response.status_code == 422


async def test_a_filter_needs_both_halves(conn, as_role):
    client = await admin_client(conn, as_role)

    response = await client.get("/api/admin/tables/account/rows", params={"filter_col": "email"})

    assert response.status_code == 422


async def test_the_page_size_is_capped(conn, as_role):
    client = await admin_client(conn, as_role)

    response = await client.get("/api/admin/tables/account/rows", params={"limit": 201})

    assert response.status_code == 422


@pytest.mark.parametrize("role", ["consumer", "worker", "government", "supplier"])
async def test_only_an_admin_can_browse(conn, as_role, role):
    client = await as_role(role, await make_account(conn))

    assert (await client.get("/api/admin/tables")).status_code == 403
    assert (await client.get("/api/admin/tables/account/rows")).status_code == 403


async def test_the_browser_has_no_write_methods(conn, as_role):
    client = await admin_client(conn, as_role)

    for method in ("POST", "PUT", "PATCH", "DELETE"):
        response = await client.request(method, "/api/admin/tables/account/rows")
        assert response.status_code == 405, method
