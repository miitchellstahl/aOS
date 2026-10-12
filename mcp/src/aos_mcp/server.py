"""The documented MCP tools are authenticated thin bridge operations."""

from __future__ import annotations

import logging
import weakref
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Any, Literal

import httpx
from fastmcp import FastMCP
from fastmcp.exceptions import NotFoundError
from fastmcp.server.dependencies import _current_server
from fastmcp.server.http import StarletteWithLifespan
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.tools.tool import ToolResult
from mcp.types import CallToolRequestParams, ToolAnnotations
from pydantic import Field, StrictInt, ValidationError
from starlette.middleware import Middleware as HttpMiddleware

from aos_mcp.proxy import BridgeProxy, tool_error
from aos_mcp.security import ApplianceTokenVerifier, HttpBoundary
from aos_mcp.settings import Settings

Revision = Annotated[StrictInt, Field(ge=0)]
Cursor = Annotated[StrictInt, Field(ge=0, le=9223372036854775807)]
EventId = Annotated[StrictInt, Field(ge=1, le=9223372036854775807)]


class ApplianceMCP(FastMCP[None]):
    @asynccontextmanager
    async def _docket_lifespan(self) -> AsyncIterator[None]:
        # FastMCP 2.14.7 starts Docket even with tasks=False. Retain its server
        # context but deliberately start no task worker, scheduler or Redis client.
        server_token = _current_server.set(weakref.ref(self))
        try:
            yield None
        finally:
            _current_server.reset(server_token)


