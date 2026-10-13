# Appliance setup

Target: Raspberry Pi 4 8GB, Raspberry Pi OS Lite **64-bit**, Flutter/flutter-pi
on full DRM/KMS, no X11/Wayland. These commands are operator instructions, **not
an installation performed or a hardware-certified tuple**. See
[hardware-validation.md](hardware-validation.md) before calling the appliance
ready. Provision/deploy refuse apply on this x64 development VM.

## 1. Prepare reviewed artifacts on a Linux build host

Python dependencies/entrypoints are root `pyproject.toml` + `uv.lock`. Use the
existing development venv for checks; do not install the optional external Muse
SDK to run tests. Obtain a reviewed, pinned `uv` executable separately; deployment
uses `uv sync --frozen --no-dev --extra cec --no-editable --python /usr/bin/python3`
and disables automatic Python downloads. The Pi must satisfy the root Python
requirement. A release uses its own venv, not the developer's editable venv.

Host Flutter tests/build used Flutter 3.38.10 and flutterpi_tool 0.10.1; the x64
GTK bundle is **not** a Pi artifact. ARM64 artifact retrieval previously failed
with GitHub rate limiting. Fetch/build a compatible ARM64 flutter-pi runtime,
matching release engine and ICU data in a reviewed environment. Nothing in
provision downloads or guesses these artifacts. Follow upstream licenses and
record source/artifact provenance; do not relabel x64 or incompatible engines.

Prepare `/srv/aos-runtime/{flutter-pi,libflutter_engine.so,icudtl.dat}` and an
explicit tuple JSON. Required fields:

| Field | Meaning |
| --- | --- |
| flutter_version | exact SDK version (host verified: 3.38.10) |
| framework_revision | full 40-character SDK Git revision |
| engine_revision | full 40-character SDK engine revision |
| flutterpi_tool_version | exact activated version (host used: 0.10.1) |
| flutter_pi_revision | full reviewed flutter-pi source commit |
| runtime_sha256 | actual flutter-pi ARM64 binary SHA-256 |
| engine_sha256 | actual release libflutter_engine.so SHA-256 |
| icu_sha256 | actual icudtl.dat SHA-256 |

For example, after reviewing/building the runtime and assigning its full source
commit (the value below is an operator variable, not a known certified revision):

```sh
export FLUTTER_SDK=/absolute/path/to/flutter-3.38.10
export PUB_CACHE=/absolute/path/to/pub-cache
export FLUTTER_PI_REVISION=FULL_REVIEWED_40_CHARACTER_COMMIT
export RUNTIME=/srv/aos-runtime
export TUPLE=/srv/aos-tuple.json
export PATH="$FLUTTER_SDK/bin:$PUB_CACHE/bin:$PATH"
python3 - <<'PY'
import hashlib, json, os, pathlib, subprocess
sdk = json.loads(subprocess.check_output([os.environ['FLUTTER_SDK'] + '/bin/flutter', '--version', '--machine']))
root = pathlib.Path(os.environ['RUNTIME'])
value = {'flutter_version': sdk['frameworkVersion'], 'framework_revision': sdk['frameworkRevision'],
         'engine_revision': sdk['engineRevision'], 'flutterpi_tool_version': '0.10.1',
         'flutter_pi_revision': os.environ['FLUTTER_PI_REVISION']}
for name, field in [('flutter-pi', 'runtime_sha256'), ('libflutter_engine.so', 'engine_sha256'), ('icudtl.dat', 'icu_sha256')]:
    value[field] = hashlib.sha256((root / name).read_bytes()).hexdigest()
pathlib.Path(os.environ['TUPLE']).write_text(json.dumps(value, indent=2) + '\n')
PY
```

Hashes/revisions only establish the supplied inputs, not compatibility. Build
scripts require ARM64 ELF runtime/engine, exact SDK and tool tuple, an ARM64 AOT
`app.so`, synced contracts and matching bundled engine/ICU hashes. Bundles always
say `hardware_validated:false`. The SDK/tool must already be installed; scripts
never install/update SDKs. `PUB_CACHE` must contain the specified activated tool.

