#!/usr/bin/env bash
# AmonaVision — post-setup validation suite
# Usage: bash /home/av/AmonaVision/test_setup.sh
#
# Run as the 'av' user (not root) for full coverage.
# Exits 0 if all tests pass, 1 if any FAIL.

APP_USER=av
APP_DIR=/home/$APP_USER/AmonaVision
VENV="$APP_DIR/venv/bin/python3"

# Non-interactive shells (SSH batch commands, the bot's subprocess, systemd
# services) get a stripped PATH that excludes /usr/sbin and /sbin -- e.g.
# "/usr/local/bin:/usr/bin:/bin:/usr/games". iw, NetworkManager and rfkill all
# live there, so without this every check using them silently no-ops or
# reports garbage (empty power-save state, skipped NM config-parse check)
# instead of a real result. This is exactly the environment /test and the
# health-check timer run in, so it matters here more than in an interactive shell.
export PATH="/usr/sbin:/sbin:$PATH"

# Query av's `systemctl --user` bus. When we're already av (interactive SSH, the
# bot subprocess, or the LAN dashboard) call it directly — the dashboard runs
# with NoNewPrivileges=yes where `sudo` cannot work and would turn every such
# probe into a false FAIL. Only shell out via sudo when running as root.
uctl() {
    local ruid; ruid=$(id -u "$APP_USER")
    if [[ "$(id -un)" == "$APP_USER" ]]; then
        XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$ruid}" \
        DBUS_SESSION_BUS_ADDRESS="${DBUS_SESSION_BUS_ADDRESS:-unix:path=/run/user/$ruid/bus}" \
            systemctl --user "$@"
    else
        sudo -u "$APP_USER" XDG_RUNTIME_DIR=/run/user/$ruid \
            systemctl --user "$@"
    fi
}

# Same idea for wpctl, which talks to av's PipeWire.
pwctl() {
    local ruid; ruid=$(id -u "$APP_USER")
    if [[ "$(id -un)" == "$APP_USER" ]]; then
        XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$ruid}" wpctl "$@"
    else
        sudo -u "$APP_USER" XDG_RUNTIME_DIR=/run/user/$ruid wpctl "$@"
    fi
}

# ── Counters & helpers ────────────────────────────────────────────────────────
PASS=0; FAIL=0; WARN=0
GREEN='\033[0;32m'; RED='\033[0;31m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'

pass() { echo -e "  ${GREEN}[PASS]${NC} $*"; (( PASS++ )) || true; }
fail() { echo -e "  ${RED}[FAIL]${NC} $*"; (( FAIL++ )) || true; }
warn() { echo -e "  ${YELLOW}[WARN]${NC} $*"; (( WARN++ )) || true; }
hdr()  { echo -e "\n${CYAN}── $* ──${NC}"; }

LOG="$APP_DIR/setup.log"
# Redirect to log only if writable (firstboot runs as av but setup.log may be root-owned).
# AMONAVISION_HEALTHCHECK=1 (set by tools/health-alert.sh and the bot's /test
# command) skips this so the caller gets the output on stdout to parse, instead
# of it vanishing into setup.log.
if [[ -z "${AMONAVISION_HEALTHCHECK:-}" ]] && { [[ -w "$LOG" ]] || ( touch "$LOG" 2>/dev/null && [[ -w "$LOG" ]] ); }; then
    exec >> "$LOG" 2>&1
fi

echo ""
echo "================================================================"
echo " AmonaVision — Installation Test Suite"
echo " $(date)"
echo " Context: ${FIRSTBOOT_RUN:-manual run}"
echo "================================================================"

# ═══════════════════════════════════════════════════════════════════════════════
hdr "SYSTEM"
# ═══════════════════════════════════════════════════════════════════════════════

# cloud-init disabled
if [[ -f /etc/cloud/cloud-init.disabled ]]; then
    pass "cloud-init is disabled"
else
    fail "cloud-init is NOT disabled — will cause WiFi conflicts and slow boot"
    echo "       Fix: sudo touch /etc/cloud/cloud-init.disabled"
fi

# config.txt watchdog
if grep -qF "dtparam=watchdog=on" /boot/firmware/config.txt 2>/dev/null; then
    pass "Hardware watchdog overlay in config.txt"
else
    warn "Hardware watchdog not in config.txt"
fi

# systemd watchdog
if grep -q "^RuntimeWatchdogSec=15" /etc/systemd/system.conf 2>/dev/null; then
    pass "systemd RuntimeWatchdogSec=15"
else
    warn "systemd watchdog not configured in system.conf"
fi

# log2ram
if systemctl is-active log2ram &>/dev/null; then
    pass "log2ram is running"
else
    warn "log2ram is not active (will start after reboot)"
fi

# journald limits
if [[ -f /etc/systemd/journald.conf.d/log2ram.conf ]]; then
    pass "journald limits configured"
else
    warn "journald limits not configured — log2ram may fill RAM"
fi

# journald persistence — without this, an outage overnight leaves no
# NetworkManager/kernel trail once someone reboots to recover
if grep -qF "Storage=persistent" /etc/systemd/journald.conf.d/log2ram.conf 2>/dev/null && [[ -d /var/log/journal ]]; then
    pass "journald is persistent — survives reboots (via log2ram)"
else
    fail "journald is volatile — logs from before a reboot are lost, can't diagnose overnight outages"
    echo "       Fix: re-run setup.sh (sets Storage=persistent + creates /var/log/journal)"
fi

# USB autosuspend — udev rule must cover autosuspend AND power/control
USB_RULE=/etc/udev/rules.d/99-usb-no-autosuspend.rules
if [[ -f "$USB_RULE" ]]; then
    if grep -q "power/control" "$USB_RULE"; then
        pass "USB autosuspend rule present (covers devices + hubs)"
    else
        warn "USB autosuspend rule present but missing power/control — USB hubs may suspend"
        echo "       Fix: re-run setup.sh to update the udev rule"
    fi
