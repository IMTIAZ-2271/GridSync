"""one LIVE work order per issue, not one ever

Revision ID: a1c4e8b70d3f
Revises: e7c4b19a2d83
Create Date: 2026-08-26 17:00:00.000000

`one_order_per_issue` was `UNIQUE (issue_id) WHERE issue_id IS NOT NULL` -- at
most one work order per complaint, for all time. That contradicts the
dispatcher's inbox, which has always been written the other way:
`dispatchable_issues` returns a complaint whose order was **cancelled or
failed**, because the fault is still real and somebody has to go again, and it
stops counting a **completed** order as coverage once the household disputes
that the visit fixed anything.

So the inbox offered exactly the row the schema refused, and clicking it was an
unhandled UniqueViolation -- a 500 on the supplier's dispatch screen, reliably,
for any complaint whose first visit did not stick.

What the index is actually for is the race the inbox cannot prevent: two
dispatchers reading the same queue must not send two crews to one complaint.
That is a statement about *open* orders. A terminal one -- completed, cancelled
or failed -- is not a visit anybody is waiting on, and must not block the next.

The index cannot mirror the inbox's disputed-visit clause: `consumer_disputed_at`
lives on `issue`, and a partial index may only see the row being written. It
therefore treats every terminal status alike and lets a second order be raised
after a completed one. The narrower question -- whether this particular
complaint deserves another visit -- is the inbox's to answer, and it does.

No data migration. The new predicate is strictly weaker, so every row that
satisfied the old index satisfies this one; a database holding two orders for
one issue could not have been created under the old index in the first place.
"""
from alembic import op

revision = "a1c4e8b70d3f"
down_revision = "e7c4b19a2d83"
branch_labels = None
depends_on = None

# Terminal: the visit is over, however it ended. None of these is coverage.
_TERMINAL = "('completed', 'cancelled', 'failed')"


def upgrade() -> None:
    op.execute("DROP INDEX one_order_per_issue")
    op.execute(
        "CREATE UNIQUE INDEX one_live_order_per_issue ON work_order (issue_id) "
        f"WHERE issue_id IS NOT NULL AND status NOT IN {_TERMINAL}"
    )


def downgrade() -> None:
    """Restore the all-time index.

    This can fail, and honestly so: if a complaint has been visited twice since
    the upgrade -- which is the whole point of the upgrade -- there is no
    correct way to squeeze it back under a one-order-ever rule, and picking an
    order to delete would be destroying operational history to satisfy an index.
    A downgrade that refuses is better than one that guesses.
    """
    op.execute("DROP INDEX one_live_order_per_issue")
    op.execute(
        "CREATE UNIQUE INDEX one_order_per_issue ON work_order (issue_id) "
        "WHERE issue_id IS NOT NULL"
    )
