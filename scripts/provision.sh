#!/usr/bin/env bash
set -euo pipefail
# default is read-only planning; never run global changes on a development VM.
apply=false
allow_system=false
source_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
tuple=''
runtime=''
drm='/dev/dri/card1'
render='/dev/dri/renderD128'
die() { printf '%s\n' "$*" >&2; exit 1; }
while (($#)); do
    case "$1" in
        --dry-run) apply=false; shift ;; --apply) apply=true; shift ;;
        --allow-system-changes) allow_system=true; shift ;;
        --source|--tuple|--runtime|--drm-device|--render-device)
            (($# >= 2)) || die 'missing option value'
            case "$1" in --source) source_root=$2 ;; --tuple) tuple=$2 ;; --runtime) runtime=$2 ;;
                --drm-device) drm=$2 ;; --render-device) render=$2 ;; esac
            shift 2 ;;
        *) die "unknown option: $1" ;;
    esac
done
[[ -f "$source_root/uv.lock" && -d "$source_root/systemd" ]] || die 'complete source/lock required'
[[ "$drm" =~ ^/dev/dri/card[0-9]+$ && "$render" =~ ^/dev/dri/renderD[0-9]+$ ]] || die 'invalid explicit DRM/render device'
bash "$source_root/scripts/build-app.sh" --validate-only --tuple "$tuple" --runtime "$runtime"
if ! $apply; then
    printf '%s\n' 'DRY RUN: require Pi4/aarch64/Lite/root and --allow-system-changes; install apt runtime deps; scoped system users, token files, state/inbox permissions, units and udev rules; no services started, no SDK/tunnel/WiFi configuration'
    exit 0
fi
$allow_system || die 'global changes require --allow-system-changes'
[[ $(id -u) == 0 && $(uname -m) == aarch64 ]] || die 'apply requires root on aarch64 Pi, not this development host'
if [[ ! -r /proc/device-tree/model ]] || ! grep -aq 'Raspberry Pi 4' /proc/device-tree/model; then
    die 'only Raspberry Pi 4 is supported'
fi
if [[ ! -r /etc/os-release ]] || ! grep -Eq '^(ID=debian|ID=raspbian)$' /etc/os-release; then
    die 'Raspberry Pi OS Lite 64-bit required'
fi
for package in xserver-xorg-core weston gdm3 lightdm; do
    if dpkg-query -W -f='${Status}' "$package" 2>/dev/null | grep -q 'install ok installed'; then
        die 'graphical stack detected; use a fresh Pi OS Lite image, not destructive removal'
    fi
done
# do not trace this script: tokens are generated in Python, never shell variables/output.
umask 027
apt-get update
apt-get install -y --no-install-recommends python3 python3-venv python3-dev gcc libc6-dev cec-utils libdrm2 libgbm1 libinput10 libxkbcommon0 libegl1 libgles2 libvulkan1 libsystemd0
for directory in /etc/aos /opt/aos /opt/aos/releases /var/lib/aos /var/lib/aos-inbox /var/lib/aos-inbox/processed /var/lib/aos-inbox/rejected /var/lib/aos-muse; do
    [[ ! -L "$directory" ]] || die 'refusing symlinked appliance directory'
done
systemd-sysusers "$source_root/systemd/aos-sysusers.conf"
python3 - <<'PY'
import grp, os, pwd
members = {
    'aos-state': {'aos-bridge', 'aos-display'}, 'aos-control': {'aos-bridge', 'aos-cec'},
    'aos-inbox': {'aos-bridge', 'aos-muse'}, 'aos-bridge-auth': {'aos-bridge', 'aos-mcp'},
    'aos-display-auth': {'aos-bridge', 'aos-display'}, 'aos-muse-auth': {'aos-bridge', 'aos-muse'},
    'aos-mcp-auth': {'aos-mcp'},
}
for name, expected in members.items():
    group = grp.getgrnam(name)
    actual = set(group.gr_mem) | {user.pw_name for user in pwd.getpwall() if user.pw_gid == group.gr_gid}
    if actual != expected:
        raise SystemExit('existing scoped group has unexpected members; manual review required')
