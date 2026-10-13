# aOS display

The fixed Flutter dashboard for aOS (agent OS). The bridge owns state; this
read-only display consumes snapshots/deltas and emits durable input intents.

See [display setup and verification](../docs/display.md) for the supported host
toolchain, runtime configuration, tests, and outstanding flutter-pi hardware gates.
Run `make app-check` or `make app-build` from the repository root.
