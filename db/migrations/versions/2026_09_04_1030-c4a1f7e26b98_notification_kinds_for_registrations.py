"""notification kinds for the two registration queues

Revision ID: c4a1f7e26b98
Revises: b6f2d90ac417
Create Date: 2026-09-04 10:30:00.000000

Two events had no name in `notification_kind`:

* **registration_pending** -- somebody has applied and an official in that
  district has to decide. Nothing announced this before, for either queue: a
  registration landed in a list somebody had to think to open. The bell is what
  brings an official to the portal, and an application nobody looks at is the
  dead-letter case CLAUDE.md already records for meter applications.
* **supplier_approval** -- the answer, sent to the applicant. `worker_approval`
  already exists and says exactly this for the other queue; reusing it for a
  supplier would make the kind a lie in any view that groups by it.

SEPARATE FROM b6f2d90ac417 ON PURPOSE
=============================================================================
That migration adds columns and reverses cleanly. This one cannot: PostgreSQL
has no DROP VALUE for an enum, so `downgrade` raises, exactly as
b3f1c9d4a7e2 (account_role) and e8b1d3f70a26 (work_order_type) do. Keeping the
two apart means the reversible half stays reversible and the wall is where it
actually is, rather than one step earlier.

Dropping a value is not merely unimplemented, it is unsafe: rows written with
it would have to go somewhere, and silently rewriting somebody's notification
history to make a downgrade succeed is worse than refusing.
"""
from __future__ import annotations

from alembic import op

revision = "c4a1f7e26b98"
down_revision = "b6f2d90ac417"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # IF NOT EXISTS so a re-run over a partially-applied database is a no-op
    # rather than an error. Adding a value inside a transaction is legal on
    # PostgreSQL 12+ as long as nothing uses it in that same transaction, and
    # nothing here does -- the first row carrying one is written by the API.
    op.execute(
        "ALTER TYPE notification_kind ADD VALUE IF NOT EXISTS "
        "'registration_pending'"
    )
    op.execute(
        "ALTER TYPE notification_kind ADD VALUE IF NOT EXISTS "
        "'supplier_approval'"
    )


def downgrade() -> None:
    raise NotImplementedError(
        "PostgreSQL cannot remove a value from an enum type. Downgrading past "
        "this migration would require recreating notification_kind and "
        "deciding what to do with every notification already written as "
        "'registration_pending' or 'supplier_approval' -- a data decision, not "
        "a schema one. Restore from a dump instead."
    )
