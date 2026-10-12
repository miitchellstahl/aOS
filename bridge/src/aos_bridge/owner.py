"""The sole transaction owner for dashboard, queue and runless stream publication."""

from __future__ import annotations

import asyncio
import hashlib
import json
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import jsonpatch

from aos_bridge.contracts import MAX_PATCH_OPERATIONS, Contracts
from aos_bridge.settings import Settings
from aos_bridge.store import AtomicStore, StorageError

MAX_EVENT_ID = 2**63 - 1


def now() -> str:
    return datetime.now(UTC).isoformat()


class BridgeError(Exception):
    def __init__(self, status: int, detail: str) -> None:
        self.status = status
        self.detail = detail
        super().__init__(detail)


@dataclass(eq=False)
class Subscriber:
    queue: asyncio.Queue[dict[str, Any]]
    disconnected: asyncio.Event = field(default_factory=asyncio.Event)


class Owner:
    def __init__(self, settings: Settings, contracts: Contracts) -> None:
        self.settings = settings
        self.contracts = contracts
        self.store = AtomicStore(settings.state_dir)
        self.lock = asyncio.Lock()
        self.subscribers: set[Subscriber] = set()
        self.state: dict[str, Any] = {}
        self.queue: dict[str, Any] = {}
        self.last_error: str | None = None

    def start(self) -> None:
        self.store.acquire()
        try:

            def initial_state() -> dict[str, Any]:
                return {**self.contracts.sample_dashboard(), "revision": 0, "updated_at": now()}

            self.state = self.store.load(
                "dashboard.json", self.contracts.validate_state, initial_state, 128 * 1024
            )
            self.queue = self.store.load(
                "events.json",
                self._validate_queue,
                lambda: {"next_id": 1, "pending": [], "history": [], "acknowledged": []},
                8 * 1024 * 1024,
            )
        except BaseException:
            self.store.close()
            raise

    def close(self) -> None:
        for subscriber in self.subscribers:
            subscriber.disconnected.set()
        self.subscribers.clear()
        self.store.close()

    def _validate_queue(self, value: object) -> None:
        if not isinstance(value, dict) or set(value) != {
            "next_id",
            "pending",
            "history",
            "acknowledged",
        }:
            raise ValueError("invalid queue document")
        next_id = value["next_id"]
        if type(next_id) is not int or not 1 <= next_id <= MAX_EVENT_ID:
            raise ValueError("invalid queue cursor")
        pending, history, acknowledged = (value["pending"], value["history"], value["acknowledged"])
        if not isinstance(pending, list) or len(pending) > 1000:
            raise ValueError("invalid queue pending list")
        if not isinstance(history, list) or len(history) > 10000:
            raise ValueError("invalid queue history")
        if not isinstance(acknowledged, list) or len(acknowledged) > 10000:
            raise ValueError("invalid queue acknowledgments")
        ids: set[int] = set()
        clients: set[str] = set()
        last_id = 0
        for event in pending:
            if not isinstance(event, dict) or set(event) != {"id", "kind", "created_at", "payload"}:
                raise ValueError("invalid pending event")
            event_id = event["id"]
            if type(event_id) is not int or not last_id < event_id < next_id:
                raise ValueError("invalid pending event ID")
            self.contracts.validate_input(event["payload"])
            payload = event["payload"]
            if event["kind"] != payload["kind"] or payload["client_event_id"] in clients:
                raise ValueError("invalid pending event identity")
            if not isinstance(event["created_at"], str):
                raise ValueError("invalid queue timestamp")
            timestamp = datetime.fromisoformat(event["created_at"].replace("Z", "+00:00"))
            if timestamp.tzinfo is None:
                raise ValueError("queue timestamp needs timezone")
            clients.add(payload["client_event_id"])
            ids.add(event_id)
            last_id = event_id
        for record in history:
            if not isinstance(record, dict) or set(record) != {
                "client_event_id",
                "id",
                "fingerprint",
            }:
                raise ValueError("invalid dedup history")
            if type(record["id"]) is not int or not 1 <= record["id"] < next_id:
                raise ValueError("invalid history ID")
            if (
                not isinstance(record["client_event_id"], str)
                or record["client_event_id"] in clients
            ):
                raise ValueError("invalid history identity")
            self.contracts.validate_input(
                {
                    "kind": "remote_key",
                    "client_event_id": record["client_event_id"],
                    "observed_revision": 0,
                    "key": "ok",
                }
            )
            digest = record["fingerprint"]
            if (
                not isinstance(digest, str)
                or len(digest) != 64
                or any(character not in "0123456789abcdef" for character in digest)
            ):
                raise ValueError("invalid history fingerprint")
            if record["id"] in ids:
                raise ValueError("duplicate history ID")
            ids.add(record["id"])
            clients.add(record["client_event_id"])
        ack_ids: set[int] = set()
        for event_id in acknowledged:
            if type(event_id) is not int or not 1 <= event_id < next_id or event_id in ack_ids:
                raise ValueError("invalid acknowledged ID")
            if any(event["id"] == event_id for event in pending):
                raise ValueError("pending acknowledged event")
            ack_ids.add(event_id)
        if any(record["id"] not in ack_ids for record in history):
            raise ValueError("history must refer to acknowledged events")

    def _commit(self, name: str, value: dict[str, Any]) -> None:
        try:
            self.store.write(name, value)
            self.last_error = None
        except StorageError:
            self.last_error = "durable storage unavailable"
            raise BridgeError(503, self.last_error) from None

    def _prepare_frames(self, event: dict[str, Any]) -> dict[Subscriber, dict[str, Any]]:
        # allocate detached subscriber frames before a durable commit can take effect.
        try:
            return {subscriber: deepcopy(event) for subscriber in self.subscribers}
        except RecursionError:
            raise ValueError("event: JSON nesting is too deep") from None

    def _publish(self, frames: dict[Subscriber, dict[str, Any]]) -> None:
        for subscriber, event in frames.items():
            try:
                subscriber.queue.put_nowait(event)
            except asyncio.QueueFull:
                subscriber.disconnected.set()
                self.subscribers.discard(subscriber)

    async def subscribe(self) -> tuple[dict[str, Any], Subscriber]:
        async with self.lock:
            snapshot = {"type": "STATE_SNAPSHOT", "snapshot": deepcopy(self.state)}
            subscriber = Subscriber(asyncio.Queue(maxsize=self.settings.stream_limit))
            self.subscribers.add(subscriber)
            return snapshot, subscriber

    async def unsubscribe(self, subscriber: Subscriber) -> None:
        async with self.lock:
            self.subscribers.discard(subscriber)
            subscriber.disconnected.set()

    async def dashboard(self) -> dict[str, Any]:
        async with self.lock:
            return deepcopy(self.state)

    async def mutate(
        self, expected_revision: int, *, payload: object = None, delta: object = None
    ) -> dict[str, Any]:
        async with self.lock:
            if expected_revision != self.state["revision"]:
                raise BridgeError(409, "stale expected_revision")
            if delta is None:
                self.contracts.validate_replacement(payload)
                assert isinstance(payload, dict)
                try:
                    updated = deepcopy(payload)
                except RecursionError:
                    raise ValueError("replacement: JSON nesting is too deep") from None
            else:
                updated = self.contracts.apply_patch(self.state, delta)
            updated.update(revision=self.state["revision"] + 1, updated_at=now())
            self.contracts.validate_state(updated)
            try:
                patch = jsonpatch.make_patch(self.state, updated).patch
            except RecursionError:
                raise ValueError("state: JSON nesting is too deep to compute a delta") from None
            patch.insert(0, {"op": "test", "path": "/revision", "value": self.state["revision"]})
            event = {"type": "STATE_DELTA", "delta": patch}
            try:
                if len(patch) > MAX_PATCH_OPERATIONS:
                    raise ValueError("large diff")
                self.contracts.validate_event(event)
            except ValueError:
                patch = [patch[0]] + [
                    {"op": "replace", "path": f"/{key}", "value": updated[key]}
                    for key in updated
                    if self.state[key] != updated[key]
                ]
                event = {"type": "STATE_DELTA", "delta": patch}
                self.contracts.validate_event(event)
            frames = self._prepare_frames(event)
            result = deepcopy(updated)
            self._commit("dashboard.json", updated)
            self.state = updated
            self._publish(frames)
            return result

    async def custom(self, event: dict[str, Any]) -> None:
        self.contracts.validate_custom(event)
        if event["name"] == "aos.overlay":
            event = {**event, "value": {**event["value"], "overlay_id": uuid4().hex}}
        async with self.lock:
            self._publish(self._prepare_frames(event))

    @staticmethod
    def _fingerprint(payload: dict[str, Any]) -> str:
        data = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(data).hexdigest()

    def _duplicate(self, payload: dict[str, Any]) -> int | None:
        client_id = payload["client_event_id"]
        for event in self.queue["pending"]:
            if event["payload"]["client_event_id"] == client_id:
                if event["payload"] != payload:
                    raise BridgeError(409, "client_event_id reused with different payload")
                return int(event["id"])
        for record in self.queue["history"]:
            if record["client_event_id"] == client_id:
                if record["fingerprint"] != self._fingerprint(payload):
                    raise BridgeError(409, "client_event_id reused with different payload")
                return int(record["id"])
        return None

    async def enqueue(self, payload: dict[str, Any]) -> int:
        self.contracts.validate_input(payload)
        async with self.lock:
            duplicate = self._duplicate(payload)
            if duplicate is not None:
                return duplicate
            if payload["kind"] == "todo_toggle":
                if payload["observed_revision"] != self.state["revision"]:
                    raise BridgeError(409, "stale observed_revision")
                section = next(
                    (s for s in self.state["sections"] if s["id"] == payload["section_id"]), None
                )
                if (
                    section is None
                    or section["type"] != "todos"
                    or not any(item["id"] == payload["target_id"] for item in section["items"])
                ):
                    raise BridgeError(422, "input: unknown todo target at /target_id")
            if len(self.queue["pending"]) >= self.settings.queue_limit:
                self.last_error = "input queue full"
                raise BridgeError(503, self.last_error)
            if self.queue["next_id"] >= MAX_EVENT_ID:
                raise BridgeError(503, "input event ID space exhausted")
            queue = deepcopy(self.queue)
            event_id = int(queue["next_id"])
            queue["next_id"] += 1
            queue["pending"].append(
                {
                    "id": event_id,
                    "kind": payload["kind"],
                    "created_at": now(),
                    "payload": deepcopy(payload),
                }
            )
            frames = (
                self._prepare_frames(
                    {
                        "type": "CUSTOM",
                        "name": "aos.overlay_dismissed",
                        "value": {"overlay_id": payload["overlay_id"]},
                    }
                )
                if payload["kind"] == "overlay_dismissed"
                else {}
            )
            self._commit("events.json", queue)
            self.queue = queue
            self._publish(frames)
            return event_id

    async def events(self, since: int, limit: int) -> dict[str, Any]:
        async with self.lock:
            events = [e for e in self.queue["pending"] if e["id"] > since][:limit]
            return {
                "events": deepcopy(events),
                "next_cursor": events[-1]["id"] if events else since,
            }

    async def ack(self, ids: list[int]) -> list[int]:
        async with self.lock:
            requested = set(ids)
            queue = deepcopy(self.queue)
            acknowledged = set(queue["acknowledged"])
            remaining = []
            changed = False
            for event in queue["pending"]:
                if event["id"] not in requested:
                    remaining.append(event)
                    continue
                changed = True
                acknowledged.add(event["id"])
                queue["acknowledged"].append(event["id"])
                queue["history"].append(
                    {
                        "client_event_id": event["payload"]["client_event_id"],
                        "id": event["id"],
                        "fingerprint": self._fingerprint(event["payload"]),
                    }
                )
            result = [event_id for event_id in dict.fromkeys(ids) if event_id in acknowledged]
            if changed:
                queue["pending"] = remaining
                queue["history"] = queue["history"][-self.settings.history_limit :]
                queue["acknowledged"] = queue["acknowledged"][-self.settings.history_limit :]
                self._commit("events.json", queue)
                self.queue = queue
            return result

    async def status(self) -> dict[str, Any]:
        async with self.lock:
            return {
                "health": "degraded" if self.last_error or self.store.poisoned else "ok",
                "revision": self.state["revision"],
                "updated_at": self.state["updated_at"],
                "pending_queue_depth": len(self.queue["pending"]),
                "stream_clients": len(self.subscribers),
                "last_error": self.last_error,
                "capabilities": {
                    "device_control": self.settings.cec_socket.is_socket(),
                    "inbox": self.settings.inbox_dir is not None,
                },
            }
