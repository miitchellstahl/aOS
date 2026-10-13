"""Loopback-only, single-worker bridge executable."""

from __future__ import annotations

import argparse
import os

import uvicorn

from aos_bridge.app import create_app
from aos_bridge.settings import Settings


def main() -> None:
    parser = argparse.ArgumentParser(description="aOS local bridge")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--workers", type=int, choices=[1], default=1)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("port must be between 1 and 65535")
    for variable in ("WEB_CONCURRENCY", "UVICORN_WORKERS"):
        if os.environ.get(variable, "1") != "1":
            parser.error("the bridge requires exactly one worker")
    uvicorn.run(create_app(Settings.from_env()), host="127.0.0.1", port=args.port, workers=1)


if __name__ == "__main__":
    main()
