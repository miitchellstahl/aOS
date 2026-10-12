from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from copy import deepcopy
from dataclasses import replace
from typing import Any

import jsonpatch
import pytest
from aos_bridge.app import create_app, stream
from aos_bridge.contracts import Contracts
from aos_bridge.owner import BridgeError, Owner
from aos_bridge.settings import Settings
from aos_bridge.store import StorageError
from starlette.types import Message


@pytest.fixture
async def owner(settings: Settings) -> AsyncIterator[Owner]:
    instance = Owner(settings, Contracts())
    instance.start()
    try:
        yield instance
    finally:
        instance.close()


def content(owner: Owner) -> dict[str, Any]:
    return {
        key: deepcopy(value)
        for key, value in owner.state.items()
        if key not in {"revision", "updated_at"}
    }


def intent(client_id: str) -> dict[str, Any]:
    return {"kind": "remote_key", "client_event_id": client_id, "observed_revision": 0, "key": "ok"}


async def test_concurrent_mutation_exactly_one_commit(owner: Owner) -> None:
    snapshot, subscriber = await owner.subscribe()
    results = await asyncio.gather(
        owner.mutate(0, payload=content(owner)),
        owner.mutate(0, payload=content(owner)),
        return_exceptions=True,
    )
    assert sum(isinstance(result, dict) for result in results) == 1
    errors = [result for result in results if isinstance(result, BridgeError)]
    assert len(errors) == 1 and errors[0].status == 409
    assert owner.state["revision"] == 1
    event = subscriber.queue.get_nowait()
    owner.contracts.validate_event(event)
    assert jsonpatch.JsonPatch(event["delta"]).apply(snapshot["snapshot"]) == owner.state
    assert subscriber.queue.empty()


async def test_subscribe_update_race_is_snapshot_or_ordered_delta(owner: Owner) -> None:
    for _ in range(12):
        revision = owner.state["revision"]
        mutation = owner.mutate(revision, payload=content(owner))
        if revision % 2:
            state, (snapshot, subscriber) = await asyncio.gather(mutation, owner.subscribe())
        else:
            (snapshot, subscriber), state = await asyncio.gather(owner.subscribe(), mutation)
        current = snapshot["snapshot"]
        while not subscriber.queue.empty():
            current = jsonpatch.JsonPatch(subscriber.queue.get_nowait()["delta"]).apply(current)
        assert current == state
        await owner.unsubscribe(subscriber)


async def test_slow_subscriber_overflow_disconnects_and_reconnects(owner: Owner) -> None:
    snapshot, subscriber = await owner.subscribe()
    for _ in range(33):
        await owner.mutate(owner.state["revision"], payload=content(owner))
    assert subscriber.queue.qsize() == 32
    assert subscriber.disconnected.is_set()
    assert subscriber not in owner.subscribers
    new_snapshot, reconnected = await owner.subscribe()
    assert snapshot["snapshot"]["revision"] == 0
    assert new_snapshot["snapshot"]["revision"] == 33
    assert reconnected.queue.empty()


async def test_stream_snapshot_heartbeat_delta_and_cleanup(owner: Owner) -> None:
    owner.settings = replace(owner.settings, heartbeat_s=0.01)
    snapshot, subscriber = await owner.subscribe()
    generator = stream(owner, snapshot, subscriber)

    def decode(frame: str) -> dict[str, Any]:
        assert frame.startswith("data: ") and frame.endswith("\n\n")
        return json.loads(frame[6:])

    assert decode(await anext(generator))["type"] == "STATE_SNAPSHOT"
    heartbeat = decode(await anext(generator))
    assert heartbeat == {"type": "CUSTOM", "name": "aos.heartbeat", "value": {"revision": 0}}
    before = deepcopy(owner.state)
    await owner.mutate(0, payload=content(owner))
    delta = decode(await anext(generator))
    while delta["type"] == "CUSTOM":
        assert delta == heartbeat
        delta = decode(await anext(generator))
    assert delta["type"] == "STATE_DELTA"
    assert jsonpatch.JsonPatch(delta["delta"]).apply(before) == owner.state
    await generator.aclose()
    assert subscriber not in owner.subscribers


async def test_stream_overflow_stops_without_draining_stale_patches(owner: Owner) -> None:
    snapshot, subscriber = await owner.subscribe()
    generator = stream(owner, snapshot, subscriber)
    await anext(generator)
    for _ in range(33):
        await owner.custom(
            {
                "type": "CUSTOM",
                "name": "aos.overlay",
                "value": {"message": "notice", "duration_s": 5},
            }
        )
    with pytest.raises(StopAsyncIteration):
        await anext(generator)
    assert subscriber not in owner.subscribers
    assert owner.state["revision"] == 0


async def test_queue_restart_redelivery_monotonic_ids_ack_and_dedup(owner: Owner) -> None:
    assert await owner.enqueue(intent("one")) == 1
    assert await owner.enqueue(intent("two")) == 2
    assert await owner.enqueue(intent("one")) == 1
    assert (await owner.events(0, 1))["next_cursor"] == 1
    assert (await owner.events(1, 1))["events"][0]["id"] == 2
    owner.close()
    owner.start()
    assert [event["id"] for event in (await owner.events(0, 100))["events"]] == [1, 2]
    assert await owner.ack([1, 1, 999]) == [1]
    assert await owner.ack([1]) == [1]
    owner.close()
    owner.start()
    assert await owner.ack([1]) == [1]
    assert await owner.enqueue(intent("one")) == 1
    assert await owner.enqueue(intent("three")) == 3
    assert [event["id"] for event in (await owner.events(0, 100))["events"]] == [2, 3]


