"""Entry point: `python -m services.jobs`.

    python -m services.jobs                     # run the scheduler
    python -m services.jobs --list              # what would be scheduled
    python -m services.jobs --once              # one pass of every job, exit
    python -m services.jobs --once rollups      # one pass of one job, exit

`--once` is how these are verified by hand and how they would be driven from an
external scheduler (cron, a Kubernetes CronJob) instead of this process. It
ignores the `JOBS_BILLING_ENABLED` gate on purpose: that switch exists to stop
billing running *unattended*, and someone typing the job's name is not that.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from .config import load
from .runner import BY_NAME, JOBS, run_once, serve


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m services.jobs",
        description="GridSync scheduled jobs.",
    )
    parser.add_argument(
        "--once",
        nargs="*",
        metavar="JOB",
        help="run the named jobs once and exit; no names means all of them",
    )
    parser.add_argument(
        "--list", action="store_true", help="show the jobs and their schedules"
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="debug logging"
    )
    return parser


def _show_schedule() -> None:
    settings = load()
    for job in JOBS:
        state = "" if job.enabled(settings) else "  [disabled]"
        print(f"{job.name:<20} {job.trigger(settings)}{state}")
        print(f"{'':<20} {job.summary}")


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )

    if args.list:
        _show_schedule()
        return 0

    if args.once is not None:
        unknown = [n for n in args.once if n not in BY_NAME]
        if unknown:
            print(
                f"unknown job(s): {', '.join(unknown)}\n"
                f"known: {', '.join(BY_NAME)}",
                file=sys.stderr,
            )
            return 2
        results = asyncio.run(run_once(args.once or None))
        # Non-zero when a job raised, so `--once` is usable from an external
        # scheduler that watches exit codes. A job that found nothing to do is
        # a success, not a silent failure.
        return 1 if any("error" in r for r in results.values()) else 0

    try:
        asyncio.run(serve())
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
