"""Integrated software boundaries; no GUI, deployment or physical device claims.

Strict xfails document reproduced defects for the integration owner to fix. All
other tests exercise real bridge routes/storage, not alternate state writers.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import subprocess
from collections.abc import AsyncIterator
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
import jsonpatch
import pytest
from aos_bridge.app import create_app, stream
from aos_bridge.contracts import MAX_STATE_BYTES
from aos_bridge.owner import Owner
from aos_bridge.settings import Settings
from fastapi import FastAPI

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def integration_settings(tmp_path: Path) -> Settings:
    for role in ("bridge", "display", "muse"):
        token = tmp_path / f"{role}.token"
        token.write_text(f"{role}-integration-capability\n")
        token.chmod(0o600)
    return Settings(
        state_dir=tmp_path / "state",
        bridge_token="disabled-environment-fallback",
        bridge_token_file=tmp_path / "bridge.token",
        display_token_file=tmp_path / "display.token",
        muse_token_file=tmp_path / "muse.token",
        cec_socket=tmp_path / "absent-cec.sock",
        heartbeat_s=0.01,
    )


@pytest.fixture
async def bridge(
    integration_settings: Settings,
) -> AsyncIterator[tuple[FastAPI, httpx.AsyncClient]]:
    app = create_app(integration_settings)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://127.0.0.1:8080",
            headers={"Authorization": "Bearer bridge-integration-capability"},
            trust_env=False,
        ) as client:
            yield app, client


def bearer(role: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {role}-integration-capability"}


def replacement(state: dict[str, Any]) -> dict[str, Any]:
    return {
        key: deepcopy(value)
        for key, value in state.items()
        if key not in {"revision", "updated_at"}
    }


def decode_frame(value: str) -> dict[str, Any]:
    assert value.startswith("data: ") and value.endswith("\n\n")
    event: dict[str, Any] = json.loads(value[6:])
    return event


def document_bytes(state_dir: Path) -> dict[str, bytes]:
    return {
        name: (state_dir / name).read_bytes()
        for name in (
            "dashboard.json",
            "dashboard.json.last-good",
            "events.json",
            "events.json.last-good",
        )
    }


@pytest.mark.parametrize(
    ("role", "method", "path", "payload", "status"),
    [
        ("display", "GET", "/v1/dashboard", None, 200),
        ("display", "GET", "/v1/schema", None, 200),
        ("display", "GET", "/v1/status", None, 200),
        ("display", "GET", "/v1/events", None, 403),
        ("display", "POST", "/v1/events/ack", {"ids": [1]}, 403),
        ("display", "POST", "/v1/dashboard", {}, 403),
        ("display", "POST", "/v1/ingest", {"type": "STATE_DELTA", "delta": []}, 403),
        ("display", "POST", "/v1/device/power", {"state": "on"}, 403),
        ("display", "POST", "/v1/device/input", {"action": "active_source"}, 403),
        ("muse", "GET", "/v1/dashboard", None, 200),
        ("muse", "GET", "/v1/events", None, 200),
        ("muse", "POST", "/v1/dashboard", {}, 403),
        ("muse", "POST", "/v1/input", {}, 403),
        ("muse", "POST", "/v1/device/power", {"state": "off"}, 403),
        (
            "muse",
            "POST",
            "/v1/ingest",
            {"type": "CUSTOM", "name": "aos.device", "value": {"action": "power_on"}},
            403,
        ),
        (
            "bridge",
            "POST",
            "/v1/ingest",
            {"type": "CUSTOM", "name": "aos.heartbeat", "value": {"revision": 0}},
            403,
        ),
    ],
)
async def test_scope_checks_precede_side_effects(
    bridge: tuple[FastAPI, httpx.AsyncClient],
    integration_settings: Settings,
    role: str,
    method: str,
    path: str,
    payload: dict[str, Any] | None,
    status: int,
) -> None:
    app, client = bridge
    owner: Owner = app.state.owner
    before = document_bytes(integration_settings.state_dir)
    _, subscriber = await owner.subscribe()
    response = await client.request(method, path, headers=bearer(role), json=payload)
    assert response.status_code == status
    assert document_bytes(integration_settings.state_dir) == before
    assert subscriber.queue.empty()
    await owner.unsubscribe(subscriber)


@pytest.mark.parametrize(
    "headers",
    [
        {"Host": "evil.example"},
        {"Host": "127.0.0.1.evil.example:8080"},
        {"Host": "localhost@evil.example"},
        {"Origin": "http://localhost"},
        {"Origin": "null"},
        [("Host", "127.0.0.1:8080"), ("Host", "localhost:8080")],
    ],
)
async def test_host_origin_rejection_preserves_all_documents(
    bridge: tuple[FastAPI, httpx.AsyncClient], integration_settings: Settings, headers: Any
) -> None:
    _, client = bridge
    before = document_bytes(integration_settings.state_dir)
    response = await client.post("/v1/dashboard", headers=headers, json={})
    assert response.status_code == 403
    assert document_bytes(integration_settings.state_dir) == before


async def test_rotated_revoked_and_colliding_local_credentials_fail_closed(
    bridge: tuple[FastAPI, httpx.AsyncClient], integration_settings: Settings
) -> None:
    _, client = bridge
    display_file = integration_settings.display_token_file
    bridge_file = integration_settings.bridge_token_file
    assert display_file is not None and bridge_file is not None
    assert (await client.get("/v1/dashboard", headers=bearer("display"))).status_code == 200
    rotated = display_file.with_suffix(".new")
    rotated.write_text("rotated-display-capability\n")
    rotated.replace(display_file)
    assert (await client.get("/v1/dashboard", headers=bearer("display"))).status_code == 401
    new_header = {"Authorization": "Bearer rotated-display-capability"}
    assert (await client.get("/v1/dashboard", headers=new_header)).status_code == 200
    assert (await client.post("/v1/dashboard", headers=new_header, json={})).status_code == 403
    display_file.write_text("bridge-integration-capability\n")
    assert (await client.get("/v1/dashboard")).status_code == 401
    display_file.unlink()
    assert (await client.get("/v1/dashboard")).status_code == 200
    bridge_file.unlink()
    assert (await client.get("/v1/dashboard")).status_code == 401
    assert (
        await client.get(
            "/v1/dashboard", headers={"Authorization": "Bearer disabled-environment-fallback"}
        )
    ).status_code == 401


async def test_invalid_patch_never_changes_files_memory_or_feed(
    bridge: tuple[FastAPI, httpx.AsyncClient], integration_settings: Settings
) -> None:
    app, client = bridge
    owner: Owner = app.state.owner
    before_state = (await client.get("/v1/dashboard")).json()
    before_files = document_bytes(integration_settings.state_dir)
    _, subscriber = await owner.subscribe()
    response = await client.post(
        "/v1/ingest",
        headers={"If-Match": '"0"'},
        json={
            "type": "STATE_DELTA",
            "delta": [
                {"op": "replace", "path": "/theme/mode", "value": "light"},
                {"op": "remove", "path": "/sections/5"},
            ],
        },
    )
    assert response.status_code == 422
    assert (await client.get("/v1/dashboard")).json() == before_state
    assert document_bytes(integration_settings.state_dir) == before_files
    assert subscriber.queue.empty()
    await owner.unsubscribe(subscriber)


async def test_snapshot_concurrent_commit_overflow_and_rehydration(
    integration_settings: Settings,
) -> None:
    app = create_app(replace(integration_settings, stream_limit=1))
    async with app.router.lifespan_context(app):
        owner: Owner = app.state.owner
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://127.0.0.1:8080",
            headers=bearer("bridge"),
        ) as client:
            snapshot, subscriber = await owner.subscribe()
            generator = stream(owner, snapshot, subscriber)
            try:
                request = {"payload": replacement(snapshot["snapshot"]), "expected_revision": 0}
                responses = await asyncio.gather(
                    client.post("/v1/dashboard", json=request),
                    client.post("/v1/dashboard", json=request),
                )
                assert sorted(response.status_code for response in responses) == [200, 409]
                first = decode_frame(await anext(generator))
                assert first == snapshot and first["snapshot"]["revision"] == 0
                # force the heartbeat ahead of queued output without advertising its revision.
                await asyncio.sleep(0.02)
                heartbeat = decode_frame(await anext(generator))
                assert heartbeat["name"] == "aos.heartbeat"
                assert heartbeat["value"]["revision"] == 0
                delta = decode_frame(await anext(generator))
                committed = jsonpatch.apply_patch(first["snapshot"], delta["delta"])
                assert committed == (await client.get("/v1/dashboard")).json()
                assert committed["revision"] == 1
                await owner.custom(
                    {
                        "type": "CUSTOM",
                        "name": "aos.overlay",
                        "value": {"message": "one", "duration_s": 5},
                    }
                )
                await owner.mutate(
                    1, delta=[{"op": "replace", "path": "/theme/mode", "value": "light"}]
                )
                assert subscriber.disconnected.is_set()
                with pytest.raises(StopAsyncIteration):
                    await anext(generator)
                fresh, resumed = await owner.subscribe()
                try:
                    assert fresh["snapshot"] == (await client.get("/v1/dashboard")).json()
                    assert fresh["snapshot"]["revision"] == 2
                    assert resumed.queue.empty()
                finally:
                    await owner.unsubscribe(resumed)
            finally:
                await generator.aclose()


async def test_todo_commit_lost_ack_restart_and_duplicate_receipt(
    integration_settings: Settings,
) -> None:
    intent = {
        "kind": "todo_toggle",
        "client_event_id": "stable-intent",
        "observed_revision": 0,
        "section_id": "todos",
        "target_id": "t1",
        "done": True,
    }
    app = create_app(integration_settings)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://127.0.0.1:8080",
            headers=bearer("bridge"),
        ) as client:
            initial = (await client.get("/v1/dashboard")).json()
            response = await client.post("/v1/input", headers=bearer("display"), json=intent)
            assert response.status_code == 200 and response.json() == {"id": 1}
            assert (await client.get("/v1/dashboard")).json() == initial
            mutation = await client.post(
                "/v1/ingest",
                headers={"If-Match": '"0"'},
                json={
                    "type": "STATE_DELTA",
                    "delta": [{"op": "replace", "path": "/sections/2/items/0/done", "value": True}],
                },
            )
            assert mutation.status_code == 200
            committed = mutation.json()
            # the response/ack is lost after a successful state commit; queue survives restart.
            assert (await client.get("/v1/events")).json()["events"][0]["id"] == 1
    (integration_settings.state_dir / "dashboard.json").write_bytes(b"corrupt")
    (integration_settings.state_dir / "events.json").write_bytes(b"corrupt")
    restarted = create_app(integration_settings)
    async with restarted.router.lifespan_context(restarted):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=restarted),
            base_url="http://127.0.0.1:8080",
            headers=bearer("bridge"),
        ) as client:
            assert (await client.get("/v1/dashboard")).json() == committed
            assert (
                await client.post("/v1/input", headers=bearer("display"), json=intent)
            ).json() == {"id": 1}
            for _ in range(2):
                ack = await client.post(
                    "/v1/events/ack", headers=bearer("muse"), json={"ids": [1, 1]}
                )
                assert ack.status_code == 200 and ack.json() == {"acknowledged": [1]}
            assert (
                await client.post("/v1/input", headers=bearer("display"), json=intent)
            ).json() == {"id": 1}
            changed = {**intent, "done": False}
            assert (
                await client.post("/v1/input", headers=bearer("display"), json=changed)
            ).status_code == 409
            assert (await client.get("/v1/events")).json()["events"] == []
            assert (await client.get("/v1/dashboard")).json() == committed
            next_intent = {
                "kind": "remote_key",
                "client_event_id": "next-id",
                "observed_revision": 1,
                "key": "ok",
            }
            assert (
                await client.post("/v1/input", headers=bearer("display"), json=next_intent)
            ).json() == {"id": 2}


async def test_ack_fsync_failure_preserves_redelivery_and_dedup(
    bridge: tuple[FastAPI, httpx.AsyncClient],
    integration_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, client = bridge
    owner: Owner = app.state.owner
    intent = {
        "kind": "remote_key",
        "client_event_id": "ack-failure",
        "observed_revision": 0,
        "key": "ok",
    }
    assert (await client.post("/v1/input", headers=bearer("display"), json=intent)).json() == {
        "id": 1
    }
    before = document_bytes(integration_settings.state_dir)
    original_sync = owner.store._sync_directory
    count = 0

    def fail_once() -> None:
        nonlocal count
        count += 1
        if count == 1:
            raise OSError("injected directory sync failure")
        original_sync()

    monkeypatch.setattr(owner.store, "_sync_directory", fail_once)
    failed = await client.post("/v1/events/ack", json={"ids": [1]})
    assert failed.status_code == 503
    assert document_bytes(integration_settings.state_dir) == before
    assert (await client.get("/v1/events")).json()["events"][0]["id"] == 1
    assert (await client.post("/v1/input", headers=bearer("display"), json=intent)).json() == {
        "id": 1
    }
    assert (await client.post("/v1/events/ack", json={"ids": [1]})).json() == {"acknowledged": [1]}


async def test_unavailable_device_does_not_publish_a_success_event(
    bridge: tuple[FastAPI, httpx.AsyncClient], integration_settings: Settings
) -> None:
    app, client = bridge
    owner: Owner = app.state.owner
    before = document_bytes(integration_settings.state_dir)
    _, subscriber = await owner.subscribe()
    try:
        response = await client.post("/v1/device/power", json={"state": "on"})
        assert response.status_code == 503
        assert subscriber.queue.empty()
        assert document_bytes(integration_settings.state_dir) == before
        assert (await client.get("/v1/status")).json()["capabilities"]["device_control"] is False
    finally:
        await owner.unsubscribe(subscriber)


async def test_every_accepted_dashboard_can_rehydrate_with_a_valid_wire_snapshot(
    bridge: tuple[FastAPI, httpx.AsyncClient],
) -> None:
    app, client = bridge
    current = (await client.get("/v1/dashboard")).json()
    current["sections"].append({"id": "future", "type": "future", "blob": ""})
    compact = json.dumps(current, ensure_ascii=False, separators=(",", ":")).encode()
    current["sections"][-1]["blob"] = "x" * (MAX_STATE_BYTES - 16 - len(compact))
    response = await client.post(
        "/v1/dashboard", json={"payload": replacement(current), "expected_revision": 0}
    )
    assert response.status_code == 200
    assert len(response.content) <= MAX_STATE_BYTES
    owner: Owner = app.state.owner
    snapshot, subscriber = await owner.subscribe()
    generator = stream(owner, snapshot, subscriber)
    try:
        event = decode_frame(await anext(generator))
        assert event["snapshot"] == response.json()
        # snapshots actually emitted by the bridge must pass its own wire validator.
        owner.contracts.validate_event(event)
    finally:
        await generator.aclose()


@pytest.mark.parametrize(
    "optimize",
    ["", "1", "2"],
)
def test_runtime_artifact_guards_survive_python_optimization(
    tmp_path: Path,
    optimize: str,
) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    # invalid synthetic files are validation inputs only; never execute them.
    for name in ("flutter-pi", "libflutter_engine.so", "icudtl.dat"):
        (runtime / name).write_bytes(b"definitely not an ARM64 appliance runtime")
    (runtime / "flutter-pi").chmod(0o755)
    value = {
        "flutter_version": "3.38.10",
        "flutterpi_tool_version": "0.10.1",
        "framework_revision": "a" * 40,
        "engine_revision": "b" * 40,
        "flutter_pi_revision": "c" * 40,
        "runtime_sha256": "0" * 64,
        "engine_sha256": "0" * 64,
        "icu_sha256": "0" * 64,
    }
    assert (
        hashlib.sha256((runtime / "flutter-pi").read_bytes()).hexdigest() != value["runtime_sha256"]
    )
    tuple_file = tmp_path / "tuple.json"
    tuple_file.write_text(json.dumps(value))
    result = subprocess.run(
        [
            "bash",
            str(ROOT / "scripts/build-app.sh"),
            "--validate-only",
            "--tuple",
            str(tuple_file),
            "--runtime",
            str(runtime),
        ],
        env={**os.environ, "PYTHONOPTIMIZE": optimize},
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode != 0
    assert "runtime/tuple validation failed" in result.stderr


@pytest.mark.parametrize(
    "optimize",
    ["", "1", "2"],
)
def test_deploy_rejects_invalid_bundle_even_with_matching_runtime_hashes(
    tmp_path: Path, optimize: str
) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    # Header-level synthetic fixtures are not a certified/usable ARM64 runtime.
    elf = b"\x7fELF\x02\x01" + b"\x00" * 12 + (183).to_bytes(2, "little")
    value = {
        "flutter_version": "3.38.10",
        "flutterpi_tool_version": "0.10.1",
        "framework_revision": "a" * 40,
        "engine_revision": "b" * 40,
        "flutter_pi_revision": "c" * 40,
    }
    for filename, key in (
        ("flutter-pi", "runtime_sha256"),
        ("libflutter_engine.so", "engine_sha256"),
        ("icudtl.dat", "icu_sha256"),
    ):
        content = b"synthetic ICU" if filename == "icudtl.dat" else elf
        (runtime / filename).write_bytes(content)
        value[key] = hashlib.sha256(content).hexdigest()
    (runtime / "flutter-pi").chmod(0o755)
    tuple_file = tmp_path / "tuple.json"
    tuple_file.write_text(json.dumps(value))
    bundle = tmp_path / "bundle"
    (bundle / "app").mkdir(parents=True)
    (bundle / "bundle.json").write_text(json.dumps({"hardware_validated": True, "tuple": value}))
    (bundle / "app/app.so").write_bytes(b"not ARM64 AOT")
    for filename in ("libflutter_engine.so", "icudtl.dat"):
        (bundle / "app" / filename).write_bytes(b"incorrect hash")
    version = "security-" + hashlib.sha256(str(tmp_path).encode()).hexdigest()[:12]
    result = subprocess.run(
        [
            "bash",
            str(ROOT / "scripts/deploy.sh"),
            "--dry-run",
            "--version",
            version,
            "--source",
            str(ROOT),
            "--bundle",
            str(bundle),
            "--runtime",
            str(runtime),
            "--tuple",
            str(tuple_file),
            "--uv",
            "/usr/bin/true",
        ],
        env={**os.environ, "PYTHONOPTIMIZE": optimize},
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode != 0
    assert "bundle is missing, differs from runtime tuple, or is not ARM64 AOT" in result.stderr
