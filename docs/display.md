# Native aOS display

`app/` is a Flutter appliance display, not a browser or an A2UI renderer. The
production target remains Pi 4 8GB, Raspberry Pi OS Lite 64-bit and flutter-pi on
DRM/KMS, without X11 or Wayland. There are no cloud services, accounts, database,
LLM, scheduler, uploads or whiteboard in the display. The bridge is the only
writer of dashboard state.

## Toolchain and dependencies

Host verification uses the official Flutter GitHub tag **3.38.10**, installed at
`/home/ubuntu/toolchains/flutter-3.38.10`: framework `c6f67dede3`, engine revision
`cafcda5721`, Dart **3.10.9**. The detached tag reports channel `[user-branch]`;
it is not an independently modified Flutter fork. The initially attempted
3.35.7 SDK cannot solve this app's test dependencies: its SDK-pinned `meta 1.16.0`
conflicts with `ag_ui ^0.3.0` requiring `meta ^1.17.0`.

A pinned checkout works when the attempted official archive URL returns 404:

```sh
git clone --branch 3.38.10 --depth 1 https://github.com/flutter/flutter.git \
  /home/ubuntu/toolchains/flutter-3.38.10
export PATH=/home/ubuntu/toolchains/flutter-3.38.10/bin:$PATH
flutter precache --linux
```

The generated `app/pubspec.lock` resolves `ag_ui 0.3.0`, `json_schema 5.2.2` and
`rfc_6902 0.3.1`. These are pure Dart; no platform-channel plugin, browser API or
web renderer is used. Networking and local reads use `dart:io`. The generated
GTK Linux runner is only an x64 host build target, **not** the Pi runtime.

## Runtime configuration

All configuration is read from `Platform.environment`, not `--dart-define`.

| Variable | Default | Purpose |
| --- | --- | --- |
| `AOS_BRIDGE_URL` | `http://127.0.0.1:8080` | Loopback HTTP bridge; no credentials, query, fragment or non-root path |
| `AOS_DASHBOARD_PATH` | `/var/lib/aos/dashboard.json` | Bridge-owned boot snapshot, read only |
| `AOS_DISPLAY_TOKEN_FILE` | `/etc/aos/display.token` | Runtime bearer token file |

The token file must be readable by the display service user and protected from
other users. No token is bundled, compiled or logged. It is read again for every
input request and stream reconnect, so atomic token-file replacement takes effect
without rebuilding. Missing/invalid credentials fail closed for networking;
the last-good display still renders. HTTP redirects are disabled and the native
client does not use environment proxies. Use the bridge's local display role,
not the editor/MCP role.

## State and contract boundary

At boot, the display loads the validated JSON file if available. Missing,
corrupt, oversized or unsupported-version files use the validated shared sample
with a visible fallback diagnostic. It never writes the snapshot file. Schemas,
examples and fixtures live in the Flutter asset bundle and are loaded with
`rootBundle`, so neither an installed bundle nor the Pi process working directory
needs access to repository-relative `../contracts` paths.

`contracts/` remains authoritative. `app/assets/contracts/` contains generated
copies, not independently maintained definitions. After any contract change run:

```sh
python3 app/tool/sync_contracts.py
python3 app/tool/sync_contracts.py --check
```

The same shared dashboard fixture cases run in Flutter tests. The validator uses
`json_schema` against the shared Draft 2020-12 schema, including `const`, `oneOf`,
`contains`, `minContains`, `maxContains`, list/text bounds, and format validation.
Known types are discovered from the schema rather than a second schema list.
Supplemental semantic checks reject duplicate section IDs, duplicate todo IDs
within a collection, and repeated singular known types. Multiple todo collections
are allowed and render together in the fixed todo region, keyed by section/item
IDs. Unknown section types are ignored; missing known types retain empty slots.
Exactly one status section is required by the shared schema.

