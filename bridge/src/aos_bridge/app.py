"""Authenticated local ASGI surface for the runless aOS appliance binding."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from aos_bridge import device
from aos_bridge.contracts import Contracts
from aos_bridge.inbox import Inbox
from aos_bridge.owner import MAX_EVENT_ID, BridgeError, Owner, Subscriber
from aos_bridge.security import LocalRequests, authorize
from aos_bridge.settings import Settings


def integer(value: object, label: str, maximum: int | None = None) -> int:
    if type(value) is not int or value < 0 or (maximum is not None and value > maximum):
        raise BridgeError(422, f"invalid nonnegative integer at /{label}")
    return value


async def body(request: Request, fields: set[str] | None = None) -> dict[str, Any]:
    try:
        value = await request.json()
    except (ValueError, UnicodeError, RecursionError):
        raise BridgeError(422, "invalid JSON body") from None
    if not isinstance(value, dict):
        raise BridgeError(422, "expected JSON object")
    if fields is not None and set(value) != fields:
        raise BridgeError(422, "unexpected or missing request fields")
    return value


def expected_header(request: Request) -> int:
    value = request.headers.getlist("if-match")
    if len(value) != 1 or not re.fullmatch(r'"[0-9]{1,20}"', value[0]):
        raise BridgeError(422, 'state ingestion requires If-Match: "N"')
    return int(value[0][1:-1])


async def stream(
    owner: Owner, snapshot: dict[str, Any], subscriber: Subscriber
) -> AsyncGenerator[str, None]:
    def frame(event: dict[str, Any]) -> str:
        return "data: " + json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n\n"

    loop = asyncio.get_running_loop()
    deadline = loop.time() + owner.settings.heartbeat_s
    delivered_revision = snapshot["snapshot"]["revision"]
    try:
        yield frame(snapshot)
        while not subscriber.disconnected.is_set():
            if loop.time() >= deadline:
                event = {
                    "type": "CUSTOM",
                    "name": "aos.heartbeat",
                    "value": {"revision": delivered_revision},
                }
                deadline = loop.time() + owner.settings.heartbeat_s
                yield frame(event)
                continue
            get_event = asyncio.create_task(subscriber.queue.get())
            disconnected = asyncio.create_task(subscriber.disconnected.wait())
            try:
                done, _ = await asyncio.wait(
                    {get_event, disconnected},
                    timeout=max(0, deadline - loop.time()),
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if subscriber.disconnected.is_set():
                    return
                if get_event in done:
                    event = get_event.result()
                    if event["type"] == "STATE_DELTA":
                        delivered_revision += 1
                    yield frame(event)
            finally:
                get_event.cancel()
                disconnected.cancel()
                await asyncio.gather(get_event, disconnected, return_exceptions=True)
    finally:
        await owner.unsubscribe(subscriber)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    contracts = Contracts(settings.contracts_dir)
    owner = Owner(settings, contracts)

    async def ingest(
        event: dict[str, Any], expected: int | None, capability: str, source_id: str | None = None
    ) -> dict[str, Any]:
        contracts.validate_event(event)
        event_type = event["type"]
        if event_type in {"STATE_SNAPSHOT", "STATE_DELTA"}:
            if expected is None:
                raise BridgeError(422, "state ingestion requires expected_revision")
            if event_type == "STATE_SNAPSHOT":
                contracts.validate_replacement(event["snapshot"])
                return await owner.mutate(expected, payload=event["snapshot"])
            return await owner.mutate(expected, delta=event["delta"])
        name = event["name"]
        if name == "aos.heartbeat":
            raise BridgeError(403, "heartbeat is bridge-owned")
        if name == "aos.device":
            if capability != "bridge":
                raise BridgeError(403, "device action requires bridge capability")
            return await control(event["value"]["action"])
        if name == "aos.remote_key":
            state = await owner.dashboard()
            event_id = await owner.enqueue(
                {
                    "kind": "remote_key",
                    "client_event_id": source_id or uuid4().hex,
                    "observed_revision": expected if source_id is not None else state["revision"],
                    "key": event["value"]["key"],
                }
            )
            return {"id": event_id}
        await owner.custom(event)
        return {"ok": True}

    async def control(action: str) -> dict[str, Any]:
        try:
            result = await device.execute(settings, action)
        except BridgeError:
            owner.last_error = "CEC service unavailable"
            raise
        await owner.custom({"type": "CUSTOM", "name": "aos.device", "value": {"action": action}})
        return result

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        owner.start()
        inbox = (
            Inbox(
                settings,
                lambda event, expected, source_id: ingest(event, expected, "muse", source_id),
            )
            if settings.inbox_dir is not None
            else None
        )
        task: asyncio.Task[None] | None = None
        try:
            if inbox is not None:
                inbox.prepare()
                task = asyncio.create_task(inbox.run())
                app.state.inbox = inbox
            yield
        finally:
            if task is not None:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            owner.close()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.owner = owner
    app.state.settings = settings
    app.add_middleware(LocalRequests)

    @app.exception_handler(BridgeError)
    async def bridge_error(request: Request, error: BridgeError) -> JSONResponse:
        headers = {"WWW-Authenticate": "Bearer"} if error.status == 401 else None
        return JSONResponse({"detail": error.detail}, status_code=error.status, headers=headers)

    @app.exception_handler(ValueError)
    async def invalid_contract(request: Request, error: ValueError) -> JSONResponse:
        return JSONResponse({"detail": str(error)}, status_code=422)

    @app.get("/v1/dashboard")
    async def dashboard(request: Request) -> JSONResponse:
        authorize(request, settings, {"display", "muse"})
        state = await owner.dashboard()
        return JSONResponse(
            state, headers={"ETag": f'"{state["revision"]}"', "Cache-Control": "no-store"}
        )

    @app.post("/v1/dashboard")
    async def replace(request: Request) -> dict[str, Any]:
        authorize(request, settings, set())
        value = await body(request, {"payload", "expected_revision"})
        expected = integer(value["expected_revision"], "expected_revision")
        contracts.validate_replacement(value["payload"])
        return await owner.mutate(expected, payload=value["payload"])

    @app.post("/v1/ingest")
    async def ingest_route(request: Request) -> dict[str, Any]:
        capability = authorize(request, settings, {"muse"})
        event = await body(request)
        expected = (
            expected_header(request)
            if event.get("type") in ("STATE_SNAPSHOT", "STATE_DELTA")
            else None
        )
        return await ingest(event, expected, capability)

    @app.post("/v1/overlay")
    async def overlay(request: Request) -> dict[str, bool]:
        authorize(request, settings, set())
        value = await body(request, {"message", "duration_s"})
        await owner.custom({"type": "CUSTOM", "name": "aos.overlay", "value": value})
        return {"ok": True}

    @app.get("/v1/stream")
    async def subscribe(request: Request) -> StreamingResponse:
        authorize(request, settings, {"display"})
        snapshot, subscriber = await owner.subscribe()
        return StreamingResponse(
            stream(owner, snapshot, subscriber),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
        )

    @app.get("/v1/schema")
    async def schema(request: Request) -> dict[str, Any]:
        authorize(request, settings, {"display", "muse"})
        return contracts.schema_contract()

    @app.get("/v1/status")
    async def status(request: Request) -> dict[str, Any]:
        authorize(request, settings, {"display", "muse"})
        status = await owner.status()
        if hasattr(app.state, "inbox"):
            status["inbox_error"] = app.state.inbox.last_error
            if status["inbox_error"]:
                status["health"] = "degraded"
        return status

    @app.post("/v1/input")
    async def input_route(request: Request) -> dict[str, int]:
        authorize(request, settings, {"display"})
        return {"id": await owner.enqueue(await body(request))}

    @app.get("/v1/events")
    async def events(request: Request) -> dict[str, Any]:
        authorize(request, settings, {"muse"})
        if set(request.query_params) - {"since", "limit"}:
            raise BridgeError(422, "unexpected pagination field")
        params: dict[str, int] = {}
        for key, default in (("since", "0"), ("limit", "100")):
            values = request.query_params.getlist(key) or [default]
            if len(values) != 1 or not re.fullmatch(r"[0-9]{1,20}", values[0]):
                raise BridgeError(422, "invalid pagination cursor/limit")
            params[key] = int(values[0])
        if params["since"] > MAX_EVENT_ID or not 1 <= params["limit"] <= 100:
            raise BridgeError(422, "pagination cursor/limit out of bounds")
        return await owner.events(params["since"], params["limit"])

    @app.post("/v1/events/ack")
    async def ack(request: Request) -> dict[str, list[int]]:
        authorize(request, settings, {"muse"})
        ids = (await body(request, {"ids"}))["ids"]
        if not isinstance(ids, list) or not 1 <= len(ids) <= 1000:
            raise BridgeError(422, "expected 1-1000 acknowledgment IDs at /ids")
        for event_id in ids:
            if integer(event_id, "ids", MAX_EVENT_ID) == 0:
                raise BridgeError(422, "acknowledgment IDs must be positive at /ids")
        return {"acknowledged": await owner.ack(ids)}

    @app.post("/v1/device/power")
    async def power(request: Request) -> dict[str, Any]:
        authorize(request, settings, set())
        value = await body(request, {"state"})
        if value["state"] not in ("on", "off"):
            raise BridgeError(422, "invalid device state at /state")
        return await control("power_on" if value["state"] == "on" else "power_off")

    @app.post("/v1/device/input")
    async def active_source(request: Request) -> dict[str, Any]:
        authorize(request, settings, set())
        if (await body(request, {"action"}))["action"] != "active_source":
            raise BridgeError(422, "invalid device action at /action")
        return await control("active_source")

    return app
