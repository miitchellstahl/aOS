from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from aos_muse.companion import Companion

EVENT = {
    "id": 1,
    "kind": "overlay_dismissed",
    "created_at": "2026-10-12T19:00:00Z",
    "payload": {"kind": "overlay_dismissed", "client_event_id": "fake-one", "observed_revision": 0},
}


async def test_ack_response_loss_retired_without_resend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    token = tmp_path / "token"
    token.write_text("fake-local-token")
    pending = [EVENT]
    sent: list[Any] = []

    async def handle(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"events": pending, "next_cursor": 1 if pending else 0})
        pending.clear()
        raise httpx.ReadError("fake lost reply")

    async def send(self: Companion, event: dict[str, Any]) -> None:
        sent.append(event)

    monkeypatch.setattr(Companion, "send", send)
    companion = Companion(
        "fake-side",
        token,
        tmp_path / "journal",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handle)),
    )
    try:
        with pytest.raises(httpx.ReadError):
            await companion.cycle()
        assert companion.journal.delivered
        assert await companion.cycle() == 0
        assert len(sent) == 1
        assert not companion.journal.delivered
        assert json.loads(companion.journal.path.read_bytes())["delivered"] == {}
    finally:
        await companion.close()


@pytest.mark.parametrize("mode", ["redirect", "oversize", "bad_order", "bad_cursor"])
async def test_invalid_transport_or_queue_never_handed_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    token = tmp_path / "token"
    token.write_text("fake-local-token")

    async def handle(request: httpx.Request) -> httpx.Response:
        if mode == "redirect":
            return httpx.Response(302, headers={"location": "https://external.invalid"})
        if mode == "oversize":
            return httpx.Response(200, content=b"x" * (256 * 1024 + 1))
        if mode == "bad_order":
            return httpx.Response(200, json={"events": [EVENT, EVENT], "next_cursor": 1})
        return httpx.Response(200, json={"events": [EVENT], "next_cursor": True})

    async def send(self: Companion, event: dict[str, Any]) -> None:
        pytest.fail("invalid response was sent to SDK")

    monkeypatch.setattr(Companion, "send", send)
    companion = Companion(
        "fake-side",
        token,
        tmp_path / "journal",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handle), follow_redirects=False),
    )
    try:
        with pytest.raises(ValueError):
            await companion.cycle()
    finally:
        await companion.close()


async def test_sdk_timeout_terminates_only_owned_child_no_ack(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    token = tmp_path / "token"
    token.write_text("fake-local-token")
    calls: list[httpx.Request] = []

    async def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"events": [EVENT], "next_cursor": 1})

    class Process:
        returncode: int | None = None
        is_terminated = False
        finished = asyncio.Event()

        async def wait(self) -> int:
            await self.finished.wait()
            return 143

        def terminate(self) -> None:
            self.is_terminated = True
            self.returncode = 143
            self.finished.set()

    process = Process()

    async def spawn(*args: str, **kwargs: Any) -> Any:
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    companion = Companion(
        "fake-side",
        token,
        tmp_path / "journal",
        sdk_timeout_s=0.01,
        client=httpx.AsyncClient(transport=httpx.MockTransport(handle)),
    )
    try:
        with pytest.raises(TimeoutError):
            await companion.cycle()
        assert process.is_terminated
        assert [request.method for request in calls] == ["GET"]
        assert not companion.journal.delivered
    finally:
        await companion.close()
