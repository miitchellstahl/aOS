"""Unit hardening tests complement real FastMCP/bridge HTTP integration."""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from aos_mcp.proxy import BridgeProxy, bridge_error
from aos_mcp.security import ApplianceTokenVerifier, HttpBoundary
from aos_mcp.server import create_app
from aos_mcp.settings import Credential, Settings
from fastmcp.exceptions import ToolError
from starlette.types import Message, Scope


@pytest.mark.parametrize("raw", [b"", b" \n", b"has space", b"a\x00b", b"\xff", b"x" * 4097])
def test_file_credentials_fail_closed(raw: bytes, tmp_path: Path) -> None:
    path = tmp_path / "credential"
    path.write_bytes(raw)
    assert Credential(value="must-not-fallback", path=path).read() is None


def test_file_precedence_rotation_and_repr(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "credential"
    path.write_text("file-secret\n")
    monkeypatch.setenv("AOS_MCP_TOKEN", "env-secret")
    monkeypatch.setenv("AOS_MCP_TOKEN_FILE", str(path))
    credential = Credential.from_env("MCP")
    assert credential.read() == "file-secret"
    path.write_text("rotated-secret")
    assert credential.read() == "rotated-secret"
    path.unlink()
    assert credential.read() is None
    assert "env-secret" not in repr(Credential(value="env-secret"))
    assert "env-secret" not in repr(Settings(mcp_token=Credential(value="env-secret")))


@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1:8080",
        "http://hostile.example",
        "http://127.0.0.1.evil",
        "http://user:password@127.0.0.1",
        "http://127.0.0.1/path",
        "http://127.0.0.1?key=secret",
        "http://127.0.0.1#fragment",
        "http://127.0.0.1:0",
        "http://127.0.0.1:99999",
    ],
)
def test_bridge_url_is_loopback_origin_only(url: str) -> None:
    with pytest.raises(ValueError, match="loopback"):
        Settings(bridge_url=url)


@pytest.mark.parametrize(
    "tokens", [(None, None), ("external", None), ("same", "same"), ("", "local")]
)
async def test_verifier_missing_invalid_or_shared_capabilities(
    tokens: tuple[str | None, str | None],
) -> None:
    verifier = ApplianceTokenVerifier(
        Settings(
            mcp_token=Credential(value=tokens[0]),
            bridge_token=Credential(value=tokens[1]),
        )
    )
    assert await verifier.verify_token(tokens[0] or "external") is None


async def test_actual_access_token_and_constant_time_comparison(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    verifier = ApplianceTokenVerifier(
        Settings(
            mcp_token=Credential(value="external"),
            bridge_token=Credential(value="local"),
        )
    )
    import aos_mcp.security as security

    compare = security.hmac.compare_digest
    comparisons: list[tuple[object, object]] = []

    def record(a: Any, b: Any) -> bool:
        comparisons.append((a, b))
        return compare(a, b)

    monkeypatch.setattr(security.hmac, "compare_digest", record)
    access = await verifier.verify_token("external")
    assert access is not None and access.client_id == "aos-harness" and access.scopes == ["aos"]
    assert (b"external", b"external") in comparisons
    assert await verifier.verify_token("wrong") is None
    assert await verifier.verify_token("\u00e9") is None
    assert await verifier.verify_token("x" * 4097) is None


class Bytes(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes], delay: float = 0) -> None:
        self.chunks = chunks
        self.delay = delay
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self.chunks:
            await asyncio.sleep(self.delay)
            yield chunk

    async def aclose(self) -> None:
        self.closed = True


