"""The admin data browser: a read-only view of every table.

    GET /api/admin/tables                  every table, its columns and size
    GET /api/admin/tables/{name}/rows      one page of rows, optionally filtered

**This module is the one place the API builds SQL from names in a request** --
a deliberate, contained exception to "handlers do no query construction".
Forty-seven tables cannot each have a hand-written statement, and a browser that
only covered some of them would not be "all data". What keeps it safe:

* **No identifier from the request reaches SQL.** A table or column name is
  looked up in the catalog (`admin_browse_tables` / `admin_browse_columns`),
  and what goes into the statement is PostgreSQL's own `quote_ident` of the
  catalog row. A name that is not in the catalog is 404 or 422 before any SQL
  is built. Partitions and every schema but `public` are not in that list.
* **Every value is a bind parameter**, cast to the column's own type as
  `format_type` spells it -- so a bad value is a 422 from PostgreSQL, and an
  index on the column can serve the filter.
* **Read-only.** Each request runs in a `READ ONLY` transaction with a short
  statement timeout, and the router has no write methods at all. (Inside a
  test's rolled-back transaction a read-only transaction cannot be started, so
  there it is a plain savepoint; the statement is a SELECT either way.)
* **Credential hashes are never selected** -- `NULL AS column` stands in for
  them, and they cannot be filtered on either, which would let a caller test
  guesses against a hash. An admin can reset a password; nobody needs its hash.
* **`device_reading` requires a device filter.** It is partitioned and grows
  by 48 rows a day per device; counting or paging all of it for a table view
  would be a long scan nobody wanted.
"""
from __future__ import annotations

import datetime as dt
import ipaddress
from decimal import Decimal
from typing import Any
from uuid import UUID

import asyncpg
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from .db import Conn
from .queries import sql
from .routes_admin_accounts import Admin

router = APIRouter(prefix="/api/admin/tables", tags=["admin"])

#: Never selected, never filterable, whatever table they turn up on.
MASKED_COLUMNS = frozenset({"password_hash", "device_key_hash", "source_key_hash"})

#: Tables too large to page without narrowing, and the column that narrows them.
REQUIRED_FILTER = {"device_reading": "device_id"}

#: Selected as text rather than decoded. `tariff_rate.end_time = '24:00'` is how
#: a window running to midnight is stored, PostgreSQL accepts it, and Python's
#: `datetime.time` cannot represent it -- asyncpg raises while *decoding* the
#: row (erd-logical.md, "Open"). As text it is exactly what is stored.
TEXT_TYPES = ("time without time zone", "time with time zone")

MAX_PAGE = 200
STATEMENT_TIMEOUT = "5s"


# --------------------------------------------------------------------------
# Wire shapes
# --------------------------------------------------------------------------

class ColumnInfo(BaseModel):
    name: str
    type: str
    nullable: bool
    primary_key: bool
    masked: bool


class TableInfo(BaseModel):
    name: str
    kind: str
    #: The planner's estimate; null for a table never analysed.
    approx_rows: int | None
    columns: list[ColumnInfo]
    #: A column that must be filtered on before rows are served, if any.
    required_filter: str | None


class RowsPage(BaseModel):
    table: str
    columns: list[str]
    #: Columns whose values are withheld (always null in `rows`).
    masked: list[str]
    #: Values as JSON-safe scalars: NUMERIC, timestamps, UUIDs and ranges as
    #: strings (rule 5 -- never a float), booleans and small integers as-is.
    rows: list[dict[str, Any]]
    total: int
    limit: int
    offset: int


# --------------------------------------------------------------------------
# Catalog
# --------------------------------------------------------------------------

class _Column:
    __slots__ = ("name", "quoted", "type", "nullable", "primary_key", "pk_position")

    def __init__(self, r: asyncpg.Record):
        self.name = r["name"]
        self.quoted = r["quoted"]
        self.type = r["type"]
        self.nullable = r["nullable"]
        self.primary_key = r["primary_key"]
        self.pk_position = r["pk_position"]

    @property
    def masked(self) -> bool:
        return self.name in MASKED_COLUMNS


async def _catalog(conn: asyncpg.Connection) -> dict[str, tuple[asyncpg.Record, list[_Column]]]:
    tables = await conn.fetch(sql("admin_browse_tables"))
    columns: dict[str, list[_Column]] = {}
    for r in await conn.fetch(sql("admin_browse_columns")):
        columns.setdefault(r["table_name"], []).append(_Column(r))
    return {t["name"]: (t, columns.get(t["name"], [])) for t in tables}


