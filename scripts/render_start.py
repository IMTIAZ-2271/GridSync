"""Start a hosted GridSync service on Render, in one of two shapes.

    python -m scripts.render_start          # Render's start command, both shapes

**`RUN_API=off` -- the device service** (its own Render service and link, kept
awake by a pinger, standing in for the utilities' hardware):

* **ingest** on `0.0.0.0:$PORT` -- the public door for telemetry, and the
  process Render watches. If it exits, the service stops and Render restarts it.
* **head-end** -- `python -m simulator --mode headend --utility all`, polling
  that ingest over localhost, with the utilities' keys from the Render Secret
  File `/etc/secrets/source_keys.json`. Its state lives on the container's disk
  and is lost on every restart; that is survivable because it rekeys its live
  meters (services/ingest/commissioning.py).

**Default -- everything in one service**: the API on `$PORT`, ingest on
127.0.0.1:8100 beside it, and the head-end. For a host with room for only one
service.

In both shapes the jobs runner (`python -m services.jobs`) starts too unless
`RUN_JOBS=off`; scheduled billing stays off unless JOBS_BILLING_ENABLED is set.
A background process that exits is restarted after a pause rather than taking
the foreground one down with it.

This is a hosting arrangement, not an architecture: locally each of these is its
own process, started by hand, and nothing here changes how any of them behaves.
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
SOURCE_KEYS = Path(os.environ.get("HEADEND_SOURCE_KEYS", "/etc/secrets/source_keys.json"))
HEADEND_STATE = os.environ.get("HEADEND_STATE_DIR", "/tmp/headend_state")
RESTART_DELAY = 10

_stopping = threading.Event()
_children: dict[str, subprocess.Popen] = {}


def log(line: str) -> None:
    print(f"[render_start] {line}", flush=True)


def _switch(name: str, default: str) -> bool:
    return os.environ.get(name, default).strip().lower() not in ("off", "0", "false", "no")


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


def ingest_argv(host: str, port: str) -> list[str]:
    return [PYTHON, "-m", "services.ingest", "--host", host, "--port", port]


def headend_argv(ingest_port: str) -> list[str] | None:
    if not SOURCE_KEYS.exists():
        log(f"no head-end keys at {SOURCE_KEYS}; head-end not started")
        return None
    return [
        PYTHON, "-m", "simulator", "--mode", "headend", "--utility", "all",
        "--ingest", f"http://127.0.0.1:{ingest_port}",
        "--source-keys", str(SOURCE_KEYS),
        "--state-dir", HEADEND_STATE,
    ]


def main() -> None:
    port = os.environ.get("PORT", "8000")
    background: list[tuple[str, list[str]]] = []

    if _switch("RUN_API", "on"):
        ingest_port = "8100"
        background.append(("ingest", ingest_argv("127.0.0.1", ingest_port)))
        foreground_name = "API"
        foreground = [PYTHON, "-m", "uvicorn", "services.api.main:app",
                      "--host", "0.0.0.0", "--port", port]
    else:
        ingest_port = port
        foreground_name = "ingest"
        foreground = ingest_argv("0.0.0.0", port)

    headend = headend_argv(ingest_port)
    if headend:
        background.append(("head-end", headend))
    if _switch("RUN_JOBS", "on"):
        background.append(("jobs", [PYTHON, "-m", "services.jobs"]))

    log(f"starting {foreground_name} on port {port}")
    main_proc = subprocess.Popen(foreground)
    for name, argv in background:
        threading.Thread(target=supervise, args=(name, argv), daemon=True).start()

    def stop(signum, _frame):
        _stopping.set()
        main_proc.send_signal(signum)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    code = main_proc.wait()
    _stopping.set()
    log(f"{foreground_name} exited with {code}; stopping the service")
    for proc in _children.values():
        if proc.poll() is None:
            proc.terminate()
    sys.exit(code)


if __name__ == "__main__":
    main()
