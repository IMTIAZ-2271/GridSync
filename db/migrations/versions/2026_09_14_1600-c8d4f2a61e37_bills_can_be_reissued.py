"""a bill can be corrected by voiding it and issuing a replacement

Revision ID: c8d4f2a61e37
Revises: a7c3e9f15b20
Create Date: 2026-09-14 16:00:00.000000

Rule 1 has always said a correction is a new bill pointing at the old via
`voided_by_bill_id`. The schema made that impossible:

* `bill_one_per_period UNIQUE (period_id)` refused the replacement while the
  original existed, and
* `bill_void_status CHECK ((voided_by_bill_id IS NOT NULL) = (status = 'void'))`
  refused voiding the original before the replacement existed.

Neither row could be written first. This migration changes what "one" means,
not whether it is enforced.

=============================================================================
1. One LIVE bill per period
=============================================================================

`bill_one_live_per_period` is an exclusion constraint over `period_id` that
ignores void bills -- at most one non-void bill per period, which is what rule 4
was protecting. It is `DEFERRABLE INITIALLY IMMEDIATE`: every ordinary insert is
checked at once, exactly as before, and only `rebill_period()` defers it, for the
moment between writing the replacement and voiding the original. A btree
exclusion constraint is a unique constraint that can carry a WHERE clause and be
deferred; a partial unique INDEX can do the first and not the second.

=============================================================================
2. One earned and one applied ledger entry per BILL
=============================================================================

`ledger_one_entry_per_period` allowed one `earned` and one `applied` entry per
(point, period). A replacement bill posts its own for the same period, after
the original's have been reversed by `adjustment` entries. The guarantee moves
to the bill: `ledger_one_entry_per_bill`. A second run for the same bill is
still refused by the database; a second bill for the same period now has
somewhere to put its credit.

Reverses cleanly while no period holds a void bill beside its replacement; the
downgrade stops and says so otherwise, because the only way back would be
deleting append-only rows.
"""
from alembic import op

revision = "c8d4f2a61e37"
down_revision = "a7c3e9f15b20"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Upgrade schema."""
    op.execute("ALTER TABLE bill DROP CONSTRAINT bill_one_per_period")
    op.execute(
        """
        ALTER TABLE bill
            ADD CONSTRAINT bill_one_live_per_period
            EXCLUDE USING btree (period_id WITH =)
            WHERE (status <> 'void')
            DEFERRABLE INITIALLY IMMEDIATE
        """
    )
    op.execute(
        """
        COMMENT ON CONSTRAINT bill_one_live_per_period ON bill IS
        'Rule 4: at most one non-void bill per billing period. Deferred only by '
        'rebill_period(), between inserting a replacement and voiding the original.'
        """
    )

    op.execute("DROP INDEX ledger_one_entry_per_period")
    op.execute(
        """
        CREATE UNIQUE INDEX ledger_one_entry_per_bill
            ON credit_ledger (bill_id, entry_type)
            WHERE entry_type IN ('earned', 'applied')
        """
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.execute(
        """
        DO $$
        DECLARE reissued int;
        BEGIN
            SELECT count(DISTINCT period_id) INTO reissued
            FROM bill WHERE status = 'void';
            IF reissued > 0 THEN
                RAISE EXCEPTION
                    'cannot restore one bill per period: % period(s) hold a void '
                    'bill beside its replacement, and bills are append-only (rule 1). '
                    'Restore from a dump instead.', reissued;
            END IF;
        END $$
        """
    )
    op.execute("DROP INDEX IF EXISTS ledger_one_entry_per_bill")
    op.execute(
        "CREATE UNIQUE INDEX ledger_one_entry_per_period "
        "ON credit_ledger (billing_point_id, period_id, entry_type) "
        "WHERE entry_type IN ('earned', 'applied')"
    )
    op.execute("ALTER TABLE bill DROP CONSTRAINT IF EXISTS bill_one_live_per_period")
    op.execute("ALTER TABLE bill ADD CONSTRAINT bill_one_per_period UNIQUE (period_id)")
