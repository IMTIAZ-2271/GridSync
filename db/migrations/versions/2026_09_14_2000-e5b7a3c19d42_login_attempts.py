"""login attempts are recorded, so repeated failures can be refused

Revision ID: e5b7a3c19d42
Revises: c8d4f2a61e37
Create Date: 2026-09-14 20:00:00.000000

`POST /api/auth/login` has never limited anything: a script could try passwords
against one account, or one password against every account, as fast as argon2
lets it. That mattered less while the strongest token read one household's
data. An admin token reads every table.

`login_attempt` is one row per attempt, written BEFORE the password is checked
and pessimistically marked `failed`, then moved to `succeeded` if it was right.
Writing first is what bounds a burst: fifty concurrent guesses each insert,
then each counts the others' rows, so they cannot all slip under the limit by
checking before anyone has written. An attempt refused by the limit is marked
`refused` and not counted, so hammering a locked account does not extend its
lock forever.

Two counts, both over a sliding window (services/api/login_limits.py holds the
numbers):

* **per email** -- failures since the later of the window's start, the last
  successful sign-in, and `account.sessions_valid_after` (an admin's password
  reset, so the owner is not locked out of the password they were just given).
  Counted for emails that match no account too: a lock that only exists for
  real accounts is an enumeration oracle.
* **per client address** -- all failures from one address, however many emails
  they spread across.

`email` is citext and not a foreign key, for the same reason: unknown emails
are recorded. Nothing here is money or a trail anyone reads later; rows older
than a day are deleted by the `login-attempts` job.
"""
from alembic import op

revision = "e5b7a3c19d42"
down_revision = "c8d4f2a61e37"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Upgrade schema."""
    op.execute(
        """
        CREATE TABLE login_attempt (
            attempt_id   bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            email        citext      NOT NULL,
            client_ip    inet,
            outcome      text        NOT NULL DEFAULT 'failed',
            attempted_at timestamptz NOT NULL DEFAULT clock_timestamp(),
            CONSTRAINT login_attempt_outcome
                CHECK (outcome IN ('failed', 'succeeded', 'refused'))
        )
        """
    )
    op.execute(
        "CREATE INDEX login_attempt_by_email ON login_attempt (email, attempted_at)"
    )
    op.execute(
        "CREATE INDEX login_attempt_by_ip ON login_attempt (client_ip, attempted_at) "
        "WHERE client_ip IS NOT NULL"
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("DROP TABLE login_attempt")
