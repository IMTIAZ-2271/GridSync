"""Shared test fixtures.

Every test runs inside one transaction that is always rolled back, so the suite
leaves no committed state behind and can be run against a developer database
without wiping it. Tests never COMMIT.

Because nothing commits, DEFERRABLE INITIALLY DEFERRED constraint triggers
would never fire on their own. `commit_check` forces them at a point the test
chooses, which is what makes commit-time enforcement testable at all.
"""
import os
from contextlib import asynccontextmanager
from pathlib import Path

import asyncpg
import pytest
import pytest_asyncio
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def database_url() -> str:
    """The sync-scheme DSN, for asyncpg's own connect().

    load_dotenv is given an explicit path: called bare it resolves relative to
    the *calling file*, which silently finds the wrong .env when tests are run
    from another directory. db/migrations/env.py does the same thing.
    """
    load_dotenv(PROJECT_ROOT / ".env")
    url = os.environ.get("DATABASE_URL")
    if not url:
        pytest.skip("DATABASE_URL is not set; copy .env.example to .env")
    # asyncpg.connect wants a bare scheme, not SQLAlchemy's postgresql+asyncpg.
    return url.replace("postgresql+asyncpg://", "postgresql://", 1)


@pytest_asyncio.fixture
async def conn():
    """A connection inside an open transaction that is rolled back on teardown."""
    connection = await asyncpg.connect(database_url())
    transaction = connection.transaction()
    await transaction.start()
    try:
        yield connection
    finally:
        await transaction.rollback()
        await connection.close()


@pytest.fixture
def commit_check(conn):
    """Fire pending deferred constraint triggers without committing.

    SET CONSTRAINTS ALL IMMEDIATE runs the deferred queue immediately, so a
    violation raises here instead of at a COMMIT the test will never issue.
    """

    async def _check() -> None:
        await conn.execute("SET CONSTRAINTS ALL IMMEDIATE")
        # Restore the deferred default so a test can stage another batch.
        await conn.execute("SET CONSTRAINTS ALL DEFERRED")

    return _check


@pytest.fixture
def savepoint(conn):
    """Scope a statement expected to fail.

    A failed statement aborts the whole transaction, so anything a test wants
    to do afterwards has to be inside a savepoint that can be unwound.
    """

    @asynccontextmanager
    async def _savepoint():
        async with conn.transaction():
            yield

    return _savepoint