```sh
cd /absolute/path/to/aOS
bash scripts/build-app.sh --dry-run --tuple "$TUPLE" --runtime "$RUNTIME" \
  --flutter-sdk "$FLUTTER_SDK" --flutterpi-tool "$PUB_CACHE/bin/flutterpi_tool" \
  --bundle-source /absolute/path/to/aOS/app/build/flutter-pi/ARM64_TARGET_DIRECTORY \
  --output /srv/aos-bundle-v1
# after reviewing the plan, repeat with --apply instead of --dry-run
```

The output directory must not exist. Choose the actual tool output directory
under `app/build/flutter-pi/` (target naming is tool-version dependent), not the
GTK runner directory. `build-app.sh` uses
`flutterpi_tool build --arch=arm64 --cpu=pi4 --release`, then packages `app/` and
`bundle.json`. Missing/incorrect runtime, tuple, output or downloaded engine
fails clearly; no synthetic bundle is produced.

## 2. Base Pi preparation

Flash a reviewed Pi OS Lite 64-bit image; record its version/checksum. Configure
SSH/network through the OS's secure interactive tooling. No WiFi credentials,
calendar/account secrets or real Muse SDK token belong in this repository.
Use that image's current full-KMS instructions; do not copy obsolete Fake-KMS
GPU settings. Verify selected `/dev/dri/cardN`, `renderDN` and `/dev/cec0` on the
actual image; card1/renderD128 below are defaults, not a proven enumeration.

Copy the complete source checkout/locked dependencies, reviewed `uv`, tuple,
runtime and ARM64 bundle to the Pi. Provision is dry-run by default:

```sh
bash scripts/provision.sh --dry-run --tuple /srv/aos-tuple.json \
  --runtime /srv/aos-runtime --drm-device /dev/dri/card1 \
  --render-device /dev/dri/renderD128
sudo bash scripts/provision.sh --apply --allow-system-changes \
  --tuple /srv/aos-tuple.json --runtime /srv/aos-runtime \
  --drm-device /dev/dri/card1 --render-device /dev/dri/renderD128
```

Apply requires root, aarch64, Pi4 model and Debian/Raspberry Pi OS; it refuses a
known graphical stack instead of removing it. It installs only required Python,
libCEC, Mesa/DRM/input runtime packages and evdev build prerequisites, creates
scoped non-login accounts, state/inbox directories, root-owned token files,
non-secret env templates, hardened units and selected device rules. It configures
uinput loading on reboot but does not load drivers, start/enable services, take
over tty1, install a tunnel/Muse SDK, modify WiFi, or edit boot firmware settings.
APT's OS packages follow the chosen image repositories; Python uses the frozen
lock. Re-running preserves all existing tokens/env files/state. Unsafe existing
files fail for manual review. Root sysusers/udev/unit definitions are reinstalled
from the reviewed source; coordinate that global change during maintenance.

Locally generated tokens are never printed or placed into shell variables.
`/etc/aos/{bridge,display,muse,mcp}.token` are separate capability secrets; Muse's
local token is **not** an SDK pairing token. Env files refer to paths only. See
[security.md](security.md) for permissions and rotation.

## 3. Deploy an immutable version

```sh
bash scripts/deploy.sh --dry-run --version v0.1.0 \
  --source /srv/aOS --bundle /srv/aos-bundle-v1 \
  --tuple /srv/aos-tuple.json --runtime /srv/aos-runtime --uv /usr/local/bin/uv
sudo bash scripts/deploy.sh --apply --version v0.1.0 \
  --source /srv/aOS --bundle /srv/aos-bundle-v1 \
  --tuple /srv/aos-tuple.json --runtime /srv/aos-runtime --uv /usr/local/bin/uv
sudo reboot
```

