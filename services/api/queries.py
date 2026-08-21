"""Loader for the named statements in `db/sql/api_queries.sql`.

The reading path is raw SQL kept in `db/sql/` (CLAUDE.md), so the handlers need
a way to reach a statement by name. The file is split on `-- name: <name>`
markers; everything up to the next marker is that statement's text, comments
included, and the comments are worth keeping -- they explain why each aggregate
filters the way it does.

Loaded once at import. The file is a few hundred lines and never changes at
runtime, so there is nothing to gain from re-reading it per request.
"""
from pathlib import Path

SQL_DIR = Path(__file__).resolve().parents[2] / "db" / "sql"
SQL_FILES = (SQL_DIR / "api_queries.sql", SQL_DIR / "auth_queries.sql")

_NAME_MARKER = "-- name:"


def _load(path: Path) -> dict[str, str]:
    statements: dict[str, str] = {}
    name: str | None = None
    buffer: list[str] = []

    def flush() -> None:
        if name is None:
            return
        body = "\n".join(buffer).strip()
        if not body:
            raise ValueError(f"{path.name}: statement '{name}' is empty")
        statements[name] = body

    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith(_NAME_MARKER):
            flush()
            name = line[len(_NAME_MARKER):].strip()
            if not name:
                raise ValueError(f"{path.name}: a '-- name:' marker has no name")
            if name in statements:
                raise ValueError(f"{path.name}: duplicate statement name '{name}'")
            buffer = []
        elif name is not None:
            buffer.append(line)
        # Lines before the first marker are the file header; drop them.

    flush()
    if not statements:
        raise ValueError(f"{path.name}: no '-- name:' markers found")
    return statements


def _load_all() -> dict[str, str]:
    """Merge every SQL file into one namespace.

    Names are global across the files, and a collision raises rather than
    letting one file silently shadow the other -- two statements answering to
    the same name is a bug whichever one wins.
    """
    merged: dict[str, str] = {}
    for path in SQL_FILES:
        for name, body in _load(path).items():
            if name in merged:
                raise ValueError(
                    f"{path.name}: statement '{name}' is already defined in "
                    "another SQL file"
                )
            merged[name] = body
    return merged


QUERIES: dict[str, str] = _load_all()


def sql(name: str) -> str:
    """Return the named statement, failing loudly on a typo.

    A missing key would otherwise surface as a confusing asyncpg syntax error
    on the string "None" somewhere deep in a handler.
    """
    try:
        return QUERIES[name]
    except KeyError:
        known = ", ".join(sorted(QUERIES))
        raise KeyError(f"no SQL statement named '{name}'. Known: {known}") from None
