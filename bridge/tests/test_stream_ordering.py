import asyncio
import json
from dataclasses import replace

from aos_bridge.app import stream
from aos_bridge.contracts import Contracts
from aos_bridge.owner import Owner
from aos_bridge.settings import Settings


async def test_heartbeat_does_not_advertise_a_queued_revision(settings: Settings) -> None:
    owner = Owner(replace(settings, heartbeat_s=0.01), Contracts())
    owner.start()
    snapshot, subscriber = await owner.subscribe()
    generator = stream(owner, snapshot, subscriber)
    try:
        await anext(generator)
        await owner.mutate(
            0,
            delta=[{"op": "replace", "path": "/sections/2/items/0/done", "value": True}],
        )
        await asyncio.sleep(0.02)
        heartbeat = json.loads((await anext(generator))[6:])
        assert heartbeat["name"] == "aos.heartbeat"
        assert heartbeat["value"]["revision"] == 0
        assert json.loads((await anext(generator))[6:])["type"] == "STATE_DELTA"
        heartbeat = json.loads((await anext(generator))[6:])
        assert heartbeat["value"]["revision"] == 1
    finally:
        await generator.aclose()
        owner.close()
