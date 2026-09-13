"""a meter is commissioned by a handshake with its utility's head-end

Revision ID: f2a9c4e1b7d6
Revises: d9c3b8a41f27
Create Date: 2026-09-13 12:00:00.000000

Schema only. Nothing in the API, ingest or the portals reads these tables yet --
this is step 1 of the commissioning work, so the shape is settled and tested
before any code depends on it.

=============================================================================
WHY
=============================================================================

`POST /api/sites/{id}/meter` creates a device with a key hash minted from a
random token that is immediately **discarded**, then writes 90 days of
readings with `backfill_readings()`. So a registered meter has a credential
nobody holds, and every reading it will ever have was generated in SQL.
`scripts/issue_device_keys.py` is the manual workaround: somebody runs it, and
the plaintext lands in a file the simulator reads.

What real metering looks like instead: the utility runs a **head-end system**
that owns its meter network. When a meter is installed, the head-end is told,
proves it actually holds that meter, receives the credential that meter will
sign its readings with, and starts delivering. This migration makes that
expressible.

=============================================================================
1. telemetry_source -- one head-end per distribution company
=============================================================================

The head-end authenticates as itself, with its own key, before it can see or
claim any offer. Per-device keys still sign every reading, so one meter can be
revoked without touching the rest of the utility's fleet.

UNIQUE on the company: two sources for one utility would be two systems each
entitled to claim the same meters, and whichever polled first would win.
Rotation replaces a compromised key; nothing needs a second row.

=============================================================================
2. device_commissioning -- one handshake attempt per row
=============================================================================

    offered    GridSync has told the head-end this meter exists
    activated  the head-end claimed it and was handed a device key
    live       a batch signed with that key has been ACCEPTED by ingest
    failed     an offer or activation lapsed, the head-end rejected it
               (typically an unknown serial -- a technician's typo), or no
               head-end serves this utility at all
    cancelled  the device was retired while the handshake was open or live

**Live is proved by data, not asserted.** Activation hands over a key; only a
reading that key successfully signed shows it reached whatever sends readings.
Until then the key has never been used, which is also why a retried activation
may safely mint a replacement (`activation_count` records how many were minted).

**One OPEN handshake per device, by partial unique index** (rule 4). Two tabs
installing one meter, or a retry racing the original, produce one row. Failed
and cancelled rows fall outside the index, so a head-end that was down when the
offer lapsed does not leave the meter uncommissionable forever; the history of
attempts stays.

**Deadlines are stored** (decision 3), as `offer_expires_at` and
`activation_expires_at`, so a query between two sweeps is already correct and
changing a duration in code cannot move yesterday's deadline.

**"No head-end serves this utility" is a row, not an absence.** `source_id` is
NULL exactly when the row failed with `no_source`, so the equipment page can
say why a meter will never report instead of showing nothing.

**`backfill_from` / `backfill_to` are the history the head-end should upload**,
computed at offer time. On a meter swap the window starts after the retired
meter's last reading, which is the clipping `register_meter` does today; NULL
on both means upload nothing.

Retiring a device does not cancel its handshake here. The API knows why a meter
left (a swap, a fault) and does it in the same transaction; ingest already
refuses a removed device either way.

No row is inserted. Sources are provisioned by a script (step 4), and the
seeded meters are offered by another -- a migration that invented credentials
would put a secret in the migration history.
"""
from alembic import op

revision = "f2a9c4e1b7d6"
down_revision = "d9c3b8a41f27"
branch_labels = None
depends_on = None


OPEN_STATES = "('offered', 'activated', 'live')"


