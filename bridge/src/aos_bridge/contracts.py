"""Shared wire validation for the runless aOS appliance binding."""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Iterable
from copy import deepcopy
from datetime import UTC, datetime
from importlib.resources import files
from importlib.resources.abc import Traversable
from pathlib import Path
from typing import Any

import jsonpatch
import jsonpointer
from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import SchemaError
from jsonschema.protocols import Validator

MAX_STATE_BYTES = 128 * 1024
MAX_EVENT_BYTES = MAX_STATE_BYTES + 1024
MAX_PATCH_OPERATIONS = 128
_PROTECTED_FIELDS = frozenset({"revision", "updated_at"})
_SINGLETON_TYPES = frozenset({"clock", "agenda", "weather", "briefing", "status"})
_EVENT_DEFINITIONS = {
    "STATE_SNAPSHOT": "StateSnapshotEvent",
    "STATE_DELTA": "StateDeltaEvent",
    "CUSTOM": "CustomEvent",
}


class Contracts:
    """Load the shared contracts once; validate without changing caller-owned values.

    Event validation covers both inbound and committed wire shapes. Authoring code
    must additionally use validate_replacement or apply_patch to protect metadata.
    """

    def __init__(self, contracts_dir: str | Path | None = None) -> None:
        self._directory = self._find_directory(contracts_dir)
        self._state_schema = self._read_object("dashboard.v1.schema.json")
        self._input_schema = self._read_object("input.v1.schema.json")
        self._custom_schema = self._read_object("custom-events.schema.json")
        self._upstream_schema = self._read_object("ag-ui/schema.json")
        provenance = self._read_object("ag-ui/provenance.json")
        digest = hashlib.sha256(self._read_bytes("ag-ui/schema.json")).hexdigest()
        if provenance.get("spec_version") != "1.0" or provenance.get("sha256") != digest:
            raise ValueError("contracts: upstream schema provenance/hash mismatch")

        self._state_validator = self._validator(self._state_schema)
        self._input_validator = self._validator(self._input_schema)
        self._custom_validator = self._validator(self._custom_schema)
        upstream_validator = self._validator(self._upstream_schema)
        self._event_validators = {
            name: upstream_validator.evolve(
                schema={**self._upstream_schema, "$ref": f"#/$defs/{definition}"}
            )
            for name, definition in _EVENT_DEFINITIONS.items()
        }
        self._patch_validator = upstream_validator.evolve(
            schema={**self._upstream_schema, "$ref": "#/$defs/JsonPatch"}
        )
        self._diagnostic_fields = self._property_names(
            [self._state_schema, self._input_schema, self._custom_schema, self._upstream_schema]
        )
        try:
            self._examples = {
                entry.name.removesuffix(".json"): self._read_object(f"examples/{entry.name}")
                for entry in sorted(
                    self._directory.joinpath("examples").iterdir(), key=lambda e: e.name
                )
                if entry.name.endswith(".json") and entry.is_file()
            }
        except OSError:
            raise ValueError("contracts: cannot read shared examples") from None
        if "dashboard" not in self._examples:
            raise ValueError("contracts: missing shared dashboard example")
        self.validate_state(self._examples["dashboard"])

    @staticmethod
    def _find_directory(contracts_dir: str | Path | None) -> Traversable:
        override = (
            contracts_dir if contracts_dir is not None else os.environ.get("AOS_CONTRACTS_DIR")
        )
        if override is not None:
            if not str(override):
                raise ValueError("contracts: contract directory override is empty")
            directory = Path(override)
            if not directory.joinpath("dashboard.v1.schema.json").is_file():
                raise ValueError("contracts: contract directory override is unavailable")
            return directory

        # wheel data lives beside this module; source checkouts own a root contracts/.
        packaged = files("aos_bridge").joinpath("contracts")
        if packaged.joinpath("dashboard.v1.schema.json").is_file():
            return packaged
        for parent in Path(__file__).resolve().parents:
            directory = parent / "contracts"
            if directory.joinpath("dashboard.v1.schema.json").is_file():
                return directory
        raise ValueError("contracts: shared contract data is unavailable")

    def _read_bytes(self, relative: str) -> bytes:
        try:
            return self._directory.joinpath(relative).read_bytes()
        except OSError:
            raise ValueError(f"contracts: cannot read {relative}") from None

    def _read_object(self, relative: str) -> dict[str, Any]:
        try:
            value = json.loads(self._read_bytes(relative))
        except (ValueError, UnicodeError, RecursionError):
            raise ValueError(f"contracts: invalid JSON in {relative}") from None
        if not isinstance(value, dict):
            raise ValueError(f"contracts: expected an object in {relative}")
        return value

    @staticmethod
    def _validator(schema: dict[str, Any]) -> Draft202012Validator:
        try:
            Draft202012Validator.check_schema(schema)
        except SchemaError:
            raise ValueError("contracts: invalid shared schema") from None
        return Draft202012Validator(schema, format_checker=FormatChecker())

    @staticmethod
    def _property_names(schemas: list[dict[str, Any]]) -> set[str]:
        names: set[str] = set()
        pending: list[Any] = list(schemas)
        while pending:
            value = pending.pop()
            if isinstance(value, dict):
                properties = value.get("properties", {})
                if isinstance(properties, dict):
                    names.update(properties)
                pending.extend(value.values())
            elif isinstance(value, list):
                pending.extend(value)
        return names

    @staticmethod
    def _check_json(value: object, label: str, max_bytes: int = MAX_STATE_BYTES) -> None:
        limit_error = f"{label}: exceeds {max_bytes // 1024} KiB JSON limit"
        try:
            encoder = json.JSONEncoder(ensure_ascii=False, allow_nan=False, separators=(",", ":"))
            size = 0
            for chunk in encoder.iterencode(value):
                size += len(chunk.encode("utf-8"))
                if size > max_bytes:
                    raise ValueError(limit_error)
        except (TypeError, UnicodeError, RecursionError, OverflowError):
            raise ValueError(f"{label}: expected bounded JSON data") from None
        except ValueError as error:
            if str(error) == limit_error:
                raise
            raise ValueError(f"{label}: expected finite, acyclic JSON data") from None

        # json's encoder otherwise permits tuples and silently coerces object keys.
        pending = [value]
        while pending:
            item = pending.pop()
            if isinstance(item, dict):
                if any(not isinstance(key, str) for key in item):
                    raise ValueError(f"{label}: JSON object keys must be strings")
                pending.extend(item.values())
            elif isinstance(item, list):
                pending.extend(item)
            elif not isinstance(item, (str, int, float, bool, type(None))):
                raise ValueError(f"{label}: expected JSON data")

    def _pointer(self, path: Iterable[object]) -> str:
        # only trusted schema field names/indices enter diagnostics, never values or arbitrary keys.
        tokens = [
            str(part) if isinstance(part, int) or part in self._diagnostic_fields else "<field>"
            for part in path
        ]
        return "/" + "/".join(token.replace("~", "~0").replace("/", "~1") for token in tokens)

    def _validate(
        self, validator: Validator, value: object, label: str, max_bytes: int = MAX_STATE_BYTES
    ) -> None:
        self._check_json(value, label, max_bytes)
        assert value is None or isinstance(value, (dict, list, str, int, float, bool))
        try:
            error = next(validator.iter_errors(value), None)
        except RecursionError:
            raise ValueError(f"{label}: JSON nesting is too deep") from None
        if error is not None:
            raise ValueError(
                f"{label}: invalid at {self._pointer(error.absolute_path)} ({error.validator})"
            )

    def validate_state(self, state: object) -> None:
        """Validate bounded dashboard structure and IDs unique within their owner."""
        self._validate(self._state_validator, state, "state")
        assert isinstance(state, dict)
        section_ids: set[str] = set()
        singleton_types: set[str] = set()
        for index, section in enumerate(state["sections"]):
            if section["id"] in section_ids:
                raise ValueError(f"state: duplicate section ID at /sections/{index}/id")
            section_ids.add(section["id"])
            section_type = section["type"]
            if section_type in _SINGLETON_TYPES:
                if section_type in singleton_types:
                    raise ValueError(f"state: duplicate section type at /sections/{index}/type")
                singleton_types.add(section_type)
            if section_type == "todos":
                todo_ids: set[str] = set()
                for todo_index, todo in enumerate(section["items"]):
                    if todo["id"] in todo_ids:
                        raise ValueError(
                            f"state: duplicate todo ID at /sections/{index}/items/{todo_index}/id"
                        )
                    todo_ids.add(todo["id"])

    def validate_input(self, intent: object) -> None:
        """Validate a queue-bound UI intent, not an authoritative dashboard write."""
        self._validate(self._input_validator, intent, "input")

    def validate_custom(self, event: object) -> None:
        """Validate a supported full CUSTOM envelope and its application payload."""
        self._validate(self._event_validators["CUSTOM"], event, "event")
        self._validate(self._custom_validator, event, "custom event")

    def validate_event(self, event: object) -> None:
        """Validate supported AG-UI shapes, including committed feed metadata.

        Snapshots may carry committed state or an authoring replacement. Deltas
        receive structural checks here; only apply_patch checks their applicability
        and authoring permissions. This does not validate AG-UI run sequencing.
        """
        self._check_json(event, "event", MAX_EVENT_BYTES)
        if not isinstance(event, dict) or not isinstance(event.get("type"), str):
            raise ValueError("event: expected an uppercase supported event type at /type")
        event_type = event["type"]
        if event_type not in self._event_validators:
            raise ValueError("event: unsupported event type at /type")
        self._validate(self._event_validators[event_type], event, "event", MAX_EVENT_BYTES)
        if event_type == "CUSTOM":
            self.validate_custom(event)
        elif event_type == "STATE_SNAPSHOT":
            snapshot = event["snapshot"]
            if isinstance(snapshot, dict) and not (_PROTECTED_FIELDS & snapshot.keys()):
                self.validate_replacement(snapshot)
            else:
                self.validate_state(snapshot)
        else:
            self._validate_delta(event["delta"])

    def _validate_delta(self, delta: object) -> None:
        self._validate(self._patch_validator, delta, "patch", MAX_EVENT_BYTES)
        assert isinstance(delta, list)
        if len(delta) > MAX_PATCH_OPERATIONS:
            raise ValueError("patch: exceeds 128 operation limit")

    def validate_replacement(self, payload: object) -> None:
        """Validate authoring content with temporary metadata, never mutate it."""
        self._check_json(payload, "replacement")
        if not isinstance(payload, dict):
            raise ValueError("replacement: expected an object")
        if _PROTECTED_FIELDS & payload.keys():
            raise ValueError("replacement: bridge-owned revision/updated_at must be omitted")
        self.validate_state({**payload, "revision": 0, "updated_at": datetime.now(UTC).isoformat()})

    def apply_patch(self, state: object, delta: object) -> dict[str, Any]:
        """Apply an authoring patch atomically to a detached, fully validated copy.

        Root operations and metadata mutations are forbidden, including move/copy
        sources. Metadata test operations remain available for preconditions. This
        does not increment revision or timestamp: the committing bridge owns them.
        """
        self.validate_state(state)
        self._validate_delta(delta)
        assert isinstance(delta, list)
        for index, operation in enumerate(delta):
            self._check_patch_path(operation["path"], index, operation["op"] == "test")
            if operation["op"] in {"move", "copy"}:
                self._check_patch_path(operation["from"], index, False)
        try:
            result = deepcopy(state)
            operations = deepcopy(delta)
        except RecursionError:
            raise ValueError("patch: JSON nesting is too deep") from None
        for operation in operations:
            try:
                result = jsonpatch.JsonPatch([operation]).apply(result, in_place=True)
            except (
                jsonpatch.JsonPatchException,
                jsonpointer.JsonPointerException,
                IndexError,
                KeyError,
                TypeError,
                ValueError,
                OverflowError,
                RecursionError,
            ):
                raise ValueError("patch: cannot apply operations to the current state") from None
            # bound intermediate copies too, preventing copy amplification before final validation.
            self._check_json(result, "state")
        self.validate_state(result)
        assert isinstance(result, dict)
        return result

    @staticmethod
    def _check_patch_path(path: str, index: int, is_test: bool) -> None:
        if path == "":
            raise ValueError(f"patch: root path is forbidden at /delta/{index}")
        first_token = path.split("/", 2)[1].replace("~1", "/").replace("~0", "~")
        if first_token in _PROTECTED_FIELDS and not is_test:
            raise ValueError(f"patch: bridge-owned metadata is protected at /delta/{index}")

    def sample_dashboard(self) -> dict[str, Any]:
        """Return a detached copy of the authoritative shared dashboard example."""
        return deepcopy(self._examples["dashboard"])

    def schema_contract(self) -> dict[str, Any]:
        """Return the /v1/schema response with authoritative schemas and examples."""
        return deepcopy(
            {
                "ag_ui_version": "1.0",
                "binding": "aos-appliance-v1",
                "state_schema": self._state_schema,
                "custom_events": self._custom_schema,
                "input_schema": self._input_schema,
                "examples": self._examples,
            }
        )
