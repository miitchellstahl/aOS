# Architecture

The locked appliance is Pi4 8GB / Pi OS Lite 64-bit / Flutter via flutter-pi,
without X11/Wayland. The Pi has no database, LLM, cloud backend, account
integrations or scheduler. A remote harness owns intelligence and scheduling.
There is no whiteboard, upload service, arbitrary renderer or media asset store.

```text
approved remote harness
  -> separately reviewed authenticated TLS tunnel, /mcp only
  -> FastMCP bearer boundary, 127.0.0.1:8090/mcp
  -> authenticated loopback bridge, 127.0.0.1:8080
       -> serialized reducer + atomic dashboard/queue files
       -> snapshot-first SSE, uppercase AG-UI shapes
       -> fixed six-region Flutter display
             -> semantic input intents -> durable bridge queue

TV remote -> cec-client -> six-key evdev keyboard -> local Flutter focus
bridge device command -> peer-restricted /run/aos/cec.sock -> CEC execution
optional Muse companion -> queue poll -> configured SDK side-chat handoff
optional Muse producer -> preconditioned inbox/ingest -> bridge reducer
```

## Code and contracts

`contracts/` and docs/schema.md own cross-component wire shapes. The bridge's
shared `Contracts` validator and generated display assets consume those schemas;
the companion reuses input validation rather than owning alternate models.
`bridge/` owns HTTP/auth, state reducer, file durability, single owner/bus,
subscriber backpressure, durable queue/dedup and optional secure inbox import.
`mcp/` is a FastMCP proxy with a separate external bearer credential; it is not
another state writer. `app/` owns validated boot/reducer/reconnect behavior and
the six fixed clock/agenda/todos/weather/briefing/status regions. Unknown types
are empty slots and array order cannot reconfigure layout. `adapters/cec/` owns
subprocess CEC monitoring/control and restricted virtual input. `adapters/muse/`
is optional outbound handoff plumbing, not an agent runtime implementation.
`scripts/` and `systemd/` describe explicit operator-managed local deployment.

## Transactions and feed

Bridge validates the entire resulting state and expected revision before
atomic durable commit; bridge metadata revision/updated_at are never
client-authored. One worker serializes transactions and publishes prepared
committed events. Snapshot capture and subscriber registration share the owner
boundary. Every reconnect gets a fresh snapshot, not SSE historical replay;
slow subscribers disconnect and rehydrate. A typed heartbeat proves connection,
not new dashboard content or agent health. Invalid patches retain last-good
state and trigger reconnect. First display paint uses a read-only validated
boot file with bundled fallback even if bridge networking is unavailable.

The runless GET feed uses AG-UI 1.0 uppercase event **shapes**; it is deliberately
not a full run transport with RUN_STARTED/RunAgentInput. The fixed display does
not implement A2UI rendering. No claim of zero-glue interoperability with
arbitrary remote AG-UI run servers is made.

## Input and ownership

CEC translates only incoming TV-to-player 44/45 frames. Outgoing echoes and
human-readable duplicate notices are ignored; arrow repeats are bounded,
activation repeats suppressed and missing releases recovered. Flutter focus is
sole navigation. CEC raw keys do not enter the durable queue; only Flutter's
semantic todo/dismissal actions do. This prevents a single remote click from
creating a raw agent toggle and a second UI intent. Patching rendered state never
injects keys or generates another semantic intent. Todo input is an intent,
not an optimistic authoritative change.

Queue IDs/dedup survive restart; success to the UI requires durable enqueue.
Delivery is at least once. One active queue consumer is supported: MCP processing
(perform authoritative change, then ack) or optional Muse handoff. Muse journals
SDK-success handoffs before ack and retries unacknowledged work from zero;
its ack transfers responsibility to the assigned side chat, not proof of agent
processing. Downstream harness deduplication is required. Muse never writes the
dashboard or gets a general/device capability.

## Appliance operations

Units use distinct service accounts. State belongs to bridge; the display gets
only a read group and its own credential. CEC alone owns uinput/CEC devices and
its bridge-UID-checked socket. Display receives selected DRM/render/tty1 and the
named virtual keyboard. Bridge/MCP/Muse have private devices. Tokens are generated
locally in protected files, never compiled into Flutter or copied into releases.
Optional SDK/tunnel credentials/installations are operator-managed, not silently
installed by provision. There are no LAN application listeners.

Packaging requires an explicit Flutter/tool/framework/engine/flutter-pi tuple
and exact ARM64 runtime artifact hashes. Host x64 GTK verification does not supply
that tuple. Build/deploy fail on missing or mismatched artifacts and retain
`hardware_validated:false`. Releases include Python/contract/app/runtime files
under immutable version directories; same-directory symlink renames activate
complete versions. Frozen dependency installation happens before activation;
rollback swaps code/runtime, never state/tokens. Provision never starts live
services or takes over the console. Doctor redacts secrets and reports structural
health, not device-action success.

See [adapters.md](adapters.md) for CEC/Muse protocol and timeout seams,
[security.md](security.md) for scopes, [setup.md](setup.md) for operator commands,
and [hardware-validation.md](hardware-validation.md) for unpassed hardware gates.
