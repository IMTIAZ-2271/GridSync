"""a solar application is fulfilled by a visit; dispatch can be open; rows say when they changed

Revision ID: e8b1d3f70a26
Revises: d4f8a2c61e95
Create Date: 2026-08-28 16:00:00.000000

Schema only. Nothing in the API or the portals reads these columns yet -- this
migration exists so the shape is settled and the ERDs are final, and so the
three workflows below can be built without another schema change.

=============================================================================
1. A SOLAR APPLICATION IS FULFILLED BY A VISIT
=============================================================================

`solar_application` has been able to reach `completed` since migration
`e7c4b19a2d83`, but nothing recorded *how*. An installer marked it done and no
inverter existed; the household had to go and register the panels themselves,
and `installed_array_id` -- the column that ties an application to the hardware
that fulfilled it -- has never once been set.

The flow this migration makes expressible:

    consumer applies
      -> installer reviews, and accepts or rejects with a reason
      -> installer raises a WORK ORDER for the installation
      -> a named technician is offered it, OR it is opened to the region
      -> the technician installs and records the inverter's serial
      -> the household confirms the visit, or disputes it with a note
      -> the installer signs off, and the array goes live

That is the same shape migration `b7d3f5a92c14` gave the meter and net-metering
flows, and it reuses their columns wherever the fact is the same one:
`installed_serial_no` is what the technician recorded, and
`consumer_confirmed_at` / `consumer_disputed_at` / `consumer_note` are the
household's verdict on the visit. Only what is genuinely new is added.

`solar_application_id` becomes `work_order`'s **fourth** origin, so
`order_single_origin` widens from three to four. An order answers a complaint,
or fulfils a meter application, or a net-metering agreement, or a solar
installation -- never two at once, because the audit trail behind "this visit
happened because of that" is the whole point of the column.

**The installer's sign-off lands on `work_order`, not on
`solar_application`.** Resolutions 11 settled this for the household's verdict
and the same reasoning applies: whether somebody turned up and did the work is
a fact about the *visit*, not a term of the application. `solar_application`
keeps only its own outcome -- `status` and `installed_array_id`.

**The array goes live by moving to `active`, not by a new column.**
`solar_array_status` has carried `offline` since the first migration. An array
installed but not yet signed off is exactly what `offline` means, and no new
state is needed to say so.

=============================================================================
2. DISPATCH CAN BE OPEN TO A REGION
=============================================================================

`work_order_assignment` is an offer to a *named* worker: the row is keyed on
(order_id, account_id), so it cannot express "any free technician in this
district may take this". Both the solar flow above and the existing complaint
flow want that -- a dispatcher who does not care which of four available
technicians goes should not have to pick one and then re-offer when they
decline.

The pool lives on the ORDER, not as a fan-out of assignment rows. Writing one
`offered` row per eligible worker would mean the first acceptance has to
release all the others, the offer-expiry sweep would fire once per worker, and
`open_jobs` -- which counts an unanswered offer as capacity already spoken for
-- would show every technician in the district as loaded by the same job.

The region itself is NOT stored. It is `site.district`, one join away, and
worker requirement 4's rule is already enforced against exactly that at the
moment an assignment is created. A copy here would be a second answer to the
same question.

`claim_expires_at` follows decision 3: deadlines are **stored** so that a query
between two sweeps is already correct, and so that editing a duration in a
config file cannot retroactively change when yesterday's offer lapsed.

=============================================================================
3. NOTHING CHASES A HOUSEHOLD THAT NEVER CONFIRMS
=============================================================================

A completed visit with no verdict blocks registration **forever**: the meter is
never issued, the application cannot advance, and
`meter_application_one_open` stops the household filing a new one. Nobody is
told, because none of the six jobs reads `work_order.completed_at`. It is the
one dead end in the fulfilment flow and it is recorded in CLAUDE.md's NOT DONE.

`verdict_deadline_at` is where a sweep will read from, stamped when the order
completes. Stored rather than computed for the same reason as every other
deadline here.

=============================================================================
4. ROWS SAY WHEN THEY LAST CHANGED
=============================================================================

`list_view_state` (migration `d4f8a2c61e95`) marks a row unread by comparing it
against a per-account watermark -- but the only timestamp every list row has is
its *creation*. So an indicator appears when something new arrives and never
when something already there changes state, which is most of what actually
happens: a visit completing, a household confirming, an official deciding, a
meter being installed.

`updated_at`, maintained by one shared trigger, is what makes
`greatest(created_at, updated_at) > watermark` answerable. Seven tables get it
-- every table behind a list that shows a status.

**`bill` deliberately does not.** Rule 1 makes it append-only and
`forbid_mutation()` permits updating exactly two columns; adding a third to
that allowance would loosen the rule's enforcement for a cosmetic feature. A
bill's creation time is enough -- corrections are new rows, which is the whole
point.

Backfilled from each table's own creation column rather than from `now()`, so
an existing row does not claim to have changed at migration time.
"""
from alembic import op

revision = "e8b1d3f70a26"
down_revision = "d4f8a2c61e95"
branch_labels = None
depends_on = None


