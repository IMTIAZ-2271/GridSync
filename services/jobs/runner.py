"""The scheduler: what runs, how often, and what happens when one fails.

APScheduler's AsyncIOScheduler, one process, no broker and no job store --
CLAUDE.md's stack, and the reason it is enough is in the package docstring:
every job here is a sweep over state the database already holds, so there is
nothing to persist between runs and nothing to hand to a worker pool.

Three settings are applied to every job and matter more than the triggers:

* `coalesce=True` -- a runner that was down for a day fires each job ONCE on
  restart, not once per missed tick. Catching up on a sweep means running it
  again, not running it forty times.
* `max_instances=1` -- a pass that overruns its interval is skipped rather than
  overlapped. The sweeps are safe to run concurrently (that is what the status
  guards are for), but two passes racing each other only makes both slower.
* `misfire_grace_time` -- generous, because being late is not a reason to skip:
  an offer that should have expired at 09:00 still needs expiring at 09:40.
"""
from __future__ import annotations

import asyncio
import logging
import signal
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import asyncpg
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from ..api.db import pool_context
from .billing import run_scheduled_billing
from .config import Settings, load
from .consumption import sweep_consumption_limits
from .deadlines import sweep_expired_offers, sweep_overdue_starts
from .maintenance import ensure_partitions
from .rollups import refresh_rollups

log = logging.getLogger("services.jobs")

JobFn = Callable[[asyncpg.Pool, Settings], Awaitable[dict[str, int]]]


@dataclass(frozen=True)
class Job:
    name: str
    summary: str
    run: JobFn
    trigger: Callable[[Settings], Any]
    # Off by default and gated on config. Only scheduled billing is.
    enabled: Callable[[Settings], bool] = lambda _s: True


JOBS: tuple[Job, ...] = (
    Job(
        name="expired-offers",
        summary="expire unanswered work-order offers and release the order",
        run=lambda pool, s: sweep_expired_offers(pool, s.batch_limit),
        trigger=lambda s: IntervalTrigger(
            minutes=s.deadline_sweep_minutes, timezone=s.timezone
        ),
    ),
    Job(
        name="overdue-starts",
        summary="reassign accepted work orders that were never started",
        run=lambda pool, s: sweep_overdue_starts(pool, s.batch_limit),
        trigger=lambda s: IntervalTrigger(
            minutes=s.deadline_sweep_minutes, timezone=s.timezone
        ),
    ),
    Job(
        name="consumption-limits",
        summary="warn households that have crossed their monthly limit",
        run=lambda pool, s: sweep_consumption_limits(pool, s.batch_limit),
        trigger=lambda s: CronTrigger(
            hour=s.consumption_hour, minute=0, timezone=s.timezone
        ),
    ),
    Job(
        name="rollups",
        summary="refresh site_daily_summary, then site_monthly_summary",
        run=lambda pool, s: refresh_rollups(pool, s.rollup_lookback_days),
        trigger=lambda s: CronTrigger(
            hour=s.rollup_hour, minute=s.rollup_minute, timezone=s.timezone
        ),
    ),
    Job(
        name="partitions",
        summary="pre-create device_reading partitions and watch the default one",
        run=lambda pool, s: ensure_partitions(pool, s.partition_months_ahead),
        trigger=lambda s: CronTrigger(
            hour=s.partition_hour, minute=10, timezone=s.timezone
        ),
    ),
    Job(
        name="billing",
        summary="bill every complete month that has readings and no bill",
        run=lambda pool, s: run_scheduled_billing(pool, s.billing_batch_limit),
        # Monthly, not nightly: a month becomes billable once, on the 1st.
        trigger=lambda s: CronTrigger(
            day=s.billing_day, hour=s.billing_hour, minute=0, timezone=s.timezone
        ),
        enabled=lambda s: s.billing_enabled,
    ),
)

BY_NAME = {job.name: job for job in JOBS}


async def run_job(job: Job, pool: asyncpg.Pool, settings: Settings) -> dict[str, int]:
    """Run one job, and never let it take the runner down.

    An exception here is logged and swallowed. The next tick tries again, which
    is the right answer for every job in this package: they are all sweeps, so a
    missed pass costs latency and nothing else. Re-raising would stop the
    scheduler thread for one job's bad night.
    """
    try:
        result = await job.run(pool, settings)
    except Exception:
        log.exception("job %s failed", job.name)
        return {"error": 1}
    if any(result.values()):
        log.info("job %s: %s", job.name, _render(result))
    else:
        # A quiet sweep is the normal case and should not fill the log.
        log.debug("job %s: %s", job.name, _render(result))
    return result


def _render(result: dict[str, int]) -> str:
    return ", ".join(f"{k}={v}" for k, v in result.items()) or "nothing to do"


async def run_once(names: list[str] | None = None) -> dict[str, dict[str, int]]:
    """Run each named job exactly once and exit. `None` means every job.

    This is the mode the jobs are exercised in by hand, and it deliberately
    ignores `enabled`: naming a job explicitly IS the switch. Scheduling billing
    unattended and asking for one billing pass are different decisions.
    """
    settings = load()
    chosen = [BY_NAME[n] for n in names] if names else list(JOBS)
    results: dict[str, dict[str, int]] = {}
    async with pool_context(min_size=1, max_size=4) as pool:
        for job in chosen:
            results[job.name] = await run_job(job, pool, settings)
    return results


async def serve() -> None:
    """Start the scheduler and block until the process is asked to stop."""
    settings = load()
    scheduler = AsyncIOScheduler(timezone=settings.timezone)

    async with pool_context(min_size=1, max_size=6) as pool:
        for job in JOBS:
            if not job.enabled(settings):
                log.info("job %s is disabled by configuration", job.name)
                continue
            scheduler.add_job(
                run_job,
                trigger=job.trigger(settings),
                args=(job, pool, settings),
                id=job.name,
                name=job.summary,
                coalesce=True,
                max_instances=1,
                misfire_grace_time=3600,
            )

        scheduler.start()
        for entry in scheduler.get_jobs():
            log.info("scheduled %-20s next run %s", entry.id, entry.next_run_time)

        stopping = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, stopping.set)
            except (NotImplementedError, AttributeError):
                # Windows' ProactorEventLoop has no add_signal_handler. Ctrl-C
                # arrives as KeyboardInterrupt out of the wait below instead,
                # which reaches the same shutdown path.
                pass

        try:
            await stopping.wait()
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass
        finally:
            log.info("stopping scheduler")
            scheduler.shutdown(wait=False)
