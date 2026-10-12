"""Serial libCEC ownership and bounded, peer-restricted newline JSON IPC."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import socket
import stat
import struct
from pathlib import Path
from typing import Literal, TypeGuard

from aos_cec.keyboard import Navigation

Action = Literal["power_on", "power_off", "active_source"]
ACTIONS = {"power_on": "on 0", "power_off": "standby 0", "active_source": "as"}
MAX_OUTPUT = 65536


def is_action(value: object) -> TypeGuard[Action]:
    return isinstance(value, str) and value in ACTIONS


async def stop_child(process: asyncio.subprocess.Process) -> None:
    if process.returncode is None:
        with contextlib.suppress(ProcessLookupError):
            process.terminate()
        try:
            await asyncio.wait_for(process.wait(), 1)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            await process.wait()


class CecClient:
    def __init__(
        self,
        navigation: Navigation,
        executable: str = "/usr/bin/cec-client",
        adapter: str | None = None,
        timeout_s: float = 18,
    ) -> None:
        self.navigation = navigation
        self.executable = executable
        self.adapter = adapter
        self.timeout_s = timeout_s
        self.lock = asyncio.Lock()
        self.process: asyncio.subprocess.Process | None = None
        self.monitor_task: asyncio.Task[None] | None = None
        self.is_closed = False

    async def spawn(self) -> asyncio.subprocess.Process:
        # -s suppresses libCEC's implicit startup wake / exit standby. Keeping stdin
        # open without a command gives a passive registered-player monitor.
        arguments = [self.executable, "-s", "-t", "p", "-d", "31", "-o", "aOS", "-aw", "0"]
        if self.adapter:
            arguments.append(self.adapter)
        return await asyncio.create_subprocess_exec(
            *arguments,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            limit=4096,
        )

    async def start_monitor(self) -> None:
        if self.is_closed:
            return
        self.process = await self.spawn()
        self.monitor_task = asyncio.create_task(self.monitor(self.process))

    async def monitor(self, process: asyncio.subprocess.Process) -> None:
        assert process.stdout is not None
        try:
            while True:
                try:
                    line = await asyncio.wait_for(process.stdout.readline(), 0.1)
                except TimeoutError:
                    self.navigation.expire()
                    continue
                if not line:
                    return
                self.navigation.feed(line.decode("utf-8", errors="replace"))
                self.navigation.expire()
        finally:
            self.navigation.release()

    async def pause_monitor(self) -> None:
        if self.monitor_task is not None:
            self.monitor_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.monitor_task
            self.monitor_task = None
        if self.process is not None:
            await stop_child(self.process)
            self.process = None
        self.navigation.release()

    async def command(self, command: str) -> str:
        process = await self.spawn()
        assert process.stdin is not None and process.stdout is not None
        try:
            process.stdin.write(command.encode("ascii") + b"\n")
            await process.stdin.drain()
            process.stdin.close()
            output = bytearray()
            while chunk := await process.stdout.read(4096):
                output.extend(chunk)
                if len(output) > MAX_OUTPUT:
                    raise ValueError("CEC output too large")
            await process.wait()
            text = output.decode("utf-8", errors="replace")
            if process.returncode != 0 or re.search(r"\bERROR\b|failed|unable to open", text, re.I):
                raise ValueError("CEC command failed")
            return text
        finally:
            await stop_child(process)

    async def execute(self, action: Action) -> None:
        async with self.lock:
            await self.pause_monitor()
            try:
                async with asyncio.timeout(self.timeout_s):
                    text = await self.command(ACTIONS[action])
                    # cec-client ignores some libCEC boolean results: a zero exit
                    # alone is NOT success. Require action-specific on-wire evidence.
                    opcode = {"power_on": "04", "power_off": "36", "active_source": "82"}[action]
                    destination = "f" if action == "active_source" else "0"
                    if not re.search(rf"<<\s+[48b]{destination}:{opcode}(?::|\s|$)", text, re.I):
                        raise ValueError("CEC transmission not observed")
                    if action != "active_source":
                        status = await self.command("pow 0")
                        expected = "on" if action == "power_on" else "standby"
                        if not re.search(rf"^power status:\s*{expected}\s*$", status, re.I | re.M):
                            raise ValueError("TV power state not confirmed")
            finally:
                # no overlapping libCEC clients on the same adapter.
                await self.start_monitor()

    async def check(self) -> None:
        if self.monitor_task is None or self.monitor_task.done():
            await self.pause_monitor()
            await self.start_monitor()

    async def close(self) -> None:
        self.is_closed = True
        async with self.lock:
            await self.pause_monitor()


class ControlServer:
    def __init__(self, client: CecClient, path: Path, allowed_uid: int) -> None:
        self.client = client
        self.path = path
        self.allowed_uid = allowed_uid
        self.server: asyncio.Server | None = None
        self.identity: tuple[int, int] | None = None
        self.tasks: set[asyncio.Task[None]] = set()

    async def start(self) -> None:
        # never unlink an arbitrary preexisting socket/file owned by another service.
        if self.path.exists() or self.path.is_symlink():
            raise ValueError("CEC socket already exists; inspect stale service before removal")
        old_umask = os.umask(0o117)
        try:
            self.server = await asyncio.start_unix_server(
                self.accept, path=self.path, limit=4096, backlog=8
            )
        finally:
            os.umask(old_umask)
        os.chmod(self.path, 0o660)
        metadata = self.path.stat()
        self.identity = metadata.st_dev, metadata.st_ino

    def accept(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if len(self.tasks) >= 8:
            writer.close()
            return
        task = asyncio.create_task(self.handle(reader, writer))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        response: dict[str, bool | str] = {"ok": False, "error": "CEC request rejected"}
        try:
            peer = writer.get_extra_info("socket")
            _, uid, _ = struct.unpack(
                "3i", peer.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
            )
            if uid != self.allowed_uid:
                raise ValueError("unauthorized peer")
            line = await asyncio.wait_for(reader.readline(), 1)
            if len(line) > 4096 or not line.endswith(b"\n"):
                raise ValueError("invalid framing")
            request = json.loads(line)
            if not isinstance(request, dict) or set(request) != {"action"}:
                raise ValueError("invalid command")
            action = request["action"]
            if not is_action(action):
                raise ValueError("invalid action")
            # bounded wait includes other callers; no unbounded socket work queue.
            async with asyncio.timeout(self.client.timeout_s + 2):
                await self.client.execute(action)
            response = {"ok": True}
        except (OSError, ValueError, UnicodeError, RecursionError, TimeoutError):
            pass
        finally:
            with contextlib.suppress(OSError, RuntimeError, TimeoutError):
                writer.write(json.dumps(response, separators=(",", ":")).encode() + b"\n")
                await asyncio.wait_for(writer.drain(), 0.25)
            writer.close()
            with contextlib.suppress(OSError, TimeoutError):
                await asyncio.wait_for(writer.wait_closed(), 0.25)

    async def close(self) -> None:
        if self.server:
            self.server.close()
            await self.server.wait_closed()
        for task in list(self.tasks):
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        with contextlib.suppress(FileNotFoundError):
            metadata = self.path.lstat()
            if (
                stat.S_ISSOCK(metadata.st_mode)
                and (metadata.st_dev, metadata.st_ino) == self.identity
            ):
                self.path.unlink()
