# aOS v1 contract

## Shared state

`contracts/dashboard.v1.schema.json` is authoritative. Section IDs and todo IDs
are unique within their owner. There is exactly one status section; at most one
clock, agenda, weather, briefing, or status. Unknown types render as empty slots.
`revision` and `updated_at` are bridge-owned. Snapshot revision starts at zero.

## HTTP interface (loopback only, 127.0.0.1:8080)

All routes require bearer authentication, including reads/SSE. `AOS_BRIDGE_TOKEN`
or `AOS_BRIDGE_TOKEN_FILE` is the MCP/general capability. A separate display
credential may read dashboard/schema/status/SSE and submit UI input or dismiss
overlays. A separate Muse credential may read contracts/state/queue, ingest
events, and acknowledge queue events, but not control the device. Missing
credentials fail closed. Reject non-local Host headers and browser Origins.

| Route | Request / response |
| --- | --- |
| GET /v1/dashboard | Full persisted dashboard, ETag quoted revision. |
| POST /v1/dashboard | `{payload: <replacement>, expected_revision: N}`. Replacement excludes revision/updated_at; version/theme/sections required. Return full committed dashboard. |
| POST /v1/ingest | One AG-UI event. State writes require `If-Match: "N"`; snapshots exclude bridge metadata. STATE_DELTA is an RFC 6902 array. Return full committed dashboard for state writes. |
| POST /v1/overlay | `{message: <text>, duration_s: 5}`; emit CUSTOM aos.overlay. |
| GET /v1/stream | SSE; first STATE_SNAPSHOT, then committed STATE_DELTA/CUSTOM; typed aos.heartbeat every 15s. Slow client disconnected. Every reconnect gets a new snapshot. |
| GET /v1/schema | `{ag_ui_version:"1.0", binding:"aos-appliance-v1", state_schema, custom_events, input_schema, examples}`. |
| GET /v1/status | Health, revision, last update timestamp, pending queue depth, capabilities. |
| POST /v1/input | One input intent per `contracts/input.v1.schema.json`; durable enqueue before success. Return `{id: N}`. |
| GET /v1/events?since=N&limit=100 | `{events:[{id,kind,created_at,payload}], next_cursor:N}`. `since` defaults to zero; it is pagination, not acknowledgment. |
| POST /v1/events/ack | `{ids:[N]}`; idempotent, return `{acknowledged:[N]}`. |
| POST /v1/device/power | `{state:"on"\|"off"}`; actual CEC service execution, never fake success. |
| POST /v1/device/input | `{action:"active_source"}`; actual CEC execution. |

State changes serialize, validate resulting state, commit with atomic durable
replacement, increment revision once, and emit normalized patches including
bridge-owned metadata. Patches cannot write `/revision`, `/updated_at`, or root;
validate final state and unique IDs. Failure changes no file or stream state.
Limits: 128 KiB request/state, 128 patch operations, 32 pending stream frames;
outbound event envelopes allow 129 KiB so maximum-size state can rehydrate;
bounded durable queue (default 1,000 pending events) and deduplication history.
422 invalid payload, 409 stale revision, 401 bad credential, 403 forbidden action,
413 excessive body, 503 storage/device/queue failure. Errors have `detail` and
where applicable field/JSON pointer diagnostics; do not include secrets.

Display todo actions are intents, not optimistic authoritative state changes.
The agent patches the dashboard and then acknowledges; delivery is at least once.
One active queue consumer (MCP polling or Muse companion); poll anew from zero
after failures. Overlay dismissal does not update dashboard content freshness.
Heartbeat revisions describe the last state delivered to that subscriber, never
a newer state whose delta is still queued. They do not change `updated_at`.

Device deadline defaults nest CEC execution (18s) inside the bridge socket wait
(22s, `AOS_CEC_TIMEOUT_S`) and MCP proxy deadline (26s, `AOS_MCP_TIMEOUT_S`). Both
environment overrides must be positive finite numbers no greater than 30s.
Changing the CEC command budget requires adjusting the outer deadlines as well.
Client tool deadlines should be at least 30s; a timeout does not undo a TV action.

## MCP interface (loopback 127.0.0.1:8090, /mcp)

External bearer: `AOS_MCP_TOKEN` or `AOS_MCP_TOKEN_FILE`, separate from local
bridge/display tokens. Use FastMCP Streamable HTTP; no custom JSON-RPC wrapper.
Read token files on authentication so rotation/revocation affects new requests.
Tools: get_schema, get_dashboard, get_status, update_dashboard(payload,
expected_revision), apply_dashboard_patch(delta, expected_revision),
show_notification(message, duration_s), get_pending_events(since_id=0, limit=100),
acknowledge_events(ids), device_power(state), device_activate_source.
All tools proxy authenticated bridge routes; no independent state writer/bus.

## Display configuration

Pure Dart dependencies, `ag_ui: ^0.3.0`, file boot snapshot with bundled fallback.
Native process environment: AOS_BRIDGE_URL (default http://127.0.0.1:8080),
AOS_DASHBOARD_PATH (/var/lib/aos/dashboard.json), AOS_DISPLAY_TOKEN_FILE
(/etc/aos/display.token). Runtime credentials must not be compiled into bundles.
Keep last valid snapshot on stream failure; reconnect until snapshot rehydrates.
Unknown sections are empty slots; fixed layout is keyed by type, not array order.
