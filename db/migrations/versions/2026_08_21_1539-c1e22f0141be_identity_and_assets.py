"""identity and assets

Revision ID: c1e22f0141be
Revises:
Create Date: 2026-08-21 15:39:43.176882

Identity and physical-asset modules from docs/erd-logical.md:

    account, worker_profile, worker_skill,
    site, device, meter_spec, inverter_spec, solar_array,
    net_metering_agreement

Telemetry, pricing, billing & credit, operations and analytics are deliberately
not touched here.

Written as raw SQL rather than SQLAlchemy ops: the exclusion constraint, the
partial unique indexes and the constraint triggers have no clean Core
equivalent, and CLAUDE.md calls for readable raw SQL on the schema path.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c1e22f0141be'
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""

    # ------------------------------------------------------------------
    # Extensions
    # ------------------------------------------------------------------
    # citext   -> account.email, case-insensitive uniqueness
    # btree_gist -> lets '=' participate in the net_metering_agreement
    #               exclusion constraint alongside a range operator
    op.execute("CREATE EXTENSION IF NOT EXISTS citext")
    op.execute("CREATE EXTENSION IF NOT EXISTS btree_gist")

    # ------------------------------------------------------------------
    # Enum types (native, per the schema rules -- not VARCHAR + CHECK)
    # ------------------------------------------------------------------
    op.execute("CREATE TYPE account_role AS ENUM ('consumer', 'worker', 'admin')")
    op.execute("CREATE TYPE account_status AS ENUM ('active', 'suspended', 'closed')")
    op.execute(
        "CREATE TYPE worker_availability AS ENUM "
        "('available', 'busy', 'off_duty', 'on_leave')"
    )
    op.execute(
        "CREATE TYPE worker_skill_type AS ENUM "
        "('meter_install', 'meter_swap', 'meter_removal', 'inverter_service', "
        "'panel_cleaning', 'inspection', 'seal_check', 'wiring')"
    )
    op.execute(
        "CREATE TYPE skill_proficiency AS ENUM ('trainee', 'competent', 'expert')"
    )
    op.execute(
        "CREATE TYPE site_connection_type AS ENUM "
        "('residential', 'commercial', 'industrial')"
    )
    op.execute(
        "CREATE TYPE site_status AS ENUM ('active', 'suspended', 'disconnected')"
    )
    op.execute("CREATE TYPE device_type AS ENUM ('meter', 'inverter')")
    op.execute("CREATE TYPE device_status AS ENUM ('active', 'faulty', 'removed')")
    op.execute("CREATE TYPE meter_flow AS ENUM ('unidirectional', 'bidirectional')")
    op.execute(
        "CREATE TYPE meter_billing_role AS ENUM "
        "('billing', 'generation_only', 'check_meter')"
    )
    op.execute(
        "CREATE TYPE solar_array_status AS ENUM "
        "('active', 'offline', 'decommissioned')"
    )
    op.execute(
        "CREATE TYPE nma_settlement_type AS ENUM "
        "('rollover_only', 'annual_cashout', 'net_billing')"
    )
    op.execute(
        "CREATE TYPE nma_status AS ENUM "
        "('pending', 'active', 'suspended', 'terminated')"
    )

    # ==================================================================
    # Identity
    # ==================================================================
    op.execute(
        """
        CREATE TABLE account (
            account_id    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            email         citext NOT NULL UNIQUE,
            password_hash text   NOT NULL,
            full_name     text   NOT NULL,
            phone         text,
            national_id   text UNIQUE,
            role          account_role   NOT NULL DEFAULT 'consumer',
            status        account_status NOT NULL DEFAULT 'active',
            created_at    timestamptz NOT NULL DEFAULT now(),
            updated_at    timestamptz NOT NULL DEFAULT now(),

            -- Both sides are cast to text on purpose. citext compares
            -- case-insensitively, so the un-cast `email = lower(email)` from
            -- the design doc is a tautology and would never reject anything.
            CONSTRAINT account_email_lower
                CHECK (email::text = lower(email::text))
        )
        """
    )

    op.execute(
        """
        CREATE TABLE worker_profile (
            account_id       uuid PRIMARY KEY
                REFERENCES account (account_id) ON DELETE CASCADE,
            employee_code    text NOT NULL UNIQUE,
            service_district text NOT NULL,
            max_daily_jobs   smallint NOT NULL DEFAULT 4,
            availability     worker_availability NOT NULL DEFAULT 'available',
            hired_on         date NOT NULL,
            left_on          date,

            CONSTRAINT worker_employment_dates
                CHECK (left_on IS NULL OR left_on >= hired_on)
        )
        """
    )

    op.execute(
        """
        CREATE TABLE worker_skill (
            account_id        uuid NOT NULL
                REFERENCES worker_profile (account_id) ON DELETE CASCADE,
            skill_type        worker_skill_type NOT NULL,
            proficiency       skill_proficiency NOT NULL DEFAULT 'competent',
            certification_ref text,
            certified_on      date NOT NULL,
            expires_on        date,

            PRIMARY KEY (account_id, skill_type),
            CONSTRAINT skill_expiry
                CHECK (expires_on IS NULL OR expires_on > certified_on)
        )
        """
    )

    # ==================================================================
    # Physical assets
    # ==================================================================
    op.execute(
        """
        CREATE TABLE site (
            site_id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            account_id         uuid NOT NULL
                REFERENCES account (account_id) ON DELETE RESTRICT,

            -- FK intentionally absent: tariff_plan belongs to the pricing
            -- module, which this migration does not create. The pricing
            -- migration adds
            --   ALTER TABLE site ADD CONSTRAINT site_tariff_plan_fk
            --     FOREIGN KEY (tariff_plan_id) REFERENCES tariff_plan (plan_id);
            tariff_plan_id     uuid NOT NULL,

            label              text NOT NULL,
            address_line       text NOT NULL,
            city               text NOT NULL,
            district           text NOT NULL,
            postal_code        text,
            latitude           numeric(9,6) NOT NULL,
            longitude          numeric(9,6) NOT NULL,
            timezone           text NOT NULL DEFAULT 'Asia/Dhaka',
            connection_type    site_connection_type NOT NULL DEFAULT 'residential',
            sanctioned_load_kw numeric(8,3) NOT NULL,
            energized_on       date,
            status             site_status NOT NULL DEFAULT 'active',
            created_at         timestamptz NOT NULL DEFAULT now(),

            CONSTRAINT site_lat_range CHECK (latitude  BETWEEN -90  AND 90),
            CONSTRAINT site_lon_range CHECK (longitude BETWEEN -180 AND 180),
            CONSTRAINT site_load_pos  CHECK (sanctioned_load_kw > 0)
        )
        """
    )

    op.execute(
        """
        CREATE TABLE device (
            device_id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            site_id               uuid NOT NULL
                REFERENCES site (site_id) ON DELETE CASCADE,
            parent_device_id      uuid
                REFERENCES device (device_id) ON DELETE SET NULL,
            device_type           device_type NOT NULL,
            serial_no             text NOT NULL UNIQUE,
            manufacturer          text,
            model                 text,
            firmware_version      text,
            reports_telemetry     boolean NOT NULL DEFAULT true,
            interval_minutes      smallint NOT NULL DEFAULT 30,
            device_key_hash       text NOT NULL,
            device_key_rotated_at timestamptz,
            installed_at          timestamptz NOT NULL DEFAULT now(),
            removed_at            timestamptz,
            last_seen_at          timestamptz,
            status                device_status NOT NULL DEFAULT 'active',

            CONSTRAINT device_interval_valid
                CHECK (interval_minutes IN (15, 30, 60)),
            CONSTRAINT device_lifecycle
                CHECK (removed_at IS NULL OR removed_at >= installed_at),
            CONSTRAINT device_not_own_parent
                CHECK (parent_device_id IS DISTINCT FROM device_id),

            -- Targets for the subtype composite FKs below. Both are implied by
            -- the primary key, and exist only so meter_spec / inverter_spec can
            -- reference (device_id, device_type) and (device_id, site_id).
            CONSTRAINT device_type_uk UNIQUE (device_id, device_type),
            CONSTRAINT device_site_uk UNIQUE (device_id, site_id)
        )
        """
    )

    op.execute(
        """
        CREATE TABLE meter_spec (
            device_id       uuid PRIMARY KEY,
            site_id         uuid NOT NULL,
            device_type     device_type NOT NULL DEFAULT 'meter',
            meter_flow      meter_flow NOT NULL,
            billing_role    meter_billing_role NOT NULL,
            ct_ratio        text,
            max_current_amp numeric(6,1),
            phase_count     smallint,
            seal_no         text,

            CONSTRAINT meter_spec_type_pinned CHECK (device_type = 'meter'),

            -- Disjointness: this row can only attach to a device that is a meter.
            CONSTRAINT meter_spec_device_fk
                FOREIGN KEY (device_id, device_type)
                REFERENCES device (device_id, device_type)
                ON UPDATE CASCADE ON DELETE CASCADE,

            -- site_id is a denormalized copy of device.site_id, held honest by
            -- this composite FK. It exists so rule 7 can be enforced by index
            -- and trigger without a join.
            CONSTRAINT meter_spec_site_fk
                FOREIGN KEY (device_id, site_id)
                REFERENCES device (device_id, site_id)
                ON UPDATE CASCADE ON DELETE CASCADE
        )
        """
    )

    op.execute(
        """
        CREATE TABLE inverter_spec (
            device_id        uuid PRIMARY KEY,
            device_type      device_type NOT NULL DEFAULT 'inverter',
            ac_capacity_kw   numeric(8,3) NOT NULL,
            dc_capacity_kw   numeric(8,3) NOT NULL,
            mppt_count       smallint,
            phase_count      smallint,
            rated_efficiency numeric(5,4),
            anti_islanding   boolean NOT NULL DEFAULT true,

            CONSTRAINT inverter_spec_type_pinned CHECK (device_type = 'inverter'),
            CONSTRAINT inverter_spec_device_fk
                FOREIGN KEY (device_id, device_type)
                REFERENCES device (device_id, device_type)
                ON UPDATE CASCADE ON DELETE CASCADE
        )
        """
    )

    op.execute(
        """
        CREATE TABLE solar_array (
            array_id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            site_id            uuid NOT NULL
                REFERENCES site (site_id) ON DELETE CASCADE,
            inverter_device_id uuid NOT NULL
                REFERENCES inverter_spec (device_id) ON DELETE CASCADE,
            label              text,
            panel_count        smallint NOT NULL,
            panel_watt_peak    smallint NOT NULL,

            -- Stored, NOT generated. After a partial panel replacement an array
            -- can mix wattages, so panel_count * panel_watt_peak stops being
            -- true. Deliberately NOT constrained to that product.
            dc_capacity_kw     numeric(8,3) NOT NULL,

            azimuth_deg        smallint NOT NULL,
            tilt_deg           smallint NOT NULL,
            shading_factor     numeric(4,3) NOT NULL DEFAULT 1.000,
            commissioned_on    date NOT NULL,
            decommissioned_on  date,
            status             solar_array_status NOT NULL DEFAULT 'active',

            CONSTRAINT array_azimuth CHECK (azimuth_deg BETWEEN 0 AND 359),
            CONSTRAINT array_tilt    CHECK (tilt_deg    BETWEEN 0 AND 90),
            CONSTRAINT array_shading CHECK (shading_factor BETWEEN 0 AND 1),
            CONSTRAINT array_dc_pos  CHECK (dc_capacity_kw > 0),
            CONSTRAINT array_decomm
                CHECK (decommissioned_on IS NULL
                       OR decommissioned_on >= commissioned_on)
        )
        """
    )

    op.execute(
        """
        CREATE TABLE net_metering_agreement (
            agreement_id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            site_id                uuid NOT NULL
                REFERENCES site (site_id) ON DELETE CASCADE,
            billing_device_id      uuid NOT NULL
                REFERENCES meter_spec (device_id) ON DELETE CASCADE,
            approval_ref           text NOT NULL UNIQUE,
            sanctioned_capacity_kw numeric(8,3) NOT NULL,
            export_cap_pct         numeric(5,2) NOT NULL DEFAULT 100.00,
            settlement_type        nma_settlement_type NOT NULL
                                       DEFAULT 'rollover_only',
            credit_rollover_months smallint,
            effective_from         date NOT NULL,
            effective_to           date,
            status                 nma_status NOT NULL DEFAULT 'pending',
            created_at             timestamptz NOT NULL DEFAULT now(),

            CONSTRAINT nma_export_cap
                CHECK (export_cap_pct BETWEEN 0 AND 100),
            CONSTRAINT nma_rollover
                CHECK (credit_rollover_months IS NULL
                       OR credit_rollover_months BETWEEN 1 AND 60),
            CONSTRAINT nma_dates
                CHECK (effective_to IS NULL OR effective_to > effective_from),

            -- A site's agreements may not overlap in time. A terminated
            -- agreement is excluded so a replacement can start the same day.
            CONSTRAINT nma_no_overlap EXCLUDE USING gist (
                site_id WITH =,
                (daterange(effective_from, effective_to, '[)')) WITH &&
            ) WHERE (status <> 'terminated')
        )
        """
    )

    # ------------------------------------------------------------------
    # Indexes
    # ------------------------------------------------------------------
    op.execute(
        "CREATE INDEX device_active_by_site ON device (site_id) "
        "WHERE removed_at IS NULL"
    )
    op.execute(
        "CREATE INDEX array_active_by_site ON solar_array (site_id) "
        "WHERE decommissioned_on IS NULL"
    )

    # Rule 7, structural half: at most one 'billing' meter row per site.
    op.execute(
        "CREATE UNIQUE INDEX one_billing_meter_per_site ON meter_spec (site_id) "
        "WHERE billing_role = 'billing'"
    )

    # One active agreement per billing device.
    op.execute(
        "CREATE UNIQUE INDEX nma_one_active_per_device "
        "ON net_metering_agreement (billing_device_id) WHERE status = 'active'"
    )

    # ------------------------------------------------------------------
    # Rule 7, behavioural half: exactly one ACTIVE billing meter per site.
    #
    # The partial unique index above cannot see device.removed_at, so it counts
    # retired meters too. This trigger closes that gap.
    #
    # DEFERRABLE INITIALLY DEFERRED matters: a meter swap retires the old device
    # and installs the new one in a single transaction, and the intermediate
    # states are legal. Only the state at COMMIT is checked.
    # ------------------------------------------------------------------
    op.execute(
        """
        CREATE FUNCTION assert_one_active_billing_meter() RETURNS trigger
        LANGUAGE plpgsql AS $fn$
        DECLARE
            affected_sites uuid[];
            target_site    uuid;
            active_billing integer;
        BEGIN
            IF TG_OP = 'DELETE' THEN
                affected_sites := ARRAY[OLD.site_id];
            ELSIF TG_OP = 'UPDATE' THEN
                affected_sites := ARRAY[NEW.site_id, OLD.site_id];
            ELSE
                affected_sites := ARRAY[NEW.site_id];
            END IF;

            FOREACH target_site IN ARRAY affected_sites LOOP
                -- Deleting a site cascades to its devices and meters. Once the
                -- site itself is gone there is nothing left to constrain.
                CONTINUE WHEN NOT EXISTS (
                    SELECT 1 FROM site WHERE site_id = target_site
                );

                SELECT count(*) INTO active_billing
                FROM meter_spec ms
                JOIN device d ON d.device_id = ms.device_id
                WHERE ms.site_id = target_site
                  AND ms.billing_role = 'billing'
                  AND d.removed_at IS NULL;

                IF active_billing <> 1 THEN
                    RAISE EXCEPTION
                        'site % has % active billing meters, exactly 1 required',
                        target_site, active_billing
                        USING ERRCODE = '23514',
                              HINT = 'rule 7: exactly one device per site has '
                                     'meter_spec.billing_role = ''billing''';
                END IF;
            END LOOP;

            RETURN NULL;
        END;
        $fn$
        """
    )

    op.execute(
        """
        CREATE CONSTRAINT TRIGGER meter_spec_one_active_billing
            AFTER INSERT OR UPDATE OR DELETE ON meter_spec
            DEFERRABLE INITIALLY DEFERRED
            FOR EACH ROW EXECUTE FUNCTION assert_one_active_billing_meter()
        """
    )

    # Retiring a device is the other way the count can change.
    op.execute(
        """
        CREATE CONSTRAINT TRIGGER device_one_active_billing
            AFTER UPDATE OF removed_at ON device
            DEFERRABLE INITIALLY DEFERRED
            FOR EACH ROW
            WHEN (NEW.device_type = 'meter')
            EXECUTE FUNCTION assert_one_active_billing_meter()
        """
    )


def downgrade() -> None:
    """Downgrade schema."""

    op.execute("DROP TRIGGER IF EXISTS device_one_active_billing ON device")
    op.execute(
        "DROP TRIGGER IF EXISTS meter_spec_one_active_billing ON meter_spec"
    )
    op.execute("DROP FUNCTION IF EXISTS assert_one_active_billing_meter()")

    # Indexes and table constraints drop with their tables.
    for table in (
        "net_metering_agreement",
        "solar_array",
        "inverter_spec",
        "meter_spec",
        "device",
        "site",
        "worker_skill",
        "worker_profile",
        "account",
    ):
        op.execute(f"DROP TABLE IF EXISTS {table}")

    for enum_type in (
        "nma_status",
        "nma_settlement_type",
        "solar_array_status",
        "meter_billing_role",
        "meter_flow",
        "device_status",
        "device_type",
        "site_status",
        "site_connection_type",
        "skill_proficiency",
        "worker_skill_type",
        "worker_availability",
        "account_status",
        "account_role",
    ):
        op.execute(f"DROP TYPE IF EXISTS {enum_type}")

    # citext and btree_gist are deliberately left installed. Extensions are
    # database-wide infrastructure rather than schema owned by this migration,
    # and dropping one that something else came to depend on is not reversible.
