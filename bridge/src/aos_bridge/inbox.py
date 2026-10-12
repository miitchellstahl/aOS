"""Optional complete-file Muse ingress, confined to directory descriptors."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import stat
from collections.abc import Awaitable, Callable
from typing import Any
from uuid import uuid4

from aos_bridge.contracts import MAX_STATE_BYTES
from aos_bridge.owner import BridgeError
from aos_bridge.settings import Settings


class Inbox:
    def __init__(
        self,
        settings: Settings,
        ingest: Callable[[dict[str, Any], int, str], Awaitable[dict[str, Any]]],
    ) -> None:
        self.settings = settings
        self.ingest = ingest
        self._root: int | None = None
        self._destinations: dict[str, int] = {}
        self.last_error: str | None = None

    def prepare(self) -> None:
        directory = self.settings.inbox_dir
        if directory is None:
            raise ValueError("inbox is not configured")
        directory.mkdir(parents=True, exist_ok=True, mode=0o750)
        if directory.resolve() != directory.absolute():
            raise ValueError("inbox path must not contain symlinks")
        try:
            self._root = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            for name in ("processed", "rejected"):
                try:
                    os.mkdir(name, mode=0o750, dir_fd=self._root)
                except FileExistsError:
                    pass
                self._destinations[name] = os.open(
                    name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=self._root
                )
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        for fd in self._destinations.values():
            os.close(fd)
        self._destinations.clear()
        if self._root is not None:
            os.close(self._root)
            self._root = None

    def _trim(self, destination: int) -> None:
        with os.scandir(destination) as iterator:
            entries = [
                (entry.stat(follow_symlinks=False).st_mtime_ns, entry.name)
                for entry in iterator
                if not entry.is_dir(follow_symlinks=False)
            ]
        for _, name in sorted(entries)[: -self.settings.inbox_retention]:
            os.unlink(name, dir_fd=destination)
        os.fsync(destination)

    async def scan_once(self) -> int:
        if self._root is None:
            raise ValueError("inbox is not prepared")
        # incomplete .tmp files are never commands; only the final rename to .json is intake.
        names: list[str] = []
        with os.scandir(self._root) as iterator:
            for entry in iterator:
                if entry.name.endswith(".json"):
                    names.append(entry.name)
                if len(names) >= 100:
                    break
        count = 0
        for name in names:
            destination_name = "rejected"
            fd: int | None = None
            identity: tuple[int, int] | None = None
            try:
                if not re.fullmatch(r"[A-Za-z0-9_-]{1,120}\.json", name):
                    raise ValueError("invalid command filename")
                fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=self._root)
                metadata = os.fstat(fd)
                identity = (metadata.st_dev, metadata.st_ino)
                if (
                    not stat.S_ISREG(metadata.st_mode)
                    or metadata.st_nlink != 1
                    or metadata.st_size > MAX_STATE_BYTES
                ):
                    raise ValueError("invalid inbox file")
                with os.fdopen(fd, "rb", closefd=False) as source:
                    data = source.read(MAX_STATE_BYTES + 1)
                if len(data) > MAX_STATE_BYTES:
                    raise ValueError("oversized inbox command")
                command = json.loads(data)
                if not isinstance(command, dict) or set(command) != {"expected_revision", "event"}:
                    raise ValueError("invalid command wrapper")
                expected = command["expected_revision"]
                if (
                    type(expected) is not int
                    or expected < 0
                    or not isinstance(command["event"], dict)
                ):
                    raise ValueError("invalid command precondition/event")
                # verify that the producer has not replaced the file we opened.
                current = os.stat(name, dir_fd=self._root, follow_symlinks=False)
                if (current.st_dev, current.st_ino) != identity:
                    raise ValueError("command changed during intake")
                # stable across restart if receipt rename fails after a durable enqueue.
                source_id = hashlib.sha256(name.encode() + b"\0" + data).hexdigest()
                await self.ingest(command["event"], expected, source_id)
                destination_name = "processed"
                self.last_error = None
            except (ValueError, OSError, UnicodeError, RecursionError, BridgeError):
                self.last_error = "inbox command rejected"
            finally:
                if fd is not None:
                    os.close(fd)
            try:
                current = os.stat(name, dir_fd=self._root, follow_symlinks=False)
                if stat.S_ISDIR(current.st_mode):
                    continue
                if identity is not None and (current.st_dev, current.st_ino) != identity:
                    continue
                destination = self._destinations[destination_name]
                # preserve the producer's command ID for receipts without overwriting history.
                receipt = (
                    name
                    if re.fullmatch(r"[A-Za-z0-9_-]{1,120}\.json", name)
                    else f"{uuid4().hex}.json"
                )
                try:
                    os.stat(receipt, dir_fd=destination, follow_symlinks=False)
                except FileNotFoundError:
                    pass
                else:
                    receipt = f"{receipt.removesuffix('.json')}-{uuid4().hex}.json"
                os.rename(name, receipt, src_dir_fd=self._root, dst_dir_fd=destination)
                os.fsync(self._root)
                os.fsync(destination)
                self._trim(destination)
                count += 1
            except OSError:
                self.last_error = "inbox receipt storage unavailable"
        return count

    async def run(self) -> None:
        try:
            while True:
                try:
                    await self.scan_once()
                except OSError:
                    self.last_error = "inbox storage unavailable"
                await asyncio.sleep(self.settings.inbox_interval_s)
        finally:
            self.close()
