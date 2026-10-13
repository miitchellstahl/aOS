from __future__ import annotations

import asyncio
import json
import os
import stat
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

import aos_bridge.__main__ as cli
import httpx
import pytest
from aos_bridge.app import create_app, stream
from aos_bridge.contracts import Contracts
from aos_bridge.owner import Owner
from aos_bridge.settings import Settings
from aos_bridge.store import StorageError
from fastapi.testclient import TestClient


async def test_subscriber_frame_preparation_failure_precedes_commit(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = Owner(settings, Contracts())
    owner.start()
    try:
        snapshot, subscriber = await owner.subscribe()
        before = (settings.state_dir / "dashboard.json").read_bytes()

        def fail_frames(event: dict[str, Any]) -> dict[Any, dict[str, Any]]:
            raise ValueError("subscriber frame allocation failed")

        monkeypatch.setattr(owner, "_prepare_frames", fail_frames)
        with pytest.raises(ValueError, match="frame allocation"):
            await owner.mutate(
                0, delta=[{"op": "replace", "path": "/theme/mode", "value": "light"}]
            )
        assert owner.state == snapshot["snapshot"]
        for name in ("dashboard.json", "dashboard.json.last-good"):
            assert (settings.state_dir / name).read_bytes() == before
        assert subscriber.queue.empty()
        assert not subscriber.disconnected.is_set()
    finally:
        owner.close()


async def test_out_of_order_ack_history_retention_survives_restart(settings: Settings) -> None:
    owner = Owner(replace(settings, history_limit=1), Contracts())
    owner.start()
    try:
        for name in ("old", "new"):
            await owner.enqueue(
                {"kind": "remote_key", "client_event_id": name, "observed_revision": 0, "key": "ok"}
            )
        assert await owner.ack([2]) == [2]
        assert await owner.ack([1]) == [1]
        assert owner.queue["acknowledged"] == [1]
        owner.close()
        owner.start()
        assert await owner.ack([1]) == [1]
        assert len(owner.queue["history"]) == 1
    finally:
        owner.close()


@pytest.mark.parametrize("failure", ["queue", "storage"])
async def test_inbox_retries_transient_ingest_failures(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    directory = settings.state_dir.parent / "inbox"
    settings = replace(settings, inbox_dir=directory, inbox_interval_s=60, queue_limit=1)
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        owner = app.state.owner
        inbox = app.state.inbox
        original_write = owner.store.write
        if failure == "queue":
            await owner.enqueue(
                {
                    "kind": "remote_key",
                    "client_event_id": "first",
                    "observed_revision": 0,
                    "key": "ok",
                }
            )
        else:

            def unavailable(*args: Any, **kwargs: Any) -> None:
                raise StorageError("unavailable")

            monkeypatch.setattr(owner.store, "write", unavailable)
        command = directory / "retry.json"
        command.write_text(
            json.dumps(
                {
                    "expected_revision": 0,
                    "event": {"type": "CUSTOM", "name": "aos.remote_key", "value": {"key": "up"}},
                }
            )
        )
        original = command.read_bytes()
        assert await inbox.scan_once() == 0
        assert command.read_bytes() == original
        assert list((directory / "rejected").iterdir()) == []
        if failure == "queue":
            await owner.ack([1])
        else:
            monkeypatch.setattr(owner.store, "write", original_write)
        assert await inbox.scan_once() == 1
        assert not command.exists()
        assert (directory / "processed/retry.json").read_bytes() == original
        assert len(owner.queue["pending"]) == 1
        assert owner.queue["pending"][0]["payload"]["key"] == "up"
        assert inbox.last_error is None


async def test_inbox_pending_dedup_after_receipt_failure_and_restart(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = settings.state_dir.parent / "inbox"
    settings = replace(settings, inbox_dir=directory, inbox_interval_s=60)
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        command = directory / "keypress.json"
        command.write_text(
            json.dumps(
                {
                    "expected_revision": 0,
                    "event": {"type": "CUSTOM", "name": "aos.remote_key", "value": {"key": "ok"}},
                }
            )
        )
        original_rename = os.rename
        failed = False

        def fail_once(*args: Any, **kwargs: Any) -> None:
            nonlocal failed
            if not failed:
                failed = True
                raise OSError("receipt rename unavailable")
            original_rename(*args, **kwargs)

        monkeypatch.setattr(os, "rename", fail_once)
        inbox = app.state.inbox
        owner = app.state.owner
        assert await inbox.scan_once() == 0
        assert command.exists() and len(owner.queue["pending"]) == 1
        payload = {
            key: value
            for key, value in owner.state.items()
            if key not in {"revision", "updated_at"}
        }
        await owner.mutate(0, payload=payload)
        owner.close()
        owner.start()
        assert await inbox.scan_once() == 1
        assert not command.exists()
        assert [event["id"] for event in owner.queue["pending"]] == [1]
        assert owner.queue["next_id"] == 2


def test_display_snapshot_is_group_readable_but_queue_is_private(settings: Settings) -> None:
    owner = Owner(settings, Contracts())
    owner.start()
    try:
        for name in ("dashboard.json", "dashboard.json.last-good"):
            assert stat.S_IMODE((settings.state_dir / name).stat().st_mode) == 0o640
        for name in ("events.json", "events.json.last-good"):
            assert stat.S_IMODE((settings.state_dir / name).stat().st_mode) == 0o600
    finally:
        owner.close()


def test_full_queue_returns_503_to_display(settings: Settings) -> None:
    with TestClient(
        create_app(replace(settings, queue_limit=1)),
        base_url="http://localhost",
        headers={"Authorization": "Bearer display-secret"},
    ) as client:
        intent = {
            "kind": "remote_key",
            "client_event_id": "first",
            "observed_revision": 0,
            "key": "ok",
        }
        assert client.post("/v1/input", json=intent).json() == {"id": 1}
        assert (
            client.post("/v1/input", json={**intent, "client_event_id": "second"}).status_code
            == 503
        )
        assert client.post("/v1/input", json=intent).json() == {"id": 1}
        assert client.get("/v1/status").json()["health"] == "degraded"


@pytest.mark.parametrize("variable", ["WEB_CONCURRENCY", "UVICORN_WORKERS"])
def test_cli_rejects_multi_worker_env(monkeypatch: pytest.MonkeyPatch, variable: str) -> None:
    monkeypatch.setattr(sys, "argv", ["aos-bridge"])
    monkeypatch.setenv(variable, "2")
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 2


@pytest.mark.parametrize("args", [["--workers", "2"], ["--port", "0"], ["--host", "0.0.0.0"]])
def test_cli_rejects_nonlocal_or_invalid_arguments(
    monkeypatch: pytest.MonkeyPatch, args: list[str]
) -> None:
    monkeypatch.setattr(sys, "argv", ["aos-bridge", *args])
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 2


def test_cli_defaults_loopback_8080_single_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["aos-bridge"])
    monkeypatch.delenv("WEB_CONCURRENCY", raising=False)
    monkeypatch.delenv("UVICORN_WORKERS", raising=False)
    calls: list[dict[str, Any]] = []

    def run(app: object, **kwargs: Any) -> None:
        calls.append(kwargs)

    monkeypatch.setattr(cli.uvicorn, "run", run)
    cli.main()
    assert calls == [{"host": "127.0.0.1", "port": 8080, "workers": 1}]


def test_environment_mapping_and_credential_file_precedence(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("AOS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("AOS_BRIDGE_TOKEN", "direct")
    monkeypatch.setenv("AOS_BRIDGE_TOKEN_FILE", str(tmp_path / "rotating.token"))
    monkeypatch.setenv("AOS_DISPLAY_TOKEN_FILE", str(tmp_path / "display.token"))
    monkeypatch.setenv("AOS_MUSE_TOKEN_FILE", str(tmp_path / "muse.token"))
    monkeypatch.setenv("AOS_CEC_SOCKET", str(tmp_path / "cec.sock"))
    monkeypatch.setenv("AOS_INBOX_DIR", str(tmp_path / "inbox"))
    settings = Settings.from_env()
    assert settings.state_dir == tmp_path / "state"
    assert settings.bridge_token == "direct"
    assert settings.bridge_token_file == tmp_path / "rotating.token"
    assert settings.display_token_file == tmp_path / "display.token"
    assert settings.muse_token_file == tmp_path / "muse.token"
    assert settings.cec_socket == tmp_path / "cec.sock"
    assert settings.inbox_dir == tmp_path / "inbox"


async def test_http_concurrent_writers_have_one_winner(settings: Settings) -> None:
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://localhost",
            headers={"Authorization": "Bearer bridge-secret"},
        ) as client:
            current = (await client.get("/v1/dashboard")).json()
            payload = {
                key: value
                for key, value in current.items()
                if key not in {"revision", "updated_at"}
            }
            _, subscriber = await app.state.owner.subscribe()
            responses = await asyncio.gather(
                *[
                    client.post("/v1/dashboard", json={"payload": payload, "expected_revision": 0})
                    for _ in range(2)
                ]
            )
            assert sorted(response.status_code for response in responses) == [200, 409]
            assert subscriber.queue.qsize() == 1
            assert (await client.get("/v1/dashboard")).json()["revision"] == 1


def test_actual_fsync_failure_rolls_back_without_sse_publication(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = create_app(settings)
    with TestClient(
        app, base_url="http://localhost", headers={"Authorization": "Bearer bridge-secret"}
    ) as client:
        owner = app.state.owner
        before = client.get("/v1/dashboard").json()
        files = {
            name: (settings.state_dir / name).read_bytes()
            for name in ("dashboard.json", "dashboard.json.last-good")
        }
        assert client.portal is not None
        _, subscriber = client.portal.call(owner.subscribe)
        original_sync = owner.store._sync_directory
        count = 0

        def fail_once() -> None:
            nonlocal count
            count += 1
            if count == 1:
                raise OSError("directory fsync failure")
            original_sync()

        monkeypatch.setattr(owner.store, "_sync_directory", fail_once)
        payload = {
            key: value for key, value in before.items() if key not in {"revision", "updated_at"}
        }
        response = client.post("/v1/dashboard", json={"payload": payload, "expected_revision": 0})
        assert response.status_code == 503
        assert owner.state == before and subscriber.queue.empty()
        assert {name: (settings.state_dir / name).read_bytes() for name in files} == files
        assert not owner.store.poisoned
        response = client.post("/v1/dashboard", json={"payload": payload, "expected_revision": 0})
        assert response.status_code == 200 and response.json()["revision"] == 1
        assert subscriber.queue.qsize() == 1


async def test_heartbeat_cadence_is_not_starved_by_other_frames(settings: Settings) -> None:
    owner = Owner(replace(settings, heartbeat_s=0.01), Contracts())
    owner.start()
    generator = None
    try:
        snapshot, subscriber = await owner.subscribe()
        generator = stream(owner, snapshot, subscriber)
        await anext(generator)
        overlay = {
            "type": "CUSTOM",
            "name": "aos.overlay",
            "value": {"message": "note", "duration_s": 5},
        }
        await owner.custom(overlay)
        assert json.loads((await anext(generator))[6:])["name"] == "aos.overlay"
        await owner.custom(overlay)
        await asyncio.sleep(0.02)
        assert json.loads((await anext(generator))[6:])["name"] == "aos.heartbeat"
        assert json.loads((await anext(generator))[6:])["name"] == "aos.overlay"
        assert owner.state == snapshot["snapshot"]
    finally:
        if generator is not None:
            await generator.aclose()
        owner.close()


def test_existing_boot_file_gets_a_validated_recovery_copy(settings: Settings) -> None:
    settings.state_dir.mkdir()
    state = Contracts().sample_dashboard()
    (settings.state_dir / "dashboard.json").write_text(json.dumps(state))
    owner = Owner(settings, Contracts())
    owner.start()
    try:
        assert json.loads((settings.state_dir / "dashboard.json.last-good").read_text()) == state
        owner.close()
        (settings.state_dir / "dashboard.json").write_text("corrupt")
        owner.start()
        assert owner.state == state
    finally:
        owner.close()
