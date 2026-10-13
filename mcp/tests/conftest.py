"""Real local HTTP servers; only this fixture's own tasks/sockets are stopped."""

from __future__ import annotations

import asyncio
import json
import socket
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest_asyncio
import uvicorn
from aos_bridge.app import create_app as create_bridge
from aos_bridge.settings import Settings as BridgeSettings
from aos_mcp.server import create_app
from aos_mcp.settings import Credential, Settings
from starlette.types import ASGIApp

EXTERNAL = "test-external-mcp-capability"
LOCAL = "test-local-bridge-capability"


@asynccontextmanager
async def serve(app: ASGIApp, listener: socket.socket | None = None) -> AsyncIterator[str]:
    sock = listener or socket.socket()
    if listener is None:
        sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        log_level="critical",
        access_log=False,
        proxy_headers=False,
        lifespan="on",
        ws="none",
    )
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        async with asyncio.timeout(10):
            while not server.started:
                if task.done():
                    await task
                    raise RuntimeError("test server failed to start")
                await asyncio.sleep(0.01)
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, timeout=10)
        sock.close()


@dataclass
class Live:
    bridge_url: str
    mcp_url: str
    mcp_file: Path
    bridge_file: Path
    bridge_app: ASGIApp
    actions: list[str]
    cec_socket: Path


@pytest_asyncio.fixture
async def live(tmp_path: Path) -> AsyncIterator[Live]:
    mcp_file = tmp_path / "mcp.token"
    bridge_file = tmp_path / "bridge.token"
    mcp_file.write_text(EXTERNAL)
    bridge_file.write_text(LOCAL)
    cec_socket = tmp_path / "cec.sock"
    actions: list[str] = []

    async def fake_cec(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        # explicitly a protocol fake: no hardware/device execution is asserted.
        try:
            request = json.loads(await reader.readline())
            actions.append(request["action"])
            writer.write(b'{"ok":true}\n')
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    cec = await asyncio.start_unix_server(fake_cec, path=str(cec_socket))
    app = create_bridge(
        BridgeSettings(
            state_dir=tmp_path / "state",
            bridge_token_file=bridge_file,
            cec_socket=cec_socket,
            heartbeat_s=0.1,
        )
    )
    try:
        async with serve(app) as bridge_url:
            listener = socket.socket()
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
            settings = Settings(
                mcp_token=Credential(path=mcp_file),
                bridge_token=Credential(path=bridge_file),
                bridge_url=bridge_url,
                allowed_hosts=(f"127.0.0.1:{port}", "tunnel.example"),
            )
            async with serve(create_app(settings), listener) as mcp_url:
                yield Live(
                    bridge_url, mcp_url + "/mcp", mcp_file, bridge_file, app, actions, cec_socket
                )
    finally:
        cec.close()
        await cec.wait_closed()


@pytest_asyncio.fixture
async def bridge_http(live: Live) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        base_url=live.bridge_url,
        headers={"Authorization": f"Bearer {LOCAL}"},
        trust_env=False,
    ) as client:
        yield client
