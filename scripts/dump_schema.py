"""Regenerate the two schema documents under docs/.

    python -m scripts.dump_schema

Writes:

  docs/schema.txt           pg_dump --schema-only, the complete and
                            authoritative artefact -- every table, type,
                            index, constraint, trigger and function
  docs/schema-readable.txt  a flat per-table listing for reading, and for
                            handing to somebody who should not have to parse
                            a pg_dump

Both were previously produced by hand and had drifted four migrations behind
the database, which is the reason this script exists: a document nobody can
regenerate is a document that silently goes stale.

The readable file keeps the column/constraint layout it already had, and adds
**indexes and triggers**. Without them the document omits most of what makes
this schema what it is -- rule 7 is enforced by two deferred constraint
triggers, rule 1 by four more, and half a dozen invariants ("one *live* work
order per complaint", "one open application per site") exist only as partial
unique indexes. A reader given only the columns would conclude none of it is
there.
"""
from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
from pathlib import Path
from urllib.parse import urlparse

import asyncpg

from services.api.db import PROJECT_ROOT, database_url

DOCS = PROJECT_ROOT / "docs"
DUMP = DOCS / "schema.txt"
READABLE = DOCS / "schema-readable.txt"

RULE = "=" * 57

#: Constraints in the order the existing document lists them: checks, then
#: foreign keys, then the primary key, then uniques. Kept so the file's shape
#: does not change just because its generator did.
#:
#: 'n' is absent on purpose. PostgreSQL 17 began materialising NOT NULL as a
#: pg_constraint row, so listing them here would repeat every column's
#: nullability a second time under a generated name.
CONTYPE_ORDER = {"c": 0, "f": 1, "p": 2, "u": 3, "x": 4, "t": 5}

#: format_type() returns the SQL standard spelling; the document uses the
#: short one, which is also what every migration in db/migrations writes.
TYPE_ALIASES = {
    "timestamp with time zone": "timestamptz",
    "timestamp without time zone": "timestamp",
    "time with time zone": "timetz",
    "time without time zone": "time",
    "character varying": "varchar",
    "double precision": "float8",
}


def short_type(t: str) -> str:
    for long, short in TYPE_ALIASES.items():
        if t == long:
            return short
        if t.startswith(long + "("):
            return short + t[len(long):]
    return t


COLUMNS_SQL = """
SELECT c.relname AS table_name,
       a.attname AS column_name,
       format_type(a.atttypid, a.atttypmod) AS data_type,
       a.attnotnull AS not_null,
       pg_get_expr(d.adbin, d.adrelid) AS default_expr,
       a.attnum
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped
LEFT JOIN pg_attrdef d ON d.adrelid = c.oid AND d.adnum = a.attnum
WHERE n.nspname = 'public'
  AND c.relkind IN ('r', 'p')
  AND c.relname <> 'alembic_version'
  -- Reading partitions are generated monthly and are not part of the design.
  AND c.relispartition IS NOT TRUE
ORDER BY c.relname, a.attnum
"""

CONSTRAINTS_SQL = """
SELECT c.relname AS table_name,
       con.conname AS name,
       con.contype::text AS contype,
       pg_get_constraintdef(con.oid) AS definition
FROM pg_constraint con
JOIN pg_class c ON c.oid = con.conrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'public' AND c.relispartition IS NOT TRUE
  AND con.contype <> 'n'
ORDER BY c.relname, con.conname
"""

INDEXES_SQL = """
SELECT c.relname AS table_name, i.indexname AS name, i.indexdef AS definition
FROM pg_indexes i
JOIN pg_class c ON c.relname = i.tablename
JOIN pg_namespace n ON n.oid = c.relnamespace AND n.nspname = i.schemaname
WHERE i.schemaname = 'public'
  AND c.relispartition IS NOT TRUE
  -- Anything backing a constraint is already listed under CONSTRAINTS.
  AND NOT EXISTS (
      SELECT 1 FROM pg_constraint con
      WHERE con.conrelid = c.oid AND con.conname = i.indexname
  )
ORDER BY c.relname, i.indexname
"""

TRIGGERS_SQL = """
SELECT c.relname AS table_name, t.tgname AS name,
       pg_get_triggerdef(t.oid) AS definition
FROM pg_trigger t
JOIN pg_class c ON c.oid = t.tgrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'public' AND NOT t.tgisinternal
  AND c.relispartition IS NOT TRUE
ORDER BY c.relname, t.tgname
"""


