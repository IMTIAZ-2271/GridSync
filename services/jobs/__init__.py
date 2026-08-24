"""GridSync scheduled jobs.

The fourth process in the repo, and the one that makes stored deadlines mean
something. Everything here runs on a clock rather than on a request, and every
job in it obeys the same three rules.

**A job is a sweep, not a state machine.** It reads what the database already
knows -- a deadline that has passed, a month that has ended, a limit that has
been crossed -- and acts on it. It keeps no cursor, no "last run" file and no
in-memory queue, so a runner that has been down for a week catches up simply by
being started, and two runners racing each other produce one effect rather than
two.

**Every job is idempotent by constraint, not by care** (rule 4). Notifications
dedupe on `notification_dedupe`; bills dedupe on `UNIQUE (period_id)`; rollups
dedupe on their primary key through ON CONFLICT DO UPDATE; the deadline sweeps
re-check the status they read before writing. None of them remember what they
did last time, because none of them have to.

**A failing job never takes the runner down.** `run_job` logs the exception and
the next tick tries again. A sweep that cannot reach the database is a reason to
alert, not a reason to stop expiring offers for the rest of the week.

Run it with `python -m services.jobs`; see `__main__.py` for the one-shot mode
that runs a single job and exits, which is how these are exercised by hand.
"""
