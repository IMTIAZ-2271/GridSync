"""billing points: many billing meters per site

Revision ID: d5a7c2b91e40
Revises: b3f1c9d4a7e2
Create Date: 2026-08-23 11:00:00.000000

A household may now hold several billing meters ("a single consumer can have
multiple billing meters"). This migration is how that is made true without
giving up what rule 3 was protecting.

**Why not key billing on the meter device.** The obvious reading of "many
billing meters" is to re-key billing_period / bill / credit_ledger from
site_id to device_id. That breaks meter swaps: retire the meter mid-month and
the period is orphaned, the month splits across two devices, and the credit
balance forks. Rule 3 exists precisely to stop that.

**What this does instead.** A `billing_point` is one metering position at a
site -- one connection the utility bills. A site may have many. A point has
exactly one *active* billing meter at a time, so rule 7 survives verbatim with
"site" replaced by "point", and a meter swap changes which device serves the
point while the point, its periods, its bills and its ledger stay put.

Every existing site gets exactly one point and every existing meter_spec row
is attached to it, so no committed bill, period or ledger entry changes
meaning. `site_id` stays on bill and billing_period as a snapshot (rule 2);
composite foreign keys against billing_point (point_id, site_id) keep the two
columns from ever disagreeing.

bill and credit_ledger carry forbid_mutation() triggers (rule 1), which would
refuse the backfill UPDATE. They stand down for that one statement and are
re-enabled before the migration ends -- a schema backfill is the one context
in which append-only does not apply, because no money is being touched, only
a key column that did not exist when those rows were written.
"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "d5a7c2b91e40"
down_revision: Union[str, Sequence[str], None] = "b3f1c9d4a7e2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Tables that gain billing_point_id, in the order they must be filled.
# `immutable` marks the ones whose forbid_mutation() trigger has to stand
# down for the backfill.
KEYED = (
    ("billing_period", "CASCADE", False),
    ("bill", "RESTRICT", True),
    ("credit_ledger", "CASCADE", True),
)


# Rule 7's check function, before and after. Held as constants because the
# downgrade has to restore the site-keyed body verbatim, and a copy that
# drifts from migration c1e22f0141be would leave a downgraded database
# enforcing something subtly different from the one it claims to recreate.
RULE_7_BY_POINT = """
CREATE OR REPLACE FUNCTION assert_one_active_billing_meter() RETURNS trigger
LANGUAGE plpgsql AS $fn$
DECLARE
    affected_points uuid[];
    target_point    uuid;
    active_billing  integer;
BEGIN
    IF TG_TABLE_NAME = 'device' THEN
        -- device has no billing_point_id of its own; find whichever point
        -- this meter serves. A device that is not a meter yields no rows,
        -- and coalesce turns that into a no-op rather than a NULL array
        -- (FOREACH over NULL raises).
        SELECT coalesce(array_agg(ms.billing_point_id), '{}')
          INTO affected_points
        FROM meter_spec ms
        WHERE ms.device_id = NEW.device_id
          AND ms.billing_point_id IS NOT NULL;
    ELSIF TG_OP = 'DELETE' THEN
        affected_points := ARRAY[OLD.billing_point_id];
    ELSIF TG_OP = 'UPDATE' THEN
        affected_points := ARRAY[NEW.billing_point_id, OLD.billing_point_id];
    ELSE
        affected_points := ARRAY[NEW.billing_point_id];
    END IF;

    FOREACH target_point IN ARRAY coalesce(affected_points, '{}') LOOP
        -- A meter serving no point (generation_only, check_meter) constrains
        -- nothing.
        CONTINUE WHEN target_point IS NULL;

        -- Deleting a site cascades to its points, devices and meters. Once
        -- the point is gone there is nothing left to constrain.
        CONTINUE WHEN NOT EXISTS (
            SELECT 1 FROM billing_point WHERE point_id = target_point
        );

        SELECT count(*) INTO active_billing
        FROM meter_spec ms
        JOIN device d ON d.device_id = ms.device_id
        WHERE ms.billing_point_id = target_point
          AND ms.billing_role = 'billing'
          AND d.removed_at IS NULL;

        IF active_billing <> 1 THEN
            RAISE EXCEPTION
                'billing point % has % active billing meters, '
                'exactly 1 required',
                target_point, active_billing
                USING ERRCODE = '23514',
                      HINT = 'rule 7: exactly one device per billing point '
                             'has meter_spec.billing_role = ''billing''';
        END IF;
    END LOOP;

    RETURN NULL;