else
    fail "USB autosuspend rule missing — 4G dongle and USB-powered display may lose power"
fi

# USB kernel autosuspend (cmdline.txt)
if grep -qF "usbcore.autosuspend=-1" /boot/firmware/cmdline.txt 2>/dev/null; then
    pass "USB kernel autosuspend disabled in cmdline.txt"
else
    warn "usbcore.autosuspend=-1 missing from cmdline.txt — USB hubs may suspend"
    echo "       Fix: add 'usbcore.autosuspend=-1' to /boot/firmware/cmdline.txt"
fi

# HDMI force-hotplug
if grep -qF "hdmi_force_hotplug=1" /boot/firmware/config.txt 2>/dev/null; then
    pass "HDMI force-hotplug enabled (prevents disconnect flicker loop)"
else
    warn "hdmi_force_hotplug=1 missing — display may flicker or disconnect"
    echo "       Fix: add 'hdmi_force_hotplug=1' to /boot/firmware/config.txt"
fi

# DSI auto-detect disabled
if grep -qF "display_auto_detect=0" /boot/firmware/config.txt 2>/dev/null; then
    pass "DSI display auto-detect disabled (HDMI-only setup)"
else
    warn "display_auto_detect not set to 0 — DSI scanning may interfere with HDMI"
    echo "       Fix: set 'display_auto_detect=0' in /boot/firmware/config.txt"
fi

# 7" panel: forced HDMI audio (panel EDID omits audio)
if grep -qF "hdmi_force_edid_audio=1" /boot/firmware/config.txt 2>/dev/null; then
    pass "hdmi_force_edid_audio=1 present (HDMI audio sink exists)"
else
    warn "hdmi_force_edid_audio=1 missing — HDMI audio sink may not appear"
    echo "       Fix: add 'hdmi_force_edid_audio=1' to /boot/firmware/config.txt"
fi

# 7" panel: pinned native video mode
if grep -qF "video=HDMI-A-1:1024x600" /boot/firmware/cmdline.txt 2>/dev/null; then
    pass "HDMI-A-1 forced on (video=HDMI-A-1:1024x600M@60D)"
else
    warn "video=HDMI-A-1:1024x600M@60D missing from cmdline.txt — panel may show nothing / wrong res"
    echo "       Fix: re-run setup.sh"
fi

# Panel EDID forced on the connector and shipped in the initramfs (vc4 loads
# from there, before the root filesystem) — otherwise a panel not detected at
# boot comes up without an EDID: DVI mode, no HDMI audio.
if grep -qF "drm.edid_firmware=HDMI-A-1:edid/amona.bin" /boot/firmware/cmdline.txt 2>/dev/null; then
    pass "Panel EDID forced on HDMI-A-1 (drm.edid_firmware)"
else
    fail "drm.edid_firmware=HDMI-A-1:edid/amona.bin missing from cmdline.txt — an undetected panel boots without HDMI audio"
    echo "       Fix: re-run setup.sh"
fi
if lsinitramfs /boot/firmware/initramfs8 2>/dev/null | grep -q 'firmware/edid/amona\.bin$'; then
    pass "Panel EDID present in the initramfs"
else
    fail "Panel EDID missing from /boot/firmware/initramfs8 — drm.edid_firmware fails at boot"
    echo "       Fix: re-run setup.sh (installs the initramfs hook), or: sudo update-initramfs -u -k all"
fi

# Live display output: the panel's connector must be connected, have an EDID
# and be driven by a CRTC ("enabled"). With the connector forced on (video=...D)
# "connected" is always true, so "enabled" + EDID are what catch a frame that
# has stopped driving the panel — the "No signal" case.
DRM_CONN=""
for c in /sys/class/drm/card*-HDMI-A-1; do [[ -d "$c" ]] && DRM_CONN="$c"; done
if [[ -z "$DRM_CONN" ]]; then
    fail "No HDMI-A-1 connector in /sys/class/drm — display driver not loaded, the panel shows \"No signal\""
    echo "       Diagnose: bash $APP_DIR/tools/hdmi-diag.sh"
else
    C_STATUS=$(cat "$DRM_CONN/status" 2>/dev/null)
    C_ENABLED=$(cat "$DRM_CONN/enabled" 2>/dev/null)
    C_EDID=$(wc -c < "$DRM_CONN/edid" 2>/dev/null || echo 0)
    if [[ "$C_STATUS" == "connected" && "$C_ENABLED" == "enabled" && "${C_EDID:-0}" -gt 0 ]]; then
        pass "Display output live on HDMI-A-1 (connected, enabled, EDID $C_EDID bytes)"
    else
        fail "Display not being driven: HDMI-A-1 status=${C_STATUS:-?} enabled=${C_ENABLED:-?} EDID=${C_EDID:-0} bytes — the panel likely shows \"No signal\""
        echo "       Diagnose: bash $APP_DIR/tools/hdmi-diag.sh"
    fi
fi

# Known-bad HDMI settings: each of these was tried on 2026-09-20 while
# chasing HDMI audio and broke video (blank/wrong-resolution screen, or the
# device hanging on boot and needing a physical power cycle). If any of them
# is present, config.txt has drifted from the only combination confirmed
# working on this device — flag it as a hard failure, not a warning.
if grep -qE '^dtoverlay=vc4-fkms-v3d$' /boot/firmware/config.txt 2>/dev/null; then
    fail "dtoverlay=vc4-fkms-v3d present — this panel needs full KMS (vc4-kms-v3d); fkms ignores cmdline.txt's video= and breaks the display"
    echo "       Fix: re-run setup.sh, or change dtoverlay back to vc4-kms-v3d in /boot/firmware/config.txt"
