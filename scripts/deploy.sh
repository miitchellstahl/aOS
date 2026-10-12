#!/usr/bin/env bash
set -euo pipefail
apply=false
restart=false
rollback=false
source_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
bundle=''
runtime=''
tuple=''
version=''
uv=''
base=/opt/aos
die() { printf '%s\n' "$*" >&2; exit 1; }
while (($#)); do
    case "$1" in
        --dry-run) apply=false; shift ;; --apply) apply=true; shift ;;
        --restart) restart=true; shift ;; --rollback) rollback=true; shift ;;
        --source|--bundle|--runtime|--tuple|--version|--uv)
            (($# >= 2)) || die 'missing option value'
            case "$1" in --source) source_root=$2 ;; --bundle) bundle=$2 ;; --runtime) runtime=$2 ;;
                --tuple) tuple=$2 ;; --version) version=$2 ;; --uv) uv=$2 ;; esac
            shift 2 ;;
        *) die "unknown option: $1" ;;
    esac
done
if $rollback; then
    [[ -L "$base/previous" ]] || die 'no previous release available'
    target=$(readlink -f -- "$base/previous")
    [[ "$target" == "$base/releases/"* && -d "$target" && ! -f "$target/.partial" ]] || die 'invalid previous release'
    bash "$source_root/scripts/build-app.sh" --validate-only --tuple "$target/tuple.json" --runtime "$target/runtime"
else
    [[ "$version" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$ && "$version" != '.' && "$version" != '..' ]] || die 'explicit safe --version required'
    [[ "$source_root" = /* && "$bundle" = /* && "$runtime" = /* && "$tuple" = /* && "$uv" = /* ]] || die 'all input paths must be absolute'
    [[ -f "$source_root/pyproject.toml" && -f "$source_root/uv.lock" && -x "$uv" ]] || die 'source lock and reviewed uv executable required'
    target="$base/releases/$version"
    [[ ! -e "$target" && ! -L "$target" ]] || die 'version already exists; never overwrite immutable releases'
    bash "$source_root/scripts/build-app.sh" --validate-only --tuple "$tuple" --runtime "$runtime"
    python3 - "$bundle" "$tuple" <<'PY'
import hashlib, json, pathlib, sys
try:
    root = pathlib.Path(sys.argv[1])
    value = json.load(open(sys.argv[2]))
    if json.loads((root / 'bundle.json').read_bytes()) != {'hardware_validated': False, 'tuple': value}:
        raise ValueError
    if any(file.is_symlink() for file in root.rglob('*')):
        raise ValueError
    app = root / 'app'
    header = (app / 'app.so').read_bytes()[:20]
    if header[:6] != b'\x7fELF\x02\x01' or int.from_bytes(header[18:20], 'little') != 183:
        raise ValueError
    if not (app / 'assets/contracts/dashboard.v1.schema.json').is_file():
        raise ValueError
    for file, key in (('libflutter_engine.so', 'engine_sha256'), ('icudtl.dat', 'icu_sha256')):
        if hashlib.sha256((app / file).read_bytes()).hexdigest() != value[key]:
            raise ValueError
except (OSError, TypeError, ValueError, KeyError):
    sys.exit('bundle is missing, differs from runtime tuple, or is not ARM64 AOT')
PY
fi
if ! $apply; then
    printf '%s\n' 'DRY RUN: Pi4/root guard; exclusive deploy lock; immutable version; frozen uv deps; atomic previous/current symlinks; no credentials/state modified'
    $restart && printf '%s\n' 'DRY RUN: explicit restart requested; would stop running Muse, display, MCP, bridge, CEC and restart only previously active services'
    exit 0
fi
[[ $(id -u) == 0 && $(uname -m) == aarch64 ]] || die 'apply requires root on aarch64 Pi'
if [[ ! -r /proc/device-tree/model ]] || ! grep -aq 'Raspberry Pi 4' /proc/device-tree/model; then
    die 'only Raspberry Pi 4 supported'
fi
[[ -d "$base/releases" && ! -L "$base/releases" ]] || die 'run provision first; unsafe release root'
exec 9> "$base/.deploy.lock"
flock -n 9 || die 'another deployment is active'
umask 022
if [[ -L "$base/current" ]]; then
    current=$(readlink -f -- "$base/current")
    [[ "$current" == "$base/releases/"* && -d "$current" ]] || die 'unsafe current release link'
elif [[ -e "$base/current" ]]; then
    die 'current must be a release symlink, not a directory'
fi
if ! $restart; then
    for component in muse display mcp bridge cec; do
        if systemctl is-active --quiet "aos-$component.service"; then
            die 'running services require explicit --restart before switching releases'
        fi
    done
fi
if ! $rollback; then
    # install at final path so venv shebangs remain valid; marker prevents partial activation.
    mkdir -m 0755 -- "$target"
    touch -- "$target/.partial"
    for directory in bridge mcp adapters contracts; do
        cp -a -- "$source_root/$directory" "$target/$directory"
    done
    cp -- "$source_root/pyproject.toml" "$source_root/uv.lock" "$target/"
    cp -a -- "$bundle/app" "$target/app"
    mkdir -- "$target/runtime"
    for artifact in flutter-pi libflutter_engine.so icudtl.dat; do
        cp -- "$runtime/$artifact" "$target/runtime/$artifact"
    done
    chmod 0644 "$target/runtime/libflutter_engine.so" "$target/runtime/icudtl.dat"
    chmod 0755 "$target/runtime/flutter-pi"
    cp -- "$tuple" "$target/tuple.json"
    cp -- "$bundle/bundle.json" "$target/bundle.json"
    UV_PYTHON_DOWNLOADS=never "$uv" sync --project "$target" --frozen --no-dev --extra cec --no-editable --python /usr/bin/python3
    "$target/.venv/bin/python" -c 'import aos_bridge, aos_mcp, aos_cec, aos_muse, evdev'
    chown -R root:root -- "$target"
    chmod -R go-w -- "$target"
    rm -- "$target/.partial"
fi
[[ -x "$target/.venv/bin/python" && -f "$target/app/app.so" ]] || die 'release incomplete'
active=()
if $restart; then
    for component in muse display mcp bridge cec; do
        if systemctl is-active --quiet "aos-$component.service"; then
            active+=("aos-$component.service")
            systemctl stop "aos-$component.service"
        fi
    done
fi
# replacing same-directory symlinks with rename is atomic; previous is a complete release.
if [[ -L "$base/current" ]]; then
    current=$(readlink -f -- "$base/current")
    [[ "$current" == "$base/releases/"* && -d "$current" ]] || die 'unsafe current release link'
    ln -s -- "$current" "$base/.previous.$$"
    mv -Tf -- "$base/.previous.$$" "$base/previous"
elif [[ -e "$base/current" ]]; then
    die 'current must be a release symlink, not a directory'
fi
ln -s -- "$target" "$base/.current.$$"
mv -Tf -- "$base/.current.$$" "$base/current"
if $restart && ((${#active[@]})); then
    # one state owner: all old consumers are stopped before the release pointer moves.
    for component in cec bridge mcp display muse; do
        for unit in "${active[@]}"; do
            if [[ "$unit" == "aos-$component.service" ]]; then
                systemctl start "$unit"
            fi
        done
    done
fi
printf '%s\n' 'release switched; tokens/state untouched; run doctor and the pending hardware checklist (rollback remains explicit)'
