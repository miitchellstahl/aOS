"""FastMCP token verification plus HTTP DNS-rebinding and resource limits."""

from __future__ import annotations

import asyncio
import hmac

from fastmcp.server.auth import AccessToken, TokenVerifier
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from aos_mcp.settings import Settings


class ApplianceTokenVerifier(TokenVerifier):
    def __init__(self, settings: Settings) -> None:
        super().__init__(required_scopes=["aos"])
        self.settings = settings

    async def verify_token(self, token: str) -> AccessToken | None:
        expected, local = await asyncio.gather(
            asyncio.to_thread(self.settings.mcp_token.read),
            asyncio.to_thread(self.settings.bridge_token.read),
        )
        # identical capabilities defeat the external/local trust boundary; fail closed.
        if expected is None or local is None or hmac.compare_digest(expected, local):
            return None
        try:
            supplied = token.encode("ascii")
        except UnicodeError:
            return None
        if len(supplied) > 4096 or not hmac.compare_digest(supplied, expected.encode("ascii")):
            return None
        return AccessToken(token=token, client_id="aos-harness", scopes=["aos"])


class HttpBoundary:
    def __init__(self, app: ASGIApp, settings: Settings) -> None:
        self.app = app
        self.settings = settings

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = scope.get("headers", [])

        def values(name: bytes) -> list[bytes]:
            return [value for key, value in headers if key.lower() == name]

        async def reject(status: int, detail: str) -> None:
            await JSONResponse({"detail": detail}, status_code=status)(scope, receive, send)

        hosts = values(b"host")
        if len(hosts) != 1 or hosts[0].decode("latin-1").lower() not in {
            host.lower() for host in self.settings.allowed_hosts
        }:
            await reject(421, "untrusted Host")
            return
        # no browser producer is part of the v1 harness contract, including localhost.
        if values(b"origin"):
            await reject(403, "browser Origins are not allowed")
            return
        if len(values(b"authorization")) > 1:
            await reject(401, "authentication required")
            return
        lengths = values(b"content-length")
        if lengths:
            try:
                if len(lengths) != 1 or not lengths[0].isdigit():
                    raise ValueError
                length = int(lengths[0])
            except ValueError:
                await reject(400, "invalid Content-Length")
                return
            if length > self.settings.request_limit:
                await reject(413, "MCP request exceeds size limit")
                return
        # bound actual chunked bytes before the SDK allocates/parses JSON; no custom RPC.
        chunks: list[bytes] = []
        size = 0
        try:
            async with asyncio.timeout(self.settings.timeout_s):
                while True:
                    message = await receive()
                    if message["type"] == "http.disconnect":
                        return
                    chunk = message.get("body", b"")
                    size += len(chunk)
                    if size > self.settings.request_limit:
                        await reject(413, "MCP request exceeds size limit")
                        return
                    chunks.append(chunk)
                    if not message.get("more_body", False):
                        break
        except TimeoutError:
            await reject(408, "MCP request body timeout")
            return
        payload = b"".join(chunks)
        delivered = False

        async def replay() -> Message:
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": payload, "more_body": False}
            return await receive()

        rejected = False

        async def safe_send(message: Message) -> None:
            nonlocal rejected
            if message["type"] == "http.response.start" and message["status"] >= 400:
                rejected = True
                status = message["status"]
                # SDK envelope validation errors embed request values. Preserve HTTP
                # status but discard those bodies without implementing JSON-RPC ourselves.
                await JSONResponse(
                    {"detail": "MCP HTTP request rejected", "status": status},
                    status_code=status,
                    headers={"WWW-Authenticate": "Bearer"} if status == 401 else None,
                )(scope, replay, send)
                return
            if not rejected:
                await send(message)

        await self.app(scope, replay, safe_send)