fi
if grep -qE '^hdmi_(cvt|group|mode|drive)(:[01])?=' /boot/firmware/config.txt 2>/dev/null; then
    fail "hdmi_cvt/hdmi_group/hdmi_mode/hdmi_drive present in config.txt — known to conflict with cmdline.txt's video= and break the display on this device"
    echo "       Fix: re-run setup.sh, or remove those hdmi_* lines from /boot/firmware/config.txt"
fi
if grep -qE '^hdmi_force_(hotplug|edid_audio):[01]=' /boot/firmware/config.txt 2>/dev/null; then
    fail "Port-suffixed hdmi_force_hotplug:N / hdmi_force_edid_audio:N present — caused this device to hang on boot; the bare (unsuffixed) form is the verified-working one"
    echo "       Fix: re-run setup.sh, or replace with bare 'hdmi_force_hotplug=1' / 'hdmi_force_edid_audio=1' in /boot/firmware/config.txt"
fi

# Screen blanking disabled
if grep -qF "consoleblank=0" /boot/firmware/cmdline.txt 2>/dev/null; then
    pass "Console screen blanking disabled"
else
    warn "consoleblank=0 missing from cmdline.txt — screen may blank unexpectedly"
fi

# Branded boot splash — Plymouth's pix theme swapped for the AmonaVision logo
PLY_SPLASH=/usr/share/plymouth/themes/pix/splash.png
if [[ -f "$APP_DIR/assets/logo.png" ]] && [[ -f "$PLY_SPLASH" ]] \
   && cmp -s "$APP_DIR/assets/logo.png" "$PLY_SPLASH"; then
    pass "Boot splash branded (Plymouth pix theme = assets/logo.png)"
else
    warn "Boot splash not branded (Plymouth pix theme still stock, or logo.png missing)"
    echo "       Fix: re-run setup.sh"
fi

# Touchscreen HID quirk (prevents "Failed to get evdev touchscreen name" crash)
if grep -qF "usbhid.quirks=0x0484:0x5750:0x0004" /boot/firmware/cmdline.txt 2>/dev/null; then
    pass "Touchscreen USB HID quirk present (prevents kmsdrm evdev crash)"
else
    warn "usbhid.quirks missing — display's touch controller can crash pygame's kmsdrm init"
    echo "       Fix: add 'usbhid.quirks=0x0484:0x5750:0x0004' to /boot/firmware/cmdline.txt"
fi

# SDL_KMSDRM_DEVICE_INDEX (card0 is v3d render-only, card1 is the real display)
if grep -qF "SDL_KMSDRM_DEVICE_INDEX=1" /etc/systemd/system/amonavision.service 2>/dev/null; then
    pass "SDL_KMSDRM_DEVICE_INDEX=1 set (avoids 'kmsdrm not available' on card0)"
else
    warn "SDL_KMSDRM_DEVICE_INDEX missing from amonavision.service — kmsdrm init may fail"
    echo "       Fix: re-run setup.sh to redeploy amonavision.service"
fi

# The installed unit should match the project copy: a live edit to only one of
# them gets silently reverted, or silently kept, by the next setup.sh run.
if cmp -s /etc/systemd/system/amonavision.service "$APP_DIR/amonavision.service"; then
    pass "Installed amonavision.service matches the project copy"
else
    warn "Installed amonavision.service differs from $APP_DIR/amonavision.service"
    echo "       Fix: sudo cp $APP_DIR/amonavision.service /etc/systemd/system/ && sudo systemctl daemon-reload"
fi

# lightdm disabled
if ! systemctl is-enabled lightdm &>/dev/null; then
    pass "lightdm is disabled"
else
    fail "lightdm is enabled — will conflict with kmsdrm display"
    echo "       Fix: sudo systemctl disable --now lightdm"
fi

# Raspberry Pi Connect not crash-looping (rpi-connect-wayvnc needs Wayland,
# which a kmsdrm frame doesn't have -- it restarts every ~5s and floods the journal)
WVNC_RESTARTS=$(uctl show rpi-connect-wayvnc.service -p NRestarts --value 2>/dev/null || echo 0)
if [[ "${WVNC_RESTARTS:-0}" -gt 20 ]]; then
    fail "rpi-connect-wayvnc has restarted $WVNC_RESTARTS times — crash-loop flooding the journal"
    echo "       Fix: re-run setup.sh (disables + masks Raspberry Pi Connect)"
else
    pass "Raspberry Pi Connect not crash-looping"
fi

# ═══════════════════════════════════════════════════════════════════════════════
hdr "HARDWARE"
# ═══════════════════════════════════════════════════════════════════════════════

# CPU temperature readable
if TEMP=$(cat /sys/class/thermal/thermal_zone0/temp 2>/dev/null); then
    TEMP_C=$(( TEMP / 1000 ))
    if (( TEMP_C < 85 )); then
        pass "CPU temperature readable — ${TEMP_C}°C"
    else
        warn "CPU temperature is HIGH — ${TEMP_C}°C (check cooling)"
    fi
else
    fail "Cannot read CPU temperature — /sys/class/thermal/thermal_zone0/temp missing"
fi

# PWM overlay
if [[ -f /boot/firmware/config.txt ]] && grep -qF "dtoverlay=pwm,pin=18,func=2" /boot/firmware/config.txt; then
    pass "PWM overlay in config.txt"
    if [[ -d /sys/class/pwm/pwmchip0 ]]; then
        pass "PWM chip accessible at /sys/class/pwm/pwmchip0"
    else
        warn "PWM overlay in config.txt but /sys/class/pwm/pwmchip0 not present — reboot needed"
    fi
else
    warn "PWM overlay not in config.txt — fan speed control unavailable"
fi

# DRM device (display)
if [[ -e /dev/dri/card0 ]]; then
    pass "DRM device present (/dev/dri/card0)"
else
    fail "DRM device /dev/dri/card0 not found — display will not work"
fi

# User in video group
if id -nG "$APP_USER" 2>/dev/null | grep -qw "video"; then
    pass "User $APP_USER is in the video group"
