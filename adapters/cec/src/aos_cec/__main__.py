"""aos-cec executable; no general-purpose input or shell command endpoint."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
import pwd
import signal
from pathlib import Path

from aos_cec.keyboard import EvdevKeyboard, Navigation
from aos_cec.service import CecClient, ControlServer


async def serve(arguments: argparse.Namespace) -> None:
    keyboard = EvdevKeyboard()
    navigation = Navigation(keyboard, arguments.logical_address)
    client = CecClient(navigation, arguments.cec_client, arguments.adapter, arguments.timeout)
    server = ControlServer(
        client, Path(arguments.socket), pwd.getpwnam(arguments.bridge_user).pw_uid
    )
    stopped = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stopped.set)
    try:
        await client.start_monitor()
        await server.start()
        while not stopped.is_set():
            try:
                await asyncio.wait_for(stopped.wait(), 1)
            except TimeoutError:
                async with client.lock:
                    await client.check()
    finally:
        await server.close()
        await client.close()
        navigation.release()
        keyboard.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="restricted aOS CEC service")
    parser.add_argument("--socket", default=os.environ.get("AOS_CEC_SOCKET", "/run/aos/cec.sock"))
    parser.add_argument("--cec-client", default="/usr/bin/cec-client")
    parser.add_argument("--adapter", default=os.environ.get("AOS_CEC_ADAPTER"))
    parser.add_argument("--bridge-user", default="aos-bridge")
    parser.add_argument(
        "--logical-address",
        type=int,
        choices=[4, 8, 11],
        default=int(os.environ.get("AOS_CEC_LOGICAL_ADDRESS", "4")),
    )
    parser.add_argument("--timeout", type=float, default=18)
    arguments = parser.parse_args()
    if not 0 < arguments.timeout <= 25:
        parser.error("timeout must be greater than zero and at most 25 seconds")
    with contextlib.suppress(KeyboardInterrupt):
        try:
            asyncio.run(serve(arguments))
        except (OSError, ValueError, KeyError, ImportError):
            parser.exit(
                1, "CEC service failed: check devices, socket, accounts and evdev dependency\n"
            )


if __name__ == "__main__":
    main()