END;
$fn$
"""

RULE_7_BY_SITE = """
CREATE OR REPLACE FUNCTION assert_one_active_billing_meter() RETURNS trigger
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


def upgrade() -> None:
    """Upgrade schema."""

    # ==================================================================
    # 1. The point itself
    # ==================================================================
    op.execute(
        """
        CREATE TABLE billing_point (
            point_id   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            site_id    uuid NOT NULL
                REFERENCES site (site_id) ON DELETE CASCADE,

            -- What the household calls this connection: 'Main', 'Shop',
            -- 'Flat 2'. Shown in the meter switcher, so it has to be
            -- distinguishable within a site.
            label      text NOT NULL,

            -- The connection / customer number on the utility's paperwork,
            -- which is the "billing meter ID" a consumer is asked for.
            -- Distinct from device.serial_no: the serial identifies the box
            -- on the wall and changes when the box is swapped; this
            -- identifies the connection and does not.
            reference  text,

            created_at timestamptz NOT NULL DEFAULT now(),
            retired_at timestamptz,

            CONSTRAINT point_label_present CHECK (btrim(label) <> ''),
            CONSTRAINT point_retired_after_created
                CHECK (retired_at IS NULL OR retired_at >= created_at),

            -- Target for the composite FKs below, which is the whole reason
            -- site_id may be denormalized onto bill and billing_period
            -- without the two ever drifting apart.
            CONSTRAINT point_site_uk UNIQUE (point_id, site_id)
        )
        """
    )

    op.execute(
        "CREATE INDEX point_active_by_site ON billing_point (site_id) "
        "WHERE retired_at IS NULL"
    )
    op.execute(
        "CREATE UNIQUE INDEX point_label_per_site ON billing_point "
        "(site_id, lower(btrim(label))) WHERE retired_at IS NULL"
    )
    op.execute(
        "CREATE UNIQUE INDEX point_reference_unique ON billing_point "
        "(lower(btrim(reference))) WHERE reference IS NOT NULL"
    )

    # One point per existing site. Sites with no meter get one too -- a point
    # awaiting its meter is a legal state, and it means the /meter endpoint
    # never has to decide whether to invent one.
    op.execute(
        "INSERT INTO billing_point (site_id, label) "
        "SELECT site_id, 'Main' FROM site"
    )

    # ==================================================================
    # 2. Meters attach to a point, not just to a site
    # ==================================================================
    op.execute("ALTER TABLE meter_spec ADD COLUMN billing_point_id uuid")

    # meter_spec_one_active_billing is a DEFERRABLE constraint trigger, so a
    # bulk UPDATE queues one pending event per row and Postgres then refuses
    # every later ALTER TABLE on the table ("pending trigger events") until
    # COMMIT -- which never comes inside a migration. Disabling it for the
    # backfill is safe and not a loophole: this UPDATE moves no meter between
    # sites, it only names the single point each meter's site just received,
    # so the count of active billing meters per site is arithmetically
    # unchanged. The invariant is re-armed, re-keyed onto the point, below.
    op.execute(
        "ALTER TABLE meter_spec DISABLE TRIGGER meter_spec_one_active_billing"
    )
    op.execute(
        """
        UPDATE meter_spec ms
        SET billing_point_id = bp.point_id
        FROM billing_point bp
        WHERE bp.site_id = ms.site_id
        """
    )
    op.execute(
        "ALTER TABLE meter_spec ENABLE TRIGGER meter_spec_one_active_billing"
    )

    op.execute(
        """
        ALTER TABLE meter_spec
            ADD CONSTRAINT meter_spec_point_fk
            FOREIGN KEY (billing_point_id, site_id)
            REFERENCES billing_point (point_id, site_id)
            ON UPDATE CASCADE ON DELETE RESTRICT
        """
    )
    # Only a 'billing' meter must name a point. A generation_only or
    # check_meter device may sit on the site without serving a connection,
    # which is what makes the column nullable rather than NOT NULL.
    op.execute(
        """
        ALTER TABLE meter_spec
            ADD CONSTRAINT meter_spec_billing_needs_point
            CHECK (billing_role <> 'billing' OR billing_point_id IS NOT NULL)
        """
    )
    op.execute(
        "CREATE INDEX meter_spec_by_point ON meter_spec (billing_point_id) "
        "WHERE billing_point_id IS NOT NULL"
    )

    # ==================================================================
    # 3. Rule 7, re-keyed onto the point
    #
    # Same deferred-constraint-trigger shape as migration c1e22f0141be, same
    # reason (a meter swap's intermediate states are legal; only COMMIT is
    # checked). Two things change: the unit is the point, and the device-side
    # trigger can no longer read the key off NEW -- `device` has no
    # billing_point_id, so it looks its meter's point up.
    # ==================================================================
    op.execute(RULE_7_BY_POINT)

    op.execute(
        """
        COMMENT ON FUNCTION assert_one_active_billing_meter() IS
        'Rule 7 check: a BILLING POINT must have exactly one meter_spec row '
        'with billing_role = ''billing'' whose device is not removed. Raises '
        '23514. Re-keyed from site to point in migration d5a7c2b91e40, when '
        'a site became able to hold several billing meters. Skips points that '
        'no longer exist, so DELETE FROM site cascades cleanly, and skips '
        'meters that serve no point at all.'
        """
    )

    op.execute(
        """
        COMMENT ON TRIGGER meter_spec_one_active_billing ON meter_spec IS
        'Rule 7: exactly one device per BILLING POINT has billing_role = '
        '''billing'' among devices with removed_at IS NULL. Sole enforcement '
        'of rule 7 since migration 0b24bc6b5a1f; re-keyed from site to point '
        'in d5a7c2b91e40. DEFERRABLE INITIALLY DEFERRED so a meter swap can '
        'retire the old device and install its replacement in one '
        'transaction: only the state at COMMIT is checked. A duplicate '
        'billing meter is therefore reported at COMMIT, not at INSERT.'
        """
    )

    # ==================================================================
    # 4. Billing tables re-keyed
    # ==================================================================
    for table, on_delete, immutable in KEYED:
        op.execute(f"ALTER TABLE {table} ADD COLUMN billing_point_id uuid")

        if immutable:
            # Rule 1's append-only trigger would refuse this UPDATE. See the
            # module docstring: no money moves here, only a key column that
            # did not exist when these rows were written.
            op.execute(f"ALTER TABLE {table} DISABLE TRIGGER {table}_immutable")

        op.execute(
            f"""
            UPDATE {table} t
            SET billing_point_id = bp.point_id
            FROM billing_point bp
            WHERE bp.site_id = t.site_id
            """
        )

        if immutable:
            op.execute(f"ALTER TABLE {table} ENABLE TRIGGER {table}_immutable")

        op.execute(
            f"ALTER TABLE {table} ALTER COLUMN billing_point_id SET NOT NULL"
        )
        op.execute(
            f"""
            ALTER TABLE {table}
                ADD CONSTRAINT {table}_point_fk
                FOREIGN KEY (billing_point_id, site_id)
                REFERENCES billing_point (point_id, site_id)
                ON UPDATE CASCADE ON DELETE {on_delete}
            """
        )

    # Rule 3, re-keyed: a POINT's periods may not overlap. Two points at one
    # site legitimately hold the same month.
    op.execute("ALTER TABLE billing_period DROP CONSTRAINT period_no_overlap")
    op.execute(
        """
        ALTER TABLE billing_period
            ADD CONSTRAINT period_no_overlap EXCLUDE USING gist (
                billing_point_id WITH =,
                (daterange(period_start, period_end, '[]')) WITH &&
            )
        """
    )

    # Rule 4, re-keyed: one earned and one applied entry per point per period.
    op.execute("DROP INDEX ledger_one_entry_per_period")
    op.execute(
        "CREATE UNIQUE INDEX ledger_one_entry_per_period "
        "ON credit_ledger (billing_point_id, period_id, entry_type) "
        "WHERE entry_type IN ('earned', 'applied')"
    )

    # Access paths follow the new key. The site-keyed ones stay: the supplier
    # and government views still ask site-wide questions.
    op.execute("DROP INDEX period_workable")
    op.execute(
        "CREATE INDEX period_workable ON billing_period "
        "(billing_point_id, period_start DESC) "
        "WHERE status IN ('open', 'frozen')"
    )
    op.execute(
        "CREATE INDEX bill_by_point ON bill (billing_point_id, issued_at DESC)"
    )
    op.execute(
        "CREATE INDEX ledger_by_point ON credit_ledger "
        "(billing_point_id, entry_id DESC)"
    )

    # ==================================================================
    # 5. Net metering is per connection, not per premises
    #
    # net_metering_agreement already names a billing_device_id; with several
    # billing meters on a site, two connections can each carry their own
    # solar installation and their own agreement. The site-keyed exclusion
    # would have refused the second one.
    # ==================================================================
    op.execute(
        "ALTER TABLE net_metering_agreement ADD COLUMN billing_point_id uuid"
    )
    op.execute(
        """
        UPDATE net_metering_agreement nma
        SET billing_point_id = ms.billing_point_id
        FROM meter_spec ms
        WHERE ms.device_id = nma.billing_device_id
        """
    )
    op.execute(
        "ALTER TABLE net_metering_agreement "
        "ALTER COLUMN billing_point_id SET NOT NULL"
    )
    op.execute(
        """
        ALTER TABLE net_metering_agreement
            ADD CONSTRAINT nma_point_fk
            FOREIGN KEY (billing_point_id, site_id)
            REFERENCES billing_point (point_id, site_id)
            ON UPDATE CASCADE ON DELETE CASCADE
        """
    )
    op.execute(
        "ALTER TABLE net_metering_agreement DROP CONSTRAINT nma_no_overlap"
    )
    op.execute(
        """
        ALTER TABLE net_metering_agreement
            ADD CONSTRAINT nma_no_overlap EXCLUDE USING gist (
                billing_point_id WITH =,
                (daterange(effective_from, effective_to, '[)')) WITH &&
            ) WHERE (status <> 'terminated')
        """
    )


