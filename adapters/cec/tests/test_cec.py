from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

import pytest
from aos_cec.keyboard import Navigation
from aos_cec.service import CecClient, ControlServer


class FakeKeyboard:
    def __init__(self) -> None:
        self.events: list[tuple[int, int]] = []

    def emit(self, key: int, value: int) -> None:
        self.events.append((key, value))

    def close(self) -> None:
        pass


def test_press_repeat_release_and_watchdog() -> None:
    keyboard = FakeKeyboard()
    now = [0.0]
    navigation = Navigation(keyboard, clock=lambda: now[0])
    navigation.feed("TRAFFIC: [1] >> 04:44:01")
    now[0] = 0.1
    navigation.feed("TRAFFIC: [1] >> 04:44:01")
    now[0] = 0.4
    navigation.feed("TRAFFIC: [1] >> 04:44:01")
    navigation.feed("TRAFFIC: [1] >> 04:45")
    navigation.feed("TRAFFIC: [1] >> 04:44:00")
    now[0] = 0.9
    navigation.feed("TRAFFIC: [1] >> 04:44:00")
    assert keyboard.events == [(103, 1), (103, 2), (103, 0), (28, 1)]
    now[0] = 2
    navigation.expire()
    assert keyboard.events[-1] == (28, 0)
    navigation.release()
    assert len(keyboard.events) == 5


@pytest.mark.parametrize(
    "line",
    [
        "TRAFFIC: << 04:44:00",
        "NOTICE: key pressed: select (0)",
        "TRAFFIC: >> 40:44:00",
        "TRAFFIC: >> 0f:44:00",
        "TRAFFIC: >> 08:44:00",
        "TRAFFIC: >> 04:44",
        "TRAFFIC: >> 04:44:00:01",
        "TRAFFIC: >> zz:44:00",
    ],
)
def test_ignore_echo_wrong_destination_or_malformed(line: str) -> None:
    keyboard = FakeKeyboard()
    Navigation(keyboard).feed(line)
    assert keyboard.events == []


def test_switch_or_unknown_key_releases_old_key() -> None:
    keyboard = FakeKeyboard()
    navigation = Navigation(keyboard)
    for key in (1, 2, 0x71):
        navigation.feed(f">> 04:44:{key:02x}")
    assert keyboard.events == [(103, 1), (103, 0), (108, 1), (108, 0)]


class FakeClient:
    timeout_s = 0.1

    def __init__(self) -> None:
        self.actions: list[str] = []
        self.failure = False
        self.hang = False

    async def execute(self, action: str) -> None:
        self.actions.append(action)
        if self.failure:
            raise OSError("device absent")
        if self.hang:
            await asyncio.sleep(10)


async def exchange(path: Path, data: bytes) -> dict[str, Any]:
    reader, writer = await asyncio.open_unix_connection(str(path))
    writer.write(data)
    await writer.drain()
    response = json.loads(await reader.readline())
    writer.close()
    await writer.wait_closed()
    return response


async def test_control_protocol_and_permissions(tmp_path: Path) -> None:
    client = FakeClient()
    server = ControlServer(client, tmp_path / "cec.sock", os.getuid())  # type: ignore[arg-type]
    await server.start()
    try:
        assert server.path.stat().st_mode & 0o777 == 0o660
        for action in ("power_on", "power_off", "active_source"):
            assert await exchange(server.path, json.dumps({"action": action}).encode() + b"\n") == {
                "ok": True
            }
        assert client.actions == ["power_on", "power_off", "active_source"]
        client.failure = True
        assert not (await exchange(server.path, b'{"action":"power_on"}\n'))["ok"]
        assert "device absent" not in str(await exchange(server.path, b'{"action":"power_on"}\n'))
    finally:
        await server.close()
    assert not server.path.exists()


@pytest.mark.parametrize(
    "wire",
    [
        b"{}\n",
        b"[]\n",
        b'{"action":1}\n',
        b'{"action":"reboot"}\n',
        b'{"action":"power_on","command":"rm"}\n',
        b"not json\n",
        b"\xff\n",
        b"[" * 1500 + b"0" + b"]" * 1500 + b"\n",
        b"x" * 4100 + b"\n",
    ],
)
async def test_rejects_invalid_socket_requests(tmp_path: Path, wire: bytes) -> None:
    client = FakeClient()
    server = ControlServer(client, tmp_path / "cec.sock", os.getuid())  # type: ignore[arg-type]
    await server.start()
    try:
        assert not (await exchange(server.path, wire))["ok"]
        assert client.actions == []
    finally:
        await server.close()


async def test_rejects_other_uid_and_preexisting_path(tmp_path: Path) -> None:
    client = FakeClient()
    server = ControlServer(client, tmp_path / "cec.sock", os.getuid() + 1)  # type: ignore[arg-type]
    await server.start()
    try:
        assert not (await exchange(server.path, b'{"action":"power_on"}\n'))["ok"]
        assert not client.actions
        with pytest.raises(ValueError):
            await server.start()
    finally:
        await server.close()


class ScriptedClient(CecClient):
    def __init__(self, output: list[str]) -> None:
        super().__init__(Navigation(FakeKeyboard()), timeout_s=0.1)
        self.output = output
        self.commands: list[str] = []
        self.started = 0

    async def pause_monitor(self) -> None:
        self.navigation.release()

    async def start_monitor(self) -> None:
        self.started += 1

    async def command(self, command: str) -> str:
        self.commands.append(command)
        return self.output.pop(0)


@pytest.mark.parametrize(
    "action,outputs,commands",
    [
        ("power_on", ["TRAFFIC: << 40:04\n", "power status: on\n"], ["on 0", "pow 0"]),
        ("power_off", ["TRAFFIC: << 40:36\n", "power status: standby\n"], ["standby 0", "pow 0"]),
        ("active_source", ["TRAFFIC: << 4f:82:10:00\n"], ["as"]),
    ],
)
async def test_control_requires_action_evidence(
    action: Any, outputs: list[str], commands: list[str]
) -> None:
    client = ScriptedClient(outputs)
    await client.execute(action)
    assert client.commands == commands
    assert client.started == 1


async def test_no_fake_success_from_zero_exit_or_wrong_state() -> None:
    for outputs in ([""], ["<< 40:04\n", "power status: unknown\n"]):
        client = ScriptedClient(list(outputs))
        with pytest.raises(ValueError):
            await client.execute("power_on")
        assert client.started == 1


async def test_command_exec_no_shell_and_bounded_output(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, ...]] = []

    class Process:
        returncode: int | None = 0
        stdin = asyncio.StreamReader()  # replaced below
        stdout = asyncio.StreamReader()

        async def wait(self) -> int:
            return 0

    class Writer:
        def write(self, value: bytes) -> None:
            assert value == b"on 0\n"

        async def drain(self) -> None:
            pass

        def close(self) -> None:
            pass

    process = Process()
    process.stdin = Writer()  # type: ignore[assignment]
    process.stdout.feed_data(b"<< 40:04\n")
    process.stdout.feed_eof()

    async def spawn(*args: str, **kwargs: Any) -> Any:
        calls.append(args)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    client = CecClient(Navigation(FakeKeyboard()), adapter="/dev/cec0")
    assert "40:04" in await client.command("on 0")
    assert calls[0][-1] == "/dev/cec0"
    assert "-s" in calls[0] and "-aw" in calls[0]