@pytest.mark.parametrize(
    "case,expected",
    [
        ("too-big", "size limit"),
        ("invalid-json", "invalid bridge JSON"),
        ("array", "response shape"),
        ("encoded", "encoded bridge"),
        ("timeout", "timed out"),
        ("transport", "unavailable"),
        ("redirect", "bridge request failed"),
    ],
)
async def test_bounded_upstream_and_no_exception_secret_leak(case: str, expected: str) -> None:
    stream = Bytes([b"{}"])
    status = 200
    headers: dict[str, str] = {}
    if case == "too-big":
        stream = Bytes([b"x" * 40, b"x" * 40])
    elif case == "invalid-json":
        stream = Bytes([b"private raw response"])
    elif case == "array":
        stream = Bytes([b"[]"])
    elif case == "encoded":
        headers["Content-Encoding"] = "gzip"
    elif case == "timeout":
        stream = Bytes([b"{}"], delay=0.1)
    elif case == "redirect":
        status = 307
        headers["Location"] = "https://hostile.example"

    async def handler(request: httpx.Request) -> httpx.Response:
        if case == "transport":
            raise httpx.ConnectError("private token and body in error")
        return httpx.Response(status, headers=headers, stream=stream)

    settings = Settings(
        bridge_token=Credential(value="local-secret"), response_limit=64, timeout_s=0.02
    )
    async with httpx.AsyncClient(
        base_url=settings.bridge_url, transport=httpx.MockTransport(handler)
    ) as http:
        with pytest.raises(ToolError, match=expected) as raised:
            await BridgeProxy(settings, http).request("GET", "/v1/dashboard")
        assert "private" not in str(raised.value) and "local-secret" not in str(raised.value)
    assert case == "transport" or stream.closed


async def test_proxy_auth_headers_if_match_and_rotation(tmp_path: Path) -> None:
    path = tmp_path / "local.token"
    path.write_text("first")
    observed: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        observed.append(request)
        return httpx.Response(200, stream=Bytes([b'{"revision":1}']))

    settings = Settings(bridge_token=Credential(path=path))
    async with httpx.AsyncClient(
        base_url=settings.bridge_url, transport=httpx.MockTransport(handler)
    ) as http:
        proxy = BridgeProxy(settings, http)
        assert await proxy.request(
            "POST", "/v1/ingest", payload={"type": "STATE_DELTA", "delta": []}, expected_revision=0
        ) == {"revision": 1}
        path.write_text("second")
        await proxy.request("GET", "/v1/events", params={"since": 3, "limit": 5})
        path.unlink()
        with pytest.raises(ToolError, match="credential unavailable"):
            await proxy.request("GET", "/v1/dashboard")
    assert observed[0].headers["Authorization"] == "Bearer first"
    assert observed[0].headers["If-Match"] == '"0"'
    assert json.loads(observed[0].content) == {"type": "STATE_DELTA", "delta": []}
    assert observed[1].headers["Authorization"] == "Bearer second"
    assert observed[1].url.query == b"since=3&limit=5"


@pytest.mark.parametrize("value", [float("nan"), float("inf"), {"value": object()}])
async def test_invalid_json_is_not_forwarded(value: object) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("invalid body reached bridge")

    settings = Settings(bridge_token=Credential(value="local"))
    async with httpx.AsyncClient(
        base_url=settings.bridge_url, transport=httpx.MockTransport(handler)
    ) as http:
        with pytest.raises(ToolError, match="finite JSON"):
            await BridgeProxy(settings, http).request("POST", "/v1/dashboard", payload=value)


@pytest.mark.parametrize(
    "status,detail,facts",
    [
        (422, "state: invalid at /sections/0 (oneOf)", {"pointer": "/sections/0", "rule": "oneOf"}),
        (422, "state: invalid at /secret-token (type)", {"rule": "type"}),
        (409, "stale expected_revision", {"reason": "stale expected_revision"}),
        (503, "durable storage unavailable", {"reason": "durable storage unavailable"}),
        (503, "input queue full", {"reason": "input queue full"}),
        (503, "private token and body", {}),
    ],
)
def test_tool_error_preserves_only_safe_facts(
    status: int, detail: str, facts: dict[str, str]
) -> None:
    result = json.loads(str(bridge_error(status, {"detail": detail, "request": "private-body"})))
    assert result["status"] == status
    assert {k: v for k, v in result.items() if k not in {"status", "detail"}} == facts
    assert "private" not in json.dumps(result) and "secret-token" not in json.dumps(result)