for name in ('aos-bridge', 'aos-mcp', 'aos-display', 'aos-cec', 'aos-muse'):
    user = pwd.getpwnam(name)
    actual = {grp.getgrgid(gid).gr_name for gid in os.getgrouplist(name, user.pw_gid)}
    expected = {name} | {group for group, accounts in members.items() if name in accounts}
    if user.pw_uid == 0 or actual != expected:
        raise SystemExit('existing service account has unexpected privileges; manual review required')
PY
install -d -o root -g root -m 0711 /etc/aos
install -d -o root -g root -m 0755 /opt/aos /opt/aos/releases
install -d -o aos-bridge -g aos-state -m 0750 /var/lib/aos
install -d -o aos-bridge -g aos-inbox -m 2770 /var/lib/aos-inbox
install -d -o aos-bridge -g aos-inbox -m 0750 /var/lib/aos-inbox/processed /var/lib/aos-inbox/rejected
install -d -o aos-muse -g aos-muse -m 0700 /var/lib/aos-muse
python3 - <<'PY'
import grp, os, pathlib, secrets, stat
root = pathlib.Path('/etc/aos')
for name, group in (('bridge', 'aos-bridge-auth'), ('display', 'aos-display-auth'), ('muse', 'aos-muse-auth'), ('mcp', 'aos-mcp-auth')):
    path = root / (name + '.token')
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o640)
    except FileExistsError:
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1 or metadata.st_uid != 0:
            raise SystemExit('refusing unsafe existing token file')
        if metadata.st_mode & 0o137 or metadata.st_gid != grp.getgrnam(group).gr_gid or metadata.st_size == 0:
            raise SystemExit('existing token permissions/group/content need manual review; not overwritten')
        continue
    try:
        os.fchown(fd, 0, grp.getgrnam(group).gr_gid)
        os.write(fd, (secrets.token_urlsafe(48) + '\n').encode())
        os.fsync(fd)
    finally:
        os.close(fd)
directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
os.fsync(directory)
os.close(directory)
PY
# existing operator configuration is never overwritten.
for component in bridge display mcp cec muse; do
    [[ ! -L "/etc/aos/$component.env" ]] || die 'refusing symlinked service environment'
    if [[ ! -e "/etc/aos/$component.env" ]]; then
        install -o root -g "aos-$component" -m 0640 "$source_root/systemd/$component.env.example" "/etc/aos/$component.env"
    fi
done
install -o root -g root -m 0644 "$source_root/systemd/70-aos-cec.rules" /etc/udev/rules.d/70-aos-cec.rules
# no broad input/video/tty group membership: only selected DRM/TTY and our virtual keyboard.
printf 'SUBSYSTEM=="drm", KERNEL=="%s", GROUP="aos-display", MODE="0660"\nSUBSYSTEM=="drm", KERNEL=="%s", GROUP="aos-display", MODE="0660"\nSUBSYSTEM=="tty", KERNEL=="tty1", GROUP="aos-display", MODE="0660"\n' "${drm##*/}" "${render##*/}" > /etc/udev/rules.d/71-aos-display.rules
for unit in "$source_root"/systemd/aos-*.service; do
    install -o root -g root -m 0644 "$unit" "/etc/systemd/system/${unit##*/}"
done
install -d -o root -g root -m 0755 /etc/systemd/system/aos-display.service.d
printf '[Service]\nDeviceAllow=\nDeviceAllow=%s rw\nDeviceAllow=%s rw\nDeviceAllow=/dev/tty1 rw\nDeviceAllow=char-input r\n' "$drm" "$render" > /etc/systemd/system/aos-display.service.d/devices.conf
install -o root -g root -m 0644 "$source_root/systemd/aos-modules.conf" /etc/modules-load.d/aos.conf
udevadm control --reload-rules
systemctl daemon-reload
printf '%s\n' 'provisioned; no services enabled/started and no existing credentials/state overwritten; deploy with frozen lock, then reboot to apply device ownership'
