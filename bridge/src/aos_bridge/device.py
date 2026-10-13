"""Bounded typed CEC Unix IPC; an absent device is never simulated as success."""

from __future__ import annotations

import asyncio
import json

from aos_bridge.owner import BridgeError
from aos_bridge.settings import Settings


async def execute(settings: Settings, action: str) -> dict[str, bool]:
    if action not in {"power_on", "power_off", "active_source"}:
        raise BridgeError(422, "unsupported device action")

    async def exchange() -> dict[str, bool]:
        reader, writer = await asyncio.open_unix_connection(str(settings.cec_socket), limit=4096)
        try:
            writer.write(json.dumps({"action": action}, separators=(",", ":")).encode() + b"\n")
            await writer.drain()
            line = await reader.readline()
            if not line.endswith(b"\n") or len(line) > 4096:
                raise ValueError("invalid CEC framing")
            response = json.loads(line)
            if not isinstance(response, dict) or not {"ok"} <= response.keys() <= {"ok", "error"}:
                raise ValueError("invalid CEC response")
            if type(response["ok"]) is not bool:
                raise ValueError("invalid CEC result")
            if "error" in response and (
                not isinstance(response["error"], str) or len(response["error"]) > 300
            ):
                raise ValueError("invalid CEC error")
            if not response["ok"]:
                raise BridgeError(503, "CEC service rejected device action")
            return {"ok": True}
        finally:
            writer.close()
            await writer.wait_closed()

    try:
        return await asyncio.wait_for(exchange(), timeout=settings.cec_timeout_s)
    except (OSError, ValueError, UnicodeError, TimeoutError, RecursionError):
        raise BridgeError(503, "CEC service unavailable or invalid response") from None
