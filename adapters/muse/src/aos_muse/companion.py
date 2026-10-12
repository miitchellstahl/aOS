"""Optional at-least-once queue handoff, not a local agent or scheduler."""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
from aos_bridge.contracts import Contracts
from aos_cec.service import stop_child
from jsonschema import FormatChecker


class Journal:
    def __init__(self, directory: Path, session_id: str) -> None:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.path = directory / "handoffs.json"
        self.lock = (directory / "companion.lock").open("a+b")
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.session_id = session_id
            self.delivered: dict[str, str] = {}
            if self.path.exists():
                if self.path.stat().st_size > 256 * 1024:
                    raise ValueError("journal too large")
                value = json.loads(self.path.read_bytes())
                if (
                    not isinstance(value, dict)
                    or set(value) != {"session_id", "delivered"}
                    or value["session_id"] != session_id
                    or not isinstance(value["delivered"], dict)
                    or len(value["delivered"]) > 1000
                ):
                    raise ValueError("invalid journal or session changed")
                for key, digest in value["delivered"].items():
                    if not re.fullmatch(r"[1-9][0-9]{0,19}", key) or not isinstance(digest, str):
                        raise ValueError("invalid journal entry")
                    if not re.fullmatch(r"[a-f0-9]{64}", digest):
                        raise ValueError("invalid journal digest")
                self.delivered = value["delivered"]
        except BaseException:
            self.lock.close()
            raise

    def save(self) -> None:
        temporary = self.path.with_suffix(".tmp")
        data = json.dumps({"session_id": self.session_id, "delivered": self.delivered}).encode()
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(fd, "wb") as output:
                output.write(data)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self.path)
            directory = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            with contextlib.suppress(FileNotFoundError):
                temporary.unlink()

    def close(self) -> None:
        self.lock.close()


class Companion:
    def __init__(
        self,
        session_id: str,
        token_file: Path,
        state_dir: Path,
        bridge_url: str = "http://127.0.0.1:8080",
        executable: str = "musegadget",
        sdk_timeout_s: float = 30,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        parsed = urlsplit(bridge_url)
        if (
            parsed.scheme != "http"
            or parsed.hostname != "127.0.0.1"
            or parsed.username
            or parsed.password
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("bridge must be an uncredentialed IPv4 loopback HTTP origin")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", session_id):
            raise ValueError("a configured side-chat session ID is required")
        self.session_id = session_id
        self.token_file = token_file
        self.bridge_url = bridge_url.rstrip("/")
        self.executable = executable
        self.sdk_timeout_s = sdk_timeout_s
        self.contracts = Contracts()
        self.client = client or httpx.AsyncClient(
            timeout=5, trust_env=False, follow_redirects=False
        )
        self.journal = Journal(state_dir, session_id)

    async def request(self, method: str, path: str, body: Any = None) -> Any:
        with self.token_file.open("rb") as source:
            raw_token = source.read(4097)
        token = raw_token.decode("ascii").strip()
        if len(raw_token) > 4096 or not token or any(character.isspace() for character in token):
            raise ValueError("missing or invalid bridge credential")
        async with self.client.stream(
            method,
            self.bridge_url + path,
            json=body,
            headers={"Authorization": f"Bearer {token}"},
        ) as response:
            if response.status_code != 200:
                # never put response bodies, headers or URLs in logs.
                raise ValueError("bridge request rejected")
            content = bytearray()
            async for chunk in response.aiter_bytes():
                content.extend(chunk)
                if len(content) > 256 * 1024:
                    raise ValueError("bridge response too large")
        return json.loads(content)

    def validate_event(self, event: Any) -> dict[str, Any]:
        if not isinstance(event, dict) or set(event) != {"id", "kind", "created_at", "payload"}:
            raise ValueError("invalid queue envelope")
        if type(event["id"]) is not int or not 0 < event["id"] < 10**20:
            raise ValueError("invalid queue ID")
        if not isinstance(event["created_at"], str) or not FormatChecker().conforms(
            event["created_at"], "date-time"
        ):
            raise ValueError("invalid queue timestamp")
        self.contracts.validate_input(event["payload"])
        if event["kind"] != event["payload"]["kind"]:
            raise ValueError("queue kind mismatch")
        return event

    async def send(self, event: dict[str, Any]) -> None:
        # event ID is the downstream deduplication key, not a purported processing receipt.
        message = json.dumps({"binding": "aos-appliance-v1", "event": event}, separators=(",", ":"))
        process = await asyncio.create_subprocess_exec(
            self.executable,
            "send-user-msg",
            "--session-id",
            self.session_id,
            message,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            await asyncio.wait_for(process.wait(), self.sdk_timeout_s)
            if process.returncode != 0:
                raise ValueError("Muse SDK handoff failed")
        finally:
            await stop_child(process)

    async def cycle(self) -> int:
        # start at zero every cycle; a cursor is pagination, never acknowledgment.
        response = await self.request("GET", "/v1/events?since=0&limit=100")
        if not isinstance(response, dict) or set(response) != {"events", "next_cursor"}:
            raise ValueError("invalid queue response")
        events = response["events"]
        if not isinstance(events, list) or len(events) > 100:
            raise ValueError("invalid queue batch")
        events = [self.validate_event(raw) for raw in events]
        ids = [event["id"] for event in events]
        cursor = response["next_cursor"]
        if ids != sorted(set(ids)) or type(cursor) is not int or cursor < 0:
            raise ValueError("invalid queue ordering/cursor")
        if ids and cursor != ids[-1]:
            raise ValueError("invalid queue cursor")
        # a lost ack response can leave a journal entry for an already removed
        # event. Only IDs older than the first pending ID (or an empty queue)
        # are safe to retire; absence from a partial page alone is insufficient.
        obsolete = [key for key in self.journal.delivered if not ids or int(key) < ids[0]]
        if obsolete:
            for key in obsolete:
                self.journal.delivered.pop(key)
            self.journal.save()
        count = 0
        for event in events:
            key = str(event["id"])
            digest = hashlib.sha256(json.dumps(event, sort_keys=True).encode()).hexdigest()
            previous = self.journal.delivered.get(key)
            if previous is not None and previous != digest:
                raise ValueError("queue ID reused with different content")
            if previous is None:
                if len(self.journal.delivered) >= 1000:
                    raise ValueError("handoff journal full; investigate unacknowledged events")
                await self.send(event)
                self.journal.delivered[key] = digest
                # no bridge ack if durable transfer-of-responsibility record fails.
                try:
                    self.journal.save()
                except OSError:
                    self.journal.delivered.pop(key)
                    raise
            acknowledgment = await self.request("POST", "/v1/events/ack", {"ids": [event["id"]]})
            if (
                acknowledgment != {"acknowledged": [event["id"]]}
                or type(acknowledgment["acknowledged"][0]) is not int
            ):
                raise ValueError("invalid bridge acknowledgment")
            self.journal.delivered.pop(key)
            self.journal.save()
            count += 1
        return count

    async def close(self) -> None:
        await self.client.aclose()
        self.journal.close()
