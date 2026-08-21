"""rule 7 enforcement consolidated onto trigger

Revision ID: 0b24bc6b5a1f
Revises: c1e22f0141be
Create Date: 2026-08-21 16:06:11.402318

Migration c1e22f0141be enforced rule 7 twice: a partial unique index
(`one_billing_meter_per_site`) and a DEFERRABLE INITIALLY DEFERRED constraint
trigger (`meter_spec_one_active_billing`). The index is redundant and, worse,
it breaks the case the trigger was deferred for.

A partial unique INDEX is not a deferrable CONSTRAINT -- Postgres has no
partial UNIQUE constraint, so this cannot be fixed by redeclaring it. The index
therefore fires mid-statement, and it reads only `meter_spec.billing_role`; it
cannot see `device.removed_at`. A meter swap that retires the old device and
then inserts the replacement is rejected on the INSERT, before the deferred
trigger is ever consulted, even though the state at COMMIT is legal.

The trigger is strictly stronger: it enforces exactly-one over *active* meters
by joining `device` and checking `removed_at`, and it does so at COMMIT.
Dropping the index loses no enforcement. It only moves the rejection of a
genuine duplicate from statement time to commit time.

Also corrects the record: migration c1e22f0141be's inline comment claims "the
intermediate states are legal", which was not true while the index existed.
The applied migration is left alone -- history is not rewritten -- and the
accurate description is attached here as a COMMENT ON the trigger, where it is
visible from the live database via \\dd rather than only in a file.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0b24bc6b5a1f'
down_revision: Union[str, Sequence[str], None] = 'c1e22f0141be'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""

    # Rule 7 is now enforced solely by meter_spec_one_active_billing and
    # device_one_active_billing. See the module docstring for why.
    op.execute("DROP INDEX IF EXISTS one_billing_meter_per_site")

    op.execute(
        """
        COMMENT ON TRIGGER meter_spec_one_active_billing ON meter_spec IS
        'Rule 7: exactly one device per site has billing_role = ''billing'' '
        'among devices with removed_at IS NULL. Sole enforcement of rule 7 as '
        'of migration 0b24bc6b5a1f; the partial unique index that used to '
        'share the job was dropped there. DEFERRABLE INITIALLY DEFERRED so a '
        'meter swap can retire the old device and install its replacement in '
        'one transaction: only the state at COMMIT is checked, and the '
        'intermediate states are genuinely legal now that no index fires '
        'mid-statement. A duplicate billing meter is therefore reported at '
        'COMMIT, not at INSERT.'
        """
    )

    op.execute(
        """
        COMMENT ON TRIGGER device_one_active_billing ON device IS
        'Rule 7, device side: setting device.removed_at changes which billing '
        'meters count as active, so retiring one re-checks the site. Shares '
        'assert_one_active_billing_meter() with '
        'meter_spec_one_active_billing and is deferred for the same reason.'
        """
    )

    op.execute(
        """
        COMMENT ON FUNCTION assert_one_active_billing_meter() IS
        'Rule 7 check: a site must have exactly one meter_spec row with '
        'billing_role = ''billing'' whose device is not removed. Raises 23514. '
        'Skips sites that no longer exist, so DELETE FROM site cascades '
        'cleanly.'
        """
    )


def downgrade() -> None:
    """Downgrade schema."""

    op.execute("COMMENT ON FUNCTION assert_one_active_billing_meter() IS NULL")
    op.execute(
        "COMMENT ON TRIGGER device_one_active_billing ON device IS NULL"
    )
    op.execute(
        "COMMENT ON TRIGGER meter_spec_one_active_billing ON meter_spec IS NULL"
    )

    # Recreating the index can fail where upgrading succeeded: any site that
    # legitimately has a retired 'billing' meter alongside its active one is
    # a duplicate by the index's blinder rules. That is the defect this
    # migration removed, and it is the correct failure mode for a downgrade --
    # better than silently discarding rows to make the index buildable.
    op.execute(
        "CREATE UNIQUE INDEX one_billing_meter_per_site ON meter_spec (site_id) "
        "WHERE billing_role = 'billing'"
    )