# --------------------------------------------------------------------------
# Values
# --------------------------------------------------------------------------

def _json_safe(value: Any) -> Any:
    """Everything a browser cell can show without losing precision."""
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        # Past 2^53 a JavaScript number is no longer exact.
        return value if abs(value) < 2**53 else str(value)
    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        return value.isoformat()
    if isinstance(value, (Decimal, float, UUID, dt.timedelta)):
        return str(value)
    if isinstance(value, (ipaddress.IPv4Address, ipaddress.IPv6Address,
                          ipaddress.IPv4Network, ipaddress.IPv6Network)):
        return str(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return "\\x" + bytes(value).hex()
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    return str(value)


# --------------------------------------------------------------------------
# Endpoints
# --------------------------------------------------------------------------

@router.get("", response_model=list[TableInfo])
async def list_tables(conn: Conn, _: Admin) -> list[TableInfo]:
    return [
        TableInfo(
            name=name,
            kind=table["kind"],
            approx_rows=table["approx_rows"],
            columns=[
                ColumnInfo(name=c.name, type=c.type, nullable=c.nullable,
                           primary_key=c.primary_key, masked=c.masked)
                for c in cols
            ],
            required_filter=REQUIRED_FILTER.get(name),
        )
        for name, (table, cols) in (await _catalog(conn)).items()
    ]


@router.get("/{name}/rows", response_model=RowsPage)
async def table_rows(
    conn: Conn,
    _: Admin,
    name: str,
    limit: int = Query(default=50, ge=1, le=MAX_PAGE),
    offset: int = Query(default=0, ge=0),
    filter_col: str | None = Query(default=None, max_length=100),
    filter_val: str | None = Query(default=None, max_length=500),
    descending: bool = False,
) -> RowsPage:
    catalog = await _catalog(conn)
    if name not in catalog:
        raise HTTPException(status_code=404, detail="no such table")
    table, columns = catalog[name]
    by_name = {c.name: c for c in columns}

    if (filter_col is None) != (filter_val is None):
        raise HTTPException(status_code=422, detail="filter_col and filter_val go together")
    required = REQUIRED_FILTER.get(name)
    if required and filter_col != required:
        raise HTTPException(
            status_code=422, detail=f"{name} can only be browsed filtered by {required}"
        )

    where, args = "", []
    if filter_col is not None:
        column = by_name.get(filter_col)
        if column is None or column.masked:
            raise HTTPException(status_code=422, detail="cannot filter on that column")
        # Both identifiers and the type are catalog strings, not request text.
        where = f" WHERE {column.quoted} = $1::{column.type}"
        args.append(filter_val)

    def selected(c: _Column) -> str:
        if c.masked:
            return f"NULL::text AS {c.quoted}"
        if c.type.startswith(TEXT_TYPES):
            return f"{c.quoted}::text AS {c.quoted}"
        return c.quoted

    select_list = ", ".join(selected(c) for c in columns)
    key = sorted((c for c in columns if c.primary_key), key=lambda c: c.pk_position)
    direction = " DESC" if descending else ""
    order = ", ".join(f"{c.quoted}{direction}" for c in key) or f"1{direction}"
    relation = table["quoted"]

    rows_sql = (
        f"SELECT {select_list} FROM {relation}{where} "
        f"ORDER BY {order} LIMIT {limit} OFFSET {offset}"
    )
    count_sql = f"SELECT count(*) FROM {relation}{where}"

    transaction = (
        conn.transaction() if conn.is_in_transaction() else conn.transaction(readonly=True)
    )
    try:
        async with transaction:
            await conn.execute(f"SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT}'")
            total = await conn.fetchval(count_sql, *args)
            records = await conn.fetch(rows_sql, *args)
    except asyncpg.DataError as exc:
        raise HTTPException(
            status_code=422, detail=f"that value does not fit {filter_col}: {exc}"
        ) from None
    except asyncpg.QueryCanceledError:
        raise HTTPException(
            status_code=422, detail="that query took too long; narrow it with a filter"
        ) from None

    return RowsPage(
        table=name,
        columns=[c.name for c in columns],
        masked=[c.name for c in columns if c.masked],
        rows=[{k: _json_safe(v) for k, v in r.items()} for r in records],
        total=total,
        limit=limit,
        offset=offset,
    )
