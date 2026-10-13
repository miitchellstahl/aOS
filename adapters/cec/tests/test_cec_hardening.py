from __future__ import annotations

import asyncio
import json
import os
import sys
import types
from pathlib import Path

import pytest
from aos_cec.keyboard import EvdevKeyboard
from aos_cec.service import ControlServer


def test_fake_evdev_capabilities_only_six_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    instances: list[FakeUInput] = []

    class FakeUInput:
        def __init__(self, capabilities: dict[int, list[int]], *, name: str) -> None:
            self.capabilities = capabilities
            self.name = name
            self.events: list[tuple[int, int, int]] = []
            self.sync_count = 0
            self.is_closed = False
            instances.append(self)

        def write(self, kind: int, key: int, value: int) -> None:
            self.events.append((kind, key, value))

        def syn(self) -> None:
            self.sync_count += 1

        def close(self) -> None:
            self.is_closed = True

    module = types.ModuleType("evdev")
    module.UInput = FakeUInput  # type: ignore[attr-defined]
    module.ecodes = types.SimpleNamespace(EV_KEY=1)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "evdev", module)
    keyboard = EvdevKeyboard()
    assert instances[0].capabilities == {1: [1, 28, 103, 105, 106, 108]}
    assert instances[0].name == "aOS CEC navigation"
    keyboard.emit(28, 1)
    keyboard.emit(28, 0)
    keyboard.close()
    assert instances[0].events == [(1, 28, 1), (1, 28, 0)]
    assert instances[0].sync_count == 2 and instances[0].is_closed


class HangingClient:
    timeout_s = 0.01
    actions: list[str]

    def __init__(self) -> None:
        self.actions = []

    async def execute(self, action: str) -> None:
        self.actions.append(action)
        await asyncio.sleep(10)


async def test_socket_read_and_control_timeouts(tmp_path: Path) -> None:
    client = HangingClient()
    server = ControlServer(client, tmp_path / "cec.sock", os.getuid())  # type: ignore[arg-type]
    await server.start()
    try:
        for frame in (b'{"action":"power_on"}', b'{"action":"power_on"}\n'):
            reader, writer = await asyncio.open_unix_connection(str(server.path))
            writer.write(frame)
            await writer.drain()
            response = json.loads(await asyncio.wait_for(reader.readline(), 4))
            assert response["ok"] is False
            assert isinstance(response["error"], str) and len(response["error"]) <= 300
            writer.close()
            await writer.wait_closed()
        assert client.actions == ["power_on"]
    finally:
        await server.close()