else
    fail "User $APP_USER is NOT in the video group — kmsdrm will be denied"
    echo "       Fix: sudo usermod -aG video $APP_USER"
fi

# ═══════════════════════════════════════════════════════════════════════════════
hdr "SERVICES"
# ═══════════════════════════════════════════════════════════════════════════════

for svc in amonavision fan-control smbd nmbd tailscaled; do
    enabled=$(systemctl is-enabled "$svc" 2>/dev/null || echo "not-found")
    active=$(systemctl is-active  "$svc" 2>/dev/null || echo "inactive")
    if [[ "$enabled" == "enabled" ]] && [[ "$active" == "active" ]]; then
        pass "$svc — enabled and running"
    elif [[ "$enabled" == "enabled" ]]; then
        warn "$svc — enabled but not running (may start after reboot)"
    elif [[ "$enabled" == "not-found" ]]; then
        fail "$svc — service not found"
    else
        fail "$svc — not enabled (enabled=$enabled, active=$active)"
    fi
done

# Liveness watchdog (catches a wedged-but-alive bot or wedged tailscaled, not just a crashed process)
WENABLED=$(systemctl is-enabled amonavision-bot-watchdog.timer 2>/dev/null || echo "not-found")
WACTIVE=$(systemctl is-active amonavision-bot-watchdog.timer 2>/dev/null || echo "inactive")
if [[ "$WENABLED" == "enabled" && "$WACTIVE" == "active" ]]; then
    pass "amonavision-bot-watchdog.timer — enabled and armed"
else
    fail "amonavision-bot-watchdog.timer — not enabled/armed (enabled=$WENABLED, active=$WACTIVE)"
fi

# Periodic health-check timer (Telegram alerting for an unattended device)
TENABLED=$(systemctl is-enabled amonavision-healthcheck.timer 2>/dev/null || echo "not-found")
TACTIVE=$(systemctl is-active amonavision-healthcheck.timer 2>/dev/null || echo "inactive")
if [[ "$TENABLED" == "enabled" && "$TACTIVE" == "active" ]]; then
    pass "amonavision-healthcheck.timer — enabled and armed"
else
    fail "amonavision-healthcheck.timer — not enabled/armed (enabled=$TENABLED, active=$TACTIVE)"
fi
if grep -qE '^TELEGRAM_ADMIN_CHAT_ID=.+' "$APP_DIR/.env" 2>/dev/null; then
    pass "TELEGRAM_ADMIN_CHAT_ID set — health-check can alert"
else
    warn "TELEGRAM_ADMIN_CHAT_ID missing — health-check timer runs but stays silent"
    echo "       Fix: add TELEGRAM_ADMIN_CHAT_ID=<your id> to $APP_DIR/.env (a person, not the group)"
fi

# LAN dashboard (its own isolated service; only starts if WEB_PASSWORD is set)
DENABLED=$(systemctl is-enabled amonavision-web.service 2>/dev/null || echo "not-found")
if [[ "$DENABLED" != "enabled" ]]; then
    warn "amonavision-web.service not enabled (enabled=$DENABLED) — re-run setup.sh"
elif grep -qE '^WEB_PASSWORD=.+' "$APP_DIR/.env" 2>/dev/null; then
    if curl -sf -m 3 -o /dev/null http://127.0.0.1:8080/healthz; then
        pass "LAN dashboard responding on :8080"
    else
        fail "amonavision-web enabled + WEB_PASSWORD set, but :8080/healthz did not respond"
        echo "       Fix: journalctl -u amonavision-web -n 40"
    fi
else
    warn "amonavision-web enabled but WEB_PASSWORD not in .env — dashboard won't start"
    echo "       Fix: add WEB_PASSWORD=<shared-password> to $APP_DIR/.env; sudo systemctl start amonavision-web"
fi

# Memory cgroup controller — the Pi bootloader disables it by default, which
# makes every MemoryMax=/MemoryHigh= (frame OOM protection + dashboard's 128M
# isolation cap) a no-op. cmdline.txt gets cgroup_enable=memory in Step 3b.
if grep -q memory /sys/fs/cgroup/cgroup.controllers 2>/dev/null; then
    pass "memory cgroup controller active — systemd MemoryMax limits enforced"
elif grep -qF 'cgroup_enable=memory' /boot/firmware/cmdline.txt 2>/dev/null; then
    warn "memory cgroup fix staged in cmdline.txt but not live yet — reboot to enforce MemoryMax"
else
    warn "memory cgroup controller off — MemoryMax=/MemoryHigh= ignored (CPUQuota/TasksMax still work)"
    echo "       Fix: add 'cgroup_enable=memory cgroup_memory=1' to /boot/firmware/cmdline.txt and reboot"
fi

# PipeWire (user service — check as av user)
if uctl is-enabled pipewire &>/dev/null; then
    pass "PipeWire user service enabled"
else
    fail "PipeWire user service not enabled — audio will not work"
fi

# PulseAudio socket (only available after login/linger starts PipeWire)
PULSE_SOCK="/run/user/$(id -u $APP_USER)/pulse/native"
if [[ -S "$PULSE_SOCK" ]]; then
    pass "PipeWire PulseAudio socket present"
else
    warn "PipeWire socket not yet present — will appear after first login/service start"
fi

# WirePlumber HDMI-priority config — without this, PipeWire defaults to
# whichever sink it enumerates first (often the analog jack), silently.
WPHDMI="/home/$APP_USER/.config/wireplumber/wireplumber.conf.d/50-hdmi-default.conf"
if [[ -f "$WPHDMI" ]]; then
    pass "WirePlumber HDMI-priority config present"
else
    fail "WirePlumber HDMI-priority config missing — audio may silently default to headphone jack"
    echo "       Fix: re-run setup.sh, or copy config/wireplumber/50-hdmi-default.conf to $WPHDMI"
fi