class SafeSDKLogs(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        # MCP's receive loop uses the root logger and embeds malformed request values.
        return not record.pathname.replace("\\", "/").endswith("/mcp/shared/session.py")


class SafeValidationErrors(Middleware):
    async def on_call_tool(
        self,
        context: MiddlewareContext[CallToolRequestParams],
        call_next: CallNext[CallToolRequestParams, ToolResult],
    ) -> ToolResult:
        try:
            return await call_next(context)
        except ValidationError:
            # pydantic's default message contains input values, not just diagnostics.
            raise tool_error(422, "invalid tool arguments") from None
        except NotFoundError:
            raise tool_error(422, "unknown tool") from None


def create_server(settings: Settings | None = None) -> FastMCP[None]:
    settings = settings or Settings.from_env()
    # SDK debug records include tool arguments; this process never logs request bodies.
    for namespace in (
        "fastmcp",
        "fastmcp.server.server",
        "mcp",
        "mcp.server.lowlevel.server",
        "mcp.shared.session",
        "httpx",
        "httpcore",
    ):
        logging.getLogger(namespace).setLevel(logging.WARNING)
    # SDK warns with unknown tool names, which are also untrusted request input.
    logging.getLogger("mcp.server.lowlevel.server").setLevel(logging.ERROR)
    root_logger = logging.getLogger()
    if not any(isinstance(filter_, SafeSDKLogs) for filter_ in root_logger.filters):
        root_logger.addFilter(SafeSDKLogs())
    proxy: BridgeProxy | None = None

    @asynccontextmanager
    async def lifespan(server: FastMCP[None]) -> AsyncIterator[None]:
        nonlocal proxy
        async with httpx.AsyncClient(
            base_url=settings.bridge_url.rstrip("/"),
            timeout=httpx.Timeout(settings.timeout_s, connect=min(2.0, settings.timeout_s)),
            limits=httpx.Limits(max_connections=16, max_keepalive_connections=8),
            follow_redirects=False,
            trust_env=False,
            headers={"Accept-Encoding": "identity"},
        ) as client:
            proxy = BridgeProxy(settings, client)
            try:
                yield None
            finally:
                proxy = None

    server: FastMCP[None] = ApplianceMCP(
        "aOS",
        instructions=(
            "The bridge alone owns dashboard state. Read schema and current revision before "
            "writing. Replacements omit revision/updated_at; patches use the observed revision. "
            "One queue consumer: restart polls from zero after failure; acknowledge only after "
            "handling and committing. Device actions require explicit approval."
        ),
        auth=ApplianceTokenVerifier(settings),
        lifespan=lifespan,
        middleware=[SafeValidationErrors()],
        mask_error_details=True,
        # SDK prevalidation reflects request values before middleware can redact them.
        # annotated arguments remain pydantic-validated and the bridge owns wire validation.
        strict_input_validation=False,
        tasks=False,
    )

    async def request(method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        if proxy is None:
            raise tool_error(503, "MCP bridge client is not running")
        return await proxy.request(method, path, **kwargs)

    read = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)
    write = ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=False)

    @server.tool(annotations=read)
    async def get_schema() -> dict[str, Any]:
        """Discover authoritative state, CUSTOM/input schemas and AG-UI binding examples."""
        return await request("GET", "/v1/schema")

    @server.tool(annotations=read)
    async def get_dashboard() -> dict[str, Any]:
        """Read the full committed dashboard, including its bridge-owned revision."""
        return await request("GET", "/v1/dashboard")

    @server.tool(annotations=read)
    async def get_status() -> dict[str, Any]:
        """Read local bridge health, queue depth and actual device capabilities."""
        return await request("GET", "/v1/status")

    @server.tool(annotations=write)
    async def update_dashboard(
        payload: dict[str, Any], expected_revision: Revision
    ) -> dict[str, Any]:
        """Replace all content using the expected revision; omit revision and updated_at."""
        return await request(
            "POST",
            "/v1/dashboard",
            payload={"payload": payload, "expected_revision": expected_revision},
        )

    @server.tool(annotations=write)
    async def apply_dashboard_patch(
        delta: Annotated[list[dict[str, Any]], Field(max_length=128)], expected_revision: Revision
    ) -> dict[str, Any]:
        """Atomically apply RFC 6902 operations; root/metadata mutations are forbidden."""
        return await request(
            "POST",
            "/v1/ingest",
            payload={"type": "STATE_DELTA", "delta": delta},
            expected_revision=expected_revision,
        )

    @server.tool(annotations=write)
    async def show_notification(
        message: Annotated[str, Field(strict=True, max_length=300)],
        duration_s: Annotated[float, Field(strict=True, ge=1, le=30)] = 5,
    ) -> dict[str, Any]:
        """Emit a transient aos.overlay; notifications do not refresh content timestamps."""
        return await request(
            "POST", "/v1/overlay", payload={"message": message, "duration_s": duration_s}
        )

    @server.tool(annotations=read)
    async def get_pending_events(
        since_id: Cursor = 0, limit: Annotated[StrictInt, Field(ge=1, le=100)] = 100
    ) -> dict[str, Any]:
        """Page unacknowledged intents; since_id is pagination, never an acknowledgment."""
        return await request("GET", "/v1/events", params={"since": since_id, "limit": limit})

    @server.tool(annotations=write)
    async def acknowledge_events(
        ids: Annotated[list[EventId], Field(min_length=1, max_length=1000)],
    ) -> dict[str, Any]:
        """Idempotently acknowledge intents only after successful handling and state commit."""
        return await request("POST", "/v1/events/ack", payload={"ids": ids})

    @server.tool(annotations=write)
    async def device_power(state: Literal["on", "off"]) -> dict[str, Any]:
        """Request actual CEC power execution; unavailable/rejected device actions fail."""
        return await request("POST", "/v1/device/power", payload={"state": state})

    @server.tool(annotations=write)
    async def device_activate_source() -> dict[str, Any]:
        """Ask the CEC service to make the Pi the active HDMI source."""
        return await request("POST", "/v1/device/input", payload={"action": "active_source"})

    return server


def create_app(settings: Settings | None = None) -> StarletteWithLifespan:
    settings = settings or Settings.from_env()
    return create_server(settings).http_app(
        path="/mcp",
        transport="streamable-http",
        stateless_http=True,
        json_response=True,
        middleware=[HttpMiddleware(HttpBoundary, settings=settings)],
    )
