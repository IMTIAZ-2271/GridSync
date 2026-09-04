"""supplier registrations are approved by an official, like workers

Revision ID: b6f2d90ac417
Revises: f2a6c1d94b7e
Create Date: 2026-09-04 10:00:00.000000

Two roles could reach a portal without anyone deciding they should. A
*private* worker was approved by the act of registering, and a supplier's
staff account was approved by knowing one shared string -- the same string for
every installer, with no rotation and no per-invite tracking, which
CLAUDE.md has carried as a known weakness since the role existed.

Both are now decided by a government official in the region the applicant
claims. The worker half needs no schema: `worker_profile.approval_status`
already exists and already defaults to 'pending' -- registration simply stops
writing 'approved' for a private worker. This migration is the supplier half.

WHY THE APPROVAL LIVES ON supplier_profile, NOT supplier_company
=============================================================================
The firm is not what is being approved -- decision 4 in CLAUDE.md keeps staff
attached to a company rather than being one, so a firm with three logins is
one supplier with one reputation. What an official decides is whether *this
person* may act for that firm in *their* district. Approving the company
would mean one rejected employee locking out their colleagues, and one
approval covering everyone the firm hires afterwards.

That is also why `service_district` sits here rather than being read off
`supplier_service_area`: the firm may cover four districts, and the official
in one of them must not be deciding on behalf of the other three. The column
is what routes the application to a queue, so it is stored, not derived.

The five columns mirror worker_profile exactly, CHECKs included, because the
two queues are the same queue with a different subject and a page that
rendered them differently would be inventing a distinction the data does not
have.

BACKFILL
=============================================================================
Existing rows are approved, not pending. They are the seeded demo staff and
anyone who registered before this ran; making them pending would lock working
accounts out of a portal they already had, to satisfy a decision nobody was
around to make. `approved_at` is set to the row's own `created_at` rather
than now(), so the timestamp does not claim a decision was taken today.

Their district is the one their firm serves -- preferring a selectable one,
since `seed_orgs.sql` narrows the selectable set to the three districts that
actually have an official, and a supplier filed under a district nobody
governs would sit in a queue with no reader (the dead-letter case CLAUDE.md
already records for meter applications).

Reverses cleanly: five columns dropped, no enum touched.
"""
from __future__ import annotations

from alembic import op

revision = "b6f2d90ac417"
down_revision = "f2a6c1d94b7e"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE supplier_profile
            ADD COLUMN service_district       text,
            ADD COLUMN approval_status        approval_status NOT NULL
                                              DEFAULT 'pending',
            ADD COLUMN approved_by_account_id uuid
                REFERENCES account (account_id) ON DELETE SET NULL,
            ADD COLUMN approved_at            timestamptz,
            ADD COLUMN rejection_reason       text
        """
    )

    # Every row that already exists is staff who could already sign in.
    # Approve them where they stand, dated when the account was made.
    op.execute(
        """
        UPDATE supplier_profile sp
        SET service_district = COALESCE(
                (
                    SELECT a.district
                    FROM supplier_service_area a
                    JOIN district d ON d.name = a.district
                    WHERE a.supplier_id = sp.supplier_id
                    -- A district with an official beats one without.
                    ORDER BY d.is_selectable DESC, a.district
                    LIMIT 1
                ),
                (
                    SELECT d.name FROM district d
                    ORDER BY d.is_selectable DESC, d.name
                    LIMIT 1
                )
            ),
            approval_status = 'approved',
            approved_at     = sp.created_at
        """
    )

    op.execute(
        """
        ALTER TABLE supplier_profile
            ALTER COLUMN service_district SET NOT NULL,
            ADD CONSTRAINT supplier_district_fk
                FOREIGN KEY (service_district) REFERENCES district (name)
                ON UPDATE CASCADE ON DELETE RESTRICT,
            -- Identical to worker_approval_timestamps: the column records
            -- when the decision was made, not whether it was favourable, so
            -- a rejection carries a timestamp too.
            ADD CONSTRAINT supplier_approval_timestamps
                CHECK ((approval_status = 'pending') = (approved_at IS NULL)),
            -- An approval carrying a leftover reason string is a lie the UI
            -- would eventually render.
            ADD CONSTRAINT supplier_rejection_reason
                CHECK (approval_status = 'rejected' OR rejection_reason IS NULL)
        """
    )

    op.execute(
        "COMMENT ON COLUMN supplier_profile.service_district IS "
        "'The district whose officials decide this registration, and the "
        "region this person acts for. Stored rather than derived from "
        "supplier_service_area: a firm may serve several districts and an "
        "official in one of them must not decide on behalf of the others.'"
    )
    op.execute(
        "COMMENT ON COLUMN supplier_profile.approval_status IS "
        "'Set by a government official in service_district. Mirrors "
        "worker_profile: require_role refuses anything but approved.'"
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE supplier_profile
            DROP CONSTRAINT supplier_rejection_reason,
            DROP CONSTRAINT supplier_approval_timestamps,
            DROP CONSTRAINT supplier_district_fk,
            DROP COLUMN rejection_reason,
            DROP COLUMN approved_at,
            DROP COLUMN approved_by_account_id,
            DROP COLUMN approval_status,
            DROP COLUMN service_district
        """
    )
