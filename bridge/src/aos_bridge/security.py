"""ASGI request limits and local capability checks, including token rotation."""

from __future__ import annotations

import asyncio
import ipaddress
import secrets
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from aos_bridge.contracts import MAX_STATE_BYTES
from aos_bridge.owner import BridgeError
from aos_bridge.settings import Settings
from aos_bridge.store import read_regular


class LocalRequests:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    @staticmethod
    def _local_host(host: str) -> bool:
        if any(character.isspace() for character in host):
            return False
        try:
            parsed = urlsplit(f"http://{host}")
            if parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment:
                return False
            if parsed.port is not None and not 1 <= parsed.port <= 65535:
                return False
            if parsed.hostname == "localhost":
                return True
            return ipaddress.ip_address(parsed.hostname or "").is_loopback
        except ValueError:
            return False

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = scope.get("headers", [])
        hosts = [value.decode("latin-1") for key, value in headers if key.lower() == b"host"]
        if len(hosts) != 1 or not self._local_host(hosts[0]):
            await JSONResponse({"detail": "non-local Host forbidden"}, status_code=403)(
                scope, receive, send
            )
            return
        if any(key.lower() == b"origin" for key, _ in headers):
            await JSONResponse({"detail": "browser Origin forbidden"}, status_code=403)(
                scope, receive, send
            )
            return
        buffered = bytearray()
        deadline = asyncio.get_running_loop().time() + 10
        while True:
            try:
                message = await asyncio.wait_for(
                    receive(), timeout=max(0, deadline - asyncio.get_running_loop().time())
                )
            except TimeoutError:
                await JSONResponse({"detail": "request body timeout"}, status_code=422)(
                    scope, receive, send
                )
                return
            if message["type"] == "http.disconnect":
                return
            body = message.get("body", b"")
            if len(buffered) + len(body) > MAX_STATE_BYTES:
                await JSONResponse({"detail": "request exceeds 128 KiB"}, status_code=413)(
                    scope, receive, send
                )
                return
            buffered.extend(body)
            if not message.get("more_body", False):
                break
        delivered = False

        async def bounded_receive() -> Message:
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": bytes(buffered), "more_body": False}
            return await receive()

        await self.app(scope, bounded_receive, send)


def _credential(value: str | None, filename: Path | None) -> str | None:
    try:
        if filename is not None:
            value = read_regular(filename, 4096).decode("utf-8").strip()
        if not value or any(c.isspace() for c in value) or len(value) > 4096:
            return None
        return value
    except (OSError, ValueError, UnicodeError):
        return None


def authorize(request: Request, settings: Settings, allowed: set[str]) -> str:
    headers = request.headers.getlist("authorization")
    if len(headers) != 1:
        raise BridgeError(401, "bearer credential required")
    scheme, separator, candidate = headers[0].partition(" ")
    if not separator or scheme.lower() != "bearer" or not candidate or len(candidate) > 4096:
        raise BridgeError(401, "invalid bearer credential")
    credentials = {
        "bridge": _credential(settings.bridge_token, settings.bridge_token_file),
        "display": _credential(settings.display_token, settings.display_token_file),
        "muse": _credential(settings.muse_token, settings.muse_token_file),
    }
    matches = [
        scope
        for scope, value in credentials.items()
        if value is not None and secrets.compare_digest(value.encode(), candidate.encode())
    ]
    if len(matches) != 1:
        raise BridgeError(401, "invalid bearer credential")
    capability = matches[0]
    if capability != "bridge" and capability not in allowed:
        raise BridgeError(403, "capability does not permit this action")
    return capability