#: (table, the column that means "when this row came into being")
TOUCHED = (
    ("work_order", "created_at"),
    ("issue", "reported_at"),
    ("solar_application", "submitted_at"),
    ("meter_application", "submitted_at"),
    ("net_metering_agreement", "created_at"),
    ("meter_asset", "issued_at"),
    ("device", "installed_at"),
)


def upgrade() -> None:
    """Upgrade schema."""

    # ==================================================================
    # 1. A solar application is fulfilled by a visit
    # ==================================================================
    # Added in its own statement and used only at runtime: PostgreSQL will not
    # let a transaction insert an enum value that same transaction added.
    op.execute("ALTER TYPE work_order_type ADD VALUE IF NOT EXISTS 'solar_install'")

    op.execute(
        """
        ALTER TABLE work_order
            ADD COLUMN solar_application_id uuid
                REFERENCES solar_application (application_id) ON DELETE CASCADE
        """
    )

    # The origin set widens from three to four.
    op.execute("ALTER TABLE work_order DROP CONSTRAINT order_single_origin")
    op.execute(
        """
        ALTER TABLE work_order
            ADD CONSTRAINT order_single_origin CHECK (
                (issue_id             IS NOT NULL)::integer
              + (meter_application_id IS NOT NULL)::integer
              + (agreement_id         IS NOT NULL)::integer
              + (solar_application_id IS NOT NULL)::integer
              <= 1
            )
        """
    )

    # One *live* visit per application, mirroring one_order_per_issue as
    # migration a1c4e8b70d3f left it: a visit that was cancelled or failed can
    # be followed by another, while two staff working one inbox cannot send two
    # crews to the same roof.
    op.execute(
        """
        CREATE UNIQUE INDEX one_order_per_solar_application
            ON work_order (solar_application_id)
            WHERE solar_application_id IS NOT NULL
              AND status NOT IN ('completed', 'cancelled', 'failed')
        """
    )
    op.execute(
        "CREATE INDEX work_order_by_solar_application "
        "ON work_order (solar_application_id) "
        "WHERE solar_application_id IS NOT NULL"
    )

    # The installer's sign-off: the second of the two approvals, and the one
    # that puts the array live.
    op.execute(
        """
        ALTER TABLE work_order
            ADD COLUMN supplier_signed_off_at timestamptz,
            ADD COLUMN supplier_signed_off_by_account_id uuid
                REFERENCES account (account_id) ON DELETE SET NULL
        """
    )
    # The household goes first. An installer signing off work the household has
    # not confirmed would make the consumer's verdict decorative, which is the
    # opposite of why it exists -- they are the one party with no interest in
    # claiming work happened that did not.
    op.execute(
        """
        ALTER TABLE work_order
            ADD CONSTRAINT order_signoff_after_confirmation CHECK (
                supplier_signed_off_at IS NULL
                OR consumer_confirmed_at IS NOT NULL
            )
        """
    )
    op.execute(
        """
        ALTER TABLE work_order
            ADD CONSTRAINT order_signoff_names_signer CHECK (
                (supplier_signed_off_at IS NULL)
                = (supplier_signed_off_by_account_id IS NULL)
            )
        """
    )

    # An application is fulfilled by exactly one array, and an array fulfils at
    # most one application. installed_array_id has existed since e7c4b19a2d83
    # and has never been set; this is the constraint that makes it trustworthy
    # once it is.
    op.execute(
        "CREATE UNIQUE INDEX one_application_per_array "
        "ON solar_application (installed_array_id) "
        "WHERE installed_array_id IS NOT NULL"
    )

    # ==================================================================
    # 2. Dispatch can be open to a region
    # ==================================================================
    op.execute(
        """
        ALTER TABLE work_order
            ADD COLUMN open_to_region boolean NOT NULL DEFAULT false,
            ADD COLUMN claim_expires_at timestamptz
        """
    )
    op.execute(
        """
        ALTER TABLE work_order
            ADD CONSTRAINT order_claim_window_needs_pool CHECK (
                claim_expires_at IS NULL OR open_to_region
            )
        """
    )
    op.execute(
        "CREATE INDEX work_order_open_pool ON work_order (site_id, claim_expires_at) "
        "WHERE open_to_region AND status = 'dispatched'"
    )
    op.execute(
        """
        COMMENT ON COLUMN work_order.open_to_region IS
        'Any approved, on-shift worker serving this site''s district may claim '
        'this order, rather than it being offered to one named technician. The '
        'district is site.district -- deliberately not copied here, since '
        'worker requirement 4 is already enforced against that one answer.'
        """
    )

    # ==================================================================
    # 3. A verdict deadline, so silence can be chased
    # ==================================================================
    op.execute("ALTER TABLE work_order ADD COLUMN verdict_deadline_at timestamptz")
    op.execute(
        """
        ALTER TABLE work_order
            ADD CONSTRAINT order_verdict_deadline_after_completion CHECK (
                verdict_deadline_at IS NULL OR completed_at IS NOT NULL
            )
        """
    )
    # Partial: the sweep only ever wants completed visits still awaiting a
    # verdict, which is a small slice of a table that mostly holds finished work.
    op.execute(
        """
        CREATE INDEX work_order_awaiting_verdict
            ON work_order (verdict_deadline_at)
            WHERE verdict_deadline_at IS NOT NULL
              AND consumer_confirmed_at IS NULL
              AND consumer_disputed_at IS NULL
        """
    )

    # ==================================================================
    # 4. Rows say when they last changed
    # ==================================================================
    op.execute(
        """
        CREATE FUNCTION touch_updated_at() RETURNS trigger
        LANGUAGE plpgsql AS $fn$
        BEGIN
            -- statement_timestamp(), not now(): now() is the TRANSACTION start,
            -- so two updates inside one transaction would claim the same
            -- instant as a row inserted at its beginning. The watermark
            -- comparison is strictly greater-than, and that tie would make a
            -- change invisible.
            NEW.updated_at := statement_timestamp();
            RETURN NEW;
        END;
        $fn$
        """
    )
    op.execute(
        """
        COMMENT ON FUNCTION touch_updated_at() IS
        'Maintains updated_at on the tables behind the portals'' lists, so an '
        'unread indicator can notice a row CHANGING and not only a row '
        'arriving. See list_view_state (migration d4f8a2c61e95).'
        """
    )

    for table, born in TOUCHED:
        op.execute(f"ALTER TABLE {table} ADD COLUMN updated_at timestamptz")
        # Backfilled from the row's own birth, not from now(): an existing row
        # has not changed just because this migration ran, and starting them
        # all at migration time would light up every list in the product for
        # every account at once.
        op.execute(f"UPDATE {table} SET updated_at = {born}")
        op.execute(
            f"ALTER TABLE {table} "
            f"ALTER COLUMN updated_at SET NOT NULL, "
            f"ALTER COLUMN updated_at SET DEFAULT now()"
        )
        op.execute(
            f"""
            CREATE TRIGGER {table}_touch_updated_at
                BEFORE UPDATE ON {table}
                FOR EACH ROW EXECUTE FUNCTION touch_updated_at()
            """
        )
        # The indicator asks "what changed since I last looked", which is a
        # range scan over this column scoped to rows the caller can see.
        op.execute(f"CREATE INDEX {table}_by_updated_at ON {table} (updated_at DESC)")


