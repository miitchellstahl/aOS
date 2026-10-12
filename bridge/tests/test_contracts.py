from __future__ import annotations

import hashlib
import json
import shutil
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aos_bridge.contracts as contracts_module
import pytest
from aos_bridge.contracts import MAX_PATCH_OPERATIONS, MAX_STATE_BYTES, Contracts

CONTRACTS_DIR = Path(__file__).resolve().parents[2] / "contracts"
SHARED_CASES = json.loads((CONTRACTS_DIR / "fixtures/dashboard-cases.json").read_text())


@pytest.fixture
def contracts(monkeypatch: pytest.MonkeyPatch) -> Contracts:
    monkeypatch.delenv("AOS_CONTRACTS_DIR", raising=False)
    return Contracts()


@pytest.mark.parametrize("case", SHARED_CASES, ids=lambda case: case["name"])
def test_shared_dashboard_fixtures(contracts: Contracts, case: dict[str, Any]) -> None:
    if case["valid"]:
        contracts.validate_state(case["state"])
    else:
        with pytest.raises(ValueError):
            contracts.validate_state(case["state"])


def test_schema_contract_is_exact_shared_data_and_detached(contracts: Contracts) -> None:
    schema = contracts.schema_contract()
    assert set(schema) == {
        "ag_ui_version",
        "binding",
        "state_schema",
        "custom_events",
        "input_schema",
        "examples",
    }
    assert schema["ag_ui_version"] == "1.0"
    assert schema["binding"] == "aos-appliance-v1"
    for field, filename in [
        ("state_schema", "dashboard.v1.schema.json"),
        ("input_schema", "input.v1.schema.json"),
        ("custom_events", "custom-events.schema.json"),
    ]:
        assert schema[field] == json.loads((CONTRACTS_DIR / filename).read_text())
    for path in (CONTRACTS_DIR / "examples").glob("*.json"):
        assert schema["examples"][path.stem] == json.loads(path.read_text())
    schema["state_schema"].clear()
    schema["examples"]["dashboard"]["sections"].clear()
    assert contracts.schema_contract()["state_schema"]["required"]
    contracts.validate_state(contracts.sample_dashboard())


def test_sample_dashboard_is_shared_and_detached(contracts: Contracts) -> None:
    state = contracts.sample_dashboard()
    assert state == json.loads((CONTRACTS_DIR / "examples/dashboard.json").read_text())
    state["sections"][0]["id"] = "changed"
    assert contracts.sample_dashboard()["sections"][0]["id"] == "clock"


@pytest.mark.parametrize("name", ["snapshot", "delta", "overlay"])
def test_shared_event_fixtures(contracts: Contracts, name: str) -> None:
    contracts.validate_event(contracts.schema_contract()["examples"][name])


def test_shared_input_fixture(contracts: Contracts) -> None:
    contracts.validate_input(contracts.schema_contract()["examples"]["input"])


@pytest.mark.parametrize("index", [0, 1, 2, 3, 4, 5])
def test_malformed_known_type_cannot_escape_to_unknown(contracts: Contracts, index: int) -> None:
    state = contracts.sample_dashboard()
    section = state["sections"][index]
    state["sections"][index] = {"id": section["id"], "type": section["type"], "wrong": []}
    with pytest.raises(ValueError, match="/sections"):
        contracts.validate_state(state)


@pytest.mark.parametrize("index", [0, 1, 3, 4, 5])
def test_known_type_multiplicity(contracts: Contracts, index: int) -> None:
    state = contracts.sample_dashboard()
    duplicate = deepcopy(state["sections"][index])
    duplicate["id"] = "another"
    state["sections"].append(duplicate)
    with pytest.raises(ValueError):
        contracts.validate_state(state)


def test_unique_ids_are_owner_scoped(contracts: Contracts) -> None:
    state = contracts.sample_dashboard()
    second_todos = deepcopy(state["sections"][2])
    second_todos["id"] = "more-todos"
    state["sections"].append(second_todos)
    contracts.validate_state(state)
    state["sections"][-1]["id"] = "todos"
    with pytest.raises(ValueError, match="duplicate section ID"):
        contracts.validate_state(state)


