"""Run the ingest service.

    python -m services.ingest              # port 8100
    python -m services.ingest --port 8200

Its own process and its own port, deliberately. The API serves people through
a browser and is CORS-scoped to the Vite dev origin; this serves hardware over
a device key and should be reachable from the field without opening the
portal's surface alongside it. They share `services.api.db`, so both talk to
the same database with the same session zone, and neither imports the other's
app.
"""
from __future__ import annotations

import argparse

import uvicorn


def main() -> None:
    parser = argparse.ArgumentParser(prog="services.ingest")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8100)
    parser.add_argument("--reload", action="store_true")
    args = parser.parse_args()

    uvicorn.run(
        "services.ingest.main:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
    )


if __name__ == "__main__":
    main()
