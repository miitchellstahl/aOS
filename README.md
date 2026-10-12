# aOS (agent OS)

A native display and control surface for agent harnesses, on the biggest screen
in the house. Raspberry Pi 4 → Raspberry Pi OS Lite → flutter-pi → Flutter.
No desktop, hosted application backend, database, or harness account credentials on the Pi.

The harness owns intelligence, integrations, approved schedules, and compute.
aOS owns validated dashboard state, a durable input queue, local display delivery,
and narrowly scoped device control.

## Current implementation

- Six fixed Flutter sections: clock, agenda, todos, weather, briefing, status.
- Revision-checked dashboard replacement/JSON Patch, atomic file persistence,
  last-good recovery, and snapshot-first SSE.
- Durable TV input intents, at-least-once polling, idempotent acknowledgments.
- Scoped bridge/display/Muse credentials; a separate authenticated FastMCP entry.
- Guarded Pi provisioning/deployment, isolated CEC and optional Muse companions.

The stream uses **AG-UI 1.0 event shapes**. It is an appliance binding, not a fully
conformant run-based AG-UI transport, and does not implement an A2UI renderer.
No drawing page, uploads, or LAN application listener.

Host checks are not Pi certification. See
[hardware gates](docs/hardware-validation.md) and [display verification](docs/display.md)
for the exact outstanding runtime/TV work. Do not deploy the x64 GTK host bundle to
the Pi. ARM64/flutter-pi engine compatibility must be established on the device.

## Development on Linux

Python 3.12, uv 0.9.5, Flutter 3.38.10 (Dart 3.10.9). Flutter's generated Linux
runner is for development only; GTK/X11/Wayland are not required on the Pi.

```sh
python3 -m pip install uv==0.9.5
uv sync --frozen
make check
make app-check
make app-build
```

Install Flutter from its official tagged source or archive and add its
`bin/` to PATH. Linux host builds require clang, cmake, ninja-build, pkg-config,
libgtk-3-dev, and libstdc++-12-dev. See [display setup](docs/display.md).

For an isolated local bridge/MCP:

```sh
./scripts/dev.sh prepare
./scripts/dev.sh bridge       # terminal 1: loopback 8080
./scripts/dev.sh mcp          # terminal 2: loopback 8090/mcp
./scripts/dev.sh display      # terminal 3: native host app
```

Credentials are generated locally under ignored `.state/credentials/`, never
printed or compiled into the app. No token means no authenticated network path;
the display can still show its last valid file snapshot.

## Architecture

```text
Harness → authenticated MCP (/mcp only through a TLS tunnel)
                        ↓ authenticated loopback
Muse inbox/ingest → Python bridge → atomic dashboard.json
                        ↓ AG-UI-shaped SSE          ↓ boot read
                        Flutter fixed dashboard ←──┘
                             ↓ durable UI intent
                        Bridge event queue → harness

Bridge device commands → restricted Unix socket → CEC service → TV
TV remote → CEC service → limited virtual keyboard → Flutter focus
```

Only the bridge commits state. MCP is a thin client, not a second state owner.
Only the CEC account needs virtual-keyboard access. Optional Muse runs without sudo
or dashboard/device ownership. Expose only MCP—not the bridge—through a tunnel.

## Documentation

- [Contracts](docs/schema.md): routes, schemas, revisions, tools, and input semantics.
- [Appliance setup](docs/setup.md): Pi prerequisites, provision/build/deploy/rollback.
- [Security](docs/security.md): credentials, service boundaries, network exposure.
- [Harness connection](docs/harnesses.md): MCP reference client and verified matrix.
- [Adapters](docs/adapters.md): CEC protocol and optional Muse integration.
- [Hardware checklist](docs/hardware-validation.md): evidence needed before release.

Apache-2.0 project code; dependency notices in [THIRD_PARTY_NOTICES](THIRD_PARTY_NOTICES).