@pytest.mark.parametrize("timestamp", ["yesterday", "2026-02-30T00:00:00Z", "2026-10-12T12:00:00"])
def test_timestamp_format_checker(contracts: Contracts, timestamp: str) -> None:
    state = contracts.sample_dashboard()
    state["updated_at"] = timestamp
    with pytest.raises(ValueError, match="/updated_at.*format"):
        contracts.validate_state(state)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_numbers(contracts: Contracts, value: float) -> None:
    state = contracts.sample_dashboard()
    state["sections"][3]["temp_f"] = value
    with pytest.raises(ValueError, match="finite"):
        contracts.validate_state(state)


def test_non_json_and_cyclic_values(contracts: Contracts) -> None:
    state = contracts.sample_dashboard()
    state["sections"].append({"id": "future", "type": "future", "data": (1, 2)})
    with pytest.raises(ValueError, match="JSON"):
        contracts.validate_state(state)
    state["sections"][-1]["data"] = {1: "not a JSON key"}
    with pytest.raises(ValueError, match="keys must be strings"):
        contracts.validate_state(state)
    state["sections"][-1]["data"] = state
    with pytest.raises(ValueError, match="acyclic"):
        contracts.validate_state(state)


def test_deeply_nested_input_is_value_error(contracts: Contracts) -> None:
    data: Any = None
    for _ in range(1500):
        data = [data]
    with pytest.raises(ValueError, match="JSON"):
        contracts.validate_state(data)


def test_state_byte_boundary_including_utf8(contracts: Contracts) -> None:
    state = contracts.sample_dashboard()
    state["sections"].append({"id": "future", "type": "future", "data": ""})
    size = len(json.dumps(state, ensure_ascii=False, separators=(",", ":")).encode())
    state["sections"][-1]["data"] = "a" * (MAX_STATE_BYTES - size)
    contracts.validate_state(state)
    state["sections"][-1]["data"] += "é"
    with pytest.raises(ValueError, match="128 KiB"):
        contracts.validate_state(state)


@pytest.mark.parametrize(
    "intent",
    [
        {"kind": "overlay_dismissed", "client_event_id": "dismiss-1", "observed_revision": 0},
        {"kind": "remote_key", "client_event_id": "remote-1", "observed_revision": 3, "key": "ok"},
    ],
)
def test_other_input_kinds(contracts: Contracts, intent: dict[str, Any]) -> None:
    contracts.validate_input(intent)


@pytest.mark.parametrize(
    "change",
    [
        {"kind": "unknown"},
        {"done": "true"},
        {"observed_revision": -1},
        {"client_event_id": ""},
        {"target_id": "../secret"},
        {"unexpected": "secret"},
    ],
)
def test_invalid_input(contracts: Contracts, change: dict[str, Any]) -> None:
    intent = contracts.schema_contract()["examples"]["input"]
    intent.update(change)
    with pytest.raises(ValueError):
        contracts.validate_input(intent)


@pytest.mark.parametrize(
    "event",
    [
        {"type": "CUSTOM", "name": "aos.overlay", "value": {"message": "Ready", "duration_s": 5}},
        {"type": "CUSTOM", "name": "aos.heartbeat", "value": {"revision": 0}},
        {"type": "CUSTOM", "name": "aos.device", "value": {"action": "active_source"}},
        {"type": "CUSTOM", "name": "aos.remote_key", "value": {"key": "up"}},
        {"type": "CUSTOM", "name": "aos.overlay_dismissed", "value": {}},
    ],
)
def test_supported_custom_events(contracts: Contracts, event: dict[str, Any]) -> None:
    contracts.validate_custom(event)
    contracts.validate_event(event)


