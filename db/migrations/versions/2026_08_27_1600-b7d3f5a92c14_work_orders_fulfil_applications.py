"""an application is fulfilled by a visit, and the household says so

Revision ID: b7d3f5a92c14
Revises: c9e2f4a71b83
Create Date: 2026-08-27 16:00:00.000000

Migration c9e2f4a71b83 made a meter something the utility issues rather than
something a web form invents, but it issued it the moment an official clicked
approve. Nobody had been to the property. The same was true of net metering:
the regulator said yes to an export agreement without anyone inspecting what
was on the roof.

Both applications now run through a **visit**:

    apply -> official raises a work order -> a worker does it
          -> the household confirms it happened
          -> the official registers the meter
          -> the household installs it and readings begin

That shape is already in the schema -- `work_order_type` has carried
`meter_install`, `meter_swap` and `inspection` since the first migration, and
the offer/start deadline sweeps have been running since 2026-08-24. What was
missing is the link between an application and the order that fulfils it, and
somewhere to put the two facts the flow turns on: the serial of the meter that
was actually fitted, and the household's verdict on whether it was.

**All six columns land on `work_order`, not on the two application tables.**
The consumer is confirming *the visit*, exactly as `issue.consumer_confirmed_at`
already does for a complaint, and the technician is recording the serial of
hardware they are holding at the property. Splitting the same three facts
across `meter_application` and `net_metering_agreement` would be two copies of
one workflow, and would put visit bookkeeping inside a contract table.

`order_single_origin` keeps the three origins exclusive. An order answers a
complaint, or fulfils a meter application, or fulfils a net-metering
agreement -- never two at once, because the audit trail behind "this visit
happened because of that" is the whole point of the column.

The two partial unique indexes mirror `one_order_per_issue` as migration
a1c4e8b70d3f left it: one *live* order per application, so a visit that was
cancelled or failed can be followed by another, while two officials working one
district cannot send two crews to the same address.
"""
from alembic import op

revision = "b7d3f5a92c14"
down_revision = "c9e2f4a71b83"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Upgrade schema."""

    # The household hears when a visit is scheduled for its application, and
    # the official hears when one is applied for or comes back failed. Added
    # here and used only at runtime: PostgreSQL will not let a transaction
    # insert an enum value that same transaction added.
    for value in ("meter_application", "work_order_failed"):
        op.execute(
            f"ALTER TYPE notification_kind ADD VALUE IF NOT EXISTS '{value}'"
        )

    op.execute(
        """
        ALTER TABLE work_order
            -- Which application this visit exists to fulfil. Both nullable:
            -- most orders still answer a complaint, or nothing at all.
            ADD COLUMN meter_application_id uuid
                REFERENCES meter_application (application_id) ON DELETE SET NULL,
            ADD COLUMN agreement_id uuid
                REFERENCES net_metering_agreement (agreement_id)
                ON DELETE SET NULL,

            -- The serial of the meter actually fitted, recorded by the
            -- technician at the property. It is NOT a device_id: the `device`
            -- row does not exist yet and will not until the household installs
            -- the meter on a connection. This is the number the official's
            -- registration step reads, so nobody has to re-type hardware they
            -- never saw.
            ADD COLUMN installed_serial_no text,

            -- The household's verdict on the visit. Same pair as
            -- issue.consumer_confirmed_at / consumer_disputed_at, and for the
            -- same reason: the person who was there is the one who knows.
            ADD COLUMN consumer_confirmed_at timestamptz,
            ADD COLUMN consumer_disputed_at timestamptz,
            ADD COLUMN consumer_note text
        """
    )

    op.execute(
        """
        ALTER TABLE work_order
            -- An order answers a complaint, or fulfils a meter application, or
            -- fulfils an agreement. Never two: the pairing is the audit trail.
            ADD CONSTRAINT order_single_origin CHECK (
                (issue_id IS NOT NULL)::int
                + (meter_application_id IS NOT NULL)::int
                + (agreement_id IS NOT NULL)::int <= 1
            ),

            -- A verdict is about something that happened. Confirming a visit
            -- nobody has completed is not a statement about the world.
            ADD CONSTRAINT order_verdict_after_completion CHECK (
                (consumer_confirmed_at IS NULL AND consumer_disputed_at IS NULL)
                OR completed_at IS NOT NULL
            ),

            -- And it is one verdict. "It worked and it did not" is not a
            -- state a household can be in.
            ADD CONSTRAINT order_one_verdict CHECK (
                consumer_confirmed_at IS NULL OR consumer_disputed_at IS NULL
            ),

            ADD CONSTRAINT order_serial_present CHECK (
                installed_serial_no IS NULL OR btrim(installed_serial_no) <> ''
            )
        """
    )

    # One LIVE order per application, exactly as one_order_per_issue was
    # narrowed in a1c4e8b70d3f: a cancelled or failed visit must not block the
    # next attempt, but two officials must not raise two.
    op.execute(
        """
        CREATE UNIQUE INDEX one_order_per_meter_application
        ON work_order (meter_application_id)
        WHERE meter_application_id IS NOT NULL
          AND status NOT IN ('completed', 'cancelled', 'failed')
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX one_order_per_agreement
        ON work_order (agreement_id)
        WHERE agreement_id IS NOT NULL
          AND status NOT IN ('completed', 'cancelled', 'failed')
        """
    )

    # The household's applications page reads the order back by application,
    # and the official's queue reads it back by agreement.
    op.execute(
        "CREATE INDEX work_order_by_meter_application "
        "ON work_order (meter_application_id) "
        "WHERE meter_application_id IS NOT NULL"
    )
    op.execute(
        "CREATE INDEX work_order_by_agreement "
        "ON work_order (agreement_id) WHERE agreement_id IS NOT NULL"
    )


def downgrade() -> None:
    """Downgrade schema.

    Dropping the columns takes the two indexes and the four constraints with
    them. The notification_kind values stay -- PostgreSQL cannot remove an enum
    label, and any notification already sent under one would become unreadable
    if it could.
    """
    op.execute("DROP INDEX IF EXISTS work_order_by_agreement")
    op.execute("DROP INDEX IF EXISTS work_order_by_meter_application")
    op.execute("DROP INDEX IF EXISTS one_order_per_agreement")
    op.execute("DROP INDEX IF EXISTS one_order_per_meter_application")
    op.execute(
        """
        ALTER TABLE work_order
            DROP CONSTRAINT order_serial_present,
            DROP CONSTRAINT order_one_verdict,
            DROP CONSTRAINT order_verdict_after_completion,
            DROP CONSTRAINT order_single_origin,
            DROP COLUMN consumer_note,
            DROP COLUMN consumer_disputed_at,
            DROP COLUMN consumer_confirmed_at,
            DROP COLUMN installed_serial_no,
            DROP COLUMN agreement_id,
            DROP COLUMN meter_application_id
        """
    )
