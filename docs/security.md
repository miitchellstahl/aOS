# Security boundaries

The Pi is a local appliance, not a trusted agent backend. It runs no LLM,
calendar/account integrations, scheduler, database or cloud application service.
The remote harness owns intelligence and approved triggers. Schema validity does
not prove user consent; bearer-token possession authorizes its scoped actions.

## Network and state ownership

Bridge HTTP listens on IPv4 loopback 8080; FastMCP Streamable HTTP on loopback
8090 `/mcp`. Keep all bridge routes local, including ingest and SSE. Only a
separately configured authenticated TLS tunnel may expose `/mcp`; never tunnel
all localhost routes or open router ports. Exact Host/Origin checks and bearer
validation remain mandatory behind the tunnel. MCP proxies authenticated bridge
operations and never writes state itself. The feed uses uppercase AG-UI shapes
but is runless/snapshot-first, not full AG-UI transport or an A2UI renderer.

`aos-bridge` alone writes `/var/lib/aos` (0750, bridge:aos-state). Dashboard
snapshots are 0640 for the display's reader group; queue, lock and internal store
files are private to the bridge. Readers have no directory write permission.
The display's semantic input is authenticated and durably queued before success;
only the agent's authorized patch produces authoritative todo state. Muse's
inbox is a separate 2770 bridge:aos-inbox directory, not the dashboard directory.
Commands require a state precondition and complete-file atomic rename; bridge
intake rejects unsafe/oversized/symlinked/malformed files and owns receipts.
Untrusted dashboard labels are plain text, not commands or executable templates.

## Locally generated credentials

Provision generates independent cryptographic tokens in Python, never shell
variables/stdout/command arguments, and never overwrites existing files. Tokens
are root-owned 0640 regular files, one hard link, under root-owned `/etc/aos`
(mode 0711: traversal only, not a token directory listing). Environments contain
paths, not values. The following groups have precisely these members:

| Token / group | Readers | Capability |
| --- | --- | --- |
| bridge.token / aos-bridge-auth | bridge, MCP | general bridge API, including device controls |
| display.token / aos-display-auth | bridge, display | schema/state/status/SSE, semantic input/dismissal |
| muse.token / aos-muse-auth | bridge, Muse | schema/state/queue, ingest and ack, no device controls |
| mcp.token / aos-mcp-auth | MCP | external FastMCP bearer, distinct from local tokens |

Bridge must read all local tokens to authenticate; that does not grant other
accounts general credentials. Service env files are root-owned 0640 in their
own account group. Muse cannot read bridge/MCP/CEC/display env files or tokens,
or modify any service configuration. Its own env contains only non-secret
paths and its assigned side-chat ID. No real vendor Muse SDK token is generated
or installed by provision. SDK storage belongs in the Muse private 0700 account
home, never alongside screen state or broad MCP credentials.

Reads reopen token files per request for rotation/revocation. Rotate with a
reviewed root-only program: generate a new same-directory file, fchown to the
original token group, fchmod 0640, flush/fsync, replace atomically and fsync the
directory. Never use `echo TOKEN`, `curl -H 'Bearer TOKEN'`, `set -x`, env dumps,
command history or committed credentials. Preserve scopes/groups. Empty/remove
credentials to fail closed, not to enable anonymous access. Rotate the external
MCP token independently and coordinate the harness's secure secret store.

## Device isolation

Only CEC owns `/dev/uinput` and `/dev/cec0` (udev owner aos-cec, mode 0600).
Its virtual keyboard advertises only arrows, Enter, Escape. A display-only udev
rule matches the virtual keyboard name; there is no broad input-group membership.
The display has selected DRM/render and tty1 permissions, not CEC/uinput.
Provision writes exact selected card/render rules and a matching systemd device
allowlist. Dynamic event numbers require `DeviceAllow=char-input r`, but Unix
permissions still restrict readable nodes to the named CEC virtual device.
Physical keyboards are not granted by these rules. Review any additional input
permission on the actual Pi rather than adding the generic input/video group.

CEC exposes one group-protected Unix socket, never TCP. It also checks the
bridge UID with `SO_PEERCRED`, accepts only three literal actions and bounded
newline JSON, and executes only fixed libCEC commands without a shell. It ignores
outgoing key traffic, suppresses activation repeats and releases stuck keys.
A physical command may occur even if confirmation times out; no software test
or health endpoint is proof of TV state. Active-source acknowledgment means an
announcement was observed, not successful HDMI selection. See adapters.md.

## Service/deployment hardening

All units use dedicated non-root non-login users, empty Linux capability sets,
NoNewPrivileges, strict system protection, private temporary files, protected
kernel/control-group settings, bounded restart policy/tasks/memory, and native
syscall architecture. Bridge/MCP/Muse have private devices, hence no uinput,
CEC or display device access. Bridge/MCP/display restrict network destinations
to localhost. CEC permits AF_UNIX only. Muse allows network access solely because
its separately reviewed SDK may need it; no local integration credentials are
provided. There is no sudo/wheel/input/video/render/tty/disk membership. Flutter
must retain GPU/input access and executable-memory support; hardening is not an
excuse to silently hide required DRM devices or run the display as root.

Release files are root-owned and non-writable by service users. Frozen Python
lock, runtime SHA-256s, ARM64 ELF checks and an explicit tuple are packaging
checks, not a Pi compatibility certificate. Apply scripts guard Pi4/aarch64/root
and explicit global-change authorization. Version directories are never reused;
atomic links/explicit rollback do not overwrite tokens or state. Symlinked
state/config roots are rejected. Provision installs definitions but never starts
services or takes tty1. Enable display from SSH only after approving its getty
conflict. No destructive cleanup, desktop removal or WiFi setup is performed.

## Limits and remaining risks

At-least-once queue handling requires downstream deduplication; exactly-once is
not promised. Muse acknowledgment transfers responsibility after SDK exit zero
and durable local handoff journaling, not after agent state processing. A crash
between SDK acceptance and journal fsync may duplicate. Only one active consumer
(MCP or Muse) is supported; local flock does not police external MCP pollers.

Bridge bounds bodies/state/patches/stream queues/durable queue and inbox receipts;
CEC bounds frames/output/deadlines/connections; companion bounds batches,
responses and journal size. Incomplete producer `.tmp` files and other private SDK
files can still consume storage: provision does not create filesystem quotas.
Budget storage/journald retention and monitor disk pressure; fail-closed write
failures are preferable to fabricated commits. Root/physical access remains an
administrative capability; sysusers/no-sudo membership is not a sandbox against
an independently misconfigured sudoers or malicious root. Inspect OS sudoers,
installed packages, outbound SDK behavior and device modes on the real image.
Do not expose the Pi API to arbitrary LAN users or treat local malware as benign.

Hardware/runtime/real SDK gates remain untested. [hardware-validation.md](hardware-validation.md)
records acceptance evidence still needed; [setup.md](setup.md) contains actual
script invocation/rotation seams without embedded secrets.
