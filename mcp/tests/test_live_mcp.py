"""Initialize/list/call using real FastMCP Streamable HTTP, never handwritten RPC."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from aos_bridge.owner import BridgeError
from aos_bridge.store import StorageError
from aos_mcp.reference_client import run as reference_run
from aos_mcp.settings import Credential
from conftest import EXTERNAL, LOCAL, Live
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
from fastmcp.exceptions import ToolError

TOOLS = {
    "get_schema",
    "get_dashboard",
    "get_status",
    "update_dashboard",
    "apply_dashboard_patch",
    "show_notification",
    "get_pending_events",
    "acknowledge_events",
    "device_power",
    "device_activate_source",
}


def client(live: Live, token: str | None = EXTERNAL) -> Client[Any]:
    return Client(StreamableHttpTransport(live.mcp_url, auth=token), timeout=5)


async def call(
    mcp: Client[Any], name: str, arguments: dict[str, Any] | None = None
) -> dict[str, Any]:
    result = await mcp.call_tool(name, arguments or {})
    assert not result.is_error
    assert isinstance(result.data, dict)
    return result.data


async def error(mcp: Client[Any], name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    result = await mcp.call_tool(name, arguments, raise_on_error=False)
    assert result.is_error
    content = result.content[0]
    assert content.type == "text"
    return json.loads(content.text)


async def event(lines: AsyncIterator[str]) -> dict[str, Any]:
    async with asyncio.timeout(5):
        async for line in lines:
            if line.startswith("data: "):
                return json.loads(line[6:])
    raise AssertionError("missing SSE frame")


async def next_kind(lines: AsyncIterator[str], kind: str) -> dict[str, Any]:
    for _ in range(100):
        value = await event(lines)
        if value["type"] == kind:
            return value
    raise AssertionError("missing matching SSE frame")


async def test_tools_live_bridge_sse_queue_and_fake_device(
    live: Live,
    bridge_http: httpx.AsyncClient,
) -> None:
    async with client(live) as mcp:
        assert {tool.name for tool in await mcp.list_tools()} == TOOLS
        assert isinstance(mcp.transport, StreamableHttpTransport)
        assert mcp.transport.get_session_id() is None
        schema = await call(mcp, "get_schema")
        assert schema["ag_ui_version"] == "1.0"
        assert schema["binding"] == "aos-appliance-v1"
        assert (
            schema["state_schema"] == (await bridge_http.get("/v1/schema")).json()["state_schema"]
        )
        state = await call(mcp, "get_dashboard")
        assert state["revision"] == 0
        status = await call(mcp, "get_status")
        assert status["revision"] == 0
        async with bridge_http.stream("GET", "/v1/stream") as stream:
            lines = stream.aiter_lines()
            initial = await event(lines)
            assert initial == {"type": "STATE_SNAPSHOT", "snapshot": state}
            payload = {k: v for k, v in state.items() if k not in {"revision", "updated_at"}}
            status_index = next(
                i for i, s in enumerate(payload["sections"]) if s["type"] == "status"
            )
            payload["sections"][status_index]["note"] = "MCP replacement"
            updated = await call(
                mcp, "update_dashboard", {"payload": payload, "expected_revision": 0}
            )
            assert updated["revision"] == 1
            assert (await next_kind(lines, "STATE_DELTA"))["delta"][0]["value"] == 0
            patched = await call(
                mcp,
                "apply_dashboard_patch",
                {
                    "delta": [
                        {
                            "op": "replace",
                            "path": f"/sections/{status_index}/note",
                            "value": "Patched",
                        }
                    ],
                    "expected_revision": 1,
                },
            )
            assert patched["revision"] == 2
            assert patched["sections"][status_index]["note"] == "Patched"
            assert (await next_kind(lines, "STATE_DELTA"))["delta"][0]["value"] == 1
            await call(mcp, "show_notification", {"message": "Hello", "duration_s": 5})
            while True:
                overlay = await next_kind(lines, "CUSTOM")
                if overlay["name"] == "aos.overlay":
                    break
            assert overlay["value"]["message"] == "Hello"
            assert overlay["value"]["duration_s"] == 5
            assert len(overlay["value"]["overlay_id"]) == 32
            assert (await call(mcp, "get_dashboard"))["updated_at"] == patched["updated_at"]
        todo_index = next(i for i, s in enumerate(patched["sections"]) if s["type"] == "todos")
        todo = patched["sections"][todo_index]
        intent = {
            "kind": "todo_toggle",
            "client_event_id": "mcp-test-intent",
            "observed_revision": 2,
            "section_id": todo["id"],
            "target_id": todo["items"][0]["id"],
            "done": True,
        }
        submitted = await bridge_http.post("/v1/input", json=intent)
        assert submitted.status_code == 200
        event_id = submitted.json()["id"]
        page = await call(mcp, "get_pending_events")
        assert page["events"][0]["payload"] == intent
        assert (await call(mcp, "get_pending_events", {"since_id": event_id}))["events"] == []
        assert (await call(mcp, "get_pending_events"))["events"][0]["id"] == event_id
        await call(
            mcp,
            "apply_dashboard_patch",
            {
                "delta": [
                    {"op": "replace", "path": f"/sections/{todo_index}/items/0/done", "value": True}
                ],
                "expected_revision": 2,
            },
        )
        ack = await call(mcp, "acknowledge_events", {"ids": [event_id]})
        assert ack == {"acknowledged": [event_id]}
        assert await call(mcp, "acknowledge_events", {"ids": [event_id]}) == ack
        assert (await call(mcp, "get_pending_events"))["events"] == []
        assert await call(mcp, "device_power", {"state": "on"}) == {"ok": True}
        assert await call(mcp, "device_power", {"state": "off"}) == {"ok": True}
        assert await call(mcp, "device_activate_source") == {"ok": True}
        assert live.actions == ["power_on", "power_off", "active_source"]


@pytest.mark.parametrize("token", [None, "wrong-token", LOCAL])
async def test_real_client_rejects_missing_bad_and_local_tokens(
    live: Live, token: str | None
) -> None:
    with pytest.raises(Exception, match="401|Unauthorized|initialize|connect"):
        async with client(live, token) as mcp:
            await mcp.list_tools()
    async with httpx.AsyncClient(trust_env=False) as http:
        headers = {} if token is None else {"Authorization": f"Bearer {token}"}
        response = await http.get(live.mcp_url, headers=headers)
        assert response.status_code == 401
        assert token is None or token not in response.text


async def test_rotation_invalidates_already_initialized_client(live: Live) -> None:
    # the SDK reports the failed request and repeats its HTTP error during cleanup.
    with pytest.raises(httpx.HTTPStatusError, match="401"):
        async with client(live) as mcp:
            await mcp.list_tools()
            live.mcp_file.write_text("rotated-external-capability")
            with pytest.raises(Exception, match="401|Unauthorized|closed|terminated"):
                await mcp.list_tools()
    with pytest.raises(httpx.HTTPStatusError, match="401"):
        async with client(live, "rotated-external-capability") as rotated:
            assert (await call(rotated, "get_dashboard"))["revision"] == 0
            live.mcp_file.unlink()
            with pytest.raises(Exception, match="401|Unauthorized|closed|terminated"):
                await rotated.call_tool("get_status")
    async with httpx.AsyncClient(trust_env=False) as http:
        assert (
            await http.get(
                live.mcp_url, headers={"Authorization": "Bearer rotated-external-capability"}
            )
        ).status_code == 401


async def test_bridge_rotation_is_read_on_every_tool_request(live: Live) -> None:
    with pytest.raises(httpx.HTTPStatusError, match="401"):
        async with client(live) as mcp:
            await call(mcp, "get_dashboard")
            live.bridge_file.write_text("rotated-local-bridge-capability")
            assert (await call(mcp, "get_status"))["revision"] == 0
            assert (await call(mcp, "get_dashboard"))["revision"] == 0
            live.bridge_file.write_text(EXTERNAL)
            with pytest.raises(Exception, match="401|Unauthorized|closed|terminated"):
                await mcp.list_tools()


async def test_safe_validation_conflict_storage_and_absent_device_errors(
    live: Live,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    async with client(live) as mcp:
        invalid = await error(mcp, "update_dashboard", {"payload": {}, "expected_revision": 0})
        assert invalid["status"] == 422 and invalid["rule"] == "required"
        protected = await error(
            mcp,
            "apply_dashboard_patch",
            {
                "delta": [{"op": "replace", "path": "/revision", "value": 1}],
                "expected_revision": 0,
            },
        )
        assert protected["status"] == 422 and protected["pointer"] == "/delta/0"
        stale = await error(mcp, "apply_dashboard_patch", {"delta": [], "expected_revision": 99})
        assert stale == {
            "status": 409,
            "detail": "bridge state conflict",
            "reason": "stale expected_revision",
        }
        secret = "private-request-body-do-not-log"
        bad_args = await error(mcp, "device_power", {"state": secret})
        assert bad_args == {"status": 422, "detail": "invalid tool arguments"}
        unknown = await error(mcp, secret, {})
        assert unknown == {"status": 422, "detail": "unknown tool"}
        live.cec_socket.unlink()
        device = await error(mcp, "device_power", {"state": "on"})
        assert device["status"] == 503
        assert device["reason"] == "CEC service unavailable or invalid response"

        def fail_write(*args: Any, **kwargs: Any) -> None:
            raise StorageError("sensitive filesystem detail")

        owner = live.bridge_app.state.owner  # type: ignore[attr-defined]
        monkeypatch.setattr(owner.store, "write", fail_write)
        storage = await error(mcp, "apply_dashboard_patch", {"delta": [], "expected_revision": 0})
        assert storage["status"] == 503 and storage["reason"] == "durable storage unavailable"
        assert (await call(mcp, "get_dashboard"))["revision"] == 0
        for private in (secret, EXTERNAL, LOCAL, "sensitive filesystem detail"):
            assert private not in caplog.text
            assert private not in json.dumps(
                [invalid, protected, stale, bad_args, unknown, device, storage]
            )


@pytest.mark.parametrize(
    "headers,status",
    [
        ({"Host": "hostile.example"}, 421),
        ({"Host": "127.0.0.1.evil:8090"}, 421),
        ({"Origin": "https://hostile.example"}, 403),
        ({"Origin": "http://localhost:8090"}, 403),
        ({"Origin": "null"}, 403),
    ],
)
async def test_http_rejects_host_and_all_browser_origins(
    live: Live, headers: dict[str, str], status: int
) -> None:
    async with httpx.AsyncClient(trust_env=False) as http:
        response = await http.get(
            live.mcp_url, headers={"Authorization": f"Bearer {EXTERNAL}", **headers}
        )
        assert response.status_code == status
        assert EXTERNAL not in response.text


async def test_exact_tunnel_host_allowed_with_real_client(live: Live) -> None:
    transport = StreamableHttpTransport(
        live.mcp_url, auth=EXTERNAL, headers={"Host": "tunnel.example"}
    )
    async with Client(transport, timeout=5) as mcp:
        assert {tool.name for tool in await mcp.list_tools()} == TOOLS


async def test_mcp_and_bridge_request_size_limits(live: Live) -> None:
    async with httpx.AsyncClient(trust_env=False) as http:
        headers = {"Authorization": f"Bearer {EXTERNAL}", "Content-Type": "application/json"}
        response = await http.post(live.mcp_url, headers=headers, content=b"x" * (256 * 1024 + 1))
        assert response.status_code == 413

        async def chunks() -> AsyncIterator[bytes]:
            yield b"x" * (128 * 1024)
            yield b"x" * (128 * 1024 + 1)

        response = await http.post(live.mcp_url, headers=headers, content=chunks())
        assert response.status_code == 413
    async with client(live) as mcp:
        excessive = await error(
            mcp,
            "update_dashboard",
            {
                "payload": {"data": "x" * (128 * 1024)},
                "expected_revision": 0,
            },
        )
        assert excessive["status"] == 413
        assert (await call(mcp, "get_dashboard"))["revision"] == 0


async def test_sdk_http_envelope_errors_do_not_reflect_body_or_credentials(live: Live) -> None:
    async with httpx.AsyncClient(trust_env=False) as http:
        response = await http.post(
            live.mcp_url,
            headers={
                "Authorization": f"Bearer {EXTERNAL}",
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            },
            json={"jsonrpc": EXTERNAL, "unexpected": LOCAL},
        )
        assert response.status_code == 400
        assert response.json() == {"detail": "MCP HTTP request rejected", "status": 400}
        assert EXTERNAL not in response.text and LOCAL not in response.text


async def test_reference_client_status_patch_and_redelivered_todo_cycle(
    live: Live,
    bridge_http: httpx.AsyncClient,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    credential = Credential(path=live.mcp_file)
    await reference_run(live.mcp_url, credential, note="Approved reference status")
    state = (await bridge_http.get("/v1/dashboard")).json()
    assert state["revision"] == 1
    assert next(s for s in state["sections"] if s["type"] == "status")["note"] == (
        "Approved reference status"
    )
    todos = next(s for s in state["sections"] if s["type"] == "todos")
    desired = not todos["items"][0]["done"]
    intent = {
        "kind": "todo_toggle",
        "client_event_id": "reference-todo-intent",
        "observed_revision": state["revision"],
        "section_id": todos["id"],
        "target_id": todos["items"][0]["id"],
        "done": desired,
    }
    first = await bridge_http.post("/v1/input", json=intent)
    assert first.status_code == 200

    async def unavailable_ack(ids: list[int]) -> list[int]:
        raise BridgeError(503, "durable storage unavailable")

    with monkeypatch.context() as patch:
        patch.setattr(live.bridge_app.state.owner, "ack", unavailable_ack)  # type: ignore[attr-defined]
        with pytest.raises(ToolError, match="durable storage unavailable"):
            await reference_run(live.mcp_url, credential, todos=True)
    committed = (await bridge_http.get("/v1/dashboard")).json()
    assert committed["revision"] == 2
    assert (
        next(s for s in committed["sections"] if s["type"] == "todos")["items"][0]["done"]
        == desired
    )
    pending = (await bridge_http.get("/v1/events")).json()["events"]
    assert [event["id"] for event in pending] == [first.json()["id"]]
    # The same unacknowledged intent is redelivered after its commit succeeded.
    await reference_run(live.mcp_url, credential, todos=True)
    assert (await bridge_http.get("/v1/dashboard")).json()["revision"] == 2
    assert (await bridge_http.get("/v1/events")).json()["events"] == []
    output = capsys.readouterr().out
    assert "tools=10 binding=aos-appliance-v1" in output
    assert output.count("acknowledged todo intents=1") == 1
    assert EXTERNAL not in output and LOCAL not in output