def downgrade() -> None:
    """Downgrade schema."""

    op.execute(
        "ALTER TABLE net_metering_agreement DROP CONSTRAINT nma_no_overlap"
    )
    op.execute(
        """
        ALTER TABLE net_metering_agreement
            ADD CONSTRAINT nma_no_overlap EXCLUDE USING gist (
                site_id WITH =,
                (daterange(effective_from, effective_to, '[)')) WITH &&
            ) WHERE (status <> 'terminated')
        """
    )
    op.execute(
        "ALTER TABLE net_metering_agreement DROP CONSTRAINT nma_point_fk"
    )
    op.execute(
        "ALTER TABLE net_metering_agreement DROP COLUMN billing_point_id"
    )

    op.execute("DROP INDEX ledger_by_point")
    op.execute("DROP INDEX bill_by_point")
    op.execute("DROP INDEX period_workable")
    op.execute(
        "CREATE INDEX period_workable ON billing_period "
        "(site_id, period_start DESC) WHERE status IN ('open', 'frozen')"
    )

    op.execute("DROP INDEX ledger_one_entry_per_period")
    op.execute(
        "CREATE UNIQUE INDEX ledger_one_entry_per_period "
        "ON credit_ledger (site_id, period_id, entry_type) "
        "WHERE entry_type IN ('earned', 'applied')"
    )

    op.execute("ALTER TABLE billing_period DROP CONSTRAINT period_no_overlap")
    op.execute(
        """
        ALTER TABLE billing_period
            ADD CONSTRAINT period_no_overlap EXCLUDE USING gist (
                site_id WITH =,
                (daterange(period_start, period_end, '[]')) WITH &&
            )
        """
    )

    for table, _on_delete, _immutable in reversed(KEYED):
        op.execute(f"ALTER TABLE {table} DROP CONSTRAINT {table}_point_fk")
        op.execute(f"ALTER TABLE {table} DROP COLUMN billing_point_id")

    op.execute(RULE_7_BY_SITE)

    op.execute("DROP INDEX meter_spec_by_point")
    op.execute(
        "ALTER TABLE meter_spec DROP CONSTRAINT meter_spec_billing_needs_point"
    )
    op.execute("ALTER TABLE meter_spec DROP CONSTRAINT meter_spec_point_fk")
    op.execute("ALTER TABLE meter_spec DROP COLUMN billing_point_id")

    op.execute("DROP TABLE billing_point")
