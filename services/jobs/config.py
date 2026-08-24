"""How often each job runs, and how much it does per pass.

Everything here is a *scheduling* decision. The business deadlines -- three
hours to answer an offer, one day to start an accepted job -- are deliberately
NOT here: they are written onto the assignment row when the offer is made
(services/api/routes_work_orders.py) and only read by the sweep. That is
CLAUDE.md decision 3. Putting the durations in the runner's config would mean a
deadline changed value retroactively every time someone edited an environment
variable, and a query between two sweeps would stop being correct.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

from ..api.db import SESSION_TIME_ZONE


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    return int(raw) if raw else default


def _flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    """Read once at startup. Nothing re-reads the environment per tick."""

    # The zone every cron trigger is interpreted in. Named rather than taken
    # from the host: "run at 00:20" has to mean 00:20 in Dhaka, because that is
    # the calendar the rollups and the billing months are cut on.
    timezone: str = SESSION_TIME_ZONE

    # Deadline sweeps. Five minutes is the granularity a three-hour offer
    # window can afford: an offer expires at most five minutes late, which is
    # invisible to a worker and cheap for the database, and it means a
    # dispatcher watching the queue sees it come back rather than having to
    # refresh for an hour.
    deadline_sweep_minutes: int = 5

    # Consumption alerts. Once a day, in the morning: the alert is about a
    # monthly budget, so it has nothing new to say at 3am, and a notification
    # nobody can act on when it arrives is a notification people learn to
    # ignore.
    consumption_hour: int = 8

    # Rollups. Just after midnight local, so the day that just ended is
    # complete. The lookback re-summarises the last few days as well, which is
    # what corrects a rollup after a late backfill (readings can arrive for a
    # day already summarised; a summary is a cache, not a record).
    rollup_hour: int = 0
    rollup_minute: int = 20
    rollup_lookback_days: int = 3

    # Partition maintenance. Early, and well ahead of need: a reading with
    # nowhere to land goes to the DEFAULT partition, and getting it back out
    # later means a full scan of that table.
    partition_hour: int = 1
    partition_months_ahead: int = 3

    # Scheduled billing. See services/jobs/billing.py for why this is off
    # unless someone turns it on.
    billing_enabled: bool = False
    billing_hour: int = 2
    billing_day: int = 1

    # Per-pass caps. A sweep that finds a pathological backlog should still
    # finish and let the next tick continue, rather than hold a connection for
    # an hour.
    batch_limit: int = 500
    billing_batch_limit: int = 2000


def load() -> Settings:
    return Settings(
        deadline_sweep_minutes=_int("JOBS_DEADLINE_SWEEP_MINUTES", 5),
        consumption_hour=_int("JOBS_CONSUMPTION_HOUR", 8),
        rollup_hour=_int("JOBS_ROLLUP_HOUR", 0),
        rollup_minute=_int("JOBS_ROLLUP_MINUTE", 20),
        rollup_lookback_days=_int("JOBS_ROLLUP_LOOKBACK_DAYS", 3),
        partition_hour=_int("JOBS_PARTITION_HOUR", 1),
        partition_months_ahead=_int("JOBS_PARTITION_MONTHS_AHEAD", 3),
        billing_enabled=_flag("JOBS_BILLING_ENABLED", False),
        billing_hour=_int("JOBS_BILLING_HOUR", 2),
        billing_day=_int("JOBS_BILLING_DAY", 1),
        batch_limit=_int("JOBS_BATCH_LIMIT", 500),
        billing_batch_limit=_int("JOBS_BILLING_BATCH_LIMIT", 2000),
    )