# App audio must reach HDMI through PipeWire: the viewer has two SDL audio
# clients (pygame's mixer + ffpyplayer) and the raw ALSA hdmi: device allows
# only one, so with SDL_AUDIODRIVER=alsa every video plays silently.
AV_UNIT=/etc/systemd/system/amonavision.service
if grep -qE '^Environment=(SDL_AUDIODRIVER=alsa|AUDIODEV=)' "$AV_UNIT" 2>/dev/null; then
    fail "amonavision.service sends audio straight to ALSA (SDL_AUDIODRIVER=alsa / AUDIODEV) — videos play silently"
    echo "       Fix: re-run setup.sh, or install $APP_DIR/amonavision.service to $AV_UNIT and restart"
elif grep -qF "SDL_AUDIODRIVER=pulse" "$AV_UNIT" 2>/dev/null; then
    pass "App audio goes through PipeWire (SDL_AUDIODRIVER=pulse)"
else
    warn "SDL_AUDIODRIVER not set in $AV_UNIT"
fi

# Runtime: PipeWire's default output must be the panel's HDMI sink (only
# checkable once PipeWire is up — the socket check above warns otherwise).
if [[ -S "$PULSE_SOCK" ]]; then
    DEF_SINK=$(pwctl inspect @DEFAULT_AUDIO_SINK@ 2>/dev/null | sed -n 's/.*node\.name = "\(.*\)"/\1/p' | head -1)
    HDMI_JACK=$(amixer -c vc4hdmi0 cget iface=CARD,name='HDMI Jack' 2>/dev/null | grep -oE 'values=(on|off)')
    if [[ "$DEF_SINK" == alsa_output.platform-fef00700.hdmi.* ]]; then
        pass "Default audio output is the panel's HDMI ($DEF_SINK)"
    elif [[ "$HDMI_JACK" == "values=off" ]]; then
        warn "Panel reports no HDMI connection (off/unplugged) — HDMI audio unavailable until it's back"
    else
        fail "Default audio output is '${DEF_SINK:-none}', not the panel's HDMI — voice notes and videos are silent"
        echo "       Fix: sudo systemctl restart amonavision (launch.sh re-selects HDMI, restarting WirePlumber if needed)"
    fi
fi

# The old dongle_only dispatcher is superseded by route metrics (dongle 100 /
# Wi-Fi 700, set in setup.sh Step 8b/8c) -- that's standard NetworkManager
# behaviour and needs no dispatcher. Flag it if a stale copy is still installed.
if [[ -f /etc/NetworkManager/dispatcher.d/99-amonavision ]]; then
    warn "Stale WiFi/4G dispatcher still installed (superseded by route metrics)"
    echo "       Fix: re-run setup.sh, or: sudo rm /etc/NetworkManager/dispatcher.d/99-amonavision"
fi

# Linger
if loginctl show-user "$APP_USER" 2>/dev/null | grep -q "Linger=yes"; then
    pass "Linger enabled for $APP_USER (PipeWire starts at boot)"
else
    fail "Linger not enabled — PipeWire won't start until user logs in"
    echo "       Fix: sudo loginctl enable-linger $APP_USER"
fi

# ═══════════════════════════════════════════════════════════════════════════════
hdr "NETWORK"
# ═══════════════════════════════════════════════════════════════════════════════

# Internet — probe TCP/443 (mobile/4G APNs often block or hijack TCP/53)
if python3 -c "import socket; socket.create_connection(('1.1.1.1',443),3)" 2>/dev/null \
|| python3 -c "import socket; socket.create_connection(('8.8.8.8',443),3)" 2>/dev/null; then
    pass "Internet reachable"
else
    fail "No internet connectivity"
fi

# Clock — a Pi has no RTC; a stale clock breaks TLS to Telegram/Tailscale until NTP steps it
if timedatectl show -p NTPSynchronized --value 2>/dev/null | grep -q "yes"; then
    pass "System clock is NTP-synchronised"
else
    fail "System clock NOT NTP-synced — TLS (api.telegram.org) will fail until it is"
    echo "       Fix: check UDP/123 is not blocked; systemctl restart systemd-timesyncd"
fi

# DNS must not depend on Tailscale MagicDNS
if grep -q "generated by tailscale" /etc/resolv.conf 2>/dev/null; then
    fail "/etc/resolv.conf is owned by Tailscale MagicDNS — DNS dies if tailscaled stops"
    echo "       Fix: sudo tailscale set --accept-dns=false && sudo systemctl restart tailscaled"
else
    pass "DNS resolution not tied to Tailscale"
fi

# WiFi
WIFI_STATE=$(nmcli -t -f DEVICE,STATE device status 2>/dev/null | grep "^wlan0" | cut -d: -f2)
if [[ "$WIFI_STATE" == "connected" ]]; then
    WIFI_IP=$(ip -4 addr show wlan0 2>/dev/null | grep -oP '(?<=inet )\S+' | head -1)
    pass "WiFi connected — $WIFI_IP"
elif [[ -z "$WIFI_STATE" ]]; then
    warn "No wlan0 interface detected"
else
    warn "WiFi not connected (state: $WIFI_STATE)"
fi

# NetworkManager must actually be running (a bad conf.d file stops it dead)
if systemctl is-active NetworkManager &>/dev/null; then
    pass "NetworkManager is running"
    # Any non-ASCII / unparseable file in conf.d takes NM down on the next boot.
    # `--print-config` needs root to read /run/NetworkManager/conf.d/* — as a
    # normal user it fails with "Permission denied" even when the config is
    # perfectly fine, which would show up as a false NetworkManager failure in
    # /test (runs as the bot's own user). Only trust this check as root.
    if [[ $EUID -eq 0 ]]; then
        if command -v NetworkManager &>/dev/null && ! NetworkManager --print-config &>/dev/null; then
            fail "NetworkManager cannot parse its config — a file in /etc/NetworkManager/conf.d/ is bad"
            echo "       Fix: check each conf.d file is plain ASCII; remove the offender; restart NetworkManager"
        fi
    fi
