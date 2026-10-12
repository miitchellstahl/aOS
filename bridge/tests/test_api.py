from __future__ import annotations

import json
from dataclasses import replace
from typing import Any, cast

import httpx
import pytest
from aos_bridge.app import create_app
from aos_bridge.owner import Owner
from aos_bridge.settings import Settings
from aos_bridge.store import StorageError
from fastapi import FastAPI
from fastapi.testclient import TestClient

READS = ["/v1/dashboard", "/v1/schema", "/v1/status", "/v1/events"]
WRITES = [
    "/v1/dashboard",
    "/v1/ingest",
    "/v1/overlay",
    "/v1/input",
    "/v1/events/ack",
    "/v1/device/power",
    "/v1/device/input",
]


def replacement(client: TestClient) -> dict[str, Any]:
    state = client.get("/v1/dashboard").json()
    state.pop("revision")
    state.pop("updated_at")
    return state


def remote(client_id: str = "keypress") -> dict[str, Any]:
    return {"kind": "remote_key", "client_event_id": client_id, "observed_revision": 0, "key": "ok"}


@pytest.mark.parametrize("path", READS + ["/v1/stream"])
def test_read_auth(client: TestClient, path: str) -> None:
    assert client.get(path, headers={"Authorization": ""}).status_code == 401
    assert client.get(path, headers={"Authorization": "Bearer wrong"}).status_code == 401


@pytest.mark.parametrize("path", WRITES)
def test_write_auth(client: TestClient, path: str) -> None:
    assert client.post(path, json={}, headers={"Authorization": "Bearer wrong"}).status_code == 401


@pytest.mark.parametrize(
    "host",
    [
        "attacker.example",
        "localhost.attacker.example",
        "192.168.1.5",
        "127.0.0.1@attacker.example",
        "127.0.0.1/evil",
        "127.0.0.1:999999",
        "[::1",
    ],
)
def test_host_defense(client: TestClient, host: str) -> None:
    assert client.get("/v1/dashboard", headers={"Host": host}).status_code == 403


@pytest.mark.parametrize("origin", ["https://attacker.example", "null", "http://localhost:8080"])
def test_browser_origins_forbidden(client: TestClient, origin: str) -> None:
    assert client.get("/v1/dashboard", headers={"Origin": origin}).status_code == 403


@pytest.mark.parametrize(
    "path",
    [
        "/v1/dashboard",
        "/v1/ingest",
        "/v1/overlay",
        "/v1/events/ack",
        "/v1/device/power",
        "/v1/device/input",
    ],
)
def test_display_forbidden(client: TestClient, path: str) -> None:
    assert (
        client.post(path, json={}, headers={"Authorization": "Bearer display-secret"}).status_code
        == 403
    )


def test_scope_reads_and_muse_control(client: TestClient) -> None:
    for scope in ("display", "muse"):
        for path in READS[:3]:
            assert (
                client.get(path, headers={"Authorization": f"Bearer {scope}-secret"}).status_code
                == 200
            )
    assert (
        client.get("/v1/events", headers={"Authorization": "Bearer display-secret"}).status_code
        == 403
    )
    assert (
        client.post(
            "/v1/input", json=remote(), headers={"Authorization": "Bearer muse-secret"}
        ).status_code
        == 403
    )
    for path in ("/v1/device/power", "/v1/device/input", "/v1/dashboard"):
        assert (
            client.post(path, json={}, headers={"Authorization": "Bearer muse-secret"}).status_code
            == 403
        )
    event = {"type": "CUSTOM", "name": "aos.device", "value": {"action": "power_on"}}
    assert (
        client.post(
            "/v1/ingest", json=event, headers={"Authorization": "Bearer muse-secret"}
        ).status_code
        == 403
    )


def test_missing_tokens_fail_closed(settings: Settings) -> None:
    with TestClient(
        create_app(replace(settings, bridge_token=None, display_token=None, muse_token=None)),
        base_url="http://127.0.0.1",
    ) as client:
        assert (
            client.get(
                "/v1/dashboard", headers={"Authorization": "Bearer bridge-secret"}
            ).status_code
            == 401
        )


def test_file_token_rotation_and_revocation(settings: Settings) -> None:
    token_file = settings.state_dir.parent / "bridge.token"
    token_file.write_text("original\n")
    settings = replace(settings, bridge_token_file=token_file)
    with TestClient(create_app(settings), base_url="http://localhost") as client:

        def read(token: str) -> int:
            return client.get(
                "/v1/dashboard", headers={"Authorization": f"Bearer {token}"}
            ).status_code

        assert read("original") == 200
        assert read("bridge-secret") == 401
        token_file.write_text("rotated\n")
        assert read("original") == 401
        assert read("rotated") == 200
        token_file.unlink()
        assert read("rotated") == 401
        assert read("bridge-secret") == 401
        token_file.symlink_to(settings.state_dir / "dashboard.json")
        assert read("original") == 401


