"""telemetry: partitioned readings, late readings, ingest batches

Revision ID: 0f6109903981
Revises: 0b24bc6b5a1f
Create Date: 2026-08-21 16:14:02.881547

The telemetry module from docs/erd-logical.md: ingest_batch, device_reading
(monthly RANGE partitions on interval_start), late_reading.

Written as raw op.execute throughout. Alembic has no native support for
PARTITION BY / PARTITION OF, and the reading CHECK constraints are expression
constraints with no clean Core equivalent.

Two notes on device_reading:

* The primary key is (device_id, interval_start). That is rule 4 -- retries are
  idempotent because the database refuses the duplicate -- and it is also what
  Postgres requires: a unique constraint on a partitioned table must contain
  every partition key column.

* Partition bounds are always written with an explicit UTC offset. An
  unqualified timestamptz literal is resolved against the session TimeZone at
  DDL time, so the same migration run by a developer in another zone would
  silently produce different month boundaries. create_reading_partition()
  below is the only place that computes a bound, so there is one place to get
  it right; services/jobs calls the same function to pre-create months.

reading_role_guard (rule 6) is created here. The other trigger
docs/erd-logical.md attaches to device_reading, reading_period_open_guard
(rule 8, divert to late_reading), needs billing_period and arrives with the
billing module -- until then late_reading is reachable only by the ingest
service writing to it directly.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0f6109903981'
down_revision: Union[str, Sequence[str], None] = '0b24bc6b5a1f'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Months to pre-create at migration time. services/jobs keeps the window
# rolling forward; this only has to cover the near term plus the simulator's
# backfill range.
INITIAL_PARTITIONS = [
    (2026, month) for month in range(6, 13)
] + [(2027, 1), (2027, 2)]


def upgrade() -> None:
    """Upgrade schema."""

    # ------------------------------------------------------------------
    # Enum types
    # ------------------------------------------------------------------
    op.execute(
        "CREATE TYPE reading_source AS ENUM "
        "('device', 'estimated', 'backfill', 'manual')"
    )
    op.execute(
        "CREATE TYPE reading_quality AS ENUM ('good', 'estimated', 'suspect')"
    )
    op.execute(
        "CREATE TYPE late_reason AS ENUM "
        "('period_billed', 'period_frozen', 'out_of_range', 'clock_skew', "
        "'unaligned')"
    )

    # ==================================================================
    # ingest_batch -- created first, device_reading references it
    # ==================================================================
    op.execute(
        """
        CREATE TABLE ingest_batch (
            batch_id        uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            device_id       uuid NOT NULL
                REFERENCES device (device_id) ON DELETE CASCADE,

            -- Rule 4: the Idempotency-Key header from POST /v1/ingest/readings.
            -- Unique across all devices, per docs/erd-logical.md, not scoped to
            -- the device. Keys are client-generated uuids, so a cross-device
            -- collision means a broken client rather than a legitimate retry.
            idempotency_key text NOT NULL UNIQUE,

            reading_count   smallint NOT NULL DEFAULT 0,
            accepted_count  smallint NOT NULL DEFAULT 0,
            duplicate_count smallint NOT NULL DEFAULT 0,
            rejected_count  smallint NOT NULL DEFAULT 0,
            client_ip       inet,
            received_at     timestamptz NOT NULL DEFAULT now(),

            CONSTRAINT batch_counts_non_negative CHECK (
                    reading_count   >= 0
                AND accepted_count  >= 0
                AND duplicate_count >= 0
                AND rejected_count  >= 0
            ),

            -- '<=' rather than '=' on purpose: the batch row is inserted before
            -- its readings are processed, so the outcome counts fill in
            -- afterwards and must be allowed to lag the total.
            CONSTRAINT batch_counts_within_total CHECK (
                accepted_count + duplicate_count + rejected_count
                    <= reading_count
            )
        )
        """
    )

    # ==================================================================
    # device_reading -- partitioned
    # ==================================================================
    op.execute(
        """
        CREATE TABLE device_reading (
            device_id        uuid        NOT NULL,
            interval_start   timestamptz NOT NULL,
            interval_minutes smallint    NOT NULL,
            import_kwh       numeric(12,4),
            export_kwh       numeric(12,4),
            generation_kwh   numeric(12,4),
            voltage_avg      numeric(6,2),
            frequency_avg    numeric(5,3),
            dc_voltage_avg   numeric(7,2),
            source           reading_source  NOT NULL DEFAULT 'device',
            quality          reading_quality NOT NULL DEFAULT 'good',
            ingest_batch_id  uuid        NOT NULL
                REFERENCES ingest_batch (batch_id),
            ingested_at      timestamptz NOT NULL DEFAULT now(),

            CONSTRAINT reading_non_negative CHECK (
                    (import_kwh     IS NULL OR import_kwh     >= 0)
                AND (export_kwh     IS NULL OR export_kwh     >= 0)
                AND (generation_kwh IS NULL OR generation_kwh >= 0)
            ),

            CONSTRAINT reading_interval_valid
                CHECK (interval_minutes IN (15, 30, 60)),

            -- A reading must carry at least one measurement family. WHICH
            -- family a given device may send depends on meter_spec.billing_role
            -- and lives in another table, so it cannot be a CHECK -- that is
            -- rule 6's trigger, deferred to a later migration.
            CONSTRAINT reading_shape CHECK (
                     (import_kwh IS NOT NULL AND export_kwh IS NOT NULL)
                  OR generation_kwh IS NOT NULL
            ),

            -- Interval alignment: no reading may straddle a TOU boundary.
            -- EXTRACT(epoch FROM timestamptz) is STABLE and so illegal in a
            -- CHECK. Pinning the zone with AT TIME ZONE 'UTC' yields a plain
            -- timestamp, and date_part over timestamp IS immutable.
            CONSTRAINT reading_aligned CHECK (
                EXTRACT(epoch FROM (interval_start AT TIME ZONE 'UTC'))::bigint
                    % (interval_minutes * 60) = 0
            ),

            -- Rule 4, and Postgres requires the partition key here.
            PRIMARY KEY (device_id, interval_start)
        ) PARTITION BY RANGE (interval_start)
        """
    )

    # device_id cannot be a table-level REFERENCES above without dragging the
    # partition key into it; declare it separately.
    op.execute(
        """
        ALTER TABLE device_reading
            ADD CONSTRAINT reading_device_fk
            FOREIGN KEY (device_id) REFERENCES device (device_id)
            ON DELETE CASCADE
        """
    )

    # ------------------------------------------------------------------
    # Partition management
    # ------------------------------------------------------------------
    # One function owns bound arithmetic so no caller hand-writes a literal.
    # Takes any date, creates the partition for the month containing it, and is
    # idempotent so the jobs process can call it unconditionally.
    op.execute(
        """
        CREATE FUNCTION create_reading_partition(month_of date)
            RETURNS text
        LANGUAGE plpgsql AS $fn$
        DECLARE
            -- Partition months are Asia/Dhaka months, matching the billing
            -- calendar and site.timezone's default. Named explicitly rather
            -- than taken from the session: a caller that had done
            -- SET TimeZone='UTC' would otherwise silently produce partitions
            -- six hours out of step with their neighbours. Changing this
            -- constant later means repartitioning, not just editing it.
            partition_zone constant text := 'Asia/Dhaka';
            month_first    timestamp;
            lower_bound    timestamptz;
            upper_bound    timestamptz;
            part_name      text;
        BEGIN
            -- Plain timestamp arithmetic first, no zone involved ...
            month_first := date_trunc('month', month_of::timestamp);
            -- ... then interpret it in the partition zone exactly once.
            lower_bound := month_first AT TIME ZONE partition_zone;
            upper_bound := (month_first + interval '1 month')
                               AT TIME ZONE partition_zone;
            -- Named from the plain timestamp so the name cannot drift from the
            -- bound when the session zone differs.
            part_name   := 'device_reading_'
                           || to_char(month_first, 'YYYY_MM');

            IF to_regclass(part_name) IS NOT NULL THEN
                RETURN part_name;
            END IF;

            -- Bounds are rendered with an explicit offset by the %L cast of a
            -- timestamptz, so the resulting DDL is zone-independent.
            EXECUTE format(
                'CREATE TABLE %I PARTITION OF device_reading '
                'FOR VALUES FROM (%L) TO (%L)',
                part_name, lower_bound, upper_bound
            );
            RETURN part_name;
        END;
        $fn$
        """
    )

    for year, month in INITIAL_PARTITIONS:
        op.execute(
            f"SELECT create_reading_partition(DATE '{year}-{month:02d}-01')"
        )

    # Catch-all so a clock-skewed device cannot fail an insert outright.
    # services/jobs alerts when this is non-empty: attaching a new partition
    # while the default holds matching rows requires a full scan of it.
    op.execute(
        "CREATE TABLE device_reading_default PARTITION OF device_reading DEFAULT"
    )

    # ------------------------------------------------------------------
    # Indexes -- partitioned, so each partition gets its own child index
    # ------------------------------------------------------------------
    # Readings are append-only and time-ordered, so BRIN is far smaller than
    # btree for the same range scans.
    op.execute(
        "CREATE INDEX reading_brin ON device_reading "
        "USING brin (interval_start)"
    )
    op.execute(
        "CREATE INDEX reading_device_ts ON device_reading "
        "(device_id, interval_start DESC)"
    )

    # ==================================================================
    # late_reading -- not partitioned; a backlog, not a time series
    # ==================================================================
    op.execute(
        """
        CREATE TABLE late_reading (
            late_id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            device_id       uuid NOT NULL
                REFERENCES device (device_id) ON DELETE CASCADE,
            interval_start  timestamptz NOT NULL,
            import_kwh      numeric(12,4),
            export_kwh      numeric(12,4),
            generation_kwh  numeric(12,4),
            reason          late_reason NOT NULL,
            ingest_batch_id uuid NOT NULL
                REFERENCES ingest_batch (batch_id),
            received_at     timestamptz NOT NULL DEFAULT now(),
            resolved        boolean NOT NULL DEFAULT false,

            CONSTRAINT late_non_negative CHECK (
                    (import_kwh     IS NULL OR import_kwh     >= 0)
                AND (export_kwh     IS NULL OR export_kwh     >= 0)
                AND (generation_kwh IS NULL OR generation_kwh >= 0)
            )
        )
        """
    )

    # Deliberately no unique constraint on (device_id, interval_start): the
    # same rejected interval may legitimately arrive many times, and each
    # arrival is evidence. Rule 1's append-only spirit applies.
    op.execute(
        "CREATE INDEX late_unresolved ON late_reading "
        "(device_id, interval_start) WHERE NOT resolved"
    )

    op.execute(
        """
        COMMENT ON TABLE late_reading IS
        'Rule 8: readings that arrived for a frozen or billed period. Never '
        'merged into a closed bill. Populated by the ingest service, and by '
        'reading_period_open_guard once billing_period exists.'
        """
    )

    # ------------------------------------------------------------------
    # Rule 6: a device may only report what it can actually measure.
    #
    # billing_role governs whether a reading COUNTS toward a bill, not whether
    # it may exist -- a check_meter measures the grid boundary exactly like the
    # billing meter, it is simply never the one billed on. So the import/export
    # side is decided by meter_flow, and only the generation side is decided by
    # role.
    #
    # BEFORE INSERT, so it runs ahead of the table's CHECK constraints and can
    # give a device-aware message instead of a bare reading_shape violation.
    # ------------------------------------------------------------------
    op.execute(
        """
        CREATE FUNCTION assert_reading_matches_device_role() RETURNS trigger
        LANGUAGE plpgsql AS $fn$
        DECLARE
            dev_type  device_type;
            flow      meter_flow;
            role      meter_billing_role;
        BEGIN
            SELECT d.device_type, ms.meter_flow, ms.billing_role
              INTO dev_type, flow, role
              FROM device d
              LEFT JOIN meter_spec ms ON ms.device_id = d.device_id
             WHERE d.device_id = NEW.device_id;

            IF dev_type IS NULL THEN
                RAISE EXCEPTION 'device % does not exist', NEW.device_id
                    USING ERRCODE = '23503';
            END IF;

            -- Generation side: inverters, and meters installed on the
            -- inverter output. Rule 6 -- an inverter cannot know the
            -- import/export split, only the grid-boundary meter can.
            IF dev_type = 'inverter' OR role = 'generation_only' THEN
                IF NEW.generation_kwh IS NULL THEN
                    RAISE EXCEPTION
                        'device % reports generation; generation_kwh required',
                        NEW.device_id
                        USING ERRCODE = '23514',
                              HINT = 'rule 6: generation-side device';
                END IF;
                IF NEW.import_kwh IS NOT NULL OR NEW.export_kwh IS NOT NULL THEN
                    RAISE EXCEPTION
                        'device % is generation-side; import_kwh and '
                        'export_kwh must be NULL', NEW.device_id
                        USING ERRCODE = '23514',
                              HINT = 'rule 6: only a grid-boundary meter '
                                     'knows the import/export split';
                END IF;
                RETURN NEW;
            END IF;

            -- Everything below is a grid-boundary meter: billing_role
            -- 'billing' or 'check_meter', which report identically.
            IF flow IS NULL THEN
                RAISE EXCEPTION
                    'device % is a meter with no meter_spec row',
                    NEW.device_id
                    USING ERRCODE = '23514';
            END IF;

            IF NEW.generation_kwh IS NOT NULL THEN
                RAISE EXCEPTION
                    'device % is a grid meter; generation_kwh must be NULL',
                    NEW.device_id
                    USING ERRCODE = '23514',
                          HINT = 'rule 6: generation is reported by the '
                                 'inverter or a generation_only meter';
            END IF;

            IF NEW.import_kwh IS NULL THEN
                RAISE EXCEPTION
                    'device % is a grid meter; import_kwh required',
                    NEW.device_id
                    USING ERRCODE = '23514';
            END IF;

            IF flow = 'unidirectional' THEN
                IF COALESCE(NEW.export_kwh, 0) <> 0 THEN
                    RAISE EXCEPTION
                        'device % is unidirectional and cannot export '
                        '(export_kwh = %)', NEW.device_id, NEW.export_kwh
                        USING ERRCODE = '23514',
                              HINT = 'rule 6: only a bidirectional meter '
                                     'can measure export';
                END IF;
                -- Normalize NULL to 0. For a unidirectional meter zero export
                -- is a measured fact, not a missing value, and reading_shape
                -- requires both halves of the import/export pair to be
                -- present. Without this, 'export_kwh IS NULL' would be
                -- accepted here and then rejected by reading_shape with a
                -- far less informative error.
                NEW.export_kwh := 0;
            ELSIF NEW.export_kwh IS NULL THEN
                RAISE EXCEPTION
                    'device % is bidirectional; export_kwh required',
                    NEW.device_id
                    USING ERRCODE = '23514';
            END IF;

            RETURN NEW;
        END;
        $fn$
        """
    )

    op.execute(
        """
        CREATE TRIGGER reading_role_guard
            BEFORE INSERT ON device_reading
            FOR EACH ROW EXECUTE FUNCTION assert_reading_matches_device_role()
        """
    )

    op.execute(
        """
        COMMENT ON FUNCTION assert_reading_matches_device_role() IS
        'Rule 6: a device may only report what it can measure. Generation '
        'side (inverter, or a meter with billing_role = ''generation_only'') '
        'reports generation_kwh only. Grid-boundary meters (billing and '
        'check_meter alike -- billing_role decides what counts toward a bill, '
        'not what may be reported) report import_kwh, plus export_kwh when '
        'meter_flow is bidirectional. A unidirectional meter''s export is '
        'normalized to 0.'
        """
    )

    op.execute(
        """
        COMMENT ON FUNCTION create_reading_partition(date) IS
        'Creates the device_reading partition for the Asia/Dhaka month '
        'containing the given date. Idempotent, and independent of the '
        'session TimeZone. Sole owner of partition bound arithmetic -- call '
        'this rather than writing FOR VALUES literals by hand, which resolve '
        'against the session TimeZone and drift between environments.'
        """
    )


def downgrade() -> None:
    """Downgrade schema."""

    # Partitions and their child indexes drop with the parent; the trigger
    # goes with device_reading, but the function it calls does not.
    op.execute("DROP TABLE IF EXISTS late_reading")
    op.execute("DROP TABLE IF EXISTS device_reading")
    op.execute("DROP FUNCTION IF EXISTS assert_reading_matches_device_role()")
    op.execute("DROP FUNCTION IF EXISTS create_reading_partition(date)")
    op.execute("DROP TABLE IF EXISTS ingest_batch")

    for enum_type in ("late_reason", "reading_quality", "reading_source"):
        op.execute(f"DROP TYPE IF EXISTS {enum_type}")
