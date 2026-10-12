# Adapters

## CEC: local navigation only

`aos-cec` / `python -m aos_cec` runs libCEC's `cec-client` and creates one
`evdev.UInput` keyboard named `aOS CEC navigation`. Capabilities are exactly
Up/Down/Left/Right/Enter/Escape. There is no arbitrary key, shell, or uinput HTTP
endpoint. The optional root dependency extra `cec` supplies evdev; no evdev
installation is required to run fake adapter tests.

Only incoming `>>` CEC traffic from TV logical address 0 to the configured
playback address (default 4; alternatives 8/11) is interpreted. User Control
Pressed (44) and Released (45) frames drive key down/up. Duplicate press frames
become rate-limited repeat events for arrows after 350 ms, at most every 120 ms;
Enter/Escape never repeat. A key change, unsupported key, monitor exit, service
shutdown, or 800 ms without another press releases a held key. Outgoing `<<`
frames and libCEC's duplicate human-readable key notices are not input.

Flutter's focus/D-pad handling is the **only** navigation owner. The adapter
never posts `remote_key` to the bridge and never consumes CUSTOM remote events.
Raw remote presses, releases, and directional repeats are therefore **not in the
durable harness queue**. Flutter queues semantic todo-toggle/overlay-dismissal
intents; agent state patches do not loop back through keyboard injection. The
shared contracts retain explicit remote-key intents, but this CEC adapter does
not generate them. Do not enable a second remote-to-agent forwarding path: one
physical Enter must not produce both a raw toggle request and a semantic toggle.

### Control seam

The Unix socket is `/run/aos/cec.sock` (`AOS_CEC_SOCKET` on both services), owned
`aos-cec:aos-control`, mode 0660 in a 0750 service-owned directory. Only the
configured bridge Unix UID is accepted via Linux `SO_PEERCRED`, even if another
account can reach the socket. The CLI resolves `--bridge-user` (default
aos-bridge). There is no TCP CEC listener.

One request and one response per connection, newline-delimited JSON, maximum
4096 bytes, one-second read deadline:

```json
{"action":"power_on"}
{"ok":true}
```

Actions are exactly `power_on`, `power_off`, `active_source`. Rejection/device
failure/timeout returns `{"ok":false,"error":"CEC request rejected"}`; errors
never include subprocess output. Extra fields, arbitrary commands, malformed
JSON and unknown actions are rejected. At most eight connections are serviced;
excess connections close, which the bridge treats as failure.

The idle monitor keeps stdin open on `cec-client -s -t p -d 31 -o aOS -aw 0`.
Single-command mode is intentional: libCEC's normal interactive mode can wake
on startup or standby on exit. Explicit control serializes adapter ownership,
releases keys and pauses the monitor, executes `on 0`, `standby 0`, or `as` with
subprocess **exec**, then restarts the monitor. No concurrent libCEC clients are
started by this service. Power commands require an observed matching transmission
and a separate `pow 0` result of `on`/`standby`; a zero exit code is insufficient.
Active-source success means an observed active-source announcement with no CLI
failure, **not** proof that the TV selected the HDMI input or displayed pixels.
Command output is bounded at 64 KiB. An 18-second command deadline covers source
activation; only this service's child processes are terminated on cancellation.
Remote reception is unavailable during an explicit device action.

**Deadline hierarchy:** CEC defaults to an 18-second operation deadline; bridge
`Settings.cec_timeout_s` defaults to 22 seconds (`AOS_CEC_TIMEOUT_S`), and MCP
requests default to 26 seconds (`AOS_MCP_TIMEOUT_S`). Keep `18 < 22 < 26` to
cover monitor restart/socket overhead. Set the remote MCP client's tool deadline
to at least 30 seconds for device operations. The supplied env templates record
these bounds; provisioning preserves existing env files, so review those files
when upgrading rather than assuming they were overwritten. Changing adapter
`--timeout` requires preserving this hierarchy within the API's bounded limits.
A caller timeout can occur after a physical command was sent; it is not evidence
that nothing happened. Do not implement automatic toggle/retry semantics. These
are explicit on/off/source actions, not a toggle operation.

