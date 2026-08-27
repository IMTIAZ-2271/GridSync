"""meters belong to a person before they belong to a site

Revision ID: c9e2f4a71b83
Revises: a1c4e8b70d3f
Create Date: 2026-08-27 11:00:00.000000

Until now a household registered a billing meter by *typing a serial number*.
That is backwards: a meter is a piece of the utility's hardware, issued to a
customer against their identity, and the household's part is saying which of
their meters serves which connection. Inventing a serial at a web form let a
consumer conjure hardware that does not exist, and gave the utility no record
of what it had handed out.

Two tables close that.

**`meter_asset`** is a meter issued to an *account* -- the consumer's identity,
the same `account.national_id` that registration made unique. It carries the
serial, make and model, and a nullable `device_id`:

    available  :=  device_id IS NULL

Assigning one to a connection creates the `device` and `meter_spec` rows
exactly as before and stamps `device_id` here, so nothing about rules 3 or 7
changes -- billing is still keyed on the billing point, and the point still has
exactly one active billing device. `device.site_id` stays NOT NULL, because a
`device` remains what it always was: hardware installed somewhere. The
unassigned meter is a different fact and gets its own row.

There is deliberately no `assigned_at` column. `device.installed_at` already
records when the meter went in and is one join away; a second copy could only
ever disagree, and `ON DELETE SET NULL` on `device_id` would strand it.

**`meter_application`** is what a household files when it has no available
meter to assign. It is reviewed by a government official scoped to the site's
own district -- the same shape as the net-metering queue and the worker
approval queue that already exist -- and accepting it *issues* a `meter_asset`
to the applicant, which is what makes the loop close: apply, be approved, find
a meter in your list, assign it.

`district` is deliberately NOT copied onto the application. The official's
scope joins `site.district`, which is the one authority on where the site is;
a denormalized copy could only drift out of step with it.

`status` reuses `application_status` rather than minting a near-identical enum.
`completed` is excluded by CHECK: a solar installation is completed when the
panels are on the roof, but issuing a meter has no second act -- acceptance
*is* the delivery.

Existing meters are backfilled as assets owned by their site's account, already
assigned, so the household's list is complete from the first render rather than
starting empty beside hardware they plainly own.
"""
from alembic import op

revision = "c9e2f4a71b83"
down_revision = "a1c4e8b70d3f"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Upgrade schema."""

    # A household hears when its meter application is decided. Added here and
    # used only at runtime: PostgreSQL will not let a transaction insert an
    # enum value that same transaction added.
    op.execute(
        "ALTER TYPE notification_kind ADD VALUE IF NOT EXISTS 'meter_application'"
    )

    op.execute(
        """
        CREATE TABLE meter_asset (
            meter_asset_id       uuid PRIMARY KEY DEFAULT gen_random_uuid(),

            -- The consumer the meter was issued to. This is the "registered to
            -- the consumer via their unique identity" half: account_id is the
            -- account whose national_id is UNIQUE.
            account_id           uuid NOT NULL
                REFERENCES account (account_id) ON DELETE CASCADE,

            serial_no            text NOT NULL UNIQUE,
            manufacturer         text,
            model                text,

            -- Which utility handed it over. Nullable because a district can be
            -- served by more than one company and a legacy meter predates the
            -- record of which one issued it.
            issued_by_company_id uuid
                REFERENCES distribution_company (company_id) ON DELETE SET NULL,
            issued_at            timestamptz NOT NULL DEFAULT now(),

            -- NULL while the meter sits in the consumer's hands unassigned.
            -- UNIQUE because one physical meter serves one installed position.
            device_id            uuid UNIQUE
                REFERENCES device (device_id) ON DELETE SET NULL,

            CONSTRAINT meter_asset_serial_present CHECK (btrim(serial_no) <> '')
        )
        """
    )

    # The list the household picks from, and the only question the assign
    # form asks: what have I got that is not in use?
    op.execute(
        "CREATE INDEX meter_asset_available ON meter_asset (account_id) "
        "WHERE device_id IS NULL"
    )

    op.execute(
        """
        CREATE TABLE meter_application (
            application_id        uuid PRIMARY KEY DEFAULT gen_random_uuid(),

            account_id            uuid NOT NULL
                REFERENCES account (account_id) ON DELETE RESTRICT,
            -- Filed "through the site": the application names where the meter
            -- is wanted, which is also what scopes it to a district official.
            site_id               uuid NOT NULL
                REFERENCES site (site_id) ON DELETE CASCADE,

            status                application_status NOT NULL
                                      DEFAULT 'submitted',
            reason                text,

            submitted_at          timestamptz NOT NULL DEFAULT now(),
            decided_at            timestamptz,
            decided_by_account_id uuid
                REFERENCES account (account_id) ON DELETE SET NULL,
            decision_notes        text,

            -- Set when acceptance issued a meter, closing application ->
            -- hardware the way solar_application.installed_array_id does.
            issued_meter_asset_id uuid UNIQUE
                REFERENCES meter_asset (meter_asset_id) ON DELETE SET NULL,

            CONSTRAINT meter_application_decided_after
                CHECK (decided_at IS NULL OR decided_at >= submitted_at),
            CONSTRAINT meter_application_decision_timestamps CHECK (
                (status IN ('submitted', 'under_review'))
                    = (decided_at IS NULL)
            ),
            -- Issuing a meter is a consequence of acceptance and of nothing
            -- else. A rejected application holding an asset id would read as
            -- hardware handed out by a refusal.
            CONSTRAINT meter_application_issue_on_accept
                CHECK (issued_meter_asset_id IS NULL OR status = 'accepted'),
            -- Acceptance IS the delivery; there is no later "completed" act.
            CONSTRAINT meter_application_no_completion
                CHECK (status <> 'completed')
        )
        """
    )

    # One live request per site. A household that has applied and is waiting
    # must not be able to file the same request twice, and two officials
    # working one district must not both issue against it.
    op.execute(
        "CREATE UNIQUE INDEX meter_application_one_open "
        "ON meter_application (site_id) "
        "WHERE status IN ('submitted', 'under_review')"
    )
    # Oldest-first, which is how the queue is read: a queue sorted by recency
    # buries whoever nobody picked up.
    op.execute(
        "CREATE INDEX meter_application_queue ON meter_application "
        "(submitted_at) WHERE status IN ('submitted', 'under_review')"
    )

    # ------------------------------------------------------------------
    # Backfill: every meter that already exists was issued to somebody.
    #
    # Includes removed meters on purpose -- they were issued, and they hold a
    # device_id, so they correctly read as unavailable rather than as spare
    # stock the household could assign again.
    # ------------------------------------------------------------------
    op.execute(
        """
        INSERT INTO meter_asset (
            account_id, serial_no, manufacturer, model,
            issued_by_company_id, issued_at, device_id
        )
        SELECT s.account_id,
               d.serial_no,
               d.manufacturer,
               d.model,
               bp.distribution_company_id,
               d.installed_at,
               d.device_id
        FROM device d
        JOIN site s ON s.site_id = d.site_id
        LEFT JOIN meter_spec ms ON ms.device_id = d.device_id
        LEFT JOIN billing_point bp ON bp.point_id = ms.billing_point_id
        WHERE d.device_type = 'meter'
        """
    )


def downgrade() -> None:
    """Downgrade schema.

    The two tables go. The 'meter_application' notification_kind value stays --
    PostgreSQL cannot remove an enum label, and any notification already sent
    under it would become unreadable if it could.
    """
    op.execute("DROP TABLE meter_application")
    op.execute("DROP TABLE meter_asset")
