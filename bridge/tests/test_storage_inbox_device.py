from __future__ import annotations

import asyncio
import json
import os
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
import pytest
from aos_bridge.app import create_app
from aos_bridge.device import execute
from aos_bridge.inbox import Inbox
from aos_bridge.owner import BridgeError
from aos_bridge.settings import Settings
from aos_bridge.store import AtomicStore, StorageError


@pytest.mark.parametrize("phase", ["stage", "replace", "directory-fsync"])
def test_atomic_store_failure_rolls_back_both_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    store = AtomicStore(tmp_path / "state")
    store.acquire()
    try:
        store.write("document.json", {"value": "previous"})
        originals = {
            name: (store.directory / name).read_bytes()
            for name in ("document.json", "document.json.last-good")
        }
        calls = 0
        if phase == "stage":
            original = store._stage

            def stage(data: bytes, mode: int = 0o600) -> Path:
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("full disk")
                return original(data, mode)

            monkeypatch.setattr(store, "_stage", stage)
        elif phase == "replace":
            original_replace = os.replace

            def replace_file(*args: Any, **kwargs: Any) -> None:
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("rename failed")
                original_replace(*args, **kwargs)

            monkeypatch.setattr(os, "replace", replace_file)
        else:
            original_sync = store._sync_directory

            def sync() -> None:
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("fsync failed")
                original_sync()

            monkeypatch.setattr(store, "_sync_directory", sync)
        with pytest.raises(StorageError):
            store.write("document.json", {"value": "next"})
        assert not store.poisoned
        assert {name: (store.directory / name).read_bytes() for name in originals} == originals
        assert not list(store.directory.glob(".aos-*"))
    finally:
        store.close()


def test_failed_rollback_poisoned_owner_refuses_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = AtomicStore(tmp_path / "state")
    store.acquire()
    try:
        store.write("document.json", {"value": 0})

        def fail() -> None:
            raise OSError("fsync unavailable")

        monkeypatch.setattr(store, "_sync_directory", fail)
        with pytest.raises(StorageError):
            store.write("document.json", {"value": 1})
        assert store.poisoned
        for name in ("document.json", "document.json.last-good"):
            assert json.loads((store.directory / name).read_text()) == {"value": 0}
        with pytest.raises(StorageError, match="requires restart"):
            store.write("document.json", {"value": 2})
    finally:
        store.close()


@pytest.mark.parametrize(
    "response,successful",
    [
        (b'{"ok":true}\n', True),
        (b'{"ok":false,"error":"not connected"}\n', False),
        (b'{"ok":"true"}\n', False),
        (b'{"ok":1}\n', False),
        (b"{}\n", False),
        (b'{"ok":true,"extra":1}\n', False),
        (b'{"ok":true}', False),
        (b"not-json\n", False),
        (b"[" * 1500 + b"]" * 1500 + b"\n", False),
        (b'{"ok":true,"error":4}\n', False),
        (b'{"ok":false,"error":"' + b"x" * 301 + b'"}\n', False),
        (b"x" * 4097 + b"\n", False),
    ],
)
async def test_cec_protocol_bounded_and_typed(
    settings: Settings, response: bytes, successful: bool
) -> None:
    requests: list[dict[str, Any]] = []

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            requests.append(json.loads(await reader.readline()))
            writer.write(response)
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_unix_server(handler, path=str(settings.cec_socket))
    async with server:
        if successful:
            assert await execute(settings, "power_on") == {"ok": True}
        else:
            with pytest.raises(BridgeError) as error:
                await execute(settings, "power_on")
            assert error.value.status == 503
    assert requests == [{"action": "power_on"}]


async def test_cec_timeout(settings: Settings) -> None:
    finished = asyncio.Event()

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await reader.read()
            await finished.wait()
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_unix_server(handler, path=str(settings.cec_socket))
    async with server:
        try:
            with pytest.raises(BridgeError) as error:
                await execute(replace(settings, cec_timeout_s=0.02), "active_source")
            assert error.value.status == 503
        finally:
            finished.set()
            await asyncio.sleep(0)


async def test_cec_routes_execute_and_publish_only_after_success(settings: Settings) -> None:
    actions: list[str] = []
    ok = True

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            actions.append(json.loads(await reader.readline())["action"])
            writer.write(json.dumps({"ok": ok}).encode() + b"\n")
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_unix_server(handler, path=str(settings.cec_socket))
    app = create_app(settings)
    async with server, app.router.lifespan_context(app):
        _, subscriber = await app.state.owner.subscribe()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://127.0.0.1",
            headers={"Authorization": "Bearer bridge-secret"},
        ) as client:
            for route, body in [
                ("power", {"state": "on"}),
                ("power", {"state": "off"}),
                ("input", {"action": "active_source"}),
            ]:
                response = await client.post(f"/v1/device/{route}", json=body)
                assert response.status_code == 200 and response.json() == {"ok": True}
            assert actions == ["power_on", "power_off", "active_source"]
            for action in actions:
                assert subscriber.queue.get_nowait() == {
                    "type": "CUSTOM",
                    "name": "aos.device",
                    "value": {"action": action},
                }
            ok = False
            assert (await client.post("/v1/device/power", json={"state": "on"})).status_code == 503
            assert subscriber.queue.empty()
            assert app.state.owner.state["revision"] == 0


