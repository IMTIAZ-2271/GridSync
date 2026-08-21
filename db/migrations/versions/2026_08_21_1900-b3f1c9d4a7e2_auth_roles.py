"""auth roles: government and supplier

Revision ID: b3f1c9d4a7e2
Revises: a4092df65997
Create Date: 2026-08-21 19:00:00.000000

Adds 'government' and 'supplier' to account_role. That is the whole schema
change for authentication -- claiming a site is an UPDATE of site.account_id,
and claiming a worker profile is an UPDATE of the account row that already
shares its primary key, so neither needs a new column.

Two notes on why this migration does nothing else.

**No demo accounts here.** Postgres will not let a transaction use an enum
value it added itself, so an INSERT with role 'government' cannot run in this
migration. Those rows live in db/sql/seed_auth.sql, applied separately.

**No 'claimed' column.** The seed writes an unusable placeholder into
account.password_hash ('$argon2id$seed$notarealhash'), which is not a valid
argon2 encoding and so can never verify against any password. An account is
claimable exactly while that placeholder is present. Deriving it from the hash
keeps unclaimed accounts unable to authenticate by construction rather than by
a flag some future code path could forget to check.
"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "b3f1c9d4a7e2"
down_revision: Union[str, Sequence[str], None] = "a4092df65997"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


NEW_ROLES = ("government", "supplier")


def upgrade() -> None:
    for role in NEW_ROLES:
        # IF NOT EXISTS keeps a re-run harmless; ADD VALUE is not
        # transactional in the way the rest of a migration is, so a partial
        # failure must not need hand-unpicking.
        op.execute(f"ALTER TYPE account_role ADD VALUE IF NOT EXISTS '{role}'")


def downgrade() -> None:
    # Postgres cannot drop a value from an enum. Undoing this means recreating
    # the type and rewriting every column that uses it, which is not worth
    # automating for two additive values that nothing else depends on.
    raise NotImplementedError(
        "account_role values cannot be removed; recreate the type by hand if "
        "this genuinely needs reverting"
    )