def downgrade() -> None:
    """Downgrade schema."""

    for table, _ in TOUCHED:
        op.execute(f"DROP INDEX IF EXISTS {table}_by_updated_at")
        op.execute(f"DROP TRIGGER IF EXISTS {table}_touch_updated_at ON {table}")
        op.execute(f"ALTER TABLE {table} DROP COLUMN IF EXISTS updated_at")
    op.execute("DROP FUNCTION IF EXISTS touch_updated_at()")

    op.execute("DROP INDEX IF EXISTS work_order_awaiting_verdict")
    op.execute(
        "ALTER TABLE work_order "
        "DROP CONSTRAINT IF EXISTS order_verdict_deadline_after_completion"
    )
    op.execute("ALTER TABLE work_order DROP COLUMN IF EXISTS verdict_deadline_at")

    op.execute("DROP INDEX IF EXISTS work_order_open_pool")
    op.execute(
        "ALTER TABLE work_order "
        "DROP CONSTRAINT IF EXISTS order_claim_window_needs_pool"
    )
    op.execute("ALTER TABLE work_order DROP COLUMN IF EXISTS claim_expires_at")
    op.execute("ALTER TABLE work_order DROP COLUMN IF EXISTS open_to_region")

    op.execute("DROP INDEX IF EXISTS one_application_per_array")
    op.execute(
        "ALTER TABLE work_order "
        "DROP CONSTRAINT IF EXISTS order_signoff_names_signer"
    )
    op.execute(
        "ALTER TABLE work_order "
        "DROP CONSTRAINT IF EXISTS order_signoff_after_confirmation"
    )
    op.execute(
        "ALTER TABLE work_order "
        "DROP COLUMN IF EXISTS supplier_signed_off_by_account_id"
    )
    op.execute("ALTER TABLE work_order DROP COLUMN IF EXISTS supplier_signed_off_at")

    op.execute("DROP INDEX IF EXISTS work_order_by_solar_application")
    op.execute("DROP INDEX IF EXISTS one_order_per_solar_application")

    # Narrow the origin set back to three. Deliberately fails if any order has
    # already been raised against a solar application: those rows are the audit
    # trail behind a real installation, and deleting them to fit an older
    # constraint would be destroying history to make a downgrade tidy.
    op.execute("ALTER TABLE work_order DROP CONSTRAINT order_single_origin")
    op.execute(
        """
        ALTER TABLE work_order
            ADD CONSTRAINT order_single_origin CHECK (
                (issue_id             IS NOT NULL)::integer
              + (meter_application_id IS NOT NULL)::integer
              + (agreement_id         IS NOT NULL)::integer
              <= 1
            )
        """
    )
    op.execute("ALTER TABLE work_order DROP COLUMN IF EXISTS solar_application_id")

    # work_order_type keeps 'solar_install'. PostgreSQL cannot remove an enum
    # value, and recreating the type would require rewriting every order. The
    # value is inert without the column above.
