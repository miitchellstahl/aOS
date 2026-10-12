from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest
from aos_bridge.app import create_app
from aos_bridge.settings import Settings
from aos_muse.companion import Companion


async def test_real_scoped_bridge_queue_handoff_not_state_processing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    token = tmp_path / "muse.token"
    token.write_text("fake-local-muse")
    app = create_app(
        Settings(
            state_dir=tmp_path / "bridge",
            bridge_token="fake-general",
            display_token="fake-display",
            muse_token="fake-local-muse",
        )
    )
    sent: list[dict[str, Any]] = []

    async def send(self: Companion, event: dict[str, Any]) -> None:
        sent.append(event)

    monkeypatch.setattr(Companion, "send", send)
    async with app.router.lifespan_context(app):
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app))
        companion = Companion("fake-side-chat", token, tmp_path / "journal", client=client)
        try:
            dashboard = (
                await client.get(
                    "http://127.0.0.1:8080/v1/dashboard",
                    headers={"Authorization": "Bearer fake-display"},
                )
            ).json()
            todos = next(section for section in dashboard["sections"] if section["type"] == "todos")
            todo = todos["items"][0]
            response = await client.post(
                "http://127.0.0.1:8080/v1/input",
                headers={"Authorization": "Bearer fake-display"},
                json={
                    "kind": "todo_toggle",
                    "client_event_id": "fake-click",
                    "observed_revision": dashboard["revision"],
                    "section_id": todos["id"],
                    "target_id": todo["id"],
                    "done": not todo["done"],
                },
            )
            assert response.status_code == 200
            assert await companion.cycle() == 1
            assert len(sent) == 1
            assert sent[0]["id"] == response.json()["id"]
            assert (await companion.request("GET", "/v1/events"))["events"] == []
            assert (await companion.request("GET", "/v1/dashboard")) == dashboard
            for path, body in (
                ("/v1/device/power", {"state": "on"}),
                ("/v1/dashboard", {"expected_revision": 0, "payload": {}}),
            ):
                result = await client.post(
                    "http://127.0.0.1:8080" + path,
                    headers={"Authorization": "Bearer fake-local-muse"},
                    json=body,
                )
                assert result.status_code == 403
        finally:
            await companion.close()