Compatibility caveat: `json_schema 5.2.2` uses Dart's permissive `DateTime.parse`
for `date-time`, which normalizes impossible dates and accepts absent time zones.
The display additionally checks `updated_at` for a zoned RFC 3339 timestamp with
valid calendar/time fields. Leap seconds are not accepted. Current schemas do
not use remote references or `unevaluatedProperties`; supporting other Draft
2020-12 keywords must be established with shared fixtures before introducing
them. This is tested compatibility with the current contracts, not a claim of
complete JSON Schema conformance.

Snapshots are validated before publication. RFC 6902 patches apply to a deep
copy, then the entire resulting dashboard is validated and frozen before it is
published. Revisions must advance exactly by one for a delta. Revision `test`
operations are enforced by the patch library; the shared example without a
leading `test` also works. Dashboard JSON is capped at 128 KiB; SSE envelopes
allow 129 KiB to accommodate maximum-size state. Patches allow 128 operations.
SSE unterminated lines and SDK message buffers are
bounded. Invalid/corrupt/inapplicable events retain the last-good state, close the
connection and reconnect for a fresh snapshot, not partial patch publication.

## Typed, supervised SSE

The binding is uppercase, snapshot-first, runless AG-UI:

- The first accepted frame on each connection must be `STATE_SNAPSHOT`.
- `STATE_DELTA` applies the committed patch atomically.
- `CUSTOM` is checked against the shared `aos.*` contract; heartbeat revisions
  must agree with the accepted dashboard revision.

`DisplayController` validates event envelopes against the vendored AG-UI schema
and decodes them through the **public `ag_ui EventDecoder`**. The native stream
uses **public `SseClient.parseStream`** for WHATWG SSE framing. A loopback HTTP
integration test also exercises **`SseClient.connect`**, authentication and typed
snapshot/CUSTOM decoding directly.

The production native HTTP supervisor is necessary with `ag_ui 0.3.0`: an initial
HTTP failure closes the SDK event controller instead of retrying, while the SDK
idle timer calls disconnection with a null URL and cannot reconnect. Both paths
are demonstrated in integration tests. SDK shutdown can also await its own
`onCancel`/controller-close cycle; the SDK probe tests do not await that cycle.
The fallback replaces only connection supervision, not event types or SSE
parsing. Native HTTP has an 8-second request timeout, 40-second stream byte-idle
timeout and bounded exponential reconnect backoff. Timeout is placed upstream
of the SDK async parser so cancellation cannot wait forever for another frame.
Every reconnect resets the snapshot-first gate and re-reads the token. No run
lifecycle events, replay cursor or `Last-Event-ID` guarantee is invented.

Content freshness always derives from bridge `updated_at`. The status region
separately shows bridge connection state and dashboard age/revision. Heartbeats,
overlays and keyboard input never make old content appear fresh. A timestamp
in the future is identified as ahead of the local clock rather than reported as
freshly updated. The huge wall clock is local OS time, independently ticking.

## Native input and layout

The six fixed regions are clock, weather, agenda, todos, briefing and status,
using an Apple TV–inspired, Liquid Glass–inspired treatment: a static atmospheric
background, translucent rounded cards, frosted blur, fine highlights and lifted
remote focus. These are native Flutter surfaces, not Apple's proprietary material
or a tvOS runtime. The contract-provided accent remains in icons and feedback;
white focus outlines stay visible independently of that accent. The 1920×1080
couch-oriented design scales to the display without arbitrary section rendering.
Schema-bounded lists scroll within their regions; rows and sections retain ID
keys. Text is bounded/ellipsized, including maximum `font_scale`. Weather summary
is one line so it cannot consume the temperature's space at enlarged text sizes.
Partially visible list items can be reached by remote focus, which scrolls them
into view.

Keyboard arrows/D-pad move focus using Flutter focus traversal. Tab/Shift-Tab
also work. Enter, Space or Select activate a focused todo. Escape/back dismiss
an overlay, otherwise move focus back. Focused rows scroll into view. Overlays
are native text panels, take input focus and disable underlying traversal; they
expire by duration or accept dismissal via Enter/Escape. No HTML is interpreted.