else
    fail "NetworkManager is NOT running — no network will come up"
    echo "       Fix: journalctl -u NetworkManager -b | tail; suspect a bad /etc/NetworkManager/conf.d/*.conf"
fi

# WiFi power-save — must be OFF on an always-on frame or WiFi drops intermittently
PS=$(iw dev wlan0 get power_save 2>/dev/null | grep -oE 'on|off' | head -1)
if [[ "$PS" == "off" ]]; then
    pass "WiFi power-save is OFF"
elif [[ -f /etc/NetworkManager/dispatcher.d/50-wifi-powersave-off ]]; then
    warn "WiFi power-save live state is '$PS' — dispatcher installed, will apply on next wlan up"
else
    fail "WiFi power-save is '$PS' and no dispatcher installed — WiFi will drop/lag"
    echo "       Fix: re-run setup.sh"
fi

# 4G dongle — this unit's PRIMARY link at the deployment site, which has no
# Wi-Fi at all (Wi-Fi is only the fallback, for testing/maintenance somewhere
# that has it — see setup.sh Step 8b/8c). Verify it's both reachable and
# actually configured as primary, not just present.
# Found by USB-modem driver (same list as setup.sh's 4g-dongle profile and
# tools/bot-watchdog.sh), not by NetworkManager state, so a stick that NM has
# given up on is reported as broken instead of as "not plugged in".
G4_DEV=""
for n in /sys/class/net/*; do
    case "$(basename "$(readlink -f "$n/device/driver" 2>/dev/null)" 2>/dev/null)" in
        cdc_ether|cdc_ncm|rndis_host|huawei_cdc_ncm) G4_DEV=$(basename "$n"); break ;;
    esac
done
[[ -z "$G4_DEV" ]] && G4_DEV=$(ip -o link show 2>/dev/null | grep -oE '(wwan[0-9]+|ppp[0-9]+)' | head -1)
if [[ -z "$G4_DEV" ]]; then
    # ZTE / Huawei stick still on the bus with no interface = the 2026-09-22/23
    # failure (re-enumeration ended in "error -71" and nothing retried).
    if grep -qxE '19d2|12d1' /sys/bus/usb/devices/*/idVendor 2>/dev/null; then
        fail "4G dongle is on the USB bus but has NO network interface (failed USB enumeration)"
        echo "       tools/bot-watchdog.sh re-plugs it within ~5 min; now: sudo bash $APP_DIR/tools/bot-watchdog.sh"
    else
        warn "No 4G interface detected (dongle unplugged or not enumerated)"
        echo "       Fine while testing on Wi-Fi, but at the deployment site (no Wi-Fi) this means no internet at all."
    fi