def column_line(row) -> str:
    """One column, in the layout the document already used.

    Field widths 27 and 21 are taken from the existing file so a table nobody
    touched renders identically.
    """
    # Widths are floors, not caps: a name or type longer than its column gets
    # one space rather than being run into the next field. The first cut
    # produced "timestamp with time zoneNOT NULL".
    name = row["column_name"].ljust(27)
    typ = short_type(row["data_type"]).ljust(21)
    parts = f"    {name} {typ} " if len(name) > 27 or len(typ) > 21 else f"    {name}{typ}"
    parts += "NOT NULL  " if row["not_null"] else ""
    if row["default_expr"]:
        parts += f"DEFAULT {row['default_expr']}"
    return parts.rstrip()


def group(rows, key="table_name") -> dict[str, list]:
    out: dict[str, list] = {}
    for r in rows:
        out.setdefault(r[key], []).append(r)
    return out


async def build_readable(conn: asyncpg.Connection) -> str:
    columns = group(await conn.fetch(COLUMNS_SQL))
    constraints = group(await conn.fetch(CONSTRAINTS_SQL))
    indexes = group(await conn.fetch(INDEXES_SQL))
    triggers = group(await conn.fetch(TRIGGERS_SQL))

    out: list[str] = []
    for table in sorted(columns):
        out.append(RULE)
        out.append(table.upper())
        out.append(RULE)
        for col in columns[table]:
            out.append(column_line(col))

        cons = sorted(
            constraints.get(table, []),
            key=lambda r: (CONTYPE_ORDER.get(r["contype"], 9), r["name"]),
        )
        if cons:
            out.append("  CONSTRAINTS")
            for c in cons:
                out.append(f"    {c['name']}: {c['definition']}")

        if table in indexes:
            out.append("  INDEXES")
            for i in indexes[table]:
                # The CREATE INDEX preamble is noise once the table is named.
                d = i["definition"]
                d = d.replace(f" ON public.{table} USING ", " USING ", 1)
                d = d.replace(f"CREATE INDEX {i['name']}", "", 1)
                d = d.replace(f"CREATE UNIQUE INDEX {i['name']}", "UNIQUE", 1)
                out.append(f"    {i['name']}: {d.strip()}")

        if table in triggers:
            out.append("  TRIGGERS")
            for t in triggers[table]:
                d = t["definition"].replace(f"CREATE TRIGGER {t['name']} ", "", 1)
                d = d.replace(f" ON public.{table} ", " ", 1)
                out.append(f"    {t['name']}: {d}")

        out.append("")
    return "\n".join(out) + "\n"


def run_pg_dump() -> None:
    """`pg_dump --schema-only` into docs/schema.txt.

    Owner and privilege statements are stripped: they name the local `postgres`
    role, which is an accident of this machine rather than part of the design.
    """
    pg_dump = shutil.which("pg_dump") or r"D:\Program Files\PostgreSQL\18\bin\pg_dump.exe"
    if not Path(pg_dump).exists() and not shutil.which("pg_dump"):
        raise SystemExit(f"pg_dump not found (looked for {pg_dump})")

    url = urlparse(database_url())
    env = dict(os.environ)
    if url.password:
        env["PGPASSWORD"] = url.password

    result = subprocess.run(
        [
            pg_dump,
            "--schema-only", "--no-owner", "--no-privileges",
            "--schema=public",
            "-h", url.hostname or "localhost",
            "-p", str(url.port or 5432),
            "-U", url.username or "postgres",
            "-d", (url.path or "/gridsync").lstrip("/"),
        ],
        capture_output=True, text=True, env=env,
    )
    if result.returncode != 0:
        raise SystemExit(f"pg_dump failed:\n{result.stderr[:2000]}")
    DUMP.write_text(result.stdout, encoding="utf-8")
    print(f"  {DUMP.relative_to(PROJECT_ROOT)}  ({len(result.stdout.splitlines())} lines)")


async def main() -> None:
    conn = await asyncpg.connect(database_url())
    try:
        readable = await build_readable(conn)
    finally:
        await conn.close()
    READABLE.write_text(readable, encoding="utf-8")
    print("Wrote:")
    print(f"  {READABLE.relative_to(PROJECT_ROOT)}  ({len(readable.splitlines())} lines)")
    run_pg_dump()


if __name__ == "__main__":
    asyncio.run(main())
