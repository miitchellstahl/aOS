"""Reproducible SDK client: inspect, approved status patch, or one todo poll cycle."""

from __future__ import annotations

import argparse
import asyncio
from typing import Any
from urllib.parse import urlsplit

from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

from aos_mcp.settings import Credential


async def call(
    client: Client[Any], tool: str, arguments: dict[str, Any] | None = None
) -> dict[str, Any]:
    result = await client.call_tool(tool, arguments or {})
    if not isinstance(result.data, dict):
        raise RuntimeError("unexpected MCP result")
    return result.data


async def handle_todos(client: Client[Any]) -> dict[str, int]:
    """One consumer only; retry desired state (not toggle), commit, then acknowledge.

    Re-read stable IDs on redelivery: a prior successful commit may have lost its
    acknowledgment. Unknown/missing targets stay pending for human/agent review.
    Conflicts fail this cycle without acknowledgment; the next starts at zero.
    """
    page = await call(client, "get_pending_events", {"since_id": 0, "limit": 100})
    acknowledged = 0
    for event in page["events"]:
        if event["kind"] != "todo_toggle":
            continue
        intent = event["payload"]
        state = await call(client, "get_dashboard")
        target = next(
            (
                (section_index, item_index, item)
                for section_index, section in enumerate(state["sections"])
                if section["type"] == "todos" and section["id"] == intent["section_id"]
                for item_index, item in enumerate(section["items"])
                if item["id"] == intent["target_id"]
            ),
            None,
        )
        if target is None:
            continue
        section_index, item_index, item = target
        if item["done"] != intent["done"]:
            await call(
                client,
                "apply_dashboard_patch",
                {
                    "delta": [
                        {
                            "op": "replace",
                            "path": f"/sections/{section_index}/items/{item_index}/done",
                            "value": intent["done"],
                        }
                    ],
                    "expected_revision": state["revision"],
                },
            )
        await call(client, "acknowledge_events", {"ids": [event["id"]]})
        acknowledged += 1
    return {"acknowledged": acknowledged}


async def run(
    url: str, credential: Credential, *, note: str | None = None, todos: bool = False
) -> None:
    token = await asyncio.to_thread(credential.read)
    if token is None:
        raise RuntimeError("MCP credential unavailable")
    # this SDK owns initialization and JSON-RPC framing; no hand-authored RPC.
    async with Client(StreamableHttpTransport(url, auth=token), timeout=15) as client:
        tools = await client.list_tools()
        schema = await call(client, "get_schema")
        state = await call(client, "get_dashboard")
        print(f"tools={len(tools)} binding={schema['binding']} revision={state['revision']}")
        if note is not None:
            index = next(
                i for i, section in enumerate(state["sections"]) if section["type"] == "status"
            )
            updated = await call(
                client,
                "apply_dashboard_patch",
                {
                    "delta": [{"op": "replace", "path": f"/sections/{index}/note", "value": note}],
                    "expected_revision": state["revision"],
                },
            )
            print(f"committed revision={updated['revision']}")
        if todos:
            handled = await handle_todos(client)
            print(f"acknowledged todo intents={handled['acknowledged']}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8090/mcp")
    parser.add_argument("--set-status", help="explicitly approve one status note patch")
    parser.add_argument(
        "--handle-todos", action="store_true", help="approve one todo-intent poll cycle"
    )
    args = parser.parse_args()
    try:
        url = urlsplit(args.url)
        if (
            url.username is not None
            or url.password is not None
            or url.query
            or url.fragment
            or url.path != "/mcp"
            or not (
                url.scheme == "https"
                and url.hostname
                or url.scheme == "http"
                and url.hostname in {"127.0.0.1", "localhost", "::1"}
            )
        ):
            parser.error("use a loopback HTTP or remote HTTPS /mcp URL without credentials/query")
        asyncio.run(
            run(args.url, Credential.from_env("MCP"), note=args.set_status, todos=args.handle_todos)
        )
    except Exception:
        # SDK/network exceptions may embed URLs or response content; do not print them.
        parser.exit(
            1, "MCP client failed; check credentials, connection and safe tool error facts\n"
        )


if __name__ == "__main__":
    main()
