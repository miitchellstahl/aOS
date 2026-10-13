"""A bounded HTTP client, not a state owner or duplicate contract validator."""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any

import httpx
from fastmcp.exceptions import ToolError

from aos_mcp.settings import Settings

MAX_BRIDGE_REQUEST = 128 * 1024


def tool_error(status: int, detail: str, **facts: object) -> ToolError:
    return ToolError(
        json.dumps({"status": status, "detail": detail, **facts}, separators=(",", ":"))
    )


def bridge_error(status: int, value: object) -> ToolError:
    """Retain actionable facts, never forward an arbitrary upstream error body."""
    categories = {
        401: "bridge authentication failed",
        403: "bridge operation forbidden",
        409: "bridge state conflict",
        413: "bridge request exceeds size limit",
        422: "bridge validation failed",
        503: "bridge storage, queue or device unavailable",
    }
    detail = value.get("detail") if isinstance(value, dict) else None
    facts: dict[str, object] = {}
    if isinstance(detail, str) and len(detail) <= 512:
        # only fixed diagnostic vocabulary enters MCP errors, never submitted values.
        known = {
            "stale expected_revision",
            "stale observed_revision",
            "client_event_id reused with different payload",
            "durable storage unavailable",
            "input queue full",
            "input event ID space exhausted",
            "CEC service rejected device action",
            "CEC service unavailable or invalid response",
            "patch: cannot apply operations to the current state",
            "replacement: bridge-owned revision/updated_at must be omitted",
        }
        if detail in known:
            facts["reason"] = detail
        pointer = re.search(r" at (/(?:[A-Za-z0-9_<>~/-]*))", detail)
        if pointer:
            path = pointer.group(1)
            fields = {
                "version",
                "revision",
                "updated_at",
                "theme",
                "mode",
                "accent",
                "font_scale",
                "sections",
                "id",
                "type",
                "items",
                "label",
                "done",
                "delta",
                "target_id",
                "observed_revision",
                "expected_revision",
                "ids",
                "state",
                "action",
                "message",
                "duration_s",
                "payload",
                "snapshot",
                "value",
                "name",
                "client_event_id",
                "<field>",
            }
            if all(not p or p.isdigit() or p in fields for p in path.split("/")):
                facts["pointer"] = path
        rule = re.search(
            r"\((type|oneOf|enum|const|required|additionalProperties|maxItems|"
            r"maxLength|minimum|maximum|pattern|format|contains|minContains|maxContains)\)$",
            detail,
        )
        if rule:
            facts["rule"] = rule.group(1)
    return tool_error(status, categories.get(status, "bridge request failed"), **facts)


class BridgeProxy:
    def __init__(self, settings: Settings, client: httpx.AsyncClient) -> None:
        self.settings = settings
        self.client = client

    async def request(
        self,
        method: str,
        path: str,
        *,
        payload: object = None,
        params: dict[str, int] | None = None,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        content: bytes | None = None
        if payload is not None:
            try:
                encoder = json.JSONEncoder(
                    ensure_ascii=False, allow_nan=False, separators=(",", ":")
                )
                chunks: list[bytes] = []
                size = 0
                for chunk in encoder.iterencode(payload):
                    encoded = chunk.encode("utf-8")
                    size += len(encoded)
                    if size > MAX_BRIDGE_REQUEST:
                        raise tool_error(413, "bridge request exceeds size limit")
                    chunks.append(encoded)
                content = b"".join(chunks)
            except (TypeError, ValueError, UnicodeError, RecursionError):
                raise tool_error(422, "request must contain bounded finite JSON") from None
        token = await asyncio.to_thread(self.settings.bridge_token.read)
        if token is None:
            raise tool_error(503, "local bridge credential unavailable")
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        if content is not None:
            headers["Content-Type"] = "application/json"
        if expected_revision is not None:
            headers["If-Match"] = f'"{expected_revision}"'
        try:
            async with asyncio.timeout(self.settings.timeout_s):
                async with self.client.stream(
                    method, path, content=content, params=params, headers=headers
                ) as response:
                    if response.headers.get("content-encoding", "identity") != "identity":
                        raise tool_error(503, "unexpected encoded bridge response")
                    data = bytearray()
                    async for response_chunk in response.aiter_raw():
                        if len(data) + len(response_chunk) > self.settings.response_limit:
                            raise tool_error(503, "bridge response exceeds size limit")
                        data.extend(response_chunk)
                    try:
                        value = json.loads(data)
                    except (ValueError, UnicodeError, RecursionError):
                        raise tool_error(503, "invalid bridge JSON response") from None
                    if not response.is_success:
                        raise bridge_error(response.status_code, value)
                    if not isinstance(value, dict):
                        raise tool_error(503, "invalid bridge response shape")
                    return value
        except (httpx.HTTPError, TimeoutError):
            raise tool_error(503, "bridge unavailable or request timed out") from None