def test_duplicate_capability_credentials_fail_closed(settings: Settings) -> None:
    with TestClient(
        create_app(replace(settings, display_token=settings.bridge_token)),
        base_url="http://localhost",
    ) as client:
        assert (
            client.get(
                "/v1/dashboard", headers={"Authorization": "Bearer bridge-secret"}
            ).status_code
            == 401
        )


def test_replace_and_patch_persist(client: TestClient, settings: Settings) -> None:
    response = client.get("/v1/dashboard")
    assert response.headers["etag"] == '"0"'
    initial = response.json()
    payload = replacement(client)
    payload["theme"]["accent"] = "#123456"
    response = client.post("/v1/dashboard", json={"payload": payload, "expected_revision": 0})
    assert response.status_code == 200
    assert response.json()["revision"] == 1
    assert response.json()["updated_at"] != initial["updated_at"]
    patch = {
        "type": "STATE_DELTA",
        "delta": [{"op": "replace", "path": "/theme/mode", "value": "light"}],
    }
    response = client.post("/v1/ingest", json=patch, headers={"If-Match": '"1"'})
    assert response.status_code == 200
    assert response.json()["revision"] == 2
    assert json.loads((settings.state_dir / "dashboard.json").read_text()) == response.json()
    assert (
        json.loads((settings.state_dir / "dashboard.json.last-good").read_text()) == response.json()
    )
    assert client.post("/v1/ingest", json=patch, headers={"If-Match": '"1"'}).status_code == 409


@pytest.mark.parametrize(
    "delta",
    [
        [{"op": "replace", "path": "", "value": {}}],
        [{"op": "replace", "path": "/revision", "value": 123}],
        [{"op": "replace", "path": "/updated_at", "value": "2026-01-01T00:00:00Z"}],
        [{"op": "remove", "path": "/theme"}],
        [{"op": "replace", "path": "/not-present", "value": 1}],
        [{"op": "test", "path": "/revision", "value": 99}],
        [
            {"op": "replace", "path": "/theme/mode", "value": "light"},
            {"op": "remove", "path": "/not-present"},
        ],
        [{"op": "replace", "path": "/theme/mode", "value": "invalid"}],
        [{"op": "move", "from": "/revision", "path": "/theme/mode"}],
        [{"op": "invalid", "path": "/theme"}],
        None,
        {},
    ],
)
def test_invalid_patch_atomic(client: TestClient, settings: Settings, delta: object) -> None:
    before = client.get("/v1/dashboard").json()
    disk = (settings.state_dir / "dashboard.json").read_bytes()
    response = client.post(
        "/v1/ingest", json={"type": "STATE_DELTA", "delta": delta}, headers={"If-Match": '"0"'}
    )
    assert response.status_code == 422
    assert "detail" in response.json()
    assert client.get("/v1/dashboard").json() == before
    assert (settings.state_dir / "dashboard.json").read_bytes() == disk


@pytest.mark.parametrize("value", [None, "0", "0", "*", 'W/"0"', '"-1"', '"0", "1"'])
def test_if_match_required(client: TestClient, value: str | None) -> None:
    headers = {"If-Match": value} if value is not None else {}
    event = {"type": "STATE_DELTA", "delta": []}
    assert client.post("/v1/ingest", json=event, headers=headers).status_code == 422


def test_ingest_snapshot_replacement_and_muse(client: TestClient) -> None:
    snapshot = replacement(client)
    response = client.post(
        "/v1/ingest",
        json={"type": "STATE_SNAPSHOT", "snapshot": snapshot},
        headers={"If-Match": '"0"', "Authorization": "Bearer muse-secret"},
    )
    assert response.status_code == 200
    event = {"type": "STATE_SNAPSHOT", "snapshot": response.json()}
    assert client.post("/v1/ingest", json=event, headers={"If-Match": '"1"'}).status_code == 422


@pytest.mark.parametrize(
    "value",
    [
        {},
        {"type": "RUN_STARTED"},
        {"type": []},
        {"type": "stateDelta", "delta": []},
        {"type": "CUSTOM", "name": "unknown", "value": {}},
        {"type": "CUSTOM", "name": "aos.overlay", "value": {"message": "hello", "duration_s": 100}},
    ],
)
def test_invalid_ingress(client: TestClient, value: dict[str, Any]) -> None:
    assert client.post("/v1/ingest", json=value).status_code == 422


@pytest.mark.parametrize("value", [True, -1, "0", None])
def test_invalid_expected_revision(client: TestClient, value: object) -> None:
    assert (
        client.post(
            "/v1/dashboard", json={"payload": replacement(client), "expected_revision": value}
        ).status_code
        == 422
    )


