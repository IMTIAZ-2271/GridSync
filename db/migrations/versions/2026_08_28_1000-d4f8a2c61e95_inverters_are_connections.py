"""an inverter is its own connection, not something hanging off a meter

Revision ID: d4f8a2c61e95
Revises: b7d3f5a92c14
Create Date: 2026-08-28 10:00:00.000000

Until now an inverter was defined by the meter in front of it.
`POST /api/sites/{id}/solar` refused unless the billing point already had a
billing meter, wrote that meter into `device.parent_device_id`, and six
statements read the link back to decide which connection a generation reading
belonged to (`site_summary`, `site_readings`, `point_solar_status`,
`site_points`, `backfill.sql`, `billing.sql`).

That made two things impossible, and both of them are things the product now
has to do:

1. **Panels before a meter.** A household applies to an installer, panels go
   on the roof and the inverter starts producing, and only later do they ask
   the utility to make the connection bidirectional. Under the old model the
   inverter could not exist until a billing meter did.

2. **Choosing an inverter and a meter separately.** Net metering is now
   applied for by picking an inverter *and then* picking which meter to swap.
   If the inverter already implied its meter there would be nothing to pick.

So the link moves off `device.parent_device_id` and onto `inverter_spec`,
mirroring exactly how `meter_spec` names its point:

    inverter_spec.site_id           NOT NULL, composite FK to device
    inverter_spec.billing_point_id  NULL,     composite FK to billing_point

**Nullable, deliberately.** A meter with `billing_role = 'billing'` must name
a point -- rule 7 counts them per point, so one that named none would be
uncountable. An inverter constrains nothing: it measures generation, it never
feeds a bill (rule 6 -- only the bidirectional meter at the grid boundary can
know the import/export split), and a site may legitimately hold one that is
not yet part of any connection. `billing_point_id IS NULL` is what "panels,
no net metering yet" looks like.

`site_id` has to come with it. The composite FK against
`billing_point (point_id, site_id)` is the thing that stops an inverter being
pointed at a connection on somebody else's site, and it is the same mechanism
`meter_spec_point_fk` uses. Without the redundant-looking `site_id` there is
nothing to compose the key from.

**`device.parent_device_id` is kept, not dropped.** It still records real
physical topology where it exists -- an inverter genuinely wired behind a
particular meter -- and dropping it would rewrite history for every inverter
already installed. It simply stops being the authority on which connection a
reading belongs to. The queries read `inverter_spec.billing_point_id` now, and
the backfill below fills that column from the parent link so no existing
inverter changes which connection it reports to.

`net_metering_agreement.inverter_device_id` records **which inverter the
application was assessed against**. The eligibility test is a measurement of a
specific roof -- 30 days of that inverter's generation against that
household's consumption -- so an agreement that did not name the inverter
would be a decision nobody could re-check. Nullable, because every agreement
that predates this migration was granted without such a test, and pretending
otherwise would be a lie in the data.

`list_view_state` is the read/unread half: one row per account per list,
holding when that account last looked at it. A row is unread when it is newer
than that timestamp. Chosen over a seen-mark per row per viewer because the
per-row table grows without bound and needs a reaper, while this one is
bounded by (accounts x lists) and answers exactly the question the UI asks.
"""
from alembic import op