async def test_boundary_body_timeout_and_duplicate_headers() -> None:
    called = False

    async def downstream(scope: Scope, receive: Any, send: Any) -> None:
        nonlocal called
        called = True

    settings = Settings(timeout_s=0.01)
    boundary = HttpBoundary(downstream, settings)
    messages: list[Message] = []

    async def send(message: Message) -> None:
        messages.append(message)

    async def stalled() -> Message:
        await asyncio.sleep(1)
        return {"type": "http.request", "body": b""}

    scope: Scope = {"type": "http", "headers": [(b"host", b"127.0.0.1:8090")]}
    await boundary(scope, stalled, send)
    assert messages[0]["status"] == 408 and not called
    for headers, status in [
        ([(b"host", b"127.0.0.1:8090"), (b"host", b"evil")], 421),
        ([(b"host", b"127.0.0.1:8090"), (b"authorization", b"a"), (b"authorization", b"b")], 401),
        ([(b"host", b"127.0.0.1:8090"), (b"content-length", b"no")], 400),
    ]:
        messages.clear()
        await boundary({"type": "http", "headers": headers}, stalled, send)
        assert messages[0]["status"] == status and not called


async def test_async_managed_httpx_lifecycle(monkeypatch: pytest.MonkeyPatch) -> None:
    import aos_mcp.server as server

    original = server.httpx.AsyncClient
    clients: list[httpx.AsyncClient] = []

    def tracked(**kwargs: Any) -> httpx.AsyncClient:
        assert kwargs["trust_env"] is False and kwargs["follow_redirects"] is False
        http = original(**kwargs)
        clients.append(http)
        return http

    monkeypatch.setattr(server.httpx, "AsyncClient", tracked)
    app = create_app(Settings())
    async with app.lifespan(app):
        assert len(clients) == 1 and not clients[0].is_closed
    assert clients[0].is_closed


async def test_no_docket_worker_scheduler_or_backend_is_started(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import fastmcp.server.server as sdk_server
    from aos_mcp.server import create_server
    from fastmcp.server.dependencies import get_server

    def forbidden(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("appliance must not start Docket or a task worker")

    monkeypatch.setattr(sdk_server, "Docket", forbidden)
    monkeypatch.setattr(sdk_server, "Worker", forbidden)
    server = create_server(Settings())
    assert all(tool.task_config.mode == "forbidden" for tool in (await server.get_tools()).values())
    app = server.http_app(path="/mcp", stateless_http=True)
    async with app.lifespan(app):
        assert server.docket is None
        assert get_server() is server
    with pytest.raises(RuntimeError, match="No FastMCP server"):
        get_server()


def test_sdk_root_validation_log_is_suppressed(caplog: pytest.LogCaptureFixture) -> None:
    create_app(Settings())
    record = logging.LogRecord(
        "root",
        logging.WARNING,
        "/venv/site-packages/mcp/shared/session.py",
        383,
        "Failed to validate request: private-body-and-token",
        (),
        None,
    )
    logging.getLogger().handle(record)
    assert "private-body-and-token" not in caplog.text


@pytest.mark.parametrize("port", [8090, 8091])
def test_cli_is_loopback_stateless_entrypoint_with_matching_host_allowlist(
    monkeypatch: pytest.MonkeyPatch, port: int
) -> None:
    import aos_mcp.__main__ as entry

    captured: dict[str, Any] = {}

    def app(settings: Settings) -> None:
        captured["settings"] = settings

    def serve(application: Any, **kwargs: Any) -> None:
        captured.update(kwargs)

    monkeypatch.delenv("AOS_MCP_ALLOWED_HOSTS", raising=False)
    monkeypatch.setenv("AOS_BRIDGE_URL", "http://127.0.0.1:8080")
    monkeypatch.setattr(sys, "argv", ["aos-mcp"] + ([] if port == 8090 else ["--port", str(port)]))
    monkeypatch.setattr(entry, "create_app", app)
    monkeypatch.setattr(entry.uvicorn, "run", serve)
    entry.main()
    assert captured["host"] == "127.0.0.1" and captured["port"] == port
    assert captured["workers"] == 1
    assert captured["access_log"] is False and captured["proxy_headers"] is False
    assert f"127.0.0.1:{port}" in captured["settings"].allowed_hosts
