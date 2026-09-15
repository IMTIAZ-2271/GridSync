"""Run the hosted backend as ONE Render service: API, ingest, head-end, jobs.

    python -m scripts.render_start          # Render's start command

Render's free tier gives one always-on web service a month's worth of hours,
so everything that must keep running shares this one:

* **API** -- uvicorn on `$PORT`, in the foreground as far as Render is
  concerned. If it exits, this script exits and Render restarts the service.
* **ingest** -- on 127.0.0.1:8100. Nothing outside the container needs it: the
  only thing that sends it readings is the head-end below, in the same
  container.
* **head-end** -- `python -m simulator --mode headend --utility all`, reading
  the utilities' keys from a Render Secret File. Skipped, with a message, when
  that file is absent. Its state lives on the container's disk and is lost on
  every restart; that is survivable because it rekeys its live meters
  (services/ingest/commissioning.py).
* **jobs** -- `python -m services.jobs`. Deadline sweeps, rollups, partitions,
  and the commissioning sweep. Scheduled billing stays off unless
  JOBS_BILLING_ENABLED is set, exactly as locally. `RUN_JOBS=off` skips it.

A background process that exits is restarted after a pause, rather than taking
the API down with it: a head-end that cannot reach ingest for a moment should
not cost the portal its uptime.

This is a hosting arrangement, not an architecture: locally each of these is
its own process, started by hand, and nothing here changes how any of them
behaves.
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

PYTHON = sys.executable
INGEST_PORT = "8100"
SOURCE_KEYS = Path(os.environ.get("HEADEND_SOURCE_KEYS", "/etc/secrets/source_keys.json"))
HEADEND_STATE = os.environ.get("HEADEND_STATE_DIR", "/tmp/headend_state")
RESTART_DELAY = 10

_stopping = threading.Event()
_children: dict[str, subprocess.Popen] = {}


def log(line: str) -> None:
    print(f"[render_start] {line}", flush=True)


def supervise(name: str, argv: list[str]) -> None:
    """Keep one background process running until the service stops."""
    while not _stopping.is_set():
        log(f"starting {name}: {' '.join(argv)}")
        proc = subprocess.Popen(argv)
        _children[name] = proc
        code = proc.wait()
        if _stopping.is_set():
            return
        log(f"{name} exited with {code}; restarting in {RESTART_DELAY}s")
        time.sleep(RESTART_DELAY)


def background() -> list[tuple[str, list[str]]]:
    jobs: list[tuple[str, list[str]]] = [
        ("ingest", [PYTHON, "-m", "services.ingest", "--host", "127.0.0.1",
                    "--port", INGEST_PORT]),
    ]
    if SOURCE_KEYS.exists():
        jobs.append(("head-end", [
            PYTHON, "-m", "simulator", "--mode", "headend", "--utility", "all",
            "--ingest", f"http://127.0.0.1:{INGEST_PORT}",
            "--source-keys", str(SOURCE_KEYS),
            "--state-dir", HEADEND_STATE,
        ]))
    else:
        log(f"no head-end keys at {SOURCE_KEYS}; head-end not started")
    if os.environ.get("RUN_JOBS", "on").strip().lower() not in ("off", "0", "false", "no"):
        jobs.append(("jobs", [PYTHON, "-m", "services.jobs"]))
    return jobs


def main() -> None:
    for name, argv in background():
        threading.Thread(target=supervise, args=(name, argv), daemon=True).start()

    port = os.environ.get("PORT", "8000")
    api = subprocess.Popen([
        PYTHON, "-m", "uvicorn", "services.api.main:app",
        "--host", "0.0.0.0", "--port", port,
    ])

    def stop(signum, _frame):
        _stopping.set()
        api.send_signal(signum)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    code = api.wait()
    _stopping.set()
    log(f"API exited with {code}; stopping the service")
    for proc in _children.values():
        if proc.poll() is None:
            proc.terminate()
    sys.exit(code)


if __name__ == "__main__":
    main()
