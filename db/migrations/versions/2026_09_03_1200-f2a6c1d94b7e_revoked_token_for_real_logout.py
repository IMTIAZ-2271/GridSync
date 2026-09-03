"""revoked_token: logout that actually invalidates a token

Revision ID: f2a6c1d94b7e
Revises: e8b1d3f70a26
Create Date: 2026-09-03 12:00:00.000000

Every JWT issued by `issue_token()` has been valid for its full 24h TTL no
matter what happened afterward -- the docstring on that function said so in
plain words: "the reason to add revocation before this is real." An account
that logged out kept working with the same token until it naturally expired,
and a suspended account did too. This migration is that revocation.

ONE TABLE, KEYED ON THE TOKEN, NOT THE ACCOUNT
=============================================================================
`revoked_token` records individual tokens, not accounts. `jti` (JWT ID) is a
fresh random UUID minted per login, so two logins -- two devices, two tabs
that both signed in -- carry two different tokens with two different `jti`s
even though they authenticate the same account. Logging out one revokes only
that one row, and a second device stays signed in. A table keyed on account_id
instead would invalidate everything the account holds in one motion, which is
a real and simpler design (see docs/decisions.md) -- this project chose
per-token because nothing about GridSync assumes "logout" means "everywhere".

`expires_at` mirrors the token's own `exp` claim. A revoked row for a token
that has since expired on its own is inert -- the `exp` check in
get_current_account rejects it first -- so `expires_at` exists only to bound
how long a dead row is kept around, not to change what gets rejected.
"""
from __future__ import annotations

from alembic import op

revision = "f2a6c1d94b7e"
down_revision = "e8b1d3f70a26"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE revoked_token (
            jti        uuid PRIMARY KEY,
            account_id uuid NOT NULL
                REFERENCES account (account_id) ON DELETE CASCADE,
            revoked_at timestamptz NOT NULL DEFAULT now(),
            expires_at timestamptz NOT NULL
        )
        """
    )
    op.execute(
        "COMMENT ON TABLE revoked_token IS "
        "'One row per logged-out token, keyed by its jti claim. "
        "get_current_account rejects any token whose jti is found here. "
        "Rows past their own expires_at are dead weight, not a correctness "
        "issue -- the JWT exp claim already rejects them -- so nothing "
        "sweeps this table yet.'"
    )
    # The lookup on every authenticated request is a PK hit already (jti is
    # the primary key), so no separate index is needed for that path. This
    # one serves the other direction: revoking every token an account holds
    # in one statement, which the account-suspension path will eventually
    # want and a bare PK index cannot serve.
    op.execute(
        "CREATE INDEX revoked_token_by_account ON revoked_token (account_id)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS revoked_token_by_account")
    op.execute("DROP TABLE IF EXISTS revoked_token")
