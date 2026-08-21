import asyncio
import os
from logging.config import fileConfig
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context

# E:/Codes/GridSync/db/migrations/env.py -> project root is two levels up.
PROJECT_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(PROJECT_ROOT / ".env")

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# Interpret the config file for Python logging.
# This line sets up loggers basically.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# No ORM models, by design (see CLAUDE.md). Migrations are hand-written raw SQL,
# so there is no metadata to autogenerate against and `alembic revision
# --autogenerate` is not part of this project's workflow.
target_metadata = None


def get_database_url() -> str:
    """Read DATABASE_URL from the environment and point it at the async driver.

    asyncpg is the only Postgres driver in requirements.txt, so the bare
    `postgresql://` scheme used in .env has to name it explicitly for
    SQLAlchemy to pick the right dialect.
    """
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError(
            "DATABASE_URL is not set. Copy .env.example to .env and fill it in."
        )
    for bare, async_scheme in (
        ("postgresql+asyncpg://", None),  # already correct, leave alone
        ("postgresql://", "postgresql+asyncpg://"),
        ("postgres://", "postgresql+asyncpg://"),
    ):
        if url.startswith(bare):
            return url if async_scheme is None else url.replace(bare, async_scheme, 1)
    raise RuntimeError(f"DATABASE_URL is not a PostgreSQL URL: {url.split('://')[0]}://…")


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    This configures the context with just a URL
    and not an Engine, though an Engine is acceptable
    here as well.  By skipping the Engine creation
    we don't even need a DBAPI to be available.

    Calls to context.execute() here emit the given string to the
    script output.

    """
    context.configure(
        url=get_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)

    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """In this scenario we need to create an Engine
    and associate a connection with the context.

    """
    configuration = config.get_section(config.config_ini_section, {})
    # Set the URL on the dict rather than via config.set_main_option(): the
    # latter runs the value through configparser interpolation, which chokes on
    # a '%' in a password.
    configuration["sqlalchemy.url"] = get_database_url()

    connectable = async_engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode."""

    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
