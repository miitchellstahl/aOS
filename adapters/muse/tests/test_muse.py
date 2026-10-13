from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from aos_muse.companion import Companion, Journal

EVENT = {
    "id": 1,
    "kind": "todo_toggle",
    "created_at": "2026-10-12T19:00:00Z",
    "payload": {
        "kind": "todo_toggle",
        "client_event_id": "one",
        "observed_revision": 0,
        "section_id": "todos",
        "target_id": "t1",
        "done": True,
    },
}


class Backend:
    def __init__(self) -> None:
        self.events = [EVENT.copy()]
        self.requests: list[httpx.Request] = []
        self.ack_failure = False

    async def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.method == "GET":
            assert request.url.params["since"] == "0"
            return httpx.Response(200, json={"events": self.events, "next_cursor": 1})
        assert json.loads(request.content) == {"ids": [1]}
        if self.ack_failure:
            return httpx.Response(503)
        self.events = []
        return httpx.Response(200, json={"acknowledged": [1]})


class FakeCompanion(Companion):
    def __init__(self, directory: Path, backend: Backend) -> None:
        token = directory / "token"
        token.write_text("secret-one")
        super().__init__(
            "side_chat",
            token,
            directory / "journal",
            client=httpx.AsyncClient(transport=httpx.MockTransport(backend.handle)),
        )
        self.sent: list[dict[str, Any]] = []
        self.failure = False

    async def send(self, event: dict[str, Any]) -> None:
        if self.failure:
            raise ValueError("sdk failed")
        self.sent.append(event)


async def test_sdk_success_then_durable_journal_then_ack(tmp_path: Path) -> None:
    backend = Backend()
    companion = FakeCompanion(tmp_path, backend)
    try:
        assert await companion.cycle() == 1
        assert companion.sent == [EVENT]
        assert [r.method for r in backend.requests] == ["GET", "POST"]
        assert json.loads(companion.journal.path.read_bytes())["delivered"] == {}
        assert companion.journal.path.stat().st_mode & 0o777 == 0o600
    finally:
        await companion.close()


async def test_failed_sdk_leaves_queue_and_no_ack(tmp_path: Path) -> None:
    backend = Backend()
    companion = FakeCompanion(tmp_path, backend)
    companion.failure = True
    try:
        with pytest.raises(ValueError):
            await companion.cycle()
        assert len(backend.requests) == 1
        assert backend.events and not companion.journal.delivered
    finally:
        await companion.close()


async def test_ack_retry_and_restart_do_not_resend_handed_off_event(tmp_path: Path) -> None:
    backend = Backend()
    backend.ack_failure = True
    companion = FakeCompanion(tmp_path, backend)
    with pytest.raises(ValueError):
        await companion.cycle()
    assert companion.sent == [EVENT]
    await companion.close()
    companion = FakeCompanion(tmp_path, backend)
    backend.ack_failure = False
    try:
        assert await companion.cycle() == 1
        assert not companion.sent
    finally:
        await companion.close()


async def test_storage_failure_never_acknowledges(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = Backend()
    companion = FakeCompanion(tmp_path, backend)

    def fail() -> None:
        raise OSError("disk full")

    monkeypatch.setattr(companion.journal, "save", fail)
    try:
        for _ in range(2):
            with pytest.raises(OSError):
                await companion.cycle()
        assert [r.method for r in backend.requests] == ["GET", "GET"]
        assert len(companion.sent) == 2  # unavoidable duplicate after failed durable handoff.
        assert not companion.journal.delivered
    finally:
        await companion.close()


async def test_token_rotation_and_revocation(tmp_path: Path) -> None:
    backend = Backend()
    companion = FakeCompanion(tmp_path, backend)
    try:
        await companion.cycle()
        companion.token_file.write_text("secret-two")
        await companion.cycle()
        assert backend.requests[-1].headers["authorization"] == "Bearer secret-two"
        companion.token_file.write_text("")
        with pytest.raises(ValueError):
            await companion.cycle()
    finally:
        await companion.close()


def test_single_consumer_lock_invalid_journal_and_session_change(tmp_path: Path) -> None:
    journal = Journal(tmp_path, "side")
    journal.save()
    try:
        with pytest.raises(BlockingIOError):
            Journal(tmp_path, "side")
    finally:
        journal.close()
    with pytest.raises(ValueError):
        Journal(tmp_path, "other")
    (tmp_path / "handoffs.json").write_text("{")
    with pytest.raises(ValueError):
        Journal(tmp_path, "side")


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:8080",
        "https://127.0.0.1:8080",
        "http://192.168.1.1",
        "http://user@127.0.0.1",
        "http://127.0.0.1/v1",
        "http://127.0.0.1?token=secret",
    ],
)
def test_non_loopback_or_credentialed_urls_rejected(tmp_path: Path, url: str) -> None:
    with pytest.raises(ValueError):
        Companion("side", tmp_path / "token", tmp_path / "state", bridge_url=url)


async def test_send_exec_and_no_stdout_receipt_parsing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, ...]] = []
    backend = Backend()
    companion = FakeCompanion(tmp_path, backend)

    class Process:
        returncode = 0

        async def wait(self) -> int:
            return 0

    async def spawn(*args: str, **kwargs: Any) -> Any:
        calls.append(args)
        assert kwargs["stdout"] == asyncio.subprocess.DEVNULL
        return Process()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    try:
        await Companion.send(companion, EVENT)
        assert calls[0][:4] == ("musegadget", "send-user-msg", "--session-id", "side_chat")
        assert json.loads(calls[0][4])["event"] == EVENT
    finally:
        await companion.close()


@pytest.mark.parametrize(
    "field,value",
    [
        ("id", True),
        ("id", 0),
        ("kind", "remote_key"),
        ("created_at", "not a date"),
        ("payload", {}),
    ],
)
async def test_invalid_event_never_sent(tmp_path: Path, field: str, value: Any) -> None:
    backend = Backend()
    backend.events = [{**EVENT, field: value}]
    companion = FakeCompanion(tmp_path, backend)
    try:
        with pytest.raises(ValueError):
            await companion.cycle()
        assert not companion.sent
        assert len(backend.requests) == 1
    finally:
        await companion.close()
