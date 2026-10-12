from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from aos_bridge.app import create_app
from aos_bridge.settings import Settings
from fastapi.testclient import TestClient


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        state_dir=tmp_path / "state",
        bridge_token="bridge-secret",
        display_token="display-secret",
        muse_token="muse-secret",
        cec_socket=tmp_path / "cec.sock",
    )


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    with TestClient(
        create_app(settings),
        base_url="http://127.0.0.1:8080",
        headers={"Authorization": "Bearer bridge-secret"},
    ) as test_client:
        yield test_client