Non-overlapping glass surfaces use one shared `BackdropGroup` and clipped
12-pixel blur filters. The overlapping notification uses an independent filter
to avoid shared-backdrop artifacts. The procedural background is isolated in a
`RepaintBoundary`; it has no assets, idle animation or network fetches. Focus lift
and highlight transitions last 180 ms and honor Flutter's reduced-motion flag,
including focused-row scrolling. Pi 4 GPU cost/frame pacing still needs hardware
measurement; host rendering is not a performance guarantee.

A todo click submits a durable `/v1/input` intent with random `client_event_id`,
`observed_revision`, section/item IDs and desired value. An HTTP receipt marks
it accepted but **does not check the box**. It stays pending until a newer
authoritative state contains the requested value (or removes the target).
Retries after uncertain/failed receipts use the same client ID and exact payload.
Rendering authoritative state never emits new input. Pending intents are
in-memory display feedback; the bridge queue owns durability after acceptance.
Overlay dismissal likewise submits validated durable input before clearing the
local overlay. Device/remote CUSTOM instructions are validated but do not pretend
to power off a display or emulate a hardware remote; hardware actuation belongs
to the appliance/input layer.

## Host verification and Pi packaging

From the repository root:

```sh
export PATH=/home/ubuntu/toolchains/flutter-3.38.10/bin:$PATH
python3 app/tool/sync_contracts.py --check
cd app
flutter pub get
dart format --output=none --set-exit-if-changed lib test
flutter analyze
flutter test --reporter expanded
flutter build linux --release
```

Verified in this environment: Dart formatting clean, `flutter analyze` clean,
Flutter tests passed, Linux x64 release built, 12 contract assets synced
and checked, and `ruff check`, `ruff format --check` and `mypy` passed for the
app contract-sync tool. Shared schemas/sample/fixture files were also confirmed
present under the built bundle's `data/flutter_assets/assets/contracts/` paths.

The host GTK build requires `clang`, `cmake`, `ninja-build`, `pkg-config`,
`libgtk-3-dev` and `libstdc++-12-dev`. These are **host build prerequisites**, not
an instruction to install X11/GTK on the Pi. The resulting x64 verification
bundle is `app/build/linux/x64/release/bundle/`. Native host visual evidence is
included on the implementation PR; offscreen Flutter widget tests exercise section rendering,
empty unknown slots, maximum font scale, D-pad/Enter and overlay Escape handling.
Loopback HTTP tests exercise SDK connect, initial-failure recovery, idle
reconnect, rotated tokens, invalid delta rehydration, SSE bounds and durable todo
receipts/retries.

Pi release packaging must use matching flutter-pi engine artifacts and the
**arm64** AOT bundle, not the GTK runner. `flutterpi_tool 0.10.1` was activated
under `/home/ubuntu/toolchains/pub-cache` for this SDK. The attempted command was:

```sh
export PUB_CACHE=/home/ubuntu/toolchains/pub-cache
export PATH=/home/ubuntu/toolchains/flutter-3.38.10/bin:$PUB_CACHE/bin:$PATH
flutterpi_tool build --arch=arm64 --cpu=pi4 --release
```

That attempt stopped at **GitHub API rate limiting** while retrieving engine
artifacts; it did not produce a verified arm64 release bundle. An authorized
build environment must fetch the matching artifacts, build the arm64 Pi 4
bundle, and deploy it beside a compatible flutter-pi binary. The intended runtime
command is `flutter-pi --release /opt/aos/app`, with the runtime environment and
token/snapshot paths above. No additional Python/Node runtime dependency is
introduced by the display. Root setup/CI should run the contract sync check and
app checks with the pinned SDK; those root files are outside this app task.

**Not yet validated on Pi/TV hardware:** actual flutter-pi engine compatibility,
DRM/KMS modesetting/rotation, 1080p HDMI couch legibility, GPU memory and frame
pacing, appliance service startup/restart, physical keyboard/remote mapping,
network drop/bridge crash on device, HDMI-CEC/power control, and multi-day
idle/reconnect stability. x64 builds, offscreen widget tests and loopback HTTP
checks do not pass these hardware gates.
