"""a supplier's organisation is a claim until an official links it

Revision ID: d9c3b8a41f27
Revises: c4a1f7e26b98
Create Date: 2026-09-04 14:00:00.000000

Registration asked an applicant to pick their employer from a dropdown of
seeded firms. That made the organisation a *fact* the moment it was submitted,
which is backwards: it is the thing the official is supposed to check, and a
list you choose from proves only that you can read.

So the applicant types the name, and the typed string is stored as exactly what
it is -- an assertion. `supplier_id` is set by the official, at approval, when
they either link the claim to a firm that already exists or create the firm
from it.

WHY NOT LET THE TYPED NAME BE THE KEY
=============================================================================
Because `supplier_company` is one row per firm however many staff logins it
has (decision 4 in docs/decisions.md), and that row is what a household picks
from a dropdown, applies to, and rates. If every applicant's spelling created
or matched a firm, "Noor Solar", "noor solar" and "Noor Solars Ltd" would be
three firms with three reputations -- the same failure this project already
fixed for `district`, which was free text until migration e7c4b19a2d83 and had
leaked 'Dhaka', 'dhaka' and 'g' into the regulator's rollup. `name` is not even
UNIQUE on that table; only `code` and `license_no` are.

Resolving the string is therefore a judgement, and this migration puts it where
the judgement already happens.

THE THREE CHANGES
=============================================================================
* `claimed_organisation` -- what was typed, kept verbatim and forever. It is
  not overwritten when the official links a firm: the record of what someone
  claimed is evidence, and rewriting it to the approved answer would erase the
  only thing a later reader could audit the decision against.
* `supplier_id` becomes NULLABLE -- an undecided registration belongs to no
  firm yet, and pointing it at a placeholder would put a stranger on that
  firm's staff list while they wait.
* `supplier_approved_has_firm` -- approved implies a firm. The nullable column
  is a window that closes at the decision, not a permanent maybe. A rejected
  row keeps NULL, correctly: nobody ever linked it.

Reverses, but not blindly -- see downgrade().
"""
from __future__ import annotations

from alembic import op

revision = "d9c3b8a41f27"
down_revision = "c4a1f7e26b98"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE supplier_profile ADD COLUMN claimed_organisation text")

    # Existing rows were created by picking from the dropdown, so what they
    # "claimed" is the firm they are already attached to. Reading it off the
    # company keeps the column honest for them rather than inventing a string.
    op.execute(
        """
        UPDATE supplier_profile sp
        SET claimed_organisation = sc.name
        FROM supplier_company sc
        WHERE sc.supplier_id = sp.supplier_id
        """
    )

    op.execute(
        """
        ALTER TABLE supplier_profile
            ALTER COLUMN claimed_organisation SET NOT NULL,
            ALTER COLUMN supplier_id DROP NOT NULL,
            ADD CONSTRAINT supplier_approved_has_firm
                CHECK (approval_status <> 'approved' OR supplier_id IS NOT NULL)
        """
    )

    op.execute(
        "COMMENT ON COLUMN supplier_profile.claimed_organisation IS "
        "'The organisation the applicant typed at registration, verbatim. An "
        "assertion, never a key -- the official resolves it to a "
        "supplier_company when they approve. Never rewritten to the approved "
        "firm''s name: it is the evidence the decision was made against.'"
    )
    op.execute(
        "COMMENT ON COLUMN supplier_profile.supplier_id IS "
        "'NULL until an official approves the registration and links it to a "
        "firm. supplier_approved_has_firm closes that window at the decision.'"
    )


def downgrade() -> None:
    # supplier_id goes back to NOT NULL, and an undecided or rejected
    # registration has none. Deciding what those rows should point at is a data
    # question -- there is no correct firm to invent for them -- so this stops
    # and says which rows are in the way rather than picking one.
    op.execute(
        """
        DO $$
        DECLARE stuck int;
        BEGIN
            SELECT count(*) INTO stuck
            FROM supplier_profile WHERE supplier_id IS NULL;
            IF stuck > 0 THEN
                RAISE EXCEPTION
                    'cannot restore supplier_profile.supplier_id NOT NULL: % '
                    'registration(s) are not linked to a firm. Decide or '
                    'delete them first -- this migration will not invent an '
                    'employer for somebody.', stuck;
            END IF;
        END $$
        """
    )
    op.execute(
        """
        ALTER TABLE supplier_profile
            DROP CONSTRAINT supplier_approved_has_firm,
            ALTER COLUMN supplier_id SET NOT NULL,
            DROP COLUMN claimed_organisation
        """
    )
