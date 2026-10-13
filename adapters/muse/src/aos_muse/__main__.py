"""Optional Muse companion, enabled only after explicit pairing and queue ownership."""

from __future__ import annotations

import argparse
import asyncio
import os
import signal
from pathlib import Path

import httpx

from aos_muse.companion import Companion


async def serve(arguments: argparse.Namespace) -> None:
    companion = Companion(
        arguments.session_id,
        Path(arguments.token_file),
        Path(arguments.state_dir),
        arguments.bridge_url,
        arguments.musegadget,
        arguments.sdk_timeout,
    )
    stopped = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stopped.set)
    delay = arguments.poll_interval
    try:
        while not stopped.is_set():
            try:
                await companion.cycle()
                delay = arguments.poll_interval
            except (OSError, ValueError, RecursionError, TimeoutError, httpx.HTTPError):
                # no raw SDK output / HTTP errors: both can contain credentials.
                print("Muse handoff unavailable; queue remains pending", flush=True)
                delay = min(max(delay * 2, 1), 60)
            try:
                await asyncio.wait_for(stopped.wait(), delay)
            except TimeoutError:
                pass
    finally:
        await companion.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="handoff aOS intents to a configured Muse side chat"
    )
    parser.add_argument("--session-id", default=os.environ.get("AOS_MUSE_SESSION_ID"))
    parser.add_argument(
        "--token-file", default=os.environ.get("AOS_MUSE_TOKEN_FILE", "/etc/aos/muse.token")
    )
    parser.add_argument(
        "--state-dir", default=os.environ.get("AOS_MUSE_STATE_DIR", "/var/lib/aos-muse")
    )
    parser.add_argument(
        "--bridge-url", default=os.environ.get("AOS_BRIDGE_URL", "http://127.0.0.1:8080")
    )
    parser.add_argument("--musegadget", default=os.environ.get("AOS_MUSEGADGET", "musegadget"))
    parser.add_argument("--poll-interval", type=float, default=2)
    parser.add_argument("--sdk-timeout", type=float, default=30)
    arguments = parser.parse_args()
    if not arguments.session_id:
        parser.error("--session-id or AOS_MUSE_SESSION_ID must name the paired side chat")
    if not 0.1 <= arguments.poll_interval <= 60 or not 0 < arguments.sdk_timeout <= 120:
        parser.error("poll interval or SDK timeout out of range")
    try:
        asyncio.run(serve(arguments))
    except KeyboardInterrupt:
        pass
    except (OSError, ValueError):
        parser.exit(1, "Muse companion startup failed; check configuration and private journal\n")


if __name__ == "__main__":
    main()
