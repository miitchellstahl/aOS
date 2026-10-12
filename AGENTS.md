# aOS

Pi 4 8GB, Raspberry Pi OS Lite 64-bit, Flutter via flutter-pi, no X11/Wayland.
No hosted backend/database, LLM, calendar credentials, or scheduling on the Pi.
The harness owns intelligence and integrations; the bridge is the sole state writer.

`contracts/` owns the wire schemas and shared fixtures. Do not duplicate contracts.
The runless, snapshot-first feed uses AG-UI 1.0 event shapes, not full transport
conformance. Fixed section renderers do not claim A2UI compatibility.

No whiteboard, drawing page, uploads, image assets, or LAN-facing application API.
Keep control listeners on loopback. Expose only MCP through an authenticated tunnel.
Muse must not have sudo, broad device access, or direct dashboard write permission.

Run `make check` and `make app-check`. Do not call hardware gates passed without
Pi/TV evidence. Use Conventional Commits, feature branches, and PRs; do not merge
unless instructed. Do not commit credentials or unrelated working files.
