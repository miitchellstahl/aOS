# MCP harness integration

The bridge is the only state writer. aOS exposes FastMCP **2.14.7** Streamable
HTTP at `http://127.0.0.1:8090/mcp`; the MCP process only proxies the bridge at
`http://127.0.0.1:8080`. It does not run an LLM, agent loop, scheduler, or hosted
service on the Pi. The fixed dashboard is not an A2UI renderer, and its runless,
snapshot-first SSE feed is not a claim of full AG-UI transport conformance.

FastMCP 2.14.7 starts its internal Docket worker even when tools have
`tasks=False`. `ApplianceMCP` therefore overrides that SDK lifecycle hook only,
preserving the SDK's server context but starting no worker, scheduler, or Redis
client. This narrowly scoped private-hook adaptation and the log redaction were
verified against **2.14.7**; retain the locked version and reverify them before
upgrading FastMCP. The SDK still owns authentication and Streamable HTTP/RPC.

## Authentication and reachability

Provision different capabilities for the external harness and the local bridge:

| Process | Configuration | Default credential file |
| --- | --- | --- |
| MCP server and reference harness | `AOS_MCP_TOKEN_FILE` or `AOS_MCP_TOKEN` | `/etc/aos/mcp.token` |
| MCP server's local bridge requests | `AOS_BRIDGE_TOKEN_FILE` or `AOS_BRIDGE_TOKEN` | `/etc/aos/bridge.token` |

An explicitly configured file takes precedence over the environment token. A
missing, unreadable, empty, oversized, or invalid file **does not** fall back to an
environment token. Printable ASCII tokens without whitespace are accepted. Token
files are capped at 4,096 bytes including an optional trailing newline; environment
tokens are capped at 4,096 bytes. Protect these files with service
ownership and restrictive permissions. Never put capabilities in URLs, command
arguments, source control, screenshots, or logs. Display/Muse capabilities must
not be substituted for the general bridge capability.

The server uses the actual FastMCP `TokenVerifier`/`AccessToken` authentication
API, constant-time comparison, and an `aos` scope. It rejects equal MCP and bridge
credentials and fails closed if either capability is unavailable. The MCP file is
reopened on each authenticated HTTP request; the bridge file is reopened on each
proxy request. Replace files atomically to rotate them. Removing or emptying the
MCP file revokes subsequent requests, including requests from an already
initialized client. Requests already authorized/in flight are not canceled.
Environment-token changes require restarting the process; file rotation does not.

Streamable HTTP is **stateless**, so an authorized session cannot bypass a later
revocation. The SDK owns initialization and JSON-RPC framing. There is no custom
JSON-RPC endpoint. HTTP requests are capped at 256 KiB, bridge payloads at 128 KiB,
bridge responses at 512 KiB, and body/proxy operations at 26 seconds by default
(`AOS_MCP_TIMEOUT_S`, bounded to 30 seconds). CEC commands have an 18-second
budget inside the bridge's 22-second deadline (`AOS_CEC_TIMEOUT_S`). Remote
clients should allow at least 30 seconds for device tool calls; a timeout may
follow an already executed TV command, so inspect the device before retrying.
The managed async HTTPX client ignores ambient proxy configuration and does not
follow redirects. Uvicorn listens only on `127.0.0.1`, with proxy-header handling
and access logs disabled. SDK request-bearing debug/validation logging is
suppressed; do not enable transport wire logging or replace these safeguards.

Every browser `Origin` is rejected, including localhost and `null`. Hosts are an
exact allowlist (not wildcards): by default `127.0.0.1`, `localhost`, and their
`:8090` forms. A CLI `--port` override adjusts those local port forms unless an
explicit allowlist was supplied. To preserve an external tunnel Host, configure,
for example:

```sh
export AOS_MCP_ALLOWED_HOSTS='127.0.0.1:8090,localhost:8090,appliance.example.net'
```