else
    G4_GW=$(ip route show dev "$G4_DEV" 2>/dev/null | awk '/default|via/{for(i=1;i<=NF;i++)if($i=="via"){print $(i+1);exit}}')
    [[ -z "$G4_GW" ]] && G4_GW=$(nmcli -g IP4.GATEWAY device show "$G4_DEV" 2>/dev/null)
    G4_HAS_INTERNET=0
    if [[ -n "$G4_GW" ]]; then
        # Bind the probe to the 4G interface (SO_BINDTODEVICE, no root needed
        # since kernel 5.7) so it genuinely uses that link and not whatever's
        # currently winning the default route. Replaces a "sudo -n ip route
        # add" that always failed when run as av (bot /test, dashboard).
        # If curl can't bind at all (exit 45), say so — do NOT fall back to
        # the default route, that would be a false PASS/FAIL either way.
        G4_HTTP=$(curl -sS -m 12 --interface "if!$G4_DEV" -o /dev/null -w '%{http_code}' https://1.1.1.1/ 2>/dev/null)
        G4_RC=$?
        if [[ $G4_RC -ne 45 ]]; then
            if [[ "$G4_HTTP" =~ ^[23] ]]; then
                pass "4G link works — reached the internet via $G4_DEV (gw $G4_GW)"
                G4_HAS_INTERNET=1
            else
                fail "4G interface $G4_DEV is UP but has NO upstream internet (SIM data?)"
                echo "       At the deployment site (no Wi-Fi) this means the frame is fully offline."
                echo "       Check SIM credit / registration in the dongle's web UI (http://$G4_GW)."
            fi
        else
            warn "4G interface $G4_DEV detected (gw $G4_GW) — could not bind a probe to it"
        fi
    else
        warn "4G interface $G4_DEV up but no gateway — cannot test its upstream path"
    fi

    # The stick's own view, from its web API (ZTE; silently skipped for
    # others). Local USB traffic only. A SIM waiting for its PIN never dials
    # after a power cut, so that's a FAIL; a weak signal is only a WARN.
    if [[ -n "$G4_GW" ]]; then
        G4_RADIO=$(curl -s -m 5 -H "Referer: http://$G4_GW/index.html" \
            "http://$G4_GW/goform/goform_get_cmd_process?isTest=false&multi_data=1&cmd=modem_main_state,ppp_status,network_type,wan_active_band,lte_rsrp,network_provider" 2>/dev/null \
            | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(0)
print("|".join(str(d.get(k, "")) for k in
      ("modem_main_state", "ppp_status", "network_type", "wan_active_band", "lte_rsrp", "network_provider")))' 2>/dev/null)
        if [[ -n "$G4_RADIO" ]]; then
            IFS='|' read -r G4_MODEM G4_PPP G4_NET G4_BAND G4_RSRP G4_OP <<<"$G4_RADIO"
            if [[ "$G4_MODEM" == *waitpin* || "$G4_MODEM" == *waitpuk* ]]; then
                fail "4G SIM is waiting for its PIN ($G4_MODEM) — the dongle can't dial after a power cut"
                echo "       Fix: disable the SIM PIN in the dongle's web UI (http://$G4_GW)."
            elif [[ "$G4_RSRP" =~ ^-?[0-9]+$ ]] && (( G4_RSRP < -110 )); then
                warn "4G signal is weak: RSRP ${G4_RSRP} dBm ($G4_NET, ${G4_BAND:-?}, ${G4_PPP:-?})"
                echo "       A USB extension cable to move the stick away from the Pi / toward a window usually helps."
            else
                pass "4G radio: ${G4_PPP:-?}, ${G4_OP:-?} $G4_NET ${G4_BAND:-}, RSRP ${G4_RSRP:-?} dBm"
            fi
        fi
    fi

    # Metric priority: the 4G link must win the default route whenever it
    # actually has internet — otherwise the frame silently runs on Wi-Fi
    # during testing at home and nobody notices it's misconfigured until
    # it's sitting at the deployment site with no Wi-Fi to fall back to.
    # This is not hypothetical: it happened for real on this exact device —
    # both links sat at metric 100 because the dongle's NetworkManager
    # profile never got Step 8c's override (it was plugged in fresh, after
    # the profile already existed with no metric set), and the kernel's
    # tie-break picked Wi-Fi instead.
    ACTUAL_DEV=$(ip route get 8.8.8.8 2>/dev/null | grep -oE ' dev [^ ]+' | awk '{print $2}')
    if [[ "$G4_HAS_INTERNET" -eq 1 && "$ACTUAL_DEV" != "$G4_DEV" ]]; then
        fail "4G link ($G4_DEV) has internet but the default route uses $ACTUAL_DEV instead — check its route-metric"
        echo "       Fix: sudo nmcli connection modify <dongle-connection-name> ipv4.route-metric 100"
        echo "       (nmcli -t -f DEVICE,TYPE,CONNECTION device status  finds the connection name)"
    elif [[ "$G4_HAS_INTERNET" -eq 1 ]]; then
        pass "Default route correctly uses the 4G link ($G4_DEV) as primary"
    elif [[ -n "$ACTUAL_DEV" ]]; then
        pass "4G has no internet right now — correctly fell back to $ACTUAL_DEV"
    fi
fi

# The dongle's dedicated profile (setup.sh Step 8c): matched by driver and
# retrying forever — it's the only uplink at the site, NM must never give up.
G4_RETRIES=$(nmcli -g connection.autoconnect-retries connection show 4g-dongle 2>/dev/null)
if [[ "$G4_RETRIES" =~ ^0 ]]; then
    pass "4G profile '4g-dongle' present (matched by driver, reconnects forever)"
elif [[ -n "$G4_RETRIES" ]]; then
    warn "4G profile '4g-dongle' gives up after $G4_RETRIES attempts — should retry forever"
    echo "       Fix: re-run setup.sh, or: sudo nmcli connection modify 4g-dongle connection.autoconnect-retries 0"
else
    warn "No '4g-dongle' connection profile — the dongle depends on whichever generic profile grabs it"
    echo "       Fix: re-run setup.sh"
fi
# Catch-all ethernet profiles (no interface / match rule) left on autoconnect
# can grab the dongle — or the Pi's own port — ahead of the dedicated ones.
while IFS=: read -r uuid typ; do
    [[ "$typ" == "802-3-ethernet" ]] || continue
    cname=$(nmcli -g connection.id connection show "$uuid" 2>/dev/null)
    [[ "$cname" == "4g-dongle" || "$cname" == "eth0-physical" ]] && continue
    bound=$(nmcli -g connection.interface-name,match.interface-name,match.driver,match.path \
                connection show "$uuid" 2>/dev/null | tr -d '[:space:]')
    if [[ -z "$bound" && "$(nmcli -g connection.autoconnect connection show "$uuid" 2>/dev/null)" == "yes" ]]; then
        warn "Catch-all ethernet profile '$cname' still autoconnects — it can steal the dongle from '4g-dongle'"
        echo "       Fix: re-run setup.sh, or: sudo nmcli connection modify '$cname' connection.autoconnect no"
    fi
done < <(nmcli -t -f UUID,TYPE connection show 2>/dev/null)

# Connectivity checking: without it a stick with no LTE (but a fine USB link)
# keeps the default route forever and Wi-Fi never takes over.
if NetworkManager --print-config 2>/dev/null | sed -n '/^\[connectivity\]/,/^\[/p' | grep -q '^uri='; then
    pass "NetworkManager connectivity check enabled (Wi-Fi takes over from a 4G stick with no LTE)"
else
    warn "NetworkManager connectivity check disabled — a dongle with no LTE keeps the default route"
    echo "       Fix: re-run setup.sh (installs /etc/NetworkManager/conf.d/20-connectivity-check.conf)"
fi

# Wired emergency access: a dedicated profile for the Pi's own physical
# Ethernet port (not the 4G dongle, which also enumerates as an ethernet
# device and already has the generic wildcard profile). Without this,
# plugging a cable in does nothing — there is nothing configured to claim it.
if nmcli -t -f NAME connection show 2>/dev/null | grep -qx "eth0-physical"; then
    pass "Wired fallback (eth0-physical) configured — a cable into the Pi's Ethernet port gets a DHCP lease automatically"
else
    warn "No 'eth0-physical' connection profile — plugging a cable into the Pi's Ethernet port won't get you in"
    echo "       Fix: re-run setup.sh"
fi

# Tailscale — self-heal only if we have passwordless sudo (firstboot runs as 'av')
if command -v tailscale &>/dev/null; then
    TS_IP=$(tailscale ip -4 2>/dev/null || echo "")
    if [[ -n "$TS_IP" ]]; then
        pass "Tailscale connected — $TS_IP"
        if tailscale debug prefs 2>/dev/null | grep -q '"CorpDNS": true'; then
            warn "Tailscale MagicDNS (accept-dns) is ON — resolv.conf breaks if tailscaled stops"
            echo "       Fix: sudo tailscale set --accept-dns=false"
        fi
    else
        warn "Tailscale not connected — attempting reconnect..."
        sudo -n systemctl restart tailscaled 2>/dev/null || true
        sleep 5
        TS_IP=$(tailscale ip -4 2>/dev/null || echo "")
        if [[ -n "$TS_IP" ]]; then
            pass "Tailscale reconnected after restart — $TS_IP"
        else
            fail "Tailscale not connected after recovery attempt"
            echo "       Fix: sudo tailscale up --authkey=<key> --hostname=amonavision --ssh --accept-dns=false"
        fi
    fi
else
    fail "Tailscale not installed"
fi

# Samba
if systemctl is-active smbd &>/dev/null; then
    LOCAL_IP=$(ip -4 addr show | grep -oP '(?<=inet )(?!127)\S+' | head -1 | cut -d/ -f1)
    pass "Samba running — share at \\\\${LOCAL_IP}\\RaspberryPi"
else
    fail "Samba not running"
fi

# ═══════════════════════════════════════════════════════════════════════════════
hdr "APPLICATION"
# ═══════════════════════════════════════════════════════════════════════════════

# .env
if [[ -f "$APP_DIR/.env" ]]; then
    HAS_TOKEN=$(grep -c "TELEGRAM_BOT_TOKEN=" "$APP_DIR/.env" 2>/dev/null | tr -d '[:space:]' || echo 0)
    HAS_CHAT=$(grep -c "TELEGRAM_GROUP_CHAT_ID=" "$APP_DIR/.env" 2>/dev/null | tr -d '[:space:]' || echo 0)
    if [[ "${HAS_TOKEN:-0}" -gt 0 ]]; then
        pass ".env has TELEGRAM_BOT_TOKEN"
    else
        fail ".env missing TELEGRAM_BOT_TOKEN — bot cannot start"
    fi
    if [[ "${HAS_CHAT:-0}" -gt 0 ]]; then
        pass ".env has TELEGRAM_GROUP_CHAT_ID"
    else
        warn ".env missing TELEGRAM_GROUP_CHAT_ID — voice reply fallback won't work"
        echo "       Fix: add TELEGRAM_GROUP_CHAT_ID=<id> to $APP_DIR/.env"
        echo "       Get it: send a message to the group, check /status response or app.log"
    fi
else
    fail ".env not found — app cannot start"
    echo "       Fix: create $APP_DIR/.env with bot token and chat ID"
fi

# Python venv
if [[ -d "$APP_DIR/venv" ]]; then
    pass "Python venv exists"
else
    fail "Python venv not found at $APP_DIR/venv"
fi

# Key Python packages
# Run the venv python directly when we're already $APP_USER (e.g. invoked by the
# LAN dashboard, which runs as av with NoNewPrivileges=yes — sudo can't work
# there and every import would falsely report as missing). Only drop privileges
# with sudo when running as root (setup.sh).
PACKAGES="telegram pygame ffpyplayer cv2 dotenv sounddevice imageio_ffmpeg"
if [[ "$(id -un)" == "$APP_USER" ]]; then
    PYPROBE=("$VENV")
else
    PYPROBE=(sudo -u "$APP_USER" "$VENV")
fi
MISSING=()
for pkg in $PACKAGES; do
    if ! "${PYPROBE[@]}" -c "import $pkg" 2>/dev/null; then
        MISSING+=("$pkg")
    fi
done
if [[ ${#MISSING[@]} -eq 0 ]]; then
    pass "All key Python packages importable"
else
    fail "Missing Python packages: ${MISSING[*]}"
    echo "       Fix: $APP_DIR/venv/bin/pip install -r $APP_DIR/requirements.txt"
fi

# Media directories
for d in received processed recordings; do
    if [[ -d "$APP_DIR/app/media/$d" ]]; then
        pass "Media dir: app/media/$d"
    else
        warn "Media dir missing: app/media/$d (created automatically on first run)"
    fi
done

# launch.sh executable (amonavision.service now runs it via `bash`, so this is
# hygiene, not fatal — but a missing +x has bitten a deploy before)
if [[ -x "$APP_DIR/launch.sh" ]]; then
    pass "launch.sh is executable"
else
    warn "launch.sh is not executable — chmod +x $APP_DIR/launch.sh"
fi

# Database directory writable
if [[ -w "$APP_DIR/app/db" ]]; then
    pass "Database directory is writable"
else
    fail "Database directory not writable — app cannot create amonavision.db"
fi

# ═══════════════════════════════════════════════════════════════════════════════
hdr "RESULTS"
# ═══════════════════════════════════════════════════════════════════════════════

TOTAL=$(( PASS + FAIL + WARN ))
echo ""
echo "  Total tests : $TOTAL"
echo -e "  ${GREEN}Passed${NC}      : $PASS"
[[ $WARN -gt 0 ]] && echo -e "  ${YELLOW}Warnings${NC}    : $WARN"
[[ $FAIL -gt 0 ]] && echo -e "  ${RED}Failed${NC}      : $FAIL"
echo ""

if [[ $FAIL -eq 0 ]] && [[ $WARN -eq 0 ]]; then
    echo -e "  ${GREEN}All tests passed — installation looks good.${NC}"
elif [[ $FAIL -eq 0 ]]; then
    echo -e "  ${YELLOW}No failures, but $WARN warning(s) worth checking.${NC}"
else
    echo -e "  ${RED}$FAIL test(s) failed — fix the issues above before deploying.${NC}"
fi

echo ""
echo "================================================================"

# Always clear the firstboot flag — regardless of pass/fail — so it doesn't
# re-run on every subsequent reboot. ExecStartPost in firstboot.service only
# runs on exit 0, so we do it here where it always executes.
rm -f /var/lib/amonavision/.firstboot-pending 2>/dev/null || true

[[ $FAIL -eq 0 ]] && exit 0 || exit 1
