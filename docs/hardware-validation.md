# Hardware validation — pending

**No Pi4, TV, HDMI/CEC, ARM64 flutter-pi runtime, physical power action or real
Muse side-chat was tested in this development environment.** Host unit/API tests,
offscreen Flutter tests, x64 GTK compilation, fake subprocesses, structural
systemd verification and dry-run scripts are not hardware evidence. Do not mark
the appliance release certified until these gates have actual measurements.
Bundles produced by build-app.sh explicitly retain `hardware_validated:false`.

## Evidence record

For each test record date/operator, Pi model/RAM, OS image/version/checksum,
kernel, full-KMS firmware configuration, TV model/firmware/HDMI port, storage and
power supply, release ID, full Flutter/tool/engine/flutter-pi tuple and artifact
hashes. Preserve non-secret journal/measurement snippets and outcome. Never
include tokens, account/WiFi credentials or private calendar data. Compatibility
requires this concrete tuple, not a broad claim about all flutter-pi engines.

| Gate | Required real-device evidence | Status |
| --- | --- | --- |
| Platform | Pi4 8GB, aarch64 Pi OS Lite, no X11/Wayland, correct reviewed image | NOT TESTED |
| Runtime | ARM64 release app.so loads matching engine/ICU and flutter-pi, visible fixed dashboard | NOT TESTED |
| Non-root DRM | Selected card/render/tty1 permissions work under hardened display unit without broad groups or root | NOT TESTED |
| First paint | Bridge stopped/network absent: safe boot snapshot/fallback visible with diagnostic | NOT TESTED |
| Layout | HDMI 1080p/full-KMS, no clipping/overscan, maximum font scale and couch legibility | NOT TESTED |
| Service boot | Reboot and explicit tty1 takeover, restart limits, read-only snapshot, no unintended TV wake/standby | NOT TESTED |
| Remote input | Six keys map correctly, hold arrows repeats, hold Enter/Escape does not double-activate; release loss/adapter disconnect releases keys | NOT TESTED |
| Logical address | libCEC allocated player matches configured 4/8/11; no unrelated player or outgoing echo is interpreted | NOT TESTED |
| Semantic queue | One remote Enter produces one durable semantic intent, never a duplicate raw-key toggle; authoritative agent patch produces no echo | NOT TESTED |
| CEC control | On/off confirmed by live TV power report, source announcement and actual visible HDMI selection observed separately | NOT TESTED |
| Device failure | Missing/denied CEC/uinput, unplugged HDMI, TV disabled CEC, command timeout all fail honestly; no fabricated success | NOT TESTED |
| Recovery | Kill/restart bridge on Pi, network/tunnel drops, malformed patch: display keeps last valid state and snapshot-rehydrates | NOT TESTED |
| Storage | Real SD/SSD power interruption around commits, last-good recovery, disk-full queue/rollback, monotonic IDs and at-least-once behavior | NOT TESTED |
| Deployment | Verified ARM64 artifact build, frozen Pi dependency install, partial-copy interruption, atomic switch, explicit previous-release rollback | NOT TESTED |
| Permissions | Scoped tokens readable only by declared groups, Muse cannot write dashboard/read general tokens/use sudo/devices | NOT TESTED |
| Muse optional | Reviewed SDK argv supported, explicit pairing, exit-zero handoff durability documented; downstream IDs deduplicated before state update | NOT TESTED |
| Stability | 48+ hour idle/reconnect, repeated inputs, bounded RAM/disk/journald, frame pacing, safe shutdown/reboot | NOT TESTED |

## Bring-up sequence

1. Record the image/runtime tuple and actual DRM/CEC devices. Review setup.md;
   do not run apply scripts on a VM or copy old Fake-KMS settings.
2. Build with authorized engine-artifact retrieval. The earlier host ARM64 build
   attempt failed due to GitHub rate limiting; the x64 GTK runner is not a fallback
   appliance runtime. Validate hashes/architecture, then provision/deploy/reboot.
3. Confirm non-root device access and protected token groups. Doctor failure is
   an acceptance failure; doctor success is only a prerequisite for visible tests.
4. Approve tty1 takeover from SSH. Verify offline first paint, actual HDMI modes
   and layout before enabling external harness input.
5. Inspect libCEC registration; configure the actual playback address. Confirm
   idle `-s` monitoring receives presses without startup/exit power side effects,
   and that serialized single-command control releases/restarts monitor safely.
6. Verify the configured deadline hierarchy is CEC 18s < bridge 22s
   (`AOS_CEC_TIMEOUT_S`) < MCP 26s (`AOS_MCP_TIMEOUT_S`), and the remote tool
   deadline is at least 30s. Existing env files are preserved on upgrade: review
   them explicitly. Measure real deadlines; a timeout may follow a TV change.
7. Test remote repeat/missing release and one semantic queued intent per click.
   Check queue from one consumer only; observe agent commit/ack and no echo.
8. Exercise actual power/source with a person watching the TV. Distinguish
   emitted active-source announcement from visible input selection. Save sanitized
   results; fake output/transmission fixtures must not be used as device proof.
9. Perform power-loss/disk-pressure/reconnect/rollback tests on disposable appliance
   state, not production credentials. Preserve results and last-good recovery.
10. Enable Muse only if required and reviewed; prove the installed positional CLI
    binding and responsibility transfer before stopping direct MCP handling.
11. Complete stability run and record versions/licenses before a release claim.

Any failure remains a failure/degraded capability until fixed and retested. Do
not broaden device permissions, disable authentication, create LAN listeners or
silently substitute a cloud backend to make these gates look green.