@pytest.mark.parametrize(
    "event",
    [
        {"kind": "overlay", "message": "Ready", "duration_s": 5},
        {"type": "CUSTOM", "name": "aos.unknown", "value": {}},
        {"type": "CUSTOM", "name": "aos.overlay", "value": {"message": "Ready", "duration_s": 0}},
        {"type": "CUSTOM", "name": "aos.overlay", "value": {"message": "a" * 301, "duration_s": 5}},
        {"type": "CUSTOM", "name": "aos.overlay", "value": {"message": "Ready", "duration_s": 31}},
        {
            "type": "CUSTOM",
            "name": "aos.overlay",
            "value": {"kind": "overlay", "message": "Ready", "duration_s": 5},
        },
        {"type": "CUSTOM", "name": "aos.heartbeat", "value": {"revision": -1}},
        {"type": "CUSTOM", "name": "aos.device", "value": {"action": "reboot"}},
        {"type": "CUSTOM", "name": "aos.remote_key", "value": {"key": "shell"}},
        {"type": "CUSTOM", "name": "aos.overlay_dismissed", "value": {"command": "secret"}},
        {"type": "CUSTOM", "name": "aos.overlay_dismissed"},
        {"type": "CUSTOM", "name": "aos.overlay_dismissed", "value": {}, "timestamp": "invalid"},
    ],
)
def test_invalid_custom_events(contracts: Contracts, event: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        contracts.validate_custom(event)
    with pytest.raises(ValueError):
        contracts.validate_event(event)


@pytest.mark.parametrize(
    "event",
    [
        None,
        [],
        "secret",
        {"type": ["STATE_SNAPSHOT"]},
        {"type": "stateDelta", "delta": []},
        {"type": "RUN_STARTED", "threadId": "t", "runId": "r"},
        {"type": "TEXT_MESSAGE_CONTENT", "messageId": "m", "delta": "text"},
        {"type": "RAW", "event": {}},
        {"type": "STATE_SNAPSHOT"},
        {"type": "STATE_DELTA", "delta": {}, "extra": "secret"},
    ],
)
def test_invalid_or_unsupported_events(contracts: Contracts, event: object) -> None:
    with pytest.raises(ValueError):
        contracts.validate_event(event)


def test_upstream_optional_fields_validated(contracts: Contracts) -> None:
    event = {"type": "STATE_DELTA", "delta": [], "timestamp": 1000, "metadata": {"source": "demo"}}
    contracts.validate_event(event)
    event["timestamp"] = 1.5
    with pytest.raises(ValueError, match="/timestamp"):
        contracts.validate_event(event)


def replacement(contracts: Contracts) -> dict[str, Any]:
    state = contracts.sample_dashboard()
    del state["revision"]
    del state["updated_at"]
    return state


def test_replacement_uses_fresh_temporary_metadata_without_mutation(
    contracts: Contracts, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = replacement(contracts)
    original = deepcopy(payload)
    captured: list[dict[str, Any]] = []
    real_validate = contracts.validate_state

    def capture(state: object) -> None:
        assert isinstance(state, dict)
        captured.append(state)
        real_validate(state)

    monkeypatch.setattr(contracts, "validate_state", capture)
    before = datetime.now(UTC)
    contracts.validate_replacement(payload)
    after = datetime.now(UTC)
    assert payload == original
    assert captured[0]["revision"] == 0
    assert before <= datetime.fromisoformat(captured[0]["updated_at"]) <= after
    contracts.validate_event({"type": "STATE_SNAPSHOT", "snapshot": payload})


@pytest.mark.parametrize(
    "metadata", [{"revision": 0}, {"updated_at": "secret"}, {"revision": None}]
)
def test_replacement_rejects_supplied_metadata(
    contracts: Contracts, metadata: dict[str, Any]
) -> None:
    payload = replacement(contracts)
    payload.update(metadata)
    with pytest.raises(ValueError, match="bridge-owned"):
        contracts.validate_replacement(payload)


@pytest.mark.parametrize("payload", [None, [], {}, {"version": 1}, {"version": 2}])
def test_invalid_replacement(contracts: Contracts, payload: object) -> None:
    with pytest.raises(ValueError):
        contracts.validate_replacement(payload)


def test_patch_all_rfc_operations_and_copy_isolation(contracts: Contracts) -> None:
    state = contracts.sample_dashboard()
    original = deepcopy(state)
    delta: list[dict[str, Any]] = [
        {"op": "test", "path": "/revision", "value": 0},
        {"op": "replace", "path": "/sections/2/items/0/done", "value": True},
        {"op": "add", "path": "/sections/1/events/-", "value": {"time": "10a", "title": "Review"}},
        {"op": "copy", "from": "/sections/1/events/0", "path": "/sections/1/events/-"},
        {"op": "move", "from": "/sections/1/events/0", "path": "/sections/1/events/1"},
        {"op": "remove", "path": "/sections/1/events/2"},
    ]
    result = contracts.apply_patch(state, delta)
    assert state == original
    assert result["sections"][2]["items"][0]["done"] is True
    assert len(result["sections"][1]["events"]) == 2
    assert result["revision"] == state["revision"]
    assert result["updated_at"] == state["updated_at"]
    delta[2]["value"]["title"] = "Caller changed the delta"
    assert result["sections"][1]["events"][0]["title"] == "Review"
    result["sections"][0]["id"] = "changed"
    assert state == original


def test_empty_patch_returns_new_state(contracts: Contracts) -> None:
    state = contracts.sample_dashboard()
    result = contracts.apply_patch(state, [])
    assert result == state and result is not state
    assert result["sections"] is not state["sections"]


@pytest.mark.parametrize("op", ["add", "replace", "remove", "move", "copy", "test"])
def test_root_patch_rejected(contracts: Contracts, op: str) -> None:
    operation = {"op": op, "path": "", "from": "/sections", "value": {}}
    with pytest.raises(ValueError, match="root"):
        contracts.apply_patch(contracts.sample_dashboard(), [operation])


@pytest.mark.parametrize("op", ["add", "replace", "remove", "move", "copy"])
@pytest.mark.parametrize(
    "path", ["/revision", "/updated_at", "/revision/child", "/updated_at/child"]
)
def test_metadata_patch_targets_rejected(contracts: Contracts, op: str, path: str) -> None:
    operation = {"op": op, "path": path, "from": "/sections/0/id", "value": 0}
    with pytest.raises(ValueError, match="metadata"):
        contracts.apply_patch(contracts.sample_dashboard(), [operation])


@pytest.mark.parametrize("op", ["move", "copy"])
@pytest.mark.parametrize(
    "source", ["", "/revision", "/updated_at", "/revision/child", "/updated_at/child"]
)
def test_metadata_and_root_patch_sources_rejected(
    contracts: Contracts, op: str, source: str
) -> None:
    with pytest.raises(ValueError, match="root|metadata"):
        contracts.apply_patch(
            contracts.sample_dashboard(), [{"op": op, "from": source, "path": "/sections/5/note"}]
        )


def test_metadata_tests_are_not_mutations(contracts: Contracts) -> None:
    state = contracts.sample_dashboard()
    assert (
        contracts.apply_patch(
            state,
            [
                {"op": "test", "path": "/revision", "value": state["revision"]},
                {"op": "test", "path": "/updated_at", "value": state["updated_at"]},
            ],
        )
        == state
    )


def test_patch_pointer_escaping(contracts: Contracts) -> None:
    state = contracts.sample_dashboard()
    state["sections"].append({"id": "future", "type": "future", "a/b": {"~name": {"value": 1}}})
    result = contracts.apply_patch(
        state, [{"op": "replace", "path": "/sections/6/a~1b/~0name/value", "value": 2}]
    )
    assert result["sections"][6]["a/b"]["~name"]["value"] == 2
    assert state["sections"][6]["a/b"]["~name"]["value"] == 1


@pytest.mark.parametrize(
    "operation",
    [
        {"op": "replace", "path": "/sections/2/items/0/done", "value": "not a boolean"},
        {"op": "replace", "path": "/sections/2/items/0/id", "value": "t2"},
        {"op": "replace", "path": "/sections/1/id", "value": "clock"},
        {"op": "remove", "path": "/sections/5"},
        {"op": "remove", "path": "/does-not-exist"},
        {"op": "test", "path": "/revision", "value": 999},
        {"op": "move", "from": "/sections/2", "path": "/sections/2/items/0"},
    ],
)
def test_failed_patch_is_atomic(contracts: Contracts, operation: dict[str, Any]) -> None:
    state = contracts.sample_dashboard()
    original = deepcopy(state)
    delta = [{"op": "replace", "path": "/sections/5/note", "value": "Uncommitted"}, operation]
    original_delta = deepcopy(delta)
    with pytest.raises(ValueError):
        contracts.apply_patch(state, delta)
    assert state == original
    assert delta == original_delta


def test_patch_final_validation_allows_temporary_schema_invalidity(contracts: Contracts) -> None:
    state = contracts.sample_dashboard()
    result = contracts.apply_patch(
        state,
        [
            {"op": "remove", "path": "/sections/5"},
            {"op": "add", "path": "/sections/-", "value": state["sections"][5]},
        ],
    )
    assert result == state


@pytest.mark.parametrize(
    "delta",
    [
        None,
        {},
        [{"op": "unknown", "path": "/sections"}],
        [{"op": "replace", "path": "/theme"}],
        [{"op": "copy", "path": "/theme"}],
        [{"op": "remove", "path": "sections"}],
        [{"op": "remove", "path": "/~2"}],
        [{"op": "remove", "path": 0}],
    ],
)
def test_invalid_patch_shapes(contracts: Contracts, delta: object) -> None:
    with pytest.raises(ValueError):
        contracts.apply_patch(contracts.sample_dashboard(), delta)
    with pytest.raises(ValueError):
        contracts.validate_event({"type": "STATE_DELTA", "delta": delta})


def test_patch_ignores_undefined_operation_members_per_rfc(contracts: Contracts) -> None:
    state = contracts.sample_dashboard()
    result = contracts.apply_patch(
        state, [{"op": "replace", "path": "/sections/5/note", "value": "Ready", "unused": 1}]
    )
    assert result["sections"][5]["note"] == "Ready"


def test_patch_operation_limit(contracts: Contracts) -> None:
    state = contracts.sample_dashboard()
    delta = [{"op": "test", "path": "/revision", "value": 0}] * MAX_PATCH_OPERATIONS
    assert contracts.apply_patch(state, delta) == state
    delta.append(delta[0])
    with pytest.raises(ValueError, match="128 operation"):
        contracts.apply_patch(state, delta)
    with pytest.raises(ValueError, match="128 operation"):
        contracts.validate_event({"type": "STATE_DELTA", "delta": delta})


def test_patch_size_limit_and_intermediate_copy_amplification(contracts: Contracts) -> None:
    state = contracts.sample_dashboard()
    state["sections"].append({"id": "future", "type": "future", "data": "a" * 60000})
    original = deepcopy(state)
    delta = [
        {"op": "copy", "from": "/sections/6/data", "path": "/sections/6/copy1"},
        {"op": "copy", "from": "/sections/6/data", "path": "/sections/6/copy2"},
        {"op": "remove", "path": "/sections/6/copy1"},
        {"op": "remove", "path": "/sections/6/copy2"},
    ]
    with pytest.raises(ValueError, match="128 KiB"):
        contracts.apply_patch(state, delta)
    assert state == original
    with pytest.raises(ValueError, match="128 KiB"):
        contracts.apply_patch(
            state, [{"op": "add", "path": "/sections/6/data", "value": "a" * MAX_STATE_BYTES}]
        )


def test_diagnostics_do_not_echo_values_keys_or_patch_library_messages(
    contracts: Contracts,
) -> None:
    secret = "bearer-super-secret-value"
    state = contracts.sample_dashboard()
    state["updated_at"] = secret
    with pytest.raises(ValueError) as error:
        contracts.validate_state(state)
    assert secret not in str(error.value)
    assert "/updated_at" in str(error.value)
    with pytest.raises(ValueError) as error:
        contracts.apply_patch(
            contracts.sample_dashboard(),
            [{"op": "test", "path": "/sections/5/note", "value": secret}],
        )
    assert secret not in str(error.value)
    with pytest.raises(ValueError) as error:
        contracts.apply_patch(
            contracts.sample_dashboard(), [{"op": "remove", "path": f"/{secret}"}]
        )
    assert secret not in str(error.value)
    with pytest.raises(ValueError) as error:
        contracts.validate_input({secret: secret})
    assert secret not in str(error.value)


def test_upstream_provenance_hash(contracts: Contracts) -> None:
    provenance = json.loads((CONTRACTS_DIR / "ag-ui/provenance.json").read_text())
    assert provenance["source"] == "https://ag-ui.com/spec/1.0/schema.json"
    assert provenance["spec_version"] == "1.0"
    assert (
        provenance["sha256"]
        == hashlib.sha256((CONTRACTS_DIR / "ag-ui/schema.json").read_bytes()).hexdigest()
    )
    assert contracts.schema_contract()["ag_ui_version"] == provenance["spec_version"]


def test_explicit_and_environment_directory_overrides(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = tmp_path / "contracts"
    shutil.copytree(CONTRACTS_DIR, data)
    monkeypatch.setenv("AOS_CONTRACTS_DIR", str(data))
    Contracts().validate_state(Contracts(data).sample_dashboard())
    monkeypatch.setenv("AOS_CONTRACTS_DIR", str(tmp_path / "missing"))
    Contracts(data).validate_state(Contracts(data).sample_dashboard())
    with pytest.raises(ValueError, match="override is unavailable"):
        Contracts()
    monkeypatch.setenv("AOS_CONTRACTS_DIR", "")
    with pytest.raises(ValueError, match="override is empty"):
        Contracts()


def test_installed_wheel_data_without_repository(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package = tmp_path / "site-packages/aos_bridge"
    shutil.copytree(CONTRACTS_DIR, package / "contracts")
    monkeypatch.delenv("AOS_CONTRACTS_DIR", raising=False)
    monkeypatch.setattr(contracts_module, "files", lambda name: package)
    monkeypatch.setattr(contracts_module, "__file__", str(package / "contracts.py"))
    contracts = Contracts()
    contracts.validate_state(contracts.sample_dashboard())
    assert contracts.schema_contract()["examples"]["overlay"]["name"] == "aos.overlay"


def test_missing_contract_data_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("AOS_CONTRACTS_DIR", raising=False)
    monkeypatch.setattr(contracts_module, "files", lambda name: tmp_path)
    monkeypatch.setattr(contracts_module, "__file__", str(tmp_path / "module.py"))
    with pytest.raises(ValueError, match="data is unavailable"):
        Contracts()


@pytest.mark.parametrize("target", ["schema", "provenance"])
def test_tampered_upstream_is_rejected(tmp_path: Path, target: str) -> None:
    data = tmp_path / "contracts"
    shutil.copytree(CONTRACTS_DIR, data)
    path = data / "ag-ui" / ("schema.json" if target == "schema" else "provenance.json")
    document = json.loads(path.read_text())
    document["sha256" if target == "provenance" else "title"] = "changed"
    path.write_text(json.dumps(document))
    with pytest.raises(ValueError, match="provenance/hash mismatch"):
        Contracts(data)


def test_invalid_schema_configuration_is_value_error(tmp_path: Path) -> None:
    data = tmp_path / "contracts"
    shutil.copytree(CONTRACTS_DIR, data)
    (data / "input.v1.schema.json").write_text('{"type": "unsupported-schema-type"}')
    with pytest.raises(ValueError, match="invalid shared schema"):
        Contracts(data)
