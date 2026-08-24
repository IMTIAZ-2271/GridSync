"""The nightly billing pass.

One call of `run_billing` per (billing point, complete month) that has readings
and no bill yet, recorded as a `billing_run` so every bill can be traced to the
pass that issued it.

**This job is off unless someone turns it on** (`JOBS_BILLING_ENABLED=on`).
That is not timidity about the engine -- `run_billing` is idempotent, refuses an
incomplete period (rule 8) and has been verified bill-for-bill against its
predecessor. It is because of rule 1: a bill is never UPDATEd and never deleted,
and the only correction is a *second* bill pointing at the first. A scheduler
left running against the demo database would therefore permanently write rows
that cannot be taken back, and it would do it at 2am with nobody watching. An
irreversible, outward-facing action gets an explicit switch; the consumer's own
`POST /api/sites/{id}/bill` stays the way a human asks for the same thing.

What counts as a failure is deliberately narrow. A month below rule 8's coverage
threshold comes back as a CheckViolation and is recorded as **skipped**, not
failed: the engine refusing to bill an incomplete month is the system working,
and a nightly "1 failure" that never clears teaches an operator to stop reading
the run log. Only an unexpected error increments `failures`.
"""
from __future__ import annotations

import logging
from datetime import date

import asyncpg

from ..api.billing import run_billing_with_retry
from ..api.queries import sql

log = logging.getLogger(__name__)

# How many distinct reasons to keep in billing_run.error_summary. The column is
# free text and the point of it is to make a failed run diagnosable at a glance,
# not to be a second copy of the log.
_MAX_ERRORS_RECORDED = 5


async def run_scheduled_billing(pool: asyncpg.Pool, limit: int) -> dict[str, int]:
    async with pool.acquire() as conn:
        todo = await conn.fetch(sql("unbilled_point_months"), limit)

    if not todo:
        log.info("scheduled billing: nothing to bill")
        return {"attempted": 0, "billed": 0, "skipped": 0, "failed": 0}

    months = [r["period_start"] for r in todo]
    period_start, period_end = min(months), max(months)

    async with pool.acquire() as conn:
        run_id = await conn.fetchval(
            sql("open_billing_run"), period_start, period_end
        )

    billed = skipped = failed = 0
    sites: set[str] = set()
    errors: list[str] = []

    for row in todo:
        point_id, month = row["point_id"], row["period_start"]
        sites.add(str(row["site_id"]))
        async with pool.acquire() as conn:
            try:
                await run_billing_with_retry(conn, point_id, month)
            except asyncpg.CheckViolationError as exc:
                # Rule 8, or rule 7's "no active billing meter". Both mean the
                # month is not billable as things stand, and both are states a
                # human resolves -- not something to retry tomorrow night and
                # count as broken every night until they do.
                skipped += 1
                log.info(
                    "skipped %s %s: %s", row["label"], month, _first_line(exc)
                )
                continue
            except asyncpg.PostgresError as exc:
                failed += 1
                log.exception("billing failed for point %s month %s", point_id, month)
                _record(errors, _first_line(exc))
                continue

            billed += 1
            # Stamped after the fact: run_billing does not know it is being run
            # by a job, and giving it a parameter it would only pass through
            # would put scheduling into the billing transaction.
            await conn.execute(sql("attach_period_to_run"), point_id, run_id, month)

    status = (
        "failed" if failed and not billed
        else "partial" if failed
        else "succeeded"
    )
    summary = "; ".join(errors) if errors else None

    async with pool.acquire() as conn:
        await conn.execute(
            sql("finish_billing_run"),
            run_id, len(sites), billed, failed, status, summary,
        )

    log.info(
        "billing run %s %s: %s billed, %s skipped, %s failed across %s site(s)",
        run_id, status, billed, skipped, failed, len(sites),
    )
    return {
        "attempted": len(todo),
        "billed": billed,
        "skipped": skipped,
        "failed": failed,
    }


def _first_line(exc: BaseException) -> str:
    return str(exc).strip().splitlines()[0] if str(exc).strip() else type(exc).__name__


def _record(errors: list[str], message: str) -> None:
    """Distinct reasons only, capped. Twenty points failing for one reason is
    one thing to fix, and printing it twenty times hides the second reason."""
    if message not in errors and len(errors) < _MAX_ERRORS_RECORDED:
        errors.append(message)