revision = "d4f8a2c61e95"
down_revision = "b7d3f5a92c14"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Upgrade schema."""

    # ==================================================================
    # 1. An inverter knows its own site
    # ==================================================================
    # Backfilled from `device` rather than defaulted: it is the same fact,
    # and `device.site_id` is NOT NULL so every row has one.
    op.execute("ALTER TABLE inverter_spec ADD COLUMN site_id uuid")
    op.execute(
        """
        UPDATE inverter_spec ivs
        SET site_id = d.site_id
        FROM device d
        WHERE d.device_id = ivs.device_id
        """
    )
    op.execute("ALTER TABLE inverter_spec ALTER COLUMN site_id SET NOT NULL")
    op.execute(
        """
        ALTER TABLE inverter_spec
            ADD CONSTRAINT inverter_spec_site_fk
            FOREIGN KEY (device_id, site_id)
            REFERENCES device (device_id, site_id)
            ON UPDATE CASCADE ON DELETE CASCADE
        """
    )

    # ==================================================================
    # 2. ...and which connection, if any, it belongs to
    # ==================================================================
    op.execute("ALTER TABLE inverter_spec ADD COLUMN billing_point_id uuid")

    # Every inverter that exists today hangs off a meter, and that meter names
    # a point. Reading the link across preserves which connection each
    # inverter reports to, so no chart, bill or netted export moves.
    op.execute(
        """
        UPDATE inverter_spec ivs
        SET billing_point_id = ms.billing_point_id
        FROM device d
        JOIN meter_spec ms ON ms.device_id = d.parent_device_id
        WHERE d.device_id = ivs.device_id
          AND ms.billing_point_id IS NOT NULL
        """
    )

    # ON DELETE SET NULL, not the RESTRICT meter_spec uses. Retiring a
    # connection must not be blocked by an inverter, and must not delete one:
    # the panels are still on the roof and still generating. The inverter
    # simply stops belonging to a connection, which is a state this column
    # already has to represent.
    #
    # **SET NULL names its column.** A bare `SET NULL` on a COMPOSITE key nulls
    # every column in it -- here that would include `site_id`, which is NOT
    # NULL, so deleting a billing point failed with a not-null violation
    # instead of releasing the inverter. The column list (PostgreSQL 15+) is
    # what confines the action to the half that is actually nullable. Caught by
    # tests/test_inverter_connections.py::
    # test_deleting_a_connection_releases_the_inverter.
    op.execute(
        """
        ALTER TABLE inverter_spec
            ADD CONSTRAINT inverter_spec_point_fk
            FOREIGN KEY (billing_point_id, site_id)
            REFERENCES billing_point (point_id, site_id)
            ON UPDATE CASCADE ON DELETE SET NULL (billing_point_id)
        """
    )

    op.execute(
        "CREATE INDEX inverter_spec_by_point ON inverter_spec (billing_point_id) "
        "WHERE billing_point_id IS NOT NULL"
    )
    op.execute("CREATE INDEX inverter_spec_by_site ON inverter_spec (site_id)")

    op.execute(
        """
        COMMENT ON COLUMN inverter_spec.billing_point_id IS
        'The connection this inverter generates behind, or NULL when the '
        'panels are not part of any connection yet. Replaced '
        'device.parent_device_id as the authority on this in migration '
        'd4f8a2c61e95; parent_device_id still records physical topology.'
        """
    )

    # ==================================================================
    # 3. An agreement names the inverter it was granted for
    # ==================================================================
    # RESTRICT: an inverter named by an agreement cannot be deleted out from
    # under it. The agreement is a decision about that hardware, and the
    # eligibility figures behind it are only re-checkable while it exists.
    op.execute(
        """
        ALTER TABLE net_metering_agreement
            ADD COLUMN inverter_device_id uuid
                REFERENCES inverter_spec (device_id) ON DELETE RESTRICT
        """
    )

    op.execute(
        """
        UPDATE net_metering_agreement nma
        SET inverter_device_id = (
            -- A correlated subquery, not UPDATE ... FROM LATERAL: the target
            -- table of an UPDATE is not visible to its own FROM clause, so
            -- the lateral form cannot see nma.billing_point_id at all.
            SELECT ivs.device_id
            FROM inverter_spec ivs
            JOIN device d ON d.device_id = ivs.device_id
            WHERE ivs.billing_point_id = nma.billing_point_id
              AND d.removed_at IS NULL
            ORDER BY d.installed_at, d.device_id
            LIMIT 1
        )
        WHERE nma.billing_point_id IS NOT NULL
        """
    )

    op.execute(
        "CREATE INDEX nma_by_inverter ON net_metering_agreement (inverter_device_id) "
        "WHERE inverter_device_id IS NOT NULL"
    )

    # ==================================================================
    # 4. What each account has already looked at
    # ==================================================================
    op.execute(
        """
        CREATE TABLE list_view_state (
            account_id     uuid NOT NULL
                REFERENCES account (account_id) ON DELETE CASCADE,

            -- Names one list in one portal, e.g. 'consumer:applications',
            -- 'government:agreements'. Free text rather than an enum: the set
            -- grows every time a page is added, and an enum would make that a
            -- migration. The API owns the vocabulary.
            view_key       text NOT NULL,

            last_viewed_at timestamptz NOT NULL DEFAULT now(),

            PRIMARY KEY (account_id, view_key),
            CONSTRAINT view_key_present CHECK (btrim(view_key) <> '')
        )
        """
    )
    op.execute(
        """
        COMMENT ON TABLE list_view_state IS
        'When each account last opened each list. A row in that list is '
        '"unread" when it is newer than last_viewed_at. Bounded by '
        '(accounts x lists), unlike a seen-mark per row per viewer.'
        """
    )


def downgrade() -> None:
    """Downgrade schema."""

    op.execute("DROP TABLE IF EXISTS list_view_state")

    op.execute("DROP INDEX IF EXISTS nma_by_inverter")
    op.execute(
        "ALTER TABLE net_metering_agreement DROP COLUMN IF EXISTS inverter_device_id"
    )

    op.execute("DROP INDEX IF EXISTS inverter_spec_by_point")
    op.execute("DROP INDEX IF EXISTS inverter_spec_by_site")
    op.execute(
        "ALTER TABLE inverter_spec DROP CONSTRAINT IF EXISTS inverter_spec_point_fk"
    )
    op.execute(
        "ALTER TABLE inverter_spec DROP CONSTRAINT IF EXISTS inverter_spec_site_fk"
    )
    op.execute("ALTER TABLE inverter_spec DROP COLUMN IF EXISTS billing_point_id")
    op.execute("ALTER TABLE inverter_spec DROP COLUMN IF EXISTS site_id")