async def test_ack_failed_commit_leaves_pending(
    owner: Owner, monkeypatch: pytest.MonkeyPatch
) -> None:
    await owner.enqueue(intent("one"))

    def fail(name: str, value: dict[str, Any]) -> None:
        raise StorageError("storage unavailable")

    monkeypatch.setattr(owner.store, "write", fail)
    with pytest.raises(BridgeError) as error:
        await owner.ack([1])
    assert error.value.status == 503
    assert (await owner.events(0, 100))["events"][0]["id"] == 1
    assert owner.queue["acknowledged"] == []


async def test_queue_full_no_ui_success_and_pending_dedup_never_evicted(owner: Owner) -> None:
    owner.settings = replace(owner.settings, queue_limit=3, history_limit=1)
    await owner.enqueue(intent("oldest"))
    await owner.enqueue(intent("second"))
    await owner.enqueue(intent("third"))
    with pytest.raises(BridgeError) as error:
        await owner.enqueue(intent("overflow"))
    assert error.value.status == 503
    assert await owner.enqueue(intent("oldest")) == 1
    await owner.ack([2])
    assert await owner.enqueue(intent("fourth")) == 4
    await owner.ack([3])
    assert await owner.enqueue(intent("oldest")) == 1
    assert len(owner.queue["history"]) == 1
    assert len(owner.queue["acknowledged"]) == 1
    owner.close()
    owner.start()
    assert await owner.enqueue(intent("oldest")) == 1
    assert await owner.enqueue(intent("fourth")) == 4


async def test_same_client_id_different_payload_rejected_after_ack(owner: Owner) -> None:
    await owner.enqueue(intent("one"))
    await owner.ack([1])
    with pytest.raises(BridgeError) as error:
        await owner.enqueue({**intent("one"), "key": "back"})
    assert error.value.status == 409


async def test_recovery_from_corrupt_primary_keeps_committed_revision(owner: Owner) -> None:
    await owner.mutate(0, payload=content(owner))
    await owner.enqueue(intent("one"))
    await owner.ack([1])
    committed = deepcopy(owner.state)
    owner.close()
    (owner.settings.state_dir / "dashboard.json").write_text("{torn")
    (owner.settings.state_dir / "events.json").write_text("{torn")
    owner.start()
    assert owner.state == committed
    assert await owner.enqueue(intent("one")) == 1
    assert await owner.enqueue(intent("two")) == 2
    state = await owner.mutate(1, payload=content(owner))
    assert state["revision"] == 2


async def test_cannot_start_two_owners_in_same_directory(owner: Owner) -> None:
    other = Owner(owner.settings, owner.contracts)
    with pytest.raises(StorageError, match="already owned"):
        other.start()
    assert owner.state["revision"] == 0
    owner.close()
    other.start()
    other.close()


async def test_corrupt_both_documents_fail_closed(owner: Owner) -> None:
    owner.close()
    for name in ("dashboard.json", "dashboard.json.last-good"):
        (owner.settings.state_dir / name).write_text("{}")
    with pytest.raises(StorageError, match="no valid"):
        owner.start()


async def test_committed_diff_limits_for_large_replacement(owner: Owner) -> None:
    payload = content(owner)
    payload["sections"] = [{"id": "status", "type": "status", "note": "ready"}] + [
        {
            "id": f"todos-{section}",
            "type": "todos",
            "items": [
                {"id": f"item-{item}", "label": f"work {item}", "done": False} for item in range(30)
            ],
        }
        for section in range(5)
    ]
    # hundreds of edits normalize to a bounded top-level diff if needed.
    await owner.mutate(0, payload=payload)
    before, subscriber = await owner.subscribe()
    for section in payload["sections"][1:]:
        for item in section["items"]:
            item["done"] = True
    after = await owner.mutate(1, payload=payload)
    event = subscriber.queue.get_nowait()
    assert len(event["delta"]) <= 128
    assert jsonpatch.JsonPatch(event["delta"]).apply(before["snapshot"]) == after


async def test_http_stream_snapshot_and_ordered_delta_without_browser(settings: Settings) -> None:
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        frames: asyncio.Queue[Message] = asyncio.Queue()
        request_sent = False
        disconnected = asyncio.Event()

        async def receive() -> Message:
            nonlocal request_sent
            if not request_sent:
                request_sent = True
                return {"type": "http.request", "body": b"", "more_body": False}
            await disconnected.wait()
            return {"type": "http.disconnect"}

        async def send(message: Message) -> None:
            await frames.put(message)

        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.0"},
            "method": "GET",
            "scheme": "http",
            "path": "/v1/stream",
            "raw_path": b"/v1/stream",
            "query_string": b"",
            "http_version": "1.1",
            "headers": [
                (b"host", b"127.0.0.1"),
                (b"authorization", b"Bearer bridge-secret"),
                (b"last-event-id", b"999"),
            ],
        }
        task = asyncio.create_task(app(scope, receive, send))
        try:
            start = await asyncio.wait_for(frames.get(), 2)
            assert start["status"] == 200
            snapshot_frame = await asyncio.wait_for(frames.get(), 2)
            snapshot = json.loads(snapshot_frame["body"][6:])
            assert snapshot["type"] == "STATE_SNAPSHOT" and snapshot["snapshot"]["revision"] == 0
            owner = app.state.owner
            after = await owner.mutate(0, payload=content(owner))
            delta_frame = await asyncio.wait_for(frames.get(), 2)
            delta = json.loads(delta_frame["body"][6:])
            assert jsonpatch.JsonPatch(delta["delta"]).apply(snapshot["snapshot"]) == after
        finally:
            disconnected.set()
            await asyncio.wait_for(task, 2)
        assert len(app.state.owner.subscribers) == 0