This only allows that exact Host; it does not add a tunnel, TLS, CORS, or an
alternate public listener. A remote harness needs a separately provisioned
private forward/tunnel or HTTPS gateway to loopback. Forward the bearer header,
keep access/wire logs free of credentials, and expose only `/mcp`, never the
bridge, token files, CEC socket, or appliance filesystem. Do not rebind the MCP
process to `0.0.0.0`. Remote HTTPS and vendor tunnel behavior remain unverified.

## Tool contract

`docs/schema.md` is authoritative and enumerates **ten** tools.

| MCP tool | Authenticated bridge operation |
| --- | --- |
| `get_schema()` | `GET /v1/schema` |
| `get_dashboard()` | `GET /v1/dashboard` |
| `get_status()` | `GET /v1/status` |
| `update_dashboard(payload, expected_revision)` | `POST /v1/dashboard` |
| `apply_dashboard_patch(delta, expected_revision)` | `POST /v1/ingest`, `STATE_DELTA`, quoted `If-Match` |
| `show_notification(message, duration_s=5)` | `POST /v1/overlay` |
| `get_pending_events(since_id=0, limit=100)` | `GET /v1/events?since=...&limit=...` |
| `acknowledge_events(ids)` | `POST /v1/events/ack` |
| `device_power(state)` | `POST /v1/device/power`, `on` or `off` |
| `device_activate_source()` | `POST /v1/device/input`, `active_source` |

Read tools return bridge data as structured SDK results. Replacement payloads
exclude bridge-owned `revision` and `updated_at`; edits use the latest revision.
RFC 6902 patches cannot mutate bridge-owned metadata. The bridge applies the
shared JSON Schemas and remains authoritative. MCP annotation validation is
redacted rather than exposing submitted argument values.

Tool failures have a JSON text error with `status`, a safe `detail`, and where
available approved `reason`, `pointer`, and `rule` diagnostics. Validation (422),
revision conflict (409), request size (413), authentication (401), forbidden
operation (403), and storage/queue/device failure (503) remain distinguishable.
Unknown upstream text, submitted values, response bodies, exception text, and
credentials are not forwarded. HTTP boundary/SDK transport rejections preserve
HTTP status but have a generic HTTP error body rather than reflected SDK values.
A lost response may follow a successful commit: reread state before retrying,
and never blindly replay a toggle or overwrite on a conflict. Device failures
are failures; `ok: true` is returned only when the bridge reports success.

## Reproducible reference client

From the repository root, using the installed development environment and
already-provisioned files, start the bridge in one terminal and MCP in another:

```sh
export AOS_BRIDGE_TOKEN_FILE=/etc/aos/bridge.token
.venv/bin/python -m aos_bridge
```

```sh
export AOS_BRIDGE_TOKEN_FILE=/etc/aos/bridge.token
export AOS_MCP_TOKEN_FILE=/etc/aos/mcp.token
.venv/bin/python -m aos_mcp
```

The reference harness only needs the external credential. It uses FastMCP's
`Client` and `StreamableHttpTransport`; no manual JSON-RPC is required:

```sh
export AOS_MCP_TOKEN_FILE=/etc/aos/mcp.token

# Read-only: list tools, retrieve schema and dashboard; print a compact summary.
.venv/bin/python -m aos_mcp.reference_client

# Explicitly approve one revision-guarded status note change.
.venv/bin/python -m aos_mcp.reference_client --set-status 'Ready for the next task'

# Explicitly approve one bounded poll/commit/ack cycle for todo intents.
.venv/bin/python -m aos_mcp.reference_client --handle-todos
```

`--url https://appliance.example.net/mcp` selects a separately provisioned remote
endpoint. The CLI rejects non-loopback plain HTTP and URL credentials, queries,
or fragments. A production harness must retrieve its capability from its own
secret mechanism; no specific hosted harness secret/configuration flow is
claimed here. The reference client reports only counts, binding, and revisions,
not tokens or raw state/error bodies.

For another SDK-based harness, the minimal read-only shape is:

```python
import asyncio
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
from aos_mcp.settings import Credential


async def inspect_appliance():
    token = Credential.from_env("MCP").read()
    if token is None:
        raise RuntimeError("MCP credential unavailable")
    async with Client(
        StreamableHttpTransport("http://127.0.0.1:8090/mcp", auth=token),
        timeout=15,
    ) as client:
        tools = await client.list_tools()
        schema = (await client.call_tool("get_schema")).data
        dashboard = (await client.call_tool("get_dashboard")).data
        assert isinstance(schema, dict) and isinstance(dashboard, dict)
        print(len(tools), schema["binding"], dashboard["revision"])


asyncio.run(inspect_appliance())
```

### Durable intents

Use exactly one active queue consumer: either the harness or the Muse companion,
not both. `since_id` is a pagination cursor, **not** acknowledgment. Pending
intents remain until `acknowledge_events`. The reference todo cycle:

1. Polls from zero with a limit of 100, so failed/unacknowledged work is retried.
2. Resolves the current section/item by stable IDs, not stale array positions.
3. Applies the intent's desired `done` value with the current revision, rather
   than toggling locally. An already-satisfied desired state needs no new patch.
4. Acknowledges only after a successful commit or a confirmed already-satisfied
   desired state. Unknown kinds/missing targets stay pending. Conflicts stop the
   cycle without acknowledging that event; rerun from zero after review.

This is an explicitly invoked harness example, not a scheduler or autonomous
agent on the Pi. It handles one page per invocation. At-least-once delivery still
requires idempotent domain decisions in a real agent, especially for arbitrary
note/button intents or external side effects.

## Verified-client matrix

Only locally executed coverage is listed as verified. The integration fixture
starts real Uvicorn bridge and MCP servers on temporary loopback ports with
separate token files; it uses the installed FastMCP 2.14.7 client. It does not
exercise an actual remote gateway or Pi display.

| Client/fixture | Verified behavior | Evidence / limits |
| --- | --- | --- |
| FastMCP Python 2.14.7 `Client` + `StreamableHttpTransport` | Initialization, list exact tool set, all ten tool calls, schema/dashboard/status, replacement, patch, overlay, pending input, idempotent ack | `mcp/tests/test_live_mcp.py`; real local bridge; SSE snapshot/delta/overlay observed through bridge HTTP |
| Same SDK client with file capabilities | Missing/bad/local token rejection, token rotation/revocation after initialization, bridge-token reread, equal credentials fail closed, no session ID | `mcp/tests/test_live_mcp.py` and `mcp/tests/test_security_proxy.py` |
| Reference client functions | Approved status patch; desired-state todo commit/ack; redelivered desired state acknowledged without another commit | `test_reference_client_status_patch_and_redelivered_todo_cycle` in `mcp/tests/test_live_mcp.py`; module CLI help also executed |
| Direct HTTPX boundary probes | Host/Origin rejection, exact simulated tunnel Host accepted by SDK client, content-length/chunked body limits, sanitized malformed SDK envelope | `mcp/tests/test_live_mcp.py`; simulated Host only, not a tunnel deployment |
| Unit/mock HTTPX probes | Managed client lifecycle, no Docket/worker/backend creation, request/response bounds, timeouts/transport failures, response encoding/shape, safe validation/conflict/storage facts | `mcp/tests/test_security_proxy.py`; upstream failures injected deliberately |
| Fake local CEC Unix socket | SDK tool wiring for power on/off and active source; absent service reported as failure | Fixture in `mcp/tests/conftest.py`; **protocol test only, no physical device claim** |

Run the scoped checks from the repository root:

```sh
.venv/bin/pytest mcp/tests -q
.venv/bin/ruff check mcp
.venv/bin/ruff format --check mcp
.venv/bin/mypy mcp/src
```

Unverified: every hosted/vendor harness (including Claude Desktop/Code, Codex,
ChatGPT, and Muse as an MCP client), remote TLS/tunnels, Pi 4 execution,
flutter-pi, HDMI/TV/physical CEC, and systemd deployment. Listing a client here
as unverified is not a compatibility claim. Root dependency/entry-point/CI
integration is owned by the main integration task, not this MCP-only scope.
