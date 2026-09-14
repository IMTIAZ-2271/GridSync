"""admin panel foundation: sessions can be ended, and the audit trail cannot be edited

Revision ID: a7c3e9f15b20
Revises: f2a9c4e1b7d6
Create Date: 2026-09-14 10:00:00.000000

The admin panel's first phase needs two things the schema does not have.

=============================================================================
1. account.sessions_valid_after -- ending every session an account holds
=============================================================================

`revoked_token` (migration f2a6c1d94b7e) revokes ONE token, by its `jti`, and
that is exactly right for logout. It cannot serve an administrator, because
GridSync never stores the tokens it issues: there is no list of an account's
live `jti`s to revoke. So suspending an account was already immediate (the
account's status is re-read on every request), but resetting a password or
revoking admin could not reach a token issued five minutes earlier -- it would
keep working, with its old role, for up to 24 hours.

`sessions_valid_after` is the cut-off. `get_current_account` refuses a token
whose `iat` is earlier, in the lookup it already runs. NULL means no cut-off,
which is every existing account, so nothing changes until an admin acts.

`iat` is whole seconds, so a token issued in the same second as the cut-off is
accepted. That window is deliberate: the alternative is refusing a login that
happens in the same second as a password reset, which is the login the reset was
for.

=============================================================================
2. audit_log is append-only
=============================================================================

`audit_log` has existed since migration a4092df65997 and nothing has written to
it. Admin actions will, and a trail the actor can edit is not a trail -- so it
joins rule 1's append-only tables, with its own trigger rather than
`forbid_mutation()`.

The one UPDATE it must allow is its foreign key's `ON DELETE SET NULL`:
deleting an account rewrites `actor_account_id` on every row that account wrote.
Refusing that would make any account with a history undeletable; allowing any
other change would make the trigger decorative. So the function permits an
UPDATE whose only difference is `actor_account_id` becoming NULL, and nothing
else.

The trigger is named `audit_log_immutable` so `scripts/reset_operational_data.py`
-- which stands the `*_immutable` triggers down around its deletes and refuses to
report success unless every one is re-armed -- covers it with a one-word change.
"""
from alembic import op

revision = "a7c3e9f15b20"
down_revision = "f2a9c4e1b7d6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Upgrade schema."""
    op.execute("ALTER TABLE account ADD COLUMN sessions_valid_after timestamptz")
    op.execute(
        """
        COMMENT ON COLUMN account.sessions_valid_after IS
        'Tokens issued before this instant are refused. Set by an admin who '
        'suspends the account, resets its password, changes its role or signs it '
        'out everywhere. NULL: no cut-off.'
        """
    )

    op.execute(
        """
        CREATE FUNCTION audit_log_append_only() RETURNS trigger
        LANGUAGE plpgsql AS $fn$
        BEGIN
            -- The foreign key's ON DELETE SET NULL, and only that: the actor
            -- going to NULL with every other column untouched.
            IF TG_OP = 'UPDATE'
               AND OLD.actor_account_id IS NOT NULL
               AND NEW.actor_account_id IS NULL
               AND to_jsonb(NEW) - 'actor_account_id'
                 = to_jsonb(OLD) - 'actor_account_id'
            THEN
                RETURN NEW;
            END IF;

            RAISE EXCEPTION
                '% on audit_log is forbidden: the audit trail is append-only',
                TG_OP
                USING ERRCODE = '23514',
                      HINT = 'write a new audit row instead';
        END;
        $fn$
        """
    )
    op.execute(
        """
        CREATE TRIGGER audit_log_immutable
            BEFORE UPDATE OR DELETE ON audit_log
            FOR EACH ROW EXECUTE FUNCTION audit_log_append_only()
        """
    )

    # The admin audit page reads by entity and by actor, newest first.
    op.execute(
        "CREATE INDEX audit_log_by_entity ON audit_log (entity_type, entity_id, audit_id DESC)"
    )
    op.execute(
        "CREATE INDEX audit_log_by_actor ON audit_log (actor_account_id, audit_id DESC)"
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("DROP INDEX IF EXISTS audit_log_by_actor")
    op.execute("DROP INDEX IF EXISTS audit_log_by_entity")
    op.execute("DROP TRIGGER IF EXISTS audit_log_immutable ON audit_log")
    op.execute("DROP FUNCTION IF EXISTS audit_log_append_only()")
    op.execute("ALTER TABLE account DROP COLUMN IF EXISTS sessions_valid_after")