def test_storage_failure_does_not_publish(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    owner: Owner = cast(FastAPI, client.app).state.owner
    before = client.get("/v1/dashboard").json()
    disk = (settings.state_dir / "dashboard.json").read_bytes()
    assert client.portal is not None
    snapshot, subscriber = client.portal.call(owner.subscribe)

    def fail(name: str, value: dict[str, Any]) -> None:
        raise StorageError("failed")

    monkeypatch.setattr(owner.store, "write", fail)
    response = client.post(
        "/v1/dashboard", json={"payload": replacement(client), "expected_revision": 0}
    )
    assert response.status_code == 503
    assert subscriber.queue.empty()
    assert client.get("/v1/dashboard").json() == before == snapshot["snapshot"]
    assert (settings.state_dir / "dashboard.json").read_bytes() == disk
    assert client.get("/v1/status").json()["health"] == "degraded"
    assert client.post("/v1/input", json=remote()).status_code == 503
    assert client.get("/v1/events").json()["events"] == []


def test_overlay_and_dismissal_do_not_refresh_content(client: TestClient) -> None:
    before = client.get("/v1/dashboard").json()
    assert client.post("/v1/overlay", json={"message": "hello", "duration_s": 5}).status_code == 200
    event = {"type": "CUSTOM", "name": "aos.heartbeat", "value": {"revision": 0}}
    assert client.post("/v1/ingest", json=event).status_code == 403
    intent = {"kind": "overlay_dismissed", "client_event_id": "dismiss", "observed_revision": 0}
    assert (
        client.post(
            "/v1/input", json=intent, headers={"Authorization": "Bearer display-secret"}
        ).status_code
        == 200
    )
    assert client.get("/v1/dashboard").json() == before
    assert client.get("/v1/events").json()["events"][0]["kind"] == "overlay_dismissed"


def test_todo_target_and_revision_checked(client: TestClient) -> None:
    state = client.get("/v1/dashboard").json()
    todos = next(section for section in state["sections"] if section["type"] == "todos")
    intent = {
        "kind": "todo_toggle",
        "client_event_id": "todo",
        "observed_revision": 0,
        "section_id": todos["id"],
        "target_id": todos["items"][0]["id"],
        "done": True,
    }
    assert client.post("/v1/input", json={**intent, "observed_revision": 1}).status_code == 409
    assert client.post("/v1/input", json={**intent, "target_id": "missing"}).status_code == 422
    assert client.post("/v1/input", json={**intent, "section_id": "status"}).status_code == 422
    assert client.post("/v1/input", json=intent).json() == {"id": 1}
    assert client.get("/v1/dashboard").json() == state
    client.post("/v1/dashboard", json={"payload": replacement(client), "expected_revision": 0})
    # retries of a still-pending valid intent remain safe after the dashboard changes.
    assert client.post("/v1/input", json=intent).json() == {"id": 1}
    assert client.post("/v1/input", json={**intent, "client_event_id": "other"}).status_code == 409
    assert client.post("/v1/input", json={**intent, "done": False}).status_code == 409


@pytest.mark.parametrize(
    "query",
    [
        "since=-1",
        "since=x",
        "since=true",
        "limit=0",
        "limit=101",
        "since=9223372036854775808",
        "limit=1&limit=2",
        "extra=1",
    ],
)
def test_pagination_invalid(client: TestClient, query: str) -> None:
    assert client.get(f"/v1/events?{query}").status_code == 422


@pytest.mark.parametrize("ids", [[0], [-1], [True], ["1"], [2**63], [], None, [1] * 1001])
def test_ack_invalid(client: TestClient, ids: object) -> None:
    assert client.post("/v1/events/ack", json={"ids": ids}).status_code == 422


def test_device_unavailable_never_success(client: TestClient) -> None:
    assert client.post("/v1/device/power", json={"state": "on"}).status_code == 503
    assert client.post("/v1/device/input", json={"action": "active_source"}).status_code == 503
    assert client.post("/v1/device/power", json={"state": "maybe"}).status_code == 422
    assert client.post("/v1/device/input", json={"action": "shell"}).status_code == 422


@pytest.mark.asyncio
async def test_streaming_body_limit_without_content_length(settings: Settings) -> None:
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://127.0.0.1",
            headers={"Authorization": "Bearer bridge-secret"},
        ) as client:

            async def chunks() -> Any:
                for _ in range(5):
                    yield b"x" * 32768

            response = await client.post("/v1/input", content=chunks())
            assert response.status_code == 413
            response = await client.post(
                "/v1/input", content=chunks(), headers={"Content-Length": "1"}
            )
            assert response.status_code == 413
            assert app.state.owner.queue["pending"] == []


def test_deep_unknown_section_is_rejected_without_committing(client: TestClient) -> None:
    initial = client.get("/v1/dashboard").json()
    payload = replacement(client)
    nested: dict[str, Any] = {}
    for _ in range(600):
        nested = {"child": nested}
    payload["sections"][-1] = {"id": "future", "type": "future", "content": nested}
    response = client.post("/v1/dashboard", json={"payload": payload, "expected_revision": 0})
    assert response.status_code == 422
    assert client.get("/v1/dashboard").json() == initial


def test_json_errors_are_422(client: TestClient) -> None:
    assert client.post("/v1/input", content=b"{broken").status_code == 422
    assert client.post("/v1/input", json=[]).status_code == 422
    assert (
        client.post(
            "/v1/overlay", json={"message": "hello", "duration_s": 5, "command": "execute"}
        ).status_code
        == 422
    )
    assert (
        client.post("/v1/overlay", content=b'{"message":"x", "duration_s":NaN}').status_code == 422
    )