A release is `/opt/aos/releases/VERSION` with Python sources, its non-editable
venv, contracts, native AOT bundle, runtime and tuple. Installation is at the
final path so venv shebangs never refer to a renamed staging directory. A
`.partial` marker blocks activation until frozen dependency installation/import
checks finish; failed versions are not reused/overwritten. `current`/`previous`
symlinks switch by same-directory atomic rename under a deploy flock. Existing
`/var/lib/aos`, Muse/inbox state and `/etc/aos` secrets are never copied/replaced.

For upgrades add `--restart`: only previously active appliance services are
stopped and restarted, in consumer-before-writer shutdown and writer-before-
consumer startup order. Without that explicit flag, running services prevent
switching. No automatic health-based rollback is claimed: start failures leave
the new pointer for diagnosis and **explicit** rollback. State format migration
compatibility must be reviewed before downgrading code.

```sh
bash scripts/deploy.sh --dry-run --rollback --restart
sudo bash scripts/deploy.sh --apply --rollback --restart
```

Rollback validates the complete previous runtime/release and swaps pointers; it
does not roll back dashboard/queue files or tokens. Interrupted staging never
changes `current`; an interruption between `previous` and `current` updates
still leaves complete releases, though an operator may need to choose the
intended version. Do not prune the previous/current versions; no destructive
cleanup is performed by these scripts.

## 4. Explicitly start services on the Pi

Use an SSH/second-console session: starting the display **conflicts with tty1
getty** and takes that console. Provision does not authorize that takeover.
First inspect account/device permissions and env paths, then:

```sh
sudo systemctl enable --now aos-cec.service aos-bridge.service aos-mcp.service
# only after explicitly approving tty1 takeover and verifying the runtime:
sudo systemctl enable --now aos-display.service
bash scripts/doctor.sh --dry-run
sudo bash scripts/doctor.sh --apply
```

Display first paint uses its validated boot file/bundled fallback without waiting
for bridge networking. CEC absence does not prevent bridge startup; device API
reports failure. Doctor is read-only, redacts credentials, fails missing runtime,
credentials/devices/services and checks the loopback bridge. Its OK lines prove
permissions/service presence, **not** visible output or successful TV commands.
Keep the device deadline hierarchy: CEC 18s, bridge `AOS_CEC_TIMEOUT_S=22`, MCP
`AOS_MCP_TIMEOUT_S=26`; remote clients need a tool deadline of at least 30s.
Provision preserves existing env files: add/review these values when upgrading.
See [adapters.md](adapters.md) before hardware device acceptance. Use
`journalctl -u aos-bridge -u aos-cec -u aos-display` for bounded operational logs; never dump env files/tokens, add bearer tokens to command lines,
or post secret logs. Configure OS journald retention for the selected storage
budget; no custom unbounded file logger is installed.

MCP stays loopback at `127.0.0.1:8090/mcp`; bridge stays `127.0.0.1:8080`. A separate
reviewed authenticated TLS tunnel may expose **only `/mcp`**, preserving external
MCP bearer validation. Configure exact allowed tunnel Host in `/etc/aos/mcp.env`;
no broad-origin bypass or LAN listener. Tunnel/account setup is operator-owned.

## 5. Optional Muse, only after pairing

By default there is no `/etc/aos/muse.enabled` and no vendor SDK. An operator must
review/pin/install the SDK outside appliance state under `/opt/muse`, arrange its
secure credentials in the Muse account's private SDK storage, verify the CLI
binding and paired side-chat ID, and edit the root-owned Muse env file. Do not
put a real SDK token in shell history, env templates or the bridge-token file.

Stop MCP-side queue polling before enabling this single queue consumer. After
reviewing the transfer-of-responsibility acknowledgment policy in adapters.md:

```sh
sudo touch /etc/aos/muse.enabled
sudo systemctl enable --now aos-muse.service
sudo bash scripts/doctor.sh --apply --require-muse
```

The Muse account has no sudo/device/general capability. If SDK installation,
pairing or CLI receipt durability cannot be established, keep the companion
disabled and consume the queue through the reference MCP client instead.
