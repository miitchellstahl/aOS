#!/usr/bin/env bash
set -euo pipefail
apply=false
require_muse=false
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
while (($#)); do
    case "$1" in
        --dry-run) apply=false ;; --apply) apply=true ;; --require-muse) require_muse=true ;;
        *) printf '%s\n' "unknown option: $1" >&2; exit 1 ;;
    esac
    shift
done
if ! $apply; then
    printf '%s\n' 'DRY RUN: read-only Pi/runtime/token ownership/account/device/service/loopback health checks; no secret values or environment dumps'
    exit 0
fi
python3 - "$require_muse" <<'PY'
import grp, http.client, json, os, pathlib, platform, pwd, stat, subprocess, sys
errors = []
def check(ok, name):
    print(('OK: ' if ok else 'FAIL: ') + name)
    if not ok:
        errors.append(name)
check(os.getuid() == 0, 'run doctor as root to inspect scoped credentials without exposing them')
model = pathlib.Path('/proc/device-tree/model')
check(platform.machine() == 'aarch64' and model.exists() and b'Raspberry Pi 4' in model.read_bytes(), 'Pi4 aarch64 platform')
accounts = ('aos-bridge', 'aos-mcp', 'aos-display', 'aos-cec', 'aos-muse')
for account in accounts:
    try:
        user = pwd.getpwnam(account)
        groups = {grp.getgrgid(g).gr_name for g in os.getgrouplist(account, user.pw_gid)}
        check(not groups.intersection({'sudo', 'wheel', 'input', 'video', 'render', 'tty', 'disk'}) and user.pw_uid != 0, account + ' has no broad privileged groups')
    except KeyError:
        check(False, account + ' account exists')
for name, group in (('bridge', 'aos-bridge-auth'), ('display', 'aos-display-auth'), ('muse', 'aos-muse-auth'), ('mcp', 'aos-mcp-auth')):
    path = pathlib.Path('/etc/aos/' + name + '.token')
    try:
        metadata = path.lstat()
        with path.open('rb') as source:
            value = source.read(4097).strip()
        ok = stat.S_ISREG(metadata.st_mode) and metadata.st_nlink == 1 and metadata.st_uid == 0 and metadata.st_gid == grp.getgrnam(group).gr_gid and stat.S_IMODE(metadata.st_mode) == 0o640 and 32 <= len(value) <= 4096 and not any(chr(c).isspace() for c in value)
    except (OSError, KeyError):
        ok = False
    check(ok, name + ' protected credential exists (value redacted)')
state = pathlib.Path('/var/lib/aos')
try:
    metadata = state.stat()
    check(not state.is_symlink() and metadata.st_uid == pwd.getpwnam('aos-bridge').pw_uid and metadata.st_gid == grp.getgrnam('aos-state').gr_gid and stat.S_IMODE(metadata.st_mode) == 0o750, 'bridge-only state directory with display reader group')
    snapshot = state / 'dashboard.json'
    metadata = snapshot.lstat()
    check(stat.S_ISREG(metadata.st_mode) and metadata.st_uid == pwd.getpwnam('aos-bridge').pw_uid and stat.S_IMODE(metadata.st_mode) == 0o640, 'read-only display boot snapshot')
except (OSError, KeyError):
    check(False, 'state and initial snapshot available after bridge startup')
current = pathlib.Path('/opt/aos/current')
try:
    resolved = current.resolve(strict=True)
    check(current.is_symlink() and resolved.parent == pathlib.Path('/opt/aos/releases') and not (resolved / '.partial').exists(), 'complete atomic current release')
except OSError:
    check(False, 'current release available')
for file in (current / 'runtime/flutter-pi', current / 'runtime/libflutter_engine.so', current / 'app/app.so', current / '.venv/bin/python'):
    check(file.is_file(), 'runtime/release file: ' + file.name)
for component in ('bridge', 'mcp', 'display', 'cec'):
    result = subprocess.run(['systemctl', 'is-active', '--quiet', 'aos-' + component + '.service'], check=False)
    check(result.returncode == 0, component + ' service active (not physical action confirmation)')
for device in ('/dev/uinput', '/dev/cec0', '/dev/tty1'):
    try:
        metadata = pathlib.Path(device).stat()
        check(stat.S_ISCHR(metadata.st_mode), 'character device exists: ' + device)
        if device in ('/dev/uinput', '/dev/cec0'):
            check(metadata.st_uid == pwd.getpwnam('aos-cec').pw_uid and stat.S_IMODE(metadata.st_mode) == 0o600, 'CEC-only device permissions: ' + device)
    except (OSError, KeyError):
        check(False, 'device available: ' + device)
check(any(pathlib.Path('/dev/dri').glob('card*')) and any(pathlib.Path('/dev/dri').glob('renderD*')), 'DRM and render devices exist; modesetting still requires hardware test')
virtual = []
for name in pathlib.Path('/sys/class/input').glob('event*/device/name'):
    try:
        if name.read_text().strip() == 'aOS CEC navigation':
            virtual.append(name)
    except OSError:
        pass
check(bool(virtual), 'restricted CEC virtual keyboard exists')
try:
    metadata = pathlib.Path('/run/aos/cec.sock').lstat()
    check(stat.S_ISSOCK(metadata.st_mode) and metadata.st_uid == pwd.getpwnam('aos-cec').pw_uid and metadata.st_gid == grp.getgrnam('aos-control').gr_gid and stat.S_IMODE(metadata.st_mode) == 0o660, 'CEC control socket ownership/mode')
except (OSError, KeyError):
    check(False, 'CEC control socket exists')
try:
    token = pathlib.Path('/etc/aos/bridge.token').read_text().strip()
    connection = http.client.HTTPConnection('127.0.0.1', 8080, timeout=3)
    connection.request('GET', '/v1/status', headers={'Authorization': 'Bearer ' + token})
    response = connection.getresponse()
    data = response.read(131073)
    check(response.status == 200 and len(data) <= 131072, 'authenticated loopback bridge status')
    if response.status == 200 and len(data) <= 131072:
        status = json.loads(data)
        # only explicitly allowed non-secret metrics, never arbitrary response/log dumps.
        revision = status.get('revision')
        if type(revision) is int:
            print('revision: ' + str(revision))
    connection.close()
except (OSError, ValueError, http.client.HTTPException):
    check(False, 'loopback bridge status unavailable (details redacted)')
if sys.argv[1] == 'true':
    check(pathlib.Path('/etc/aos/muse.enabled').is_file(), 'Muse explicitly enabled')
    check(pathlib.Path('/opt/muse/bin/musegadget').is_file(), 'operator-installed Muse SDK exists at configured default')
    check(subprocess.run(['systemctl', 'is-active', '--quiet', 'aos-muse.service'], check=False).returncode == 0, 'paired Muse companion active')
print('hardware certification: PENDING; service/health checks do not prove TV actions')
sys.exit(bool(errors))
PY
bash "$root/scripts/build-app.sh" --validate-only --tuple /opt/aos/current/tuple.json --runtime /opt/aos/current/runtime