def upgrade() -> None:
    """Upgrade schema."""

    # The district office hears when a head-end refuses a meter (usually a
    # serial the technician recorded wrong), and later when a handshake lapses.
    # Added in its own statement and used only at runtime: PostgreSQL will not
    # let a transaction insert an enum value that same transaction added.
    op.execute(
        "ALTER TYPE notification_kind ADD VALUE IF NOT EXISTS 'device_commissioning'"
    )

    # ==================================================================
    # 1. telemetry_source
    # ==================================================================
    op.execute(
        """
        CREATE TABLE telemetry_source (
            source_id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            distribution_company_id uuid NOT NULL UNIQUE
                REFERENCES distribution_company (company_id) ON DELETE RESTRICT,
            name                    text NOT NULL,
            source_key_hash         text NOT NULL,
            key_rotated_at          timestamptz,
            created_at              timestamptz NOT NULL DEFAULT now(),
            disabled_at             timestamptz,

            CONSTRAINT telemetry_source_name_present CHECK (btrim(name) <> ''),
            CONSTRAINT telemetry_source_disabled_after_created
                CHECK (disabled_at IS NULL OR disabled_at >= created_at)
        )
        """
    )
    op.execute(
        """
        COMMENT ON TABLE telemetry_source IS
        'A utility''s head-end system: the external service that owns its meter '
        'network, claims newly installed meters through the commissioning '
        'handshake and delivers their readings to ingest. One per distribution '
        'company. Authenticates with its own key; readings are still signed '
        'per device.'
        """
    )

    # ==================================================================
    # 2. device_commissioning
    # ==================================================================
    op.execute(
        "CREATE TYPE commissioning_status AS ENUM "
        "('offered', 'activated', 'live', 'failed', 'cancelled')"
    )
    op.execute(
        "CREATE TYPE commissioning_failure AS ENUM "
        "('no_source', 'offer_expired', 'activation_expired', 'rejected_by_source')"
    )

    op.execute(
        """
        CREATE TABLE device_commissioning (
            commissioning_id        uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            device_id               uuid NOT NULL
                REFERENCES device (device_id) ON DELETE CASCADE,
            source_id               uuid
                REFERENCES telemetry_source (source_id) ON DELETE RESTRICT,
            status                  commissioning_status NOT NULL DEFAULT 'offered',
            requested_by_account_id uuid
                REFERENCES account (account_id) ON DELETE SET NULL,

            offered_at              timestamptz NOT NULL DEFAULT now(),
            offer_expires_at        timestamptz NOT NULL,
            activated_at            timestamptz,
            activation_expires_at   timestamptz,
            activation_count        smallint NOT NULL DEFAULT 0,
            live_at                 timestamptz,
            ended_at                timestamptz,
            failed_reason           commissioning_failure,
            failure_detail          text,

            backfill_from           date,
            backfill_to             date,

            updated_at              timestamptz NOT NULL DEFAULT now(),

            -- Deadlines.
            CONSTRAINT commissioning_offer_window
                CHECK (offer_expires_at > offered_at),
            CONSTRAINT commissioning_activation_window
                CHECK (activation_expires_at IS NULL
                       OR activation_expires_at > activated_at),

            -- An activation is a timestamp, a deadline and a minted key,
            -- together or not at all.
            CONSTRAINT commissioning_activation_pair
                CHECK ((activated_at IS NULL) = (activation_expires_at IS NULL)),
            CONSTRAINT commissioning_activation_counted
                CHECK ((activated_at IS NULL) = (activation_count = 0)),
            CONSTRAINT commissioning_activation_count_nonneg
                CHECK (activation_count >= 0),
            CONSTRAINT commissioning_activated_after_offer
                CHECK (activated_at IS NULL OR activated_at >= offered_at),

            -- Status agrees with its evidence.
            CONSTRAINT commissioning_activated_state
                CHECK (status NOT IN ('activated', 'live')
                       OR activated_at IS NOT NULL),
            CONSTRAINT commissioning_live_state
                CHECK (status <> 'live' OR live_at IS NOT NULL),
            CONSTRAINT commissioning_live_after_activation
                CHECK (live_at IS NULL
                       OR (activated_at IS NOT NULL AND live_at >= activated_at)),
            CONSTRAINT commissioning_ended_state
                CHECK ((ended_at IS NOT NULL) = (status IN ('failed', 'cancelled'))),
            CONSTRAINT commissioning_ended_after_offer
                CHECK (ended_at IS NULL OR ended_at >= offered_at),
            CONSTRAINT commissioning_failure_state
                CHECK ((failed_reason IS NOT NULL) = (status = 'failed')),

            -- NULL source means exactly one thing: nobody serves this utility.
            CONSTRAINT commissioning_needs_source
                CHECK ((source_id IS NULL)
                       = (failed_reason IS NOT DISTINCT FROM 'no_source')),

            -- History to upload: a window, or nothing.
            CONSTRAINT commissioning_backfill_pair
                CHECK ((backfill_from IS NULL) = (backfill_to IS NULL)),
            CONSTRAINT commissioning_backfill_ordered
                CHECK (backfill_from IS NULL OR backfill_from <= backfill_to)
        )
        """
    )
    op.execute(
        """
        COMMENT ON TABLE device_commissioning IS
        'One attempt at the handshake that hands a device''s key to its '
        'utility''s head-end. Live only once ingest has accepted a batch signed '
        'with that key. At most one open (offered/activated/live) row per device.'
        """
    )

    # Rule 4: one open handshake per device. Failed and cancelled rows are
    # outside it, so a lapsed offer can be retried and the attempts are kept.
    op.execute(
        f"""
        CREATE UNIQUE INDEX one_open_commissioning_per_device
            ON device_commissioning (device_id)
            WHERE status IN {OPEN_STATES}
        """
    )
    # The head-end's feed: what is open for this source.
    op.execute(
        f"""
        CREATE INDEX commissioning_open_by_source
            ON device_commissioning (source_id, status)
            WHERE status IN {OPEN_STATES}
        """
    )
    # The two sweeps each scan one small slice of a table that mostly holds
    # finished handshakes.
    op.execute(
        "CREATE INDEX commissioning_offers_to_expire "
        "ON device_commissioning (offer_expires_at) WHERE status = 'offered'"
    )
    op.execute(
        "CREATE INDEX commissioning_activations_to_expire "
        "ON device_commissioning (activation_expires_at) WHERE status = 'activated'"
    )
    # A device's attempts, newest first, for the equipment page.
    op.execute(
        "CREATE INDEX commissioning_by_device "
        "ON device_commissioning (device_id, offered_at DESC)"
    )

    # Rows say when they changed (migration e8b1d3f70a26's shared trigger).
    op.execute(
        """
        CREATE TRIGGER device_commissioning_touch_updated_at
            BEFORE UPDATE ON device_commissioning
            FOR EACH ROW EXECUTE FUNCTION touch_updated_at()
        """
    )
    op.execute(
        "CREATE INDEX device_commissioning_by_updated_at "
        "ON device_commissioning (updated_at DESC)"
    )


def downgrade() -> None:
    """Downgrade schema.

    The tables and their types go. The 'device_commissioning' notification_kind
    value stays -- PostgreSQL cannot remove an enum label, and any notification
    already written with it would become unreadable if it could. It is inert
    without the tables.
    """
    op.execute("DROP TABLE IF EXISTS device_commissioning")
    op.execute("DROP TYPE IF EXISTS commissioning_failure")
    op.execute("DROP TYPE IF EXISTS commissioning_status")
    op.execute("DROP TABLE IF EXISTS telemetry_source")