def command(event: dict[str, Any], expected: int = 0) -> bytes:
    return json.dumps({"expected_revision": expected, "event": event}).encode()


async def test_inbox_valid_patch_invalid_stale_and_scoped_device(settings: Settings) -> None:
    settings = replace(settings, inbox_dir=settings.state_dir.parent / "inbox", inbox_interval_s=60)
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        inbox: Inbox = app.state.inbox
        directory = settings.inbox_dir
        assert directory is not None
        patch = {
            "type": "STATE_DELTA",
            "delta": [{"op": "replace", "path": "/theme/mode", "value": "light"}],
        }
        (directory / "ready.json").write_bytes(command(patch))
        assert await inbox.scan_once() == 1
        assert app.state.owner.state["revision"] == 1
        assert app.state.owner.state["theme"]["mode"] == "light"
        assert len(list((directory / "processed").glob("*.json"))) == 1
        (directory / "stale.json").write_bytes(command(patch))
        (directory / "bare.json").write_text(json.dumps(patch))
        (directory / "device.json").write_bytes(
            command({"type": "CUSTOM", "name": "aos.device", "value": {"action": "power_on"}}, 1)
        )
        assert await inbox.scan_once() == 3
        assert app.state.owner.state["revision"] == 1
        assert len(list((directory / "rejected").glob("*.json"))) == 3


async def test_inbox_no_symlinks_traversal_or_partial_files(settings: Settings) -> None:
    directory = settings.state_dir.parent / "inbox"
    settings = replace(settings, inbox_dir=directory, inbox_interval_s=60)
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        inbox = app.state.inbox
        state = deepcopy(app.state.owner.state)
        outside = directory.parent / "outside.json"
        outside.write_bytes(
            command(
                {
                    "type": "CUSTOM",
                    "name": "aos.overlay",
                    "value": {"message": "outside", "duration_s": 5},
                }
            )
        )
        before = outside.read_bytes()
        (directory / "symlink.json").symlink_to(outside)
        (directory / "..json").write_bytes(before)
        (directory / "partial.tmp").write_bytes(b"{incomplete")
        (directory / "huge.json").write_bytes(b" " * (128 * 1024 + 1))
        assert await inbox.scan_once() == 3
        assert (directory / "partial.tmp").exists()
        assert outside.read_bytes() == before
        assert app.state.owner.state == state
        assert len(list((directory / "rejected").glob("*.json"))) == 3
        assert any(entry.is_symlink() for entry in (directory / "rejected").iterdir())


async def test_inbox_retention_and_complete_rename(settings: Settings) -> None:
    directory = settings.state_dir.parent / "inbox"
    settings = replace(settings, inbox_dir=directory, inbox_interval_s=60, inbox_retention=2)
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        inbox = app.state.inbox
        overlay = {
            "type": "CUSTOM",
            "name": "aos.overlay",
            "value": {"message": "note", "duration_s": 5},
        }
        for index in range(5):
            temporary = directory / "command.tmp"
            temporary.write_bytes(command(overlay))
            temporary.rename(directory / f"command-{index}.json")
            assert await inbox.scan_once() == 1
        assert len(list((directory / "processed").glob("*.json"))) == 2
        for index in range(5):
            (directory / f"bad-{index}.json").write_text("{}")
        assert await inbox.scan_once() == 5
        assert len(list((directory / "rejected").glob("*.json"))) == 2


def test_symlink_inbox_directory_is_rejected(settings: Settings) -> None:
    directory = settings.state_dir.parent / "real-inbox"
    directory.mkdir()
    symlink = directory.parent / "inbox"
    symlink.symlink_to(directory, target_is_directory=True)

    async def ingest(event: dict[str, Any], expected: int, source_id: str) -> dict[str, Any]:
        return {}

    inbox = Inbox(replace(settings, inbox_dir=symlink), ingest)
    with pytest.raises(ValueError, match="symlinks"):
        inbox.prepare()


async def test_unconfigured_inbox_has_no_importer_task(settings: Settings) -> None:
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        assert not hasattr(app.state, "inbox")
        assert (await app.state.owner.status())["capabilities"]["inbox"] is False
