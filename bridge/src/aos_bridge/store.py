"""Bounded, durable JSON documents and single-process state-directory ownership."""

from __future__ import annotations

import fcntl
import json
import os
import stat
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any


class StorageError(Exception):
    """A durable operation failed; diagnostics never contain document contents."""


def read_regular(path: Path, limit: int) -> bytes:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError("not a regular file")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            data = stream.read(limit + 1)
        if len(data) > limit:
            raise ValueError("file exceeds size limit")
        return data
    finally:
        os.close(fd)


class AtomicStore:
    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self._lock_fd: int | None = None
        self.poisoned = False

    def acquire(self) -> None:
        try:
            self.directory.mkdir(parents=True, exist_ok=True, mode=0o750)
            if self.directory.is_symlink():
                raise OSError("symlink state directory")
            fd = os.open(
                self.directory / ".bridge.lock",
                os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW,
                0o600,
            )
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                os.close(fd)
                raise StorageError("state directory already owned by another bridge") from None
            self._lock_fd = fd
        except OSError:
            raise StorageError("cannot acquire state directory") from None

    def close(self) -> None:
        if self._lock_fd is not None:
            os.close(self._lock_fd)
            self._lock_fd = None

    def _sync_directory(self) -> None:
        fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def _stage(self, data: bytes, mode: int = 0o600) -> Path:
        fd, filename = tempfile.mkstemp(prefix=".aos-", dir=self.directory)
        path = Path(filename)
        try:
            with os.fdopen(fd, "wb") as stream:
                os.fchmod(stream.fileno(), mode)
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        return path

    def write(self, name: str, value: dict[str, Any]) -> None:
        """Commit primary and validated recovery copy, rolling back failed fsyncs.

        A process crash may leave either old or new documents, both valid. Failed
        rollback poisons this owner: it must not publish or accept further writes.
        """
        if self.poisoned:
            raise StorageError("storage requires restart and recovery")
        primary = self.directory / name
        recovery = self.directory / f"{name}.last-good"
        staged: list[Path] = []
        rollback: dict[Path, Path | None] = {}
        replacements: dict[Path, Path] = {}
        changed: list[Path] = []
        # display can read boot snapshots through a shared read-only group, not the queue.
        mode = 0o640 if name == "dashboard.json" else 0o600
        try:
            data = json.dumps(
                value, ensure_ascii=False, allow_nan=False, separators=(",", ":")
            ).encode()
            for path in (primary, recovery):
                try:
                    previous = read_regular(path, 8 * 1024 * 1024)
                except FileNotFoundError:
                    rollback[path] = None
                else:
                    # prepare rollback before touching either file: full disk must not
                    # require another successful allocation or file fsync to restore it.
                    rollback_path = self._stage(previous, mode)
                    rollback[path] = rollback_path
                    staged.append(rollback_path)
                replacements[path] = self._stage(data, mode)
                staged.append(replacements[path])
            # primary is the commit record; backup never advances ahead of it.
            for path in (primary, recovery):
                os.replace(replacements[path], path)
                changed.append(path)
                self._sync_directory()
        except (OSError, ValueError):
            try:
                for path in reversed(changed):
                    previous_path = rollback[path]
                    if previous_path is None:
                        path.unlink(missing_ok=True)
                    else:
                        os.replace(previous_path, path)
                if changed:
                    self._sync_directory()
            except OSError:
                self.poisoned = True
            raise StorageError("durable file commit failed") from None
        finally:
            for path in staged:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    # orphaned temporary files are not a commit record or a recovery source.
                    pass

    def load(
        self,
        name: str,
        validate: Callable[[object], None],
        initial: Callable[[], dict[str, Any]],
        limit: int,
    ) -> dict[str, Any]:
        primary = self.directory / name
        recovery = self.directory / f"{name}.last-good"
        exists = False
        for path in (primary, recovery):
            try:
                data = read_regular(path, limit)
                exists = True
                value = json.loads(data)
                validate(value)
                if not isinstance(value, dict):
                    raise ValueError("expected document")
                if path == recovery:
                    self.write(name, value)
                else:
                    try:
                        backup = json.loads(read_regular(recovery, limit))
                        validate(backup)
                        needs_backup = backup != value
                    except (OSError, ValueError, UnicodeError, RecursionError):
                        needs_backup = True
                    if needs_backup:
                        self.write(name, value)
                return value
            except FileNotFoundError:
                continue
            except (OSError, ValueError, UnicodeError, RecursionError):
                exists = True
        if exists:
            raise StorageError("no valid last-good document")
        value = initial()
        validate(value)
        self.write(name, value)
        return value
