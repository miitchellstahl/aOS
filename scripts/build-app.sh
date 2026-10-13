#!/usr/bin/env bash
set -euo pipefail
# packaging verifies supplied provenance/hashes, not hardware compatibility.
apply=false
validate_only=false
tuple=''
runtime=''
sdk=''
tool=''
bundle_source=''
output=''
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
die() { printf '%s\n' "$*" >&2; exit 1; }
while (($#)); do
    case "$1" in
        --dry-run) apply=false; shift ;;
        --apply) apply=true; shift ;;
        --validate-only) validate_only=true; shift ;;
        --tuple|--runtime|--flutter-sdk|--flutterpi-tool|--bundle-source|--output)
            (($# >= 2)) || die "missing option value"
            case "$1" in
                --tuple) tuple=$2 ;; --runtime) runtime=$2 ;; --flutter-sdk) sdk=$2 ;;
                --flutterpi-tool) tool=$2 ;; --bundle-source) bundle_source=$2 ;; --output) output=$2 ;;
            esac
            shift 2 ;;
        *) die "unknown option: $1" ;;
    esac
done
[[ -n "$tuple" && -n "$runtime" ]] || die "explicit --tuple JSON and --runtime directory required; no certified bundle is supplied"
[[ -f "$tuple" && -d "$runtime" ]] || die "tuple or runtime unavailable"
python3 - "$tuple" "$runtime" <<'PY'
import hashlib, json, pathlib, re, sys
try:
    value = json.loads(pathlib.Path(sys.argv[1]).read_bytes())
    for key in ('flutter_version', 'flutterpi_tool_version'):
        if not isinstance(value[key], str) or not re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+', value[key]):
            raise ValueError
    for key in ('framework_revision', 'engine_revision', 'flutter_pi_revision'):
        if not re.fullmatch(r'[a-f0-9]{40}', value[key]):
            raise ValueError
    root = pathlib.Path(sys.argv[2])
    for filename, key in (('flutter-pi', 'runtime_sha256'), ('libflutter_engine.so', 'engine_sha256'), ('icudtl.dat', 'icu_sha256')):
        file = root / filename
        if file.is_symlink() or not file.is_file() or not re.fullmatch(r'[a-f0-9]{64}', value[key]):
            raise ValueError
        if hashlib.sha256(file.read_bytes()).hexdigest() != value[key]:
            raise ValueError
        if filename != 'icudtl.dat':
            header = file.read_bytes()[:20]
            if header[:6] != b'\x7fELF\x02\x01' or int.from_bytes(header[18:20], 'little') != 183:
                raise ValueError
    if not (root / 'flutter-pi').stat().st_mode & 0o111:
        raise ValueError
except (KeyError, TypeError, ValueError, OSError):
    sys.exit('runtime/tuple validation failed: require matching ARM64 ELF artifacts, full revisions and SHA-256 hashes')
PY
if $validate_only; then
    printf '%s\n' 'runtime hashes/ARM64 provenance checked; Pi compatibility remains untested'
    exit 0
fi
[[ -n "$sdk" && -n "$tool" && -n "$bundle_source" && -n "$output" ]] || die "build requires --flutter-sdk --flutterpi-tool --bundle-source --output"
[[ "$sdk" = /* && "$tool" = /* && "$bundle_source" = /* && "$output" = /* ]] || die "use absolute build paths"
[[ -x "$sdk/bin/flutter" && -x "$tool" ]] || die "explicit Flutter SDK or flutterpi_tool executable unavailable"
[[ ! -e "$output" && ! -L "$output" && -d "$(dirname -- "$output")" ]] || die "output must be a new path in an existing directory"
if ! $apply; then
    printf '%s\n' 'DRY RUN: verify exact Flutter/tool tuple; sync contracts; flutter pub get; build arm64/pi4 release; verify AOT/runtime hashes; copy immutable output'
    exit 0
fi
export PATH="$sdk/bin:$PATH"
python3 - "$tuple" "$sdk/bin/flutter" "$sdk/bin/dart" <<'PY'
import json, subprocess, sys
value = json.load(open(sys.argv[1]))
actual = json.loads(subprocess.check_output([sys.argv[2], '--version', '--machine']))
for expected, key in (('flutter_version', 'frameworkVersion'), ('framework_revision', 'frameworkRevision'), ('engine_revision', 'engineRevision')):
    if value[expected] != actual.get(key):
        sys.exit('Flutter SDK does not match explicit tuple')
packages = subprocess.check_output([sys.argv[3], 'pub', 'global', 'list'], text=True).splitlines()
if 'flutterpi_tool ' + value['flutterpi_tool_version'] not in packages:
    sys.exit('flutterpi_tool version does not match tuple/PUB_CACHE')
PY
python3 "$root/app/tool/sync_contracts.py" --check
(cd -- "$root/app" && "$sdk/bin/flutter" pub get --enforce-lockfile && "$tool" build --arch=arm64 --cpu=pi4 --release)
python3 - "$tuple" "$bundle_source" <<'PY'
import hashlib, json, pathlib, sys
try:
    value = json.load(open(sys.argv[1]))
    root = pathlib.Path(sys.argv[2])
    header = (root / 'app.so').read_bytes()[:20]
    if header[:6] != b'\x7fELF\x02\x01' or int.from_bytes(header[18:20], 'little') != 183:
        raise ValueError
    if not (root / 'assets/contracts/dashboard.v1.schema.json').is_file():
        raise ValueError
    for filename, key in (('libflutter_engine.so', 'engine_sha256'), ('icudtl.dat', 'icu_sha256')):
        if hashlib.sha256((root / filename).read_bytes()).hexdigest() != value[key]:
            raise ValueError
    if any(file.is_symlink() for file in root.rglob('*')):
        raise ValueError
except (KeyError, TypeError, ValueError, OSError):
    sys.exit('bundle unavailable, not ARM64 AOT, or engine/ICU differs from tuple')
PY
mkdir -- "$output"
cp -a -- "$bundle_source" "$output/app"
python3 - "$tuple" "$output/bundle.json" <<'PY'
import json, pathlib, sys
pathlib.Path(sys.argv[2]).write_text(json.dumps({'hardware_validated': False, 'tuple': json.load(open(sys.argv[1]))}, indent=2) + '\n')
PY
printf '%s\n' 'bundle packaged; NOT Pi/TV certified; complete docs/hardware-validation.md'