Kernel `/dev/cec0`, libCEC CLI format/flags, logical address allocation, full-KMS
adapter support and non-root permissions remain Pi hardware gates. Check
`cec-client -l` as the CEC account and inspect allocation before choosing
`AOS_CEC_LOGICAL_ADDRESS`. Addresses are deliberately not guessed from unrelated
bus traffic. Service readiness is not hardware success. A stale socket is never
blindly removed; systemd owns `/run/aos` cleanup when the service stops.

## Optional Muse companion

`aos-muse` / `python -m aos_muse` is a poller and handoff adapter, not an agent,
LLM, integration scheduler or alternate state owner. It is disabled until an
operator pairs an independently reviewed SDK, sets `AOS_MUSE_SESSION_ID` to an
assigned side chat and creates `/etc/aos/muse.enabled`. Provision does **not**
install Muse, fetch an installer, or generate/configure a real Muse SDK token.

Configuration: `AOS_BRIDGE_URL=http://127.0.0.1:8080`,
`AOS_MUSE_TOKEN_FILE=/etc/aos/muse.token` (the **local bridge capability**),
`AOS_MUSE_STATE_DIR=/var/lib/aos-muse`, `AOS_MUSEGADGET=/opt/muse/bin/musegadget`,
`AOS_MUSE_SESSION_ID=<paired-side-chat>`. URLs must be IPv4 loopback HTTP origins
without credentials/path/query. Credentials are reopened on every request;
redirects/proxy environment use are disabled. CLI exposes bounded SDK timeout
and poll interval. Failures back off up to 60 seconds and log only a generic
message, never HTTP bodies, SDK output or credentials.

Each cycle reads `/v1/events?since=0&limit=100`, validates input against the shared
contracts and invokes this argv (message is one JSON positional argument):

```text
musegadget send-user-msg --session-id SIDE_CHAT {"binding":"aos-appliance-v1","event":...}
```

The installed SDK must support this CLI form; fake subprocess tests verify argv,
not live SDK interoperability. No shell executes payloads. Exit zero is SDK
success; stdout/stderr are discarded, not interpreted as harness processing
receipts. Only after SDK success does the companion durably journal the event
ID/content digest, then POST `/v1/events/ack`. Failed SDK/storage means no ack.
A failed ack retries from zero; journaled handoffs retry ack without another send,
including after restart. One local flock prevents two companion processes; do
not poll the same queue through MCP simultaneously. The companion cannot enforce
that remote operators obey the one-consumer rule.

**Acknowledgment transfers responsibility to the configured Muse side chat**:
it does NOT assert the agent committed a dashboard mutation. Unlike direct MCP
handling (patch, then ack), this is a transport handoff. The downstream harness
must deduplicate by bridge event ID, preserve responsibility durably, use the
observed revision/stable IDs, and author authoritative updates through ingest
or the inbox. A crash after SDK acceptance but before journal fsync can resend;
delivery is at least once, not exactly once. Switching the session while a
journal exists fails closed; drain/review the old handoffs before migration.

The account may write its 0700 private handoff/SDK directory and the scoped inbox,
read only its local token, and call its allowed bridge APIs. It cannot write
`dashboard.json`, read MCP/general/display credentials, edit units/configuration,
use sudo or access CEC/uinput/DRM devices. Inbox commands use complete-file atomic
rename to a safe `.json` basename and wrapper
`{"expected_revision":N,"event":<uppercase AG-UI event>}`. The bridge alone imports,
validates, commits and emits; processed/rejected files are receipts, not another
state store. Keep producer `.tmp` files private until final chmod/group and rename.

## Verification

Run `.venv/bin/pytest adapters/cec/tests adapters/muse/tests -q`,
`.venv/bin/ruff check adapters`, `.venv/bin/ruff format --check adapters`, and
`.venv/bin/mypy adapters/cec/src adapters/muse/src adapters/cec/tests adapters/muse/tests`.
Tests fake outbound SDK/cec-client/evdev, exercise the real local Unix socket and
local mock HTTP boundary, and do not require a Pi or install the Muse SDK.
