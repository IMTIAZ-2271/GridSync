"""pricing: tariff plan and rates, site tariff FK

Revision ID: f8adcdc1e0ca
Revises: 0f6109903981
Create Date: 2026-08-21 16:23:47.219034

tariff_plan and tariff_rate, then the foreign key migration c1e22f0141be left
a note promising:

    ALTER TABLE site ADD CONSTRAINT site_tariff_plan_fk
      FOREIGN KEY (tariff_plan_id) REFERENCES tariff_plan (plan_id);

site.tariff_plan_id has been NOT NULL since migration 1 but referentially
unchecked, so a site could name a plan that does not exist. It stays NOT NULL
-- the column states something true, every site really is billed under a plan
-- and this migration makes the database enforce it, rather than moving the
check into run_billing(). Rule 4's principle, applied outside idempotency:
invariants belong in constraints, not in code.

holiday_calendar is the remaining Pricing table and is not created here.
tariff_rate.day_type = 'holiday' does not depend on it; the calendar only
resolves which dates are holidays, at billing time.

Two enums the ERD names but never enumerates -- their values are chosen here:

* tou_period ('peak', 'shoulder', 'off_peak', 'flat'). 'flat' lets a non-TOU
  plan be modelled as a single 00:00-24:00 window instead of a special case in
  the billing engine.
* rate_day_type ('weekday', 'weekend', 'holiday'), matching holiday_calendar's
  reason for existing.

customer_class reuses the existing site_connection_type enum rather than
declaring a parallel copy: site.connection_type and tariff_plan.customer_class
have to be comparable for a plan to be matched to a site, and two enums with
identical labels could not be compared without a cast.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f8adcdc1e0ca'
down_revision: Union[str, Sequence[str], None] = '0f6109903981'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""

    # ------------------------------------------------------------------
    # Types
    # ------------------------------------------------------------------
    # PostgreSQL has no built-in range over `time`, and rate_no_overlapping
    # _windows needs one.
    op.execute("CREATE TYPE timerange AS RANGE (subtype = time)")

    op.execute(
        "CREATE TYPE tou_period AS ENUM "
        "('peak', 'shoulder', 'off_peak', 'flat')"
    )
    op.execute(
        "CREATE TYPE rate_day_type AS ENUM ('weekday', 'weekend', 'holiday')"
    )

    # ==================================================================
    # tariff_plan
    # ==================================================================
    op.execute(
        """
        CREATE TABLE tariff_plan (
            plan_id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),

            -- Stable across versions: a rate change is a NEW ROW sharing this
            -- code with a later effective_from, never an UPDATE. Rule 1.
            code                 text NOT NULL,
            name                 text NOT NULL,
            customer_class       site_connection_type NOT NULL,
            currency             char(3) NOT NULL DEFAULT 'BDT',
            fixed_monthly_charge numeric(14,4) NOT NULL DEFAULT 0,
            demand_charge_per_kw numeric(14,4),
            tax_rate             numeric(6,4) NOT NULL DEFAULT 0,
            effective_from       date NOT NULL,
            effective_to         date,
            created_at           timestamptz NOT NULL DEFAULT now(),

            CONSTRAINT plan_tax CHECK (tax_rate BETWEEN 0 AND 1),
            CONSTRAINT plan_dates
                CHECK (effective_to IS NULL OR effective_to > effective_from),
            CONSTRAINT plan_charges_non_negative CHECK (
                    fixed_monthly_charge >= 0
                AND (demand_charge_per_kw IS NULL OR demand_charge_per_kw >= 0)
            ),
            CONSTRAINT plan_currency_shape
                CHECK (currency = upper(currency) AND length(currency) = 3),

            -- Versions of one plan may not overlap in time. An open-ended
            -- version (effective_to IS NULL) blocks any later one until it is
            -- closed off, which is the intended workflow: close the current
            -- version, then open its successor.
            CONSTRAINT plan_no_overlapping_versions EXCLUDE USING gist (
                code WITH =,
                (daterange(effective_from, effective_to, '[)')) WITH &&
            )
        )
        """
    )

    # ==================================================================
    # tariff_rate
    # ==================================================================
    op.execute(
        """
        CREATE TABLE tariff_rate (
            rate_id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            plan_id            uuid NOT NULL
                REFERENCES tariff_plan (plan_id) ON DELETE CASCADE,
            period_name        tou_period    NOT NULL,
            day_type           rate_day_type NOT NULL,
            start_time         time NOT NULL,
            end_time           time NOT NULL,   -- exclusive
            import_rate        numeric(10,6) NOT NULL,
            export_credit_rate numeric(10,6) NOT NULL,

            -- An overnight window (22:00 -> 06:00) is not a valid range.
            -- Model it as two rows, 22:00-24:00 and 00:00-06:00 -- which is
            -- precisely why start_time belongs in the natural key.
            CONSTRAINT rate_window CHECK (end_time > start_time),
            CONSTRAINT rate_non_negative
                CHECK (import_rate >= 0 AND export_credit_rate >= 0),

            -- Conceptual partial key of the TARIFF_RATE weak entity. Subsumed
            -- by rate_no_overlapping_windows below, kept because it is the
            -- declared identity of the entity.
            CONSTRAINT rate_natural_key
                UNIQUE (plan_id, period_name, day_type, start_time),

            -- TOU windows must not overlap within a plan + day_type, or an
            -- interval could be billed at two rates.
            CONSTRAINT rate_no_overlapping_windows EXCLUDE USING gist (
                plan_id  WITH =,
                day_type WITH =,
                (timerange(start_time, end_time, '[)')) WITH &&
            )
        )
        """
    )

    op.execute(
        "CREATE INDEX rate_by_plan ON tariff_rate (plan_id, day_type, start_time)"
    )

    # Finding the current version of a plan code is the hot lookup.
    op.execute(
        "CREATE INDEX plan_by_code ON tariff_plan (code, effective_from DESC)"
    )

    # ==================================================================
    # The FK migration 1 promised
    # ==================================================================
    # RESTRICT, not CASCADE: rule 1. A plan that any site references must not
    # be deletable, and a plan referenced by a historical bill must survive
    # regardless -- bills snapshot tariff_plan_id precisely so they stay
    # correct when rates change (rule 2).
    op.execute(
        """
        ALTER TABLE site
            ADD CONSTRAINT site_tariff_plan_fk
            FOREIGN KEY (tariff_plan_id) REFERENCES tariff_plan (plan_id)
            ON DELETE RESTRICT
        """
    )

    # The referencing side of an FK is not indexed automatically, and
    # "which sites are on this plan" is asked on every rate change.
    op.execute("CREATE INDEX site_by_tariff_plan ON site (tariff_plan_id)")

    op.execute(
        """
        COMMENT ON TABLE tariff_plan IS
        'Rule 1: rates are never edited. A rate change is a new row sharing '
        'the same code with a later effective_from; plan_no_overlapping_'
        'versions keeps the timeline honest. Bills snapshot tariff_plan_id '
        'so a new version cannot rewrite an issued bill.'
        """
    )

    op.execute(
        """
        COMMENT ON CONSTRAINT site_tariff_plan_fk ON site IS
        'Promised by migration c1e22f0141be, which created the column NOT '
        'NULL but could not reference tariff_plan before it existed.'
        """
    )


def downgrade() -> None:
    """Downgrade schema."""

    op.execute("DROP INDEX IF EXISTS site_by_tariff_plan")
    op.execute("ALTER TABLE site DROP CONSTRAINT IF EXISTS site_tariff_plan_fk")

    # Indexes and table constraints drop with their tables.
    op.execute("DROP TABLE IF EXISTS tariff_rate")
    op.execute("DROP TABLE IF EXISTS tariff_plan")

    for type_name in ("rate_day_type", "tou_period", "timerange"):
        op.execute(f"DROP TYPE IF EXISTS {type_name}")
