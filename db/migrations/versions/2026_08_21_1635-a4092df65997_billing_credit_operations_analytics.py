"""billing, credit, operations, analytics

Revision ID: a4092df65997
Revises: f8adcdc1e0ca
Create Date: 2026-08-21 16:35:12.664201

Every remaining table: holiday_calendar, billing_run, billing_period, bill,
bill_line_item, credit_ledger, payment, issue, issue_comment, work_order,
work_order_assignment, audit_log, site_daily_summary, site_monthly_summary.

tariff_plan, tariff_rate and site_tariff_plan_fk are already in f8adcdc1e0ca.

Includes forbid_mutation() on bill / bill_line_item / credit_ledger /
tariff_rate -- rule 1, money is immutable. Bills carry an exemption for
status and voided_by_bill_id, which must still move.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a4092df65997'
down_revision: Union[str, Sequence[str], None] = 'f8adcdc1e0ca'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


ENUMS = {
    "period_status": ("open", "frozen", "billed", "closed"),
    "bill_status": ("issued", "partially_paid", "paid", "overdue", "void"),
    "bill_line_type": ("energy_import", "export_credit", "fixed", "demand",
                       "tax", "adjustment"),
    "ledger_entry_type": ("earned", "applied", "expired", "adjustment",
                          "cashout"),
    "payment_method": ("bkash", "nagad", "rocket", "card", "bank_transfer"),
    "payment_status": ("pending", "succeeded", "failed", "refunded"),
    "billing_run_status": ("running", "succeeded", "partial", "failed"),
    "issue_category": ("billing_dispute", "meter_fault", "inverter_fault",
                       "outage", "export_not_credited", "data_gap", "other"),
    "issue_severity": ("low", "medium", "high", "critical"),
    "issue_status": ("open", "acknowledged", "in_progress", "resolved",
                     "closed", "duplicate"),
    "work_order_type": ("meter_install", "meter_swap", "meter_removal",
                        "inverter_service", "inspection", "seal_check",
                        "disconnection", "reconnection"),
    "work_order_status": ("draft", "scheduled", "dispatched", "in_progress",
                          "completed", "failed", "cancelled"),
    "assignment_role": ("lead", "assistant", "inspector"),
    "assignment_status": ("offered", "accepted", "declined", "released",
                          "completed"),
}


def upgrade() -> None:
    """Upgrade schema."""

    for name, values in ENUMS.items():
        labels = ", ".join(f"'{v}'" for v in values)
        op.execute(f"CREATE TYPE {name} AS ENUM ({labels})")

    # ==================================================================
    # Pricing: the last table of the module
    # ==================================================================
    op.execute(
        """
        CREATE TABLE holiday_calendar (
            holiday_date date PRIMARY KEY,
            name         text NOT NULL,
            region       text
        )
        """
    )

    # ==================================================================
    # Billing & credit
    # ==================================================================
    op.execute(
        """
        CREATE TABLE billing_run (
            run_id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            triggered_by     uuid REFERENCES account (account_id)
                                 ON DELETE SET NULL,
            period_start     date NOT NULL,
            period_end       date NOT NULL,
            started_at       timestamptz NOT NULL DEFAULT now(),
            finished_at      timestamptz,
            sites_processed  integer NOT NULL DEFAULT 0,
            bills_issued     integer NOT NULL DEFAULT 0,
            failures         integer NOT NULL DEFAULT 0,
            status           billing_run_status NOT NULL DEFAULT 'running',
            error_summary    text,

            CONSTRAINT run_dates CHECK (period_end >= period_start),
            CONSTRAINT run_finished
                CHECK (finished_at IS NULL OR finished_at >= started_at),
            CONSTRAINT run_counts CHECK (
                sites_processed >= 0 AND bills_issued >= 0 AND failures >= 0
            )
        )
        """
    )

    op.execute(
        """
        CREATE TABLE billing_period (
            period_id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            site_id                  uuid NOT NULL
                REFERENCES site (site_id) ON DELETE CASCADE,
            billing_run_id           uuid
                REFERENCES billing_run (run_id) ON DELETE SET NULL,
            period_start             date NOT NULL,
            period_end               date NOT NULL,
            status                   period_status NOT NULL DEFAULT 'open',
            total_import_kwh         numeric(12,4) NOT NULL DEFAULT 0,
            total_export_kwh         numeric(12,4) NOT NULL DEFAULT 0,
            total_generation_kwh     numeric(12,4) NOT NULL DEFAULT 0,
            net_kwh numeric(12,4) GENERATED ALWAYS AS
                (total_import_kwh - total_export_kwh) STORED,
            reading_count            integer NOT NULL DEFAULT 0,
            expected_reading_count   integer NOT NULL,
            coverage_pct numeric(5,2) GENERATED ALWAYS AS (
                round(reading_count * 100.0
                      / NULLIF(expected_reading_count, 0), 2)
            ) STORED,
            contributing_device_count smallint NOT NULL DEFAULT 0,
            frozen_at                timestamptz,
            billed_at                timestamptz,

            CONSTRAINT period_dates CHECK (period_end >= period_start),
            CONSTRAINT period_counts
                CHECK (reading_count >= 0 AND expected_reading_count > 0),
            CONSTRAINT period_frozen_before_billed CHECK (
                billed_at IS NULL
                OR (frozen_at IS NOT NULL AND billed_at >= frozen_at)
            ),
            CONSTRAINT period_status_timestamps CHECK (
                    (status = 'open') = (frozen_at IS NULL)
                AND (status IN ('billed', 'closed')) = (billed_at IS NOT NULL)
            ),

            -- Rule 3: a site's periods may not overlap.
            CONSTRAINT period_no_overlap EXCLUDE USING gist (
                site_id WITH =,
                (daterange(period_start, period_end, '[]')) WITH &&
            )
        )
        """
    )

    op.execute(
        """
        CREATE TABLE bill (
            bill_id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            period_id             uuid NOT NULL
                REFERENCES billing_period (period_id) ON DELETE RESTRICT,

            -- Rule 2: snapshots, not joins. A bill stays correct after an
            -- ownership transfer or a rate change.
            site_id               uuid NOT NULL
                REFERENCES site (site_id) ON DELETE RESTRICT,
            account_id            uuid NOT NULL
                REFERENCES account (account_id) ON DELETE RESTRICT,
            tariff_plan_id        uuid NOT NULL
                REFERENCES tariff_plan (plan_id) ON DELETE RESTRICT,

            currency              char(3) NOT NULL DEFAULT 'BDT',
            energy_charge         numeric(14,4) NOT NULL DEFAULT 0,
            export_credit_earned  numeric(14,4) NOT NULL DEFAULT 0,
            fixed_charge          numeric(14,4) NOT NULL DEFAULT 0,
            tax_amount            numeric(14,4) NOT NULL DEFAULT 0,
            gross_amount          numeric(14,4) NOT NULL DEFAULT 0,
            credit_opening_kwh    numeric(12,4) NOT NULL DEFAULT 0,
            credit_applied_kwh    numeric(12,4) NOT NULL DEFAULT 0,
            credit_applied_amount numeric(14,4) NOT NULL DEFAULT 0,
            credit_closing_kwh    numeric(12,4) NOT NULL DEFAULT 0,
            amount_due            numeric(14,4) NOT NULL DEFAULT 0,
            due_date              date,
            issued_at             timestamptz NOT NULL DEFAULT now(),
            status                bill_status NOT NULL DEFAULT 'issued',
            voided_by_bill_id     uuid REFERENCES bill (bill_id)
                                      ON DELETE RESTRICT,

            -- Rule 4: one bill per period, enforced by the database.
            CONSTRAINT bill_one_per_period UNIQUE (period_id),

            CONSTRAINT bill_amounts_non_negative CHECK (
                    energy_charge >= 0 AND export_credit_earned >= 0
                AND fixed_charge  >= 0 AND tax_amount >= 0
            ),
            CONSTRAINT bill_credit_applied_bounded CHECK (
                credit_applied_kwh >= 0
                AND credit_applied_kwh <= credit_opening_kwh
            ),
            CONSTRAINT bill_not_self_void
                CHECK (voided_by_bill_id IS DISTINCT FROM bill_id),
            CONSTRAINT bill_void_status
                CHECK ((voided_by_bill_id IS NOT NULL) = (status = 'void'))
        )
        """
    )

    op.execute(
        """
        CREATE TABLE bill_line_item (
            bill_id      uuid NOT NULL
                REFERENCES bill (bill_id) ON DELETE CASCADE,
            sort_order   smallint NOT NULL,
            rate_id      uuid REFERENCES tariff_rate (rate_id)
                              ON DELETE SET NULL,
            line_type    bill_line_type NOT NULL,
            period_name  tou_period,
            quantity_kwh numeric(12,4),

            -- Rule 2: frozen at billing time, never a lookup. A rate
            -- correction next year must not rewrite last year's bills.
            rate_applied numeric(10,6),
            amount       numeric(14,4) NOT NULL,

            PRIMARY KEY (bill_id, sort_order),

            CONSTRAINT line_period_only_for_energy CHECK (
                line_type IN ('energy_import', 'export_credit')
                OR period_name IS NULL
            )
        )
        """
    )

    op.execute(
        """
        CREATE TABLE credit_ledger (
            entry_id             bigint GENERATED ALWAYS AS IDENTITY
                                     PRIMARY KEY,
            site_id              uuid NOT NULL
                REFERENCES site (site_id) ON DELETE CASCADE,
            period_id            uuid
                REFERENCES billing_period (period_id) ON DELETE SET NULL,
            bill_id              uuid
                REFERENCES bill (bill_id) ON DELETE SET NULL,
            entry_type           ledger_entry_type NOT NULL,
            kwh_delta            numeric(12,4) NOT NULL,
            amount_delta         numeric(14,4) NOT NULL DEFAULT 0,

            -- Materialized running total. SUM(kwh_delta) stays the source of
            -- truth; the nightly job asserts they agree.
            balance_kwh_after    numeric(12,4) NOT NULL,
            balance_amount_after numeric(14,4) NOT NULL DEFAULT 0,
            expires_on           date,
            note                 text,
            created_at           timestamptz NOT NULL DEFAULT now(),

            CONSTRAINT ledger_sign CHECK (
                CASE entry_type
                    WHEN 'earned'  THEN kwh_delta >= 0
                    WHEN 'applied' THEN kwh_delta <= 0
                    WHEN 'expired' THEN kwh_delta <= 0
                    WHEN 'cashout' THEN kwh_delta <= 0
                    ELSE TRUE
                END
            )
        )
        """
    )

    op.execute(
        """
        CREATE TABLE payment (
            payment_id   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            bill_id      uuid NOT NULL
                REFERENCES bill (bill_id) ON DELETE RESTRICT,
            account_id   uuid NOT NULL
                REFERENCES account (account_id) ON DELETE RESTRICT,
            amount       numeric(14,4) NOT NULL,
            currency     char(3) NOT NULL DEFAULT 'BDT',
            method       payment_method NOT NULL,
            provider_ref text UNIQUE,
            status       payment_status NOT NULL DEFAULT 'pending',
            initiated_at timestamptz NOT NULL DEFAULT now(),
            settled_at   timestamptz,

            CONSTRAINT payment_positive CHECK (amount > 0),
            CONSTRAINT payment_settled
                CHECK (settled_at IS NULL OR settled_at >= initiated_at)
        )
        """
    )

    # ==================================================================
    # Operations
    # ==================================================================
    op.execute(
        """
        CREATE TABLE issue (
            issue_id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            reported_by_account_id uuid NOT NULL
                REFERENCES account (account_id) ON DELETE RESTRICT,
            site_id                uuid NOT NULL
                REFERENCES site (site_id) ON DELETE CASCADE,
            device_id              uuid
                REFERENCES device (device_id) ON DELETE SET NULL,
            bill_id                uuid
                REFERENCES bill (bill_id) ON DELETE SET NULL,
            duplicate_of_issue_id  uuid
                REFERENCES issue (issue_id) ON DELETE SET NULL,
            category               issue_category NOT NULL,
            severity               issue_severity NOT NULL DEFAULT 'medium',
            status                 issue_status NOT NULL DEFAULT 'open',
            title                  text NOT NULL,
            description            text,
            priority               smallint NOT NULL DEFAULT 3,
            reported_at            timestamptz NOT NULL DEFAULT now(),
            sla_due_at             timestamptz,
            acknowledged_at        timestamptz,
            resolved_at            timestamptz,
            closed_at              timestamptz,
            resolution_notes       text,

            CONSTRAINT issue_not_own_duplicate
                CHECK (duplicate_of_issue_id IS DISTINCT FROM issue_id),
            CONSTRAINT issue_duplicate_status CHECK (
                (duplicate_of_issue_id IS NOT NULL) = (status = 'duplicate')
            ),
            CONSTRAINT issue_priority CHECK (priority BETWEEN 1 AND 5)
        )
        """
    )

    op.execute(
        """
        CREATE TABLE issue_comment (
            issue_id          uuid NOT NULL
                REFERENCES issue (issue_id) ON DELETE CASCADE,
            comment_id        smallint NOT NULL,
            author_account_id uuid NOT NULL
                REFERENCES account (account_id) ON DELETE RESTRICT,
            body              text NOT NULL,
            is_internal       boolean NOT NULL DEFAULT false,
            created_at        timestamptz NOT NULL DEFAULT now(),

            PRIMARY KEY (issue_id, comment_id)
        )
        """
    )

    op.execute(
        """
        CREATE TABLE work_order (
            order_id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            issue_id               uuid
                REFERENCES issue (issue_id) ON DELETE SET NULL,
            site_id                uuid NOT NULL
                REFERENCES site (site_id) ON DELETE CASCADE,
            device_id              uuid
                REFERENCES device (device_id) ON DELETE SET NULL,
            created_by_account_id  uuid NOT NULL
                REFERENCES account (account_id) ON DELETE RESTRICT,
            order_type             work_order_type NOT NULL,
            status                 work_order_status NOT NULL DEFAULT 'draft',
            priority               smallint NOT NULL DEFAULT 3,
            scheduled_for          timestamptz,
            started_at             timestamptz,
            completed_at           timestamptz,
            completion_notes       text,
            failure_reason         text,
            created_at             timestamptz NOT NULL DEFAULT now(),

            CONSTRAINT order_priority CHECK (priority BETWEEN 1 AND 5),
            CONSTRAINT order_completion
                CHECK (completed_at IS NULL OR started_at IS NULL
                       OR completed_at >= started_at)
        )
        """
    )

    op.execute(
        """
        CREATE TABLE work_order_assignment (
            order_id       uuid NOT NULL
                REFERENCES work_order (order_id) ON DELETE CASCADE,
            account_id     uuid NOT NULL
                REFERENCES worker_profile (account_id) ON DELETE RESTRICT,
            job_role       assignment_role NOT NULL DEFAULT 'assistant',
            status         assignment_status NOT NULL DEFAULT 'offered',
            assigned_at    timestamptz NOT NULL DEFAULT now(),
            responded_at   timestamptz,
            released_at    timestamptz,
            decline_reason text,

            PRIMARY KEY (order_id, account_id)
        )
        """
    )

    # ==================================================================
    # Analytics & audit
    # ==================================================================
    op.execute(
        """
        CREATE TABLE audit_log (
            audit_id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            actor_account_id uuid REFERENCES account (account_id)
                                  ON DELETE SET NULL,
            action           text NOT NULL,
            entity_type      text NOT NULL,
            entity_id        text,
            before_state     jsonb,
            after_state      jsonb,
            client_ip        inet,
            occurred_at      timestamptz NOT NULL DEFAULT now()
        )
        """
    )

    op.execute(
        """
        CREATE TABLE site_daily_summary (
            site_id                   uuid NOT NULL
                REFERENCES site (site_id) ON DELETE CASCADE,
            summary_date              date NOT NULL,
            import_kwh                numeric(12,4) NOT NULL DEFAULT 0,
            export_kwh                numeric(12,4) NOT NULL DEFAULT 0,
            generation_kwh            numeric(12,4) NOT NULL DEFAULT 0,
            self_consumption_kwh numeric(12,4) GENERATED ALWAYS AS
                (generation_kwh - export_kwh) STORED,
            peak_import_kwh           numeric(12,4),
            import_peak_window_kwh    numeric(12,4),
            import_offpeak_window_kwh numeric(12,4),
            interval_count            smallint NOT NULL DEFAULT 0,
            refreshed_at              timestamptz NOT NULL DEFAULT now(),

            PRIMARY KEY (site_id, summary_date)
        )
        """
    )

    op.execute(
        """
        CREATE TABLE site_monthly_summary (
            site_id              uuid NOT NULL
                REFERENCES site (site_id) ON DELETE CASCADE,
            month_start          date NOT NULL,
            import_kwh           numeric(12,4) NOT NULL DEFAULT 0,
            export_kwh           numeric(12,4) NOT NULL DEFAULT 0,
            generation_kwh       numeric(12,4) NOT NULL DEFAULT 0,
            self_consumption_kwh numeric(12,4) GENERATED ALWAYS AS
                (generation_kwh - export_kwh) STORED,
            peak_demand_kw       numeric(10,3),
            self_sufficiency_pct numeric(5,2),
            refreshed_at         timestamptz NOT NULL DEFAULT now(),

            PRIMARY KEY (site_id, month_start),

            CONSTRAINT monthly_first_of_month
                CHECK (month_start = date_trunc('month', month_start)::date),
            CONSTRAINT monthly_sufficiency_range CHECK (
                self_sufficiency_pct IS NULL
                OR self_sufficiency_pct BETWEEN 0 AND 100
            )
        )
        """
    )

    # ------------------------------------------------------------------
    # Indexes
    # ------------------------------------------------------------------
    for statement in (
        "CREATE INDEX period_workable ON billing_period (site_id, period_start DESC) "
        "WHERE status IN ('open', 'frozen')",
        "CREATE INDEX bill_by_account ON bill (account_id, issued_at DESC)",
        "CREATE INDEX bill_by_site ON bill (site_id, issued_at DESC)",
        "CREATE INDEX ledger_by_site ON credit_ledger (site_id, entry_id DESC)",
        "CREATE INDEX ledger_expiring ON credit_ledger (expires_on) "
        "WHERE expires_on IS NOT NULL",
        "CREATE UNIQUE INDEX ledger_one_entry_per_period "
        "ON credit_ledger (site_id, period_id, entry_type) "
        "WHERE entry_type IN ('earned', 'applied')",
        "CREATE INDEX payment_by_bill ON payment (bill_id)",
        "CREATE UNIQUE INDEX one_order_per_issue ON work_order (issue_id) "
        "WHERE issue_id IS NOT NULL",
        "CREATE UNIQUE INDEX one_lead_per_order ON work_order_assignment (order_id) "
        "WHERE job_role = 'lead' AND status IN ('offered', 'accepted', 'completed')",
        "CREATE INDEX issue_open_by_site ON issue (site_id, reported_at DESC) "
        "WHERE status NOT IN ('resolved', 'closed', 'duplicate')",
        "CREATE INDEX audit_by_entity ON audit_log (entity_type, entity_id, "
        "occurred_at DESC)",
    ):
        op.execute(statement)

    # ------------------------------------------------------------------
    # Rule 1: money is immutable
    # ------------------------------------------------------------------
    op.execute(
        """
        CREATE FUNCTION forbid_mutation() RETURNS trigger
        LANGUAGE plpgsql AS $fn$
        BEGIN
            -- A bill's status and void pointer must still move; everything
            -- else about it is frozen. Corrections are new rows.
            IF TG_OP = 'UPDATE' AND TG_TABLE_NAME = 'bill'
               AND to_jsonb(NEW) - 'status' - 'voided_by_bill_id'
                 = to_jsonb(OLD) - 'status' - 'voided_by_bill_id'
            THEN
                RETURN NEW;
            END IF;

            RAISE EXCEPTION
                '% on % is forbidden: this table is append-only',
                TG_OP, TG_TABLE_NAME
                USING ERRCODE = '23514',
                      HINT = 'rule 1: money is immutable -- write a new row';
        END;
        $fn$
        """
    )

    for table in ("bill", "bill_line_item", "credit_ledger", "tariff_rate"):
        op.execute(
            f"""
            CREATE TRIGGER {table}_immutable
                BEFORE UPDATE OR DELETE ON {table}
                FOR EACH ROW EXECUTE FUNCTION forbid_mutation()
            """
        )


def downgrade() -> None:
    """Downgrade schema."""

    for table in ("bill", "bill_line_item", "credit_ledger", "tariff_rate"):
        op.execute(f"DROP TRIGGER IF EXISTS {table}_immutable ON {table}")
    op.execute("DROP FUNCTION IF EXISTS forbid_mutation()")

    for table in (
        "site_monthly_summary",
        "site_daily_summary",
        "audit_log",
        "work_order_assignment",
        "work_order",
        "issue_comment",
        "issue",
        "payment",
        "credit_ledger",
        "bill_line_item",
        "bill",
        "billing_period",
        "billing_run",
        "holiday_calendar",
    ):
        op.execute(f"DROP TABLE IF EXISTS {table}")

    for name in reversed(list(ENUMS)):
        op.execute(f"DROP TYPE IF EXISTS {name}")
