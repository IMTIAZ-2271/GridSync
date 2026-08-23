"""organisations, notifications, ratings, applications, worker approval

Revision ID: e7c4b19a2d83
Revises: d5a7c2b91e40
Create Date: 2026-08-23 14:00:00.000000

The foundation the four portals' new requirements all stand on. Nothing here
is a portal feature by itself; every one of them is a table that several
portals need and that is expensive to bolt on later.

**Organisations become real.** 'supplier' and 'government' were bare
`account_role` values with nothing behind them -- no company to pick from a
dropdown, no service area, nothing to attach a rating or an application to.
`distribution_company` (the regulated utility that owns a meter) and
`supplier_company` (the private installer that fits solar) are separate
entities, not one table with a type flag: they are regulated differently, a
consumer picks between them for different reasons, and only one of them is
rated. Staff accounts attach through `supplier_profile` / `government_profile`,
so a firm with two logins is still one supplier in every dropdown and its
reputation does not split.

**Districts become a table.** `site.district` was free text canonicalised in
Python, and that had already leaked `Dhaka`, `dhaka` and `g` into the
regulator's rollup. District now appears on five more tables, so it gets a
foreign key. Existing non-canonical values are preserved as `district` rows
with `is_selectable = false` -- they stay visible and joinable, but no new
site, worker or company can be filed under them. Deleting them is a data
decision, not a migration's call.

**Government access stops being a shared secret.** `government_official_code`
is a pre-issued list, one code per official, each carrying the district that
official governs and claimable exactly once. This replaces the single
environment-variable code that CLAUDE.md has been carrying as a known
weakness; the registration route follows in the same change.

**Deadlines are stored, not computed.** `work_order_assignment` gains
`offer_expires_at` and `start_deadline_at`, and `assignment_status` gains
'expired'. Storing the instant means a query is correct between sweeps of the
jobs runner rather than only just after one.

Enum values added here are deliberately not USED here: PostgreSQL will not let
a transaction insert a value its own transaction added to an enum. Seeds and
code that need them come after this commits.
"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "e7c4b19a2d83"
down_revision: Union[str, Sequence[str], None] = "d5a7c2b91e40"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


NEW_ENUMS = {
    "worker_kind": ("government", "private"),
    "approval_status": ("pending", "approved", "rejected"),
    "application_status": (
        "submitted", "under_review", "accepted", "rejected",
        "withdrawn", "completed",
    ),
    "rating_subject": ("worker", "supplier"),
    "org_status": ("active", "suspended", "closed"),
    "notification_severity": ("info", "warning", "critical"),
    "notification_kind": (
        "consumption_threshold",
        "work_order_offered",
        "work_order_offer_expired",
        "work_order_start_overdue",
        "work_order_started",
        "work_order_completed",
        "work_completion_review",
        "issue_filed",
        "issue_updated",
        "worker_approval",
        "solar_application",
        "net_metering_application",
        "rating_request",
        "announcement",
    ),
}

# Extensions to enums that already exist. Additive only -- PostgreSQL cannot
# remove an enum value, which is why downgrade() below leaves them in place.
ENUM_ADDITIONS = {
    # A work order offer that nobody accepted inside the window (supplier
    # requirement 5).
    "assignment_status": ("expired",),
    # Consumer requirement 6's issue-type dropdown needs somewhere to file a
    # complaint about the installer or the net-metering process itself,
    # neither of which is a fault in a piece of hardware.
    "issue_category": ("solar_installation", "supplier_service", "net_metering"),
}

# The eight canonical Dhaka districts, moved out of services/api/routes_sites.py
# so the schema is the source of truth rather than a dict in a handler.
DISTRICTS = (
    ("Badda", "23.780000", "90.425000"),
    ("Banani", "23.793000", "90.404000"),
    ("Bashundhara", "23.815000", "90.433000"),
    ("Dhanmondi", "23.746000", "90.376000"),
    ("Gulshan", "23.791000", "90.414000"),
    ("Mirpur", "23.806000", "90.365000"),
    ("Mohammadpur", "23.766000", "90.359000"),
    ("Uttara", "23.868000", "90.399000"),
)


def upgrade() -> None:
    """Upgrade schema."""

    for name, values in NEW_ENUMS.items():
        labels = ", ".join(f"'{v}'" for v in values)
        op.execute(f"CREATE TYPE {name} AS ENUM ({labels})")

    for name, values in ENUM_ADDITIONS.items():
        for value in values:
            op.execute(f"ALTER TYPE {name} ADD VALUE IF NOT EXISTS '{value}'")

    # ==================================================================
    # 1. District: free text becomes a key
    # ==================================================================
    op.execute(
        """
        CREATE TABLE district (
            name          text PRIMARY KEY,
            latitude      numeric(9,6) NOT NULL,
            longitude     numeric(9,6) NOT NULL,

            -- False for values that exist only because they were typed in
            -- before the column had a key. They stay joinable so no history
            -- is lost, but nothing new may be filed under them.
            is_selectable boolean NOT NULL DEFAULT true,

            CONSTRAINT district_lat_range CHECK (latitude  BETWEEN -90  AND 90),
            CONSTRAINT district_lon_range CHECK (longitude BETWEEN -180 AND 180)
        )
        """
    )

    values = ", ".join(
        f"('{n}', {lat}, {lon}, true)" for n, lat, lon in DISTRICTS
    )
    op.execute(
        f"INSERT INTO district (name, latitude, longitude, is_selectable) "
        f"VALUES {values}"
    )

    # Anything already on a site that is not one of the eight. Centroid is
    # taken from the site itself rather than invented -- these rows exist to
    # keep the FK addable and the rollup honest, not to be used again.
    op.execute(
        """
        INSERT INTO district (name, latitude, longitude, is_selectable)
        SELECT s.district,
               round(avg(s.latitude), 6),
               round(avg(s.longitude), 6),
               false
        FROM site s
        WHERE NOT EXISTS (
            SELECT 1 FROM district d WHERE d.name = s.district
        )
        GROUP BY s.district
        """
    )
    # Same for worker service districts, which are also free text today.
    op.execute(
        """
        INSERT INTO district (name, latitude, longitude, is_selectable)
        SELECT DISTINCT w.service_district, 23.780000, 90.279000, false
        FROM worker_profile w
        WHERE NOT EXISTS (
            SELECT 1 FROM district d WHERE d.name = w.service_district
        )
        """
    )

    op.execute(
        """
        ALTER TABLE site ADD CONSTRAINT site_district_fk
            FOREIGN KEY (district) REFERENCES district (name)
            ON UPDATE CASCADE ON DELETE RESTRICT
        """
    )
    op.execute(
        """
        ALTER TABLE worker_profile ADD CONSTRAINT worker_district_fk
            FOREIGN KEY (service_district) REFERENCES district (name)
            ON UPDATE CASCADE ON DELETE RESTRICT
        """
    )

    # ==================================================================
    # 2. Organisations
    # ==================================================================
    op.execute(
        """
        CREATE TABLE distribution_company (
            company_id    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            code          text NOT NULL UNIQUE,
            name          text NOT NULL,
            contact_email citext,
            contact_phone text,
            status        org_status NOT NULL DEFAULT 'active',
            created_at    timestamptz NOT NULL DEFAULT now(),

            CONSTRAINT dc_code_present CHECK (btrim(code) <> '')
        )
        """
    )

    op.execute(
        """
        CREATE TABLE distribution_company_area (
            company_id uuid NOT NULL
                REFERENCES distribution_company (company_id) ON DELETE CASCADE,
            district   text NOT NULL
                REFERENCES district (name) ON UPDATE CASCADE ON DELETE RESTRICT,

            PRIMARY KEY (company_id, district)
        )
        """
    )

    op.execute(
        """
        CREATE TABLE supplier_company (
            supplier_id   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            code          text NOT NULL UNIQUE,
            name          text NOT NULL,
            license_no    text UNIQUE,
            contact_email citext,
            contact_phone text,
            status        org_status NOT NULL DEFAULT 'active',
            created_at    timestamptz NOT NULL DEFAULT now(),

            CONSTRAINT supplier_code_present CHECK (btrim(code) <> '')
        )
        """
    )

    op.execute(
        """
        CREATE TABLE supplier_service_area (
            supplier_id uuid NOT NULL
                REFERENCES supplier_company (supplier_id) ON DELETE CASCADE,
            district    text NOT NULL
                REFERENCES district (name) ON UPDATE CASCADE ON DELETE RESTRICT,

            PRIMARY KEY (supplier_id, district)
        )
        """
    )

    # Staff. One account belongs to one company; a company has many accounts.
    op.execute(
        """
        CREATE TABLE supplier_profile (
            account_id  uuid PRIMARY KEY
                REFERENCES account (account_id) ON DELETE CASCADE,
            supplier_id uuid NOT NULL
                REFERENCES supplier_company (supplier_id) ON DELETE RESTRICT,
            job_title   text,
            created_at  timestamptz NOT NULL DEFAULT now()
        )
        """
    )

    # ==================================================================
    # 3. Government access by pre-issued code
    # ==================================================================
    op.execute(
        """
        CREATE TABLE government_official_code (
            code                  text PRIMARY KEY,
            district              text NOT NULL
                REFERENCES district (name) ON UPDATE CASCADE ON DELETE RESTRICT,
            issued_to             text NOT NULL,
            claimed_by_account_id uuid UNIQUE
                REFERENCES account (account_id) ON DELETE SET NULL,
            claimed_at            timestamptz,
            created_at            timestamptz NOT NULL DEFAULT now(),

            -- Claimed is one fact, not two. Without this a code could be
            -- marked used while naming nobody, which is indistinguishable
            -- from a code burnt by a bug.
            CONSTRAINT code_claim_consistent
                CHECK ((claimed_by_account_id IS NULL) = (claimed_at IS NULL))
        )
        """
    )

    op.execute(
        """
        CREATE TABLE government_profile (
            account_id    uuid PRIMARY KEY
                REFERENCES account (account_id) ON DELETE CASCADE,
            district      text NOT NULL
                REFERENCES district (name) ON UPDATE CASCADE ON DELETE RESTRICT,
            official_code text NOT NULL UNIQUE
                REFERENCES government_official_code (code)
                ON UPDATE CASCADE ON DELETE RESTRICT,
            created_at    timestamptz NOT NULL DEFAULT now()
        )
        """
    )

    # ==================================================================
    # 4. Workers: kind, employer, and an approval gate
    # ==================================================================
    op.execute(
        """
        ALTER TABLE worker_profile
            ADD COLUMN worker_kind worker_kind NOT NULL DEFAULT 'private',
            ADD COLUMN distribution_company_id uuid
                REFERENCES distribution_company (company_id) ON DELETE RESTRICT,
            ADD COLUMN approval_status approval_status NOT NULL
                DEFAULT 'pending',
            ADD COLUMN approved_by_account_id uuid
                REFERENCES account (account_id) ON DELETE SET NULL,
            ADD COLUMN approved_at timestamptz,
            ADD COLUMN rejection_reason text
        """
    )

    # Workers who already exist are already working. Defaulting them to
    # 'pending' would silently empty every queue the worker portal renders.
    op.execute(
        "UPDATE worker_profile SET approval_status = 'approved', "
        "approved_at = now()"
    )

    op.execute(
        """
        ALTER TABLE worker_profile
            ADD CONSTRAINT worker_kind_employer CHECK (
                (worker_kind = 'government')
                    = (distribution_company_id IS NOT NULL)
            ),
            ADD CONSTRAINT worker_approval_timestamps CHECK (
                (approval_status = 'pending') = (approved_at IS NULL)
            ),
            ADD CONSTRAINT worker_rejection_reason CHECK (
                approval_status = 'rejected' OR rejection_reason IS NULL
            )
        """
    )

    # ==================================================================
    # 5. Who owns the connection, who installed the panels
    #
    # Consumer requirement 6: filing a meter fault asks which distribution
    # company handles that meter; filing an installation complaint asks which
    # supplier fitted it. Neither question had an answer in the schema.
    # ==================================================================
    op.execute(
        """
        ALTER TABLE billing_point
            ADD COLUMN distribution_company_id uuid
                REFERENCES distribution_company (company_id) ON DELETE RESTRICT
        """
    )
    op.execute(
        """
        ALTER TABLE solar_array
            ADD COLUMN installed_by_supplier_id uuid
                REFERENCES supplier_company (supplier_id) ON DELETE RESTRICT
        """
    )

    # ==================================================================
    # 6. Issues: who it is against, and did it actually get fixed
    # ==================================================================
    op.execute(
        """
        ALTER TABLE issue
            ADD COLUMN distribution_company_id uuid
                REFERENCES distribution_company (company_id) ON DELETE SET NULL,
            ADD COLUMN supplier_id uuid
                REFERENCES supplier_company (supplier_id) ON DELETE SET NULL,

            -- Consumer requirement 10: the household confirms the work, or
            -- says it is not fixed. Two nullable instants rather than a
            -- boolean, because "not answered yet" is a third state and the
            -- time it was answered is worth keeping.
            ADD COLUMN consumer_confirmed_at timestamptz,
            ADD COLUMN consumer_disputed_at timestamptz,
            ADD COLUMN consumer_feedback text
        """
    )
    op.execute(
        """
        ALTER TABLE issue
            ADD CONSTRAINT issue_verdict_is_one CHECK (
                consumer_confirmed_at IS NULL OR consumer_disputed_at IS NULL
            )
        """
    )

    # ==================================================================
    # 7. Assignment deadlines
    # ==================================================================
    op.execute(
        """
        ALTER TABLE work_order_assignment
            -- Supplier requirement 5: an unaccepted offer expires (3h).
            ADD COLUMN offer_expires_at timestamptz,
            -- Worker requirement 5: accepted but not started (1 day) goes
            -- back to the supplier for reassignment.
            ADD COLUMN start_deadline_at timestamptz,
            ADD COLUMN expired_at timestamptz
        """
    )
    op.execute(
        "CREATE INDEX assignment_offer_expiring ON work_order_assignment "
        "(offer_expires_at) WHERE status = 'offered' "
        "AND offer_expires_at IS NOT NULL"
    )
    op.execute(
        "CREATE INDEX assignment_start_overdue ON work_order_assignment "
        "(start_deadline_at) WHERE status = 'accepted' "
        "AND start_deadline_at IS NOT NULL"
    )

    # ==================================================================
    # 8. Notifications
    # ==================================================================
    op.execute(
        """
        CREATE TABLE notification (
            notification_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            account_id      uuid NOT NULL
                REFERENCES account (account_id) ON DELETE CASCADE,
            kind            notification_kind NOT NULL,
            severity        notification_severity NOT NULL DEFAULT 'info',
            title           text NOT NULL,
            body            text,

            -- What it is about, loosely. Deliberately not a foreign key: a
            -- notification outlives the row that caused it, and a cascade
            -- that deleted someone's history to tidy up an issue would be
            -- worse than a dangling id the UI simply does not link.
            entity_type     text,
            entity_id       text,

            created_at      timestamptz NOT NULL DEFAULT now(),
            read_at         timestamptz,

            -- Rule 4 applied to a background job: the sweep that writes these
            -- must be safe to run twice. A dedupe key makes the database
            -- refuse the duplicate instead of the job having to remember.
            dedupe_key      text,

            CONSTRAINT notification_read_after
                CHECK (read_at IS NULL OR read_at >= created_at)
        )
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX notification_dedupe ON notification "
        "(account_id, dedupe_key) WHERE dedupe_key IS NOT NULL"
    )
    op.execute(
        "CREATE INDEX notification_unread ON notification "
        "(account_id, created_at DESC) WHERE read_at IS NULL"
    )
    op.execute(
        "CREATE INDEX notification_recent ON notification "
        "(account_id, created_at DESC)"
    )

    # ==================================================================
    # 9. Ratings
    # ==================================================================
    op.execute(
        """
        CREATE TABLE service_rating (
            rating_id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            order_id            uuid NOT NULL
                REFERENCES work_order (order_id) ON DELETE CASCADE,
            rated_by_account_id uuid NOT NULL
                REFERENCES account (account_id) ON DELETE RESTRICT,
            subject             rating_subject NOT NULL,

            -- Exactly one of these is set, per rating_subject_target below.
            worker_account_id   uuid
                REFERENCES worker_profile (account_id) ON DELETE SET NULL,
            supplier_id         uuid
                REFERENCES supplier_company (supplier_id) ON DELETE SET NULL,

            stars               smallint NOT NULL,
            comment             text,
            created_at          timestamptz NOT NULL DEFAULT now(),

            CONSTRAINT rating_stars CHECK (stars BETWEEN 1 AND 5),

            -- A rating names a worker or a supplier, never both and never
            -- neither. Without this, "subject = 'worker'" could be filed with
            -- a supplier_id and would quietly land in the supplier's average.
            CONSTRAINT rating_subject_target CHECK (
                (subject = 'worker'
                     AND worker_account_id IS NOT NULL
                     AND supplier_id IS NULL)
                OR
                (subject = 'supplier'
                     AND supplier_id IS NOT NULL
                     AND worker_account_id IS NULL)
            ),

            -- One verdict per rater per subject per job. Supplier
            -- requirement 4 sorts workers by rating, so a household able to
            -- rate the same job twice could move that ordering at will.
            CONSTRAINT rating_one_per_subject
                UNIQUE (order_id, rated_by_account_id, subject)
        )
        """
    )
    op.execute(
        "CREATE INDEX rating_by_worker ON service_rating (worker_account_id) "
        "WHERE worker_account_id IS NOT NULL"
    )
    op.execute(
        "CREATE INDEX rating_by_supplier ON service_rating (supplier_id) "
        "WHERE supplier_id IS NOT NULL"
    )

    # ==================================================================
    # 10. Solar applications
    #
    # Consumer requirement 7 splits in two, and the split matters: an
    # application to INSTALL goes to a supplier and lives here; an application
    # to EXPORT under net metering goes to the government and is already
    # modelled -- it is a net_metering_agreement in status 'pending', which
    # the regulator's approval queue reads today. No second table for it.
    # ==================================================================
    op.execute(
        """
        CREATE TABLE solar_application (
            application_id        uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            site_id               uuid NOT NULL
                REFERENCES site (site_id) ON DELETE CASCADE,
            billing_point_id      uuid NOT NULL,
            account_id            uuid NOT NULL
                REFERENCES account (account_id) ON DELETE RESTRICT,
            supplier_id           uuid NOT NULL
                REFERENCES supplier_company (supplier_id) ON DELETE RESTRICT,

            status                application_status NOT NULL
                                      DEFAULT 'submitted',
            requested_capacity_kw numeric(8,3) NOT NULL,
            panel_count           smallint,
            notes                 text,

            submitted_at          timestamptz NOT NULL DEFAULT now(),
            decided_at            timestamptz,
            decided_by_account_id uuid
                REFERENCES account (account_id) ON DELETE SET NULL,
            decision_notes        text,

            -- Set when the installation actually happened, closing the loop
            -- from application to hardware.
            installed_array_id    uuid
                REFERENCES solar_array (array_id) ON DELETE SET NULL,

            CONSTRAINT application_capacity CHECK (requested_capacity_kw > 0),
            CONSTRAINT application_panels
                CHECK (panel_count IS NULL OR panel_count > 0),
            CONSTRAINT application_decided_after
                CHECK (decided_at IS NULL OR decided_at >= submitted_at),
            CONSTRAINT application_decision_timestamps CHECK (
                (status IN ('submitted', 'under_review'))
                    = (decided_at IS NULL)
            ),

            -- The application is for one connection, and that connection has
            -- to be on the site it names.
            CONSTRAINT application_point_fk
                FOREIGN KEY (billing_point_id, site_id)
                REFERENCES billing_point (point_id, site_id)
                ON UPDATE CASCADE ON DELETE CASCADE
        )
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX solar_application_one_open "
        "ON solar_application (billing_point_id) "
        "WHERE status IN ('submitted', 'under_review')"
    )
    op.execute(
        "CREATE INDEX solar_application_queue ON solar_application "
        "(supplier_id, submitted_at DESC) "
        "WHERE status IN ('submitted', 'under_review')"
    )

    # ==================================================================
    # 11. Consumption limit
    #
    # Consumer requirement 5. The MONTHLY figure is what the household sets;
    # the daily average is derived from it and the length of the month, so it
    # is not stored -- storing both invites them to disagree.
    # ==================================================================
    op.execute(
        """
        CREATE TABLE site_consumption_limit (
            site_id       uuid PRIMARY KEY
                REFERENCES site (site_id) ON DELETE CASCADE,
            monthly_kwh   numeric(12,4) NOT NULL,
            notify_at_pct numeric(5,2) NOT NULL DEFAULT 80.00,
            set_by_account_id uuid
                REFERENCES account (account_id) ON DELETE SET NULL,
            updated_at    timestamptz NOT NULL DEFAULT now(),

            CONSTRAINT limit_positive CHECK (monthly_kwh > 0),
            CONSTRAINT limit_notify_pct
                CHECK (notify_at_pct BETWEEN 1 AND 100)
        )
        """
    )


def downgrade() -> None:
    """Downgrade schema.

    The enum VALUES added to assignment_status and issue_category are not
    removed: PostgreSQL cannot drop an enum label, and recreating either type
    means rewriting every column that uses it. They are additive and unused
    once the tables below are gone.
    """

    op.execute("DROP TABLE IF EXISTS site_consumption_limit")
    op.execute("DROP TABLE IF EXISTS solar_application")
    op.execute("DROP TABLE IF EXISTS service_rating")
    op.execute("DROP TABLE IF EXISTS notification")

    op.execute("DROP INDEX IF EXISTS assignment_start_overdue")
    op.execute("DROP INDEX IF EXISTS assignment_offer_expiring")
    op.execute(
        "ALTER TABLE work_order_assignment "
        "DROP COLUMN expired_at, "
        "DROP COLUMN start_deadline_at, "
        "DROP COLUMN offer_expires_at"
    )

    op.execute("ALTER TABLE issue DROP CONSTRAINT issue_verdict_is_one")
    op.execute(
        "ALTER TABLE issue "
        "DROP COLUMN consumer_feedback, "
        "DROP COLUMN consumer_disputed_at, "
        "DROP COLUMN consumer_confirmed_at, "
        "DROP COLUMN supplier_id, "
        "DROP COLUMN distribution_company_id"
    )

    op.execute(
        "ALTER TABLE solar_array DROP COLUMN installed_by_supplier_id"
    )
    op.execute(
        "ALTER TABLE billing_point DROP COLUMN distribution_company_id"
    )

    for constraint in (
        "worker_rejection_reason",
        "worker_approval_timestamps",
        "worker_kind_employer",
    ):
        op.execute(f"ALTER TABLE worker_profile DROP CONSTRAINT {constraint}")
    op.execute(
        "ALTER TABLE worker_profile "
        "DROP COLUMN rejection_reason, "
        "DROP COLUMN approved_at, "
        "DROP COLUMN approved_by_account_id, "
        "DROP COLUMN approval_status, "
        "DROP COLUMN distribution_company_id, "
        "DROP COLUMN worker_kind"
    )

    op.execute("DROP TABLE IF EXISTS government_profile")
    op.execute("DROP TABLE IF EXISTS government_official_code")
    op.execute("DROP TABLE IF EXISTS supplier_profile")
    op.execute("DROP TABLE IF EXISTS supplier_service_area")
    op.execute("DROP TABLE IF EXISTS supplier_company")
    op.execute("DROP TABLE IF EXISTS distribution_company_area")
    op.execute("DROP TABLE IF EXISTS distribution_company")

    op.execute(
        "ALTER TABLE worker_profile DROP CONSTRAINT worker_district_fk"
    )
    op.execute("ALTER TABLE site DROP CONSTRAINT site_district_fk")
    op.execute("DROP TABLE IF EXISTS district")

    for name in reversed(list(NEW_ENUMS)):
        op.execute(f"DROP TYPE IF EXISTS {name}")
