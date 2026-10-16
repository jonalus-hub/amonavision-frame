#!/usr/bin/env bash
# AmonaVision — Raspberry Pi provisioning script
# Usage: sudo bash setup.sh [--tailscale-key=tskey-auth-...]
#
# Idempotent: safe to re-run on an already-configured device.
# All output is mirrored to $APP_DIR/setup.log for post-boot diagnostics.

set -euo pipefail

# ── Helpers ───────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
ok()   { echo -e "${GREEN}[OK]${NC}  $*"; }
step() { echo -e "${YELLOW}[..]${NC}  $*"; }
warn() { echo -e "${RED}[!!]${NC}  $*"; }
hdr()  { echo -e "\n${CYAN}━━━ $* ━━━${NC}"; }

# ── Error trap ────────────────────────────────────────────────────────────────
trap 'echo -e "\n${RED}[FATAL]${NC} Setup failed at line $LINENO — check setup.log" >&2' ERR

# ── Root check ────────────────────────────────────────────────────────────────
[[ $EUID -ne 0 ]] && { warn "Run as root: sudo bash setup.sh"; exit 1; }

# ── Argument parsing ──────────────────────────────────────────────────────────
TAILSCALE_KEY=""
for arg in "$@"; do
    case $arg in --tailscale-key=*) TAILSCALE_KEY="${arg#*=}" ;; esac
done

APP_USER=av
APP_DIR=/home/$APP_USER/AmonaVision
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG="$APP_DIR/setup.log"
TS_IP=""
# DRM connector the panel is plugged into (HDMI0 = HDMI-A-1). Step 3b forces it
# on with the panel's own EDID so a panel that's slow to wake still gets a signal.
HDMI_CONNECTOR=HDMI-A-1

# ── Logging — mirror all output to setup.log ──────────────────────────────────
mkdir -p "$APP_DIR"
exec > >(tee -a "$LOG") 2>&1
echo ""
echo "================================================================"
echo " AmonaVision setup — $(date)"
echo " Log: $LOG"
echo "================================================================"

# ═══════════════════════════════════════════════════════════════════════════════
hdr "PRE-FLIGHT"
# ═══════════════════════════════════════════════════════════════════════════════

# ── Step 0: Disable cloud-init (MUST be first) ────────────────────────────────
# cloud-init is enabled by Raspberry Pi Imager and causes:
#   • 60–120s boot delay waiting for unreachable cloud metadata servers
#   • WiFi configuration conflicts — it overwrites NetworkManager settings
#     mid-boot, leaving wlan0 disconnected after every reboot
# Disabling it here prevents all of the above on every subsequent boot.
#
# SAFETY: on current Raspberry Pi OS the first-boot Wi-Fi credentials from the
# Imager are applied by cloud-init. If we disable it before it has written a
# PERSISTENT NetworkManager profile to /etc/NetworkManager/system-connections/,
# the Pi loses Wi-Fi on the next reboot. So: (1) let any in-progress cloud-init
# run finish, (2) only disable it once a persistent Wi-Fi profile exists.
step "Disabling cloud-init (safely)..."
if command -v cloud-init &>/dev/null; then
    cloud-init status --wait 2>/dev/null || true
fi
NM_WIFI_PROFILE=""
if compgen -G "/etc/NetworkManager/system-connections/*.nmconnection" >/dev/null; then
    NM_WIFI_PROFILE=$(grep -lFx "type=wifi" /etc/NetworkManager/system-connections/*.nmconnection 2>/dev/null | head -1 || true)
fi
if [[ -n "$NM_WIFI_PROFILE" ]] || ! command -v cloud-init &>/dev/null || ip -o link show | grep -qE ' eth[0-9]+:'; then
    touch /etc/cloud/cloud-init.disabled
    systemctl disable cloud-init cloud-init-local cloud-config cloud-final 2>/dev/null || true
    ok "cloud-init disabled — boot delay and WiFi conflicts eliminated."
else
    warn "cloud-init NOT disabled: no persistent Wi-Fi profile found and no wired"
    warn "fallback detected. Disabling now could leave the Pi with no network."
    warn "Fix: connect Ethernet, or run 'sudo nmcli device wifi connect <SSID> password <PSK>'"
    warn "     to create a persistent profile, then re-run setup.sh."
fi

# ── Step 1: Wait for internet ─────────────────────────────────────────────────
# apt, Tailscale install, and pip all need internet.
# On a fresh Pi the network may still be coming up, a 4G SIM can take 1–2 min to
# register, and a USB dongle needs ~5–10s to mode-switch — retry for up to ~2min.
#
# Probe port 443, NOT 53: many mobile/4G APNs block or hijack outbound TCP/53
# (carrier-grade NAT + transparent DNS) while HTTPS works fine, so a TCP/53
# probe reports "no internet" on a perfectly working cellular link.
#
# Do NOT hard-exit on failure: apt/pip may still succeed via a provisioning
# Ethernet/Wi-Fi link, and a transient check failure shouldn't abandon a
# half-configured box. Warn loudly and continue; the later steps surface their
# own errors if the network really is down.
step "Waiting for internet connectivity..."
CONNECTED=false
for i in $(seq 1 40); do
    if python3 -c "import socket; socket.create_connection(('1.1.1.1',443),3)" 2>/dev/null \
    || python3 -c "import socket; socket.create_connection(('8.8.8.8',443),3)" 2>/dev/null; then
        CONNECTED=true
        break
    fi
    echo "  No internet yet — retrying ($i/40)..."
    sleep 3
done
if [[ "$CONNECTED" != "true" ]]; then
    warn "No internet after ~2min. apt / Tailscale / pip steps may fail."
    warn "Continuing anyway — re-run setup.sh once the link is up to finish."
else
    ok "Internet reachable."
fi

# ── Step 1b: Strip CRLF from .env ────────────────────────────────────────────
# A .env edited or copied over from Windows arrives with CRLF endings.
# bash grep -c returns "1\r" on CRLF files, breaking [[ "1\r" -gt 0 ]] checks.
# Strip here — before any script reads .env — so it's clean for all subsequent steps.
if [[ -f "$APP_DIR/.env" ]]; then
    sed -i 's/\r//' "$APP_DIR/.env"
    ok ".env CRLF stripped."
fi

# ── Step 2: Backup boot config ────────────────────────────────────────────────
CONFIG=/boot/firmware/config.txt
BACKUP="${CONFIG}.bak.$(date +%Y%m%d_%H%M%S)"
cp "$CONFIG" "$BACKUP"
ok "config.txt backed up → $BACKUP"

# ═══════════════════════════════════════════════════════════════════════════════
hdr "SYSTEM PACKAGES"
# ═══════════════════════════════════════════════════════════════════════════════

step "Installing system packages..."
# DEBIAN_FRONTEND=noninteractive suppresses interactive debconf prompts
# that block the script in non-TTY environments (SSH, plink, etc.)
DEBIAN_FRONTEND=noninteractive apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y \
    python3-dev python3-pip python3-venv \
    python3-gpiozero python3-lgpio \
    pipewire pipewire-audio pipewire-pulse wireplumber \
    libpulse0 libportaudio2 \
    fontconfig \
    ffmpeg \
    log2ram \
    samba \
    modemmanager usb-modeswitch usb-modeswitch-data libmbim-utils libqmi-utils \
    fake-hwclock rfkill \
    uhubctl
# uhubctl: /screen power-cycles the panel through the Pi's USB ports.
# modemmanager + usb-modeswitch: HiLink/NCM dongles (e.g. ZTE cdc_ether) work
#   without these, but serial/QMI/MBIM dongles need mode-switching + ModemManager
#   to ever come online. Installing them makes the 4G path dongle-agnostic.
# fake-hwclock: the Pi has no RTC. A cold-boot stale clock breaks TLS to
#   api.telegram.org / Tailscale / apt ("certificate is not yet valid") until
#   NTP steps it. (Masked on RPi OS trixie, which uses timesyncd's own clock
#   floor — see Step 8d — but harmless to install for images where it isn't.)
ok "System packages installed."

# ═══════════════════════════════════════════════════════════════════════════════
hdr "HARDWARE CONFIGURATION"
# ═══════════════════════════════════════════════════════════════════════════════

# ── Step 3: HDMI and display configuration ───────────────────────────────────
step "Configuring HDMI output..."
# Display: LCDWiki 10.1" HDMI Display-H (MPI1001), native 1024x600, HDMI input
# + built-in stereo speakers/3.5mm jack. https://www.lcdwiki.com/10.1inch_HDMI_Display-H
#
# ══════════════════════════════════════════════════════════════════════════
# DO NOT "IMPROVE" THIS SECTION WITHOUT RE-READING THIS COMMENT.
# This exact combination (these two bare config.txt keys + the "video="
# kernel parameter in Step 3b below) is the ONLY configuration verified,
# on the real device, to give BOTH correct 1024x600 video AND working HDMI
# audio through the panel's own speakers. It was arrived at after several
# plausible-looking "fixes" each actively broke the display on 2026-09-20:
#   - dtoverlay=vc4-fkms-v3d (switching to fake-KMS): the display's
#     cmdline.txt "video=" mode gets ignored under fkms, silently falling
#     back to 1920x1080 on a 1024x600 panel -> broken image, pageflip
#     errors, and once even an unreachable device requiring a physical
#     power cycle.
#   - hdmi_cvt / hdmi_group / hdmi_mode / hdmi_timings (any firmware-level
#     custom mode/timing override): conflicts with the kernel-level
#     "video=" override already in cmdline.txt -> same breakage as above.
#   - hdmi_drive:1=2 / hdmi_force_hotplug:1=1 / hdmi_force_edid_audio:1=1
#     (port-suffixed forcing, even though the panel is physically on HDMI
#     port 1): caused the device to hang on boot and require a physical
#     power cycle, twice.
# If HDMI audio ever needs revisiting, treat it as a research task on a
# spare Pi/panel first — not a live edit on the family's running frame.
# ══════════════════════════════════════════════════════════════════════════
#
# hdmi_force_hotplug: always output HDMI signal even if Pi thinks the monitor
# is disconnected. Without this, the display flickers/disconnects in a loop.
# hdmi_force_edid_audio: the panel does not advertise audio in its EDID, so
# kmsdrm/ALSA would not expose the HDMI sink at all without this.
# Deliberately bare (no ":0"/":1" port suffix) — this is the verified-working
# form, confirmed via journalctl + wpctl on the live device.
grep -qF "hdmi_force_hotplug" "$CONFIG" || echo "hdmi_force_hotplug=1" >> "$CONFIG"
grep -qF "hdmi_force_edid_audio" "$CONFIG" || echo "hdmi_force_edid_audio=1" >> "$CONFIG"
# Remove any port-suffixed or hdmi_drive lines from a previous (broken) run.
sed -i '/^hdmi_force_hotplug:[01]=1$/d; /^hdmi_force_edid_audio:[01]=1$/d; /^hdmi_drive:[01]=2$/d; /^hdmi_group:[01]=2$/d; /^hdmi_mode:[01]=87$/d; /^hdmi_cvt:[01]=.*$/d' "$CONFIG"
sed -i 's/^dtoverlay=vc4-fkms-v3d$/dtoverlay=vc4-kms-v3d/' "$CONFIG"
# display_auto_detect: disable continuous DSI display scanning — we use HDMI
# only. The Pi-default value (1) causes interference with the HDMI signal.
sed -i 's/^display_auto_detect=.*/display_auto_detect=0/' "$CONFIG"
ok "HDMI force-hotplug + forced EDID audio enabled, DSI auto-detect disabled."

# ── Step 3b: kernel command line hardening ────────────────────────────────────
step "Hardening cmdline.txt..."
CMDLINE=/boot/firmware/cmdline.txt
CMDLINE_BAK="$CMDLINE.bak-$(date +%Y%m%d-%H%M%S)"
# Pin one kernel parameter: nothing to do if $2 is already the only token
# starting with $1. Otherwise back cmdline.txt up (once per run), drop every
# such token and append $2 — never a duplicate, and it stays a single line.
pin_cmdline() {
    if awk -v p="$1" -v t="$2" '{ for (i = 1; i <= NF; i++) if (index($i, p) == 1) { n++; if ($i == t) hit = 1 } }
            END { exit !(n == 1 && hit) }' "$CMDLINE"; then
        return 0
    fi
    [[ -e "$CMDLINE_BAK" ]] || cp -p "$CMDLINE" "$CMDLINE_BAK"
    awk -v p="$1" -v t="$2" '{ for (i = 1; i <= NF; i++) if (index($i, p) != 1) printf "%s ", $i }
        END { printf "%s", t }' "$CMDLINE" > "$CMDLINE.new" && mv -f "$CMDLINE.new" "$CMDLINE"
    ok "  cmdline.txt: set $2 (previous version kept as $CMDLINE_BAK)"
}
# video=...D forces the HDMI connector on even if the panel isn't detected at
# boot, and drm.edid_firmware= hands it the panel's own EDID (Step 3d puts the
# file in the initramfs), so it always comes up in HDMI mode with audio.
# The panel's native 1024x600 timing has an odd hsync start, which the Pi 4
# HDMI block can't produce: the kernel drops it ("User-defined mode not
# supported" in dmesg) and the panel is actually driven at 1920x1080, scaled.
pin_cmdline "video=${HDMI_CONNECTOR}:" "video=${HDMI_CONNECTOR}:1024x600M@60D"
pin_cmdline "drm.edid_firmware=" "drm.edid_firmware=${HDMI_CONNECTOR}:edid/amona.bin"
# usbcore.autosuspend=-1: disable USB autosuspend at kernel level — more
# reliable than the udev rule alone for USB-powered peripherals (displays, hubs)
grep -qF "usbcore.autosuspend" "$CMDLINE" || sed -i 's/$/ usbcore.autosuspend=-1/' "$CMDLINE"
# consoleblank=0: prevent the kernel from blanking the framebuffer display
grep -qF "consoleblank" "$CMDLINE"       || sed -i 's/$/ consoleblank=0/' "$CMDLINE"
# usbhid.quirks=...:0x0004 (HID_QUIRK_IGNORE): the display's built-in USB
# touchscreen (QDtech MPI7003, 0484:5750) fails an SDL evdev name-query ioctl
# on this kernel, which aborts pygame's kmsdrm init entirely with
# "Failed to get evdev touchscreen name". We don't use touch input (GPIO
# buttons handle everything) — telling usbhid to ignore this exact
# vendor:product prevents the kernel from ever binding it, so it's never
# enumerated in the first place.
grep -qF "usbhid.quirks" "$CMDLINE"      || sed -i 's/$/ usbhid.quirks=0x0484:0x5750:0x0004/' "$CMDLINE"
# cgroup_enable=memory cgroup_memory=1: the Pi bootloader injects
# "cgroup_disable=memory" into the kernel command line by default, which makes
# the memory cgroup controller unavailable — every MemoryMax=/MemoryHigh= in
# our systemd units (amonavision.service's OOM protection, amonavision-web's
# 128M isolation cap) is then silently ignored. Re-enabling it here restores
# those limits. Takes effect on the next reboot. CPUQuota/TasksMax work
# regardless (cpu + pids controllers are always on).
grep -qF "cgroup_enable=memory" "$CMDLINE" || sed -i 's/$/ cgroup_enable=memory cgroup_memory=1/' "$CMDLINE"
ok "cmdline.txt: USB autosuspend + blanking off, touchscreen HID ignored, HDMI forced on with panel EDID, memory cgroup enabled."

# ── Step 3c: branded boot splash (Plymouth) ───────────────────────────────────
# cmdline.txt already carries "quiet splash", so Plymouth's "pix" theme runs at
# boot regardless — this just swaps its image for ours. The theme's script
# (pix.script) reads the image's own dimensions and letterboxes/centers it
# against a black canvas automatically, so any size PNG works. Plymouth runs
# from the initramfs, so the new image only shows once the initramfs is
# rebuilt — Step 3d does that.
step "Installing branded boot splash..."
PLY_SPLASH=/usr/share/plymouth/themes/pix/splash.png
if [[ -f "$APP_DIR/assets/logo.png" ]] && [[ -d "$(dirname "$PLY_SPLASH")" ]]; then
    if [[ -f "$PLY_SPLASH" ]] && [[ ! -f "${PLY_SPLASH}.orig" ]]; then
        cp "$PLY_SPLASH" "${PLY_SPLASH}.orig"  # keep the stock raspberry logo, once
    fi
    cp "$APP_DIR/assets/logo.png" "$PLY_SPLASH"
    ok "Boot splash (Plymouth pix theme) set to AmonaVision logo."
else
    warn "assets/logo.png or the pix Plymouth theme not found — boot splash left as-is."
fi

# ── Step 3d: panel EDID in the initramfs ──────────────────────────────────────
# vc4 is loaded from the initramfs, before the root filesystem is mounted, so
# drm.edid_firmware= (Step 3b) only works if the EDID file is inside it.
# Without it every early probe fails ("Requesting EDID firmware ... failed
# (err=-2)") and a panel not detected at boot gets no EDID: DVI mode, no HDMI
# audio. The hook re-adds the file on every update-initramfs, so kernel
# updates keep it.
step "Installing the panel EDID into the initramfs..."
install -D -m 0644 "$SCRIPT_DIR/config/edid/amona.bin" /lib/firmware/edid/amona.bin
install -D -m 0755 "$SCRIPT_DIR/config/initramfs-tools/hooks/amona-edid" /etc/initramfs-tools/hooks/amona-edid
update-initramfs -u -k all
ok "Panel EDID installed and baked into the initramfs (kept on every kernel update)."

# ── Step 4: PWM fan overlay ───────────────────────────────────────────────────
# BCM 18 / physical pin 12 — 4-pin PWM fan at 25 kHz.
# CRITICAL: Verify the .dtbo overlay file exists before adding the directive.
# Adding an overlay that doesn't exist on the current firmware causes an
# unrecoverable boot loop — the kernel panics and the Pi reboots endlessly.
step "Configuring PWM overlay (BCM 18 / physical pin 12)..."
if [[ -f /boot/firmware/overlays/pwm.dtbo ]]; then
    if ! grep -qF "dtoverlay=pwm,pin=18,func=2" "$CONFIG"; then
        echo "dtoverlay=pwm,pin=18,func=2" >> "$CONFIG"
        ok "PWM overlay added to config.txt."
    else
        ok "PWM overlay already present — skipping."
    fi
else
    warn "pwm.dtbo not found in /boot/firmware/overlays — skipping PWM overlay."
    warn "Fan controller (fan_control.py) will fail to open /sys/class/pwm/pwmchip0."
    warn "Check firmware version: sudo rpi-update"
fi

# ── Step 4: Hardware watchdog ─────────────────────────────────────────────────
step "Enabling hardware watchdog..."
grep -qF "dtparam=watchdog=on" "$CONFIG" || echo "dtparam=watchdog=on" >> "$CONFIG"
SYSCONFD=/etc/systemd/system.conf
sed -i '/^#\?RuntimeWatchdogSec=/d' "$SYSCONFD"
sed -i '/^#\?RebootWatchdogSec=/d'  "$SYSCONFD"
{ echo "RuntimeWatchdogSec=15"; echo "RebootWatchdogSec=2min"; } >> "$SYSCONFD"
ok "Watchdog configured (feeds every 15s, reboot timeout 2min)."

# ── Step 5: journald limits + persistence ─────────────────────────────────────
# Storage=persistent + /var/log/journal: without this the journal lives only
# in /run (volatile RAM, separate from log2ram's /var/log tmpfs) and is wiped
# on every reboot -- exactly the evidence needed to diagnose an overnight
# network outage. log2ram already syncs /var/log to /var/hdd.log every 15 min and on
# shutdown (JOURNALD_AWARE=true, ordered Before=systemd-journald.service) so
# putting the journal under /var/log makes it survive reboots for free.
# 192M cap (log2ram SIZE raised to match in Step 6): at the frame's normal
# ~3-5 MB/day log rate that's weeks of history -- enough to still see a
# quiet overnight incident days later. Must stay well under log2ram's SIZE
# since /var/log is a tmpfs.
step "Configuring journald (persistent, 192M cap)..."
mkdir -p /etc/systemd/journald.conf.d
cat > /etc/systemd/journald.conf.d/log2ram.conf << 'EOF'
[Journal]
Storage=persistent
SystemMaxUse=192M
RuntimeMaxUse=192M
EOF
mkdir -p /var/log/journal
chown root:systemd-journal /var/log/journal
chmod 2755 /var/log/journal
systemctl restart systemd-journald
# RPi OS ships its own /usr/lib/systemd/journald.conf.d/40-rpi-volatile-storage.conf
# forcing Storage=volatile; ours wins in the merged config (later filename),
# but the already-running runtime journal doesn't migrate to /var/log/journal
# on its own -- --flush explicitly asks journald to do that now instead of
# only on the next natural restart.
journalctl --flush 2>/dev/null || true
ok "journald limits set (192M cap), persistent across reboots via log2ram."

# ── Step 6: log2ram ───────────────────────────────────────────────────────────
step "Configuring log2ram (RAM-backed logs, SD sync every 15 min)..."
if [[ -f /etc/log2ram.conf ]]; then
    if grep -q "^JOURNALD_AWARE=" /etc/log2ram.conf; then
        sed -i 's/^JOURNALD_AWARE=.*/JOURNALD_AWARE=true/' /etc/log2ram.conf
    else
        echo "JOURNALD_AWARE=true" >> /etc/log2ram.conf
    fi
    # /var/log is this tmpfs -- it must be bigger than the journal cap (192M)
    # plus room for everything else. 300M out of ~1.8G RAM is comfortable.
    sed -i 's/^SIZE=.*/SIZE=300M/' /etc/log2ram.conf
fi
# Sync RAM logs to the SD every 15 min: the frame is often unplugged without a
# clean shutdown, and hourly syncs lost up to an hour of logs on every power
# cut. Only changed blocks are rewritten, so the extra SD wear stays small.
mkdir -p /etc/systemd/system/log2ram-daily.timer.d
cat > /etc/systemd/system/log2ram-daily.timer.d/override.conf << 'EOF'
[Timer]
OnCalendar=
OnCalendar=*:0/15
EOF
ok "log2ram configured."

# ── Step 8: USB autosuspend ───────────────────────────────────────────────────
step "Disabling USB autosuspend (keeps 4G dongle and USB-powered display alive)..."
cat > /etc/udev/rules.d/99-usb-no-autosuspend.rules << 'EOF'
ACTION=="add", SUBSYSTEM=="usb", TEST=="power/autosuspend_delay_ms", ATTR{power/autosuspend_delay_ms}="-1"
ACTION=="add", SUBSYSTEM=="usb", TEST=="power/autosuspend", ATTR{power/autosuspend}="-1"
ACTION=="add", SUBSYSTEM=="usb", TEST=="power/control", ATTR{power/control}="on"
EOF
udevadm control --reload-rules
ok "USB autosuspend disabled (devices, hubs, and root controllers)."

# ── Step 8b: Wi-Fi reliability ───────────────────────────────────────────────
# The Pi's brcmfmac WiFi driver enables power-save by default. On an always-on
# wall-mounted frame that causes intermittent latency/drops and slow reconnect
# after AP roaming or idle. Turn it off three ways:
#   1. a NetworkManager conf.d drop-in (connection default)
#   2. the active Wi-Fi profile's own setting
#   3. a dispatcher that runs `iw ... power_save off` on every wlan up
#      (belt-and-braces: brcmfmac frequently ignores 1 and 2)
#
# CRITICAL: files in /etc/NetworkManager/conf.d/ MUST be pure ASCII / valid
# UTF-8 with no stray bytes — glib's keyfile parser rejects the whole file
# otherwise and NetworkManager then refuses to start (no network at all on the
# next boot). So: write ASCII only, then verify NM can still parse its config
# and roll the file back if not.
step "Disabling Wi-Fi power-save (brcmfmac)..."
mkdir -p /etc/NetworkManager/conf.d
PS_CONF=/etc/NetworkManager/conf.d/00-wifi-powersave-off.conf
printf '%s\n' \
    '# Installed by AmonaVision setup.sh - keep Wi-Fi awake on an always-on frame.' \
    '# 2 = disable powersave, for every Wi-Fi connection present and future.' \
    '[connection]' \
    'wifi.powersave=2' > "$PS_CONF"
# Fail-safe: if the drop-in makes NM unparseable, remove it (dispatcher below
# still handles power-save) rather than leave the box unable to network.
if command -v NetworkManager &>/dev/null && ! NetworkManager --print-config &>/dev/null; then
    warn "conf.d drop-in broke NM config parsing — removing it (dispatcher will cover power-save)."
    rm -f "$PS_CONF"
fi

# Dispatcher: the reliable one. Runs on every interface 'up'.
cat > /etc/NetworkManager/dispatcher.d/50-wifi-powersave-off << 'EOF'
#!/bin/sh
# AmonaVision: force Wi-Fi power-save off whenever a wireless device comes up.
[ "$2" = "up" ] || exit 0
case "$1" in wl*|wlan*) iw dev "$1" set power_save off 2>/dev/null ;; esac
exit 0
EOF
chmod 755 /etc/NetworkManager/dispatcher.d/50-wifi-powersave-off

rfkill unblock wifi 2>/dev/null || true
nmcli radio wifi on 2>/dev/null || true
iw dev wlan0 set power_save off 2>/dev/null || true

# Pin the active Wi-Fi profile as the FALLBACK route + never power-save.
# (Priority reversed from an earlier revision: the deployed unit lives
# somewhere with no Wi-Fi at all — only the 4G dongle — so Wi-Fi is only
# ever useful here for testing/maintenance at a location that has it. See
# Step 8c for the 4G-primary side of this.)
WIFI_CON=$(nmcli -t -f NAME,TYPE connection show --active 2>/dev/null | awk -F: '$2=="802-11-wireless"{print $1; exit}' || true)
if [[ -n "${WIFI_CON:-}" ]]; then
    nmcli connection modify "$WIFI_CON" \
        802-11-wireless.powersave 2 \
        ipv4.route-metric 700 ipv6.route-metric 700 \
        connection.autoconnect yes connection.autoconnect-priority -10 2>/dev/null || true
    ok "Wi-Fi power-save off (conf.d + profile + dispatcher); '$WIFI_CON' pinned as fallback."
else
    ok "Wi-Fi power-save off (conf.d + dispatcher; no active profile to pin yet)."
fi

# ── Step 8c: 4G dongle as the PRIMARY link (Wi-Fi is the fallback) ────────────
# This unit is deployed somewhere with no Wi-Fi — the 4G dongle IS the
# internet connection, not a backup for one. Enforce: metered (it's real
# mobile data), low route-metric, high autoconnect priority — so
# NetworkManager always prefers the dongle over Wi-Fi and only falls back
# to Wi-Fi if the dongle is unplugged / has no signal / SIM has no data.
#
# Testing at a location that DOES have Wi-Fi (e.g. developing at home): this
# is exactly why Wi-Fi is a fallback and not disabled outright — plug into
# Wi-Fi there and it's used automatically whenever the dongle can't reach
# the internet, no manual switching needed either way.
#
# HiLink/ECM/NCM/RNDIS sticks (the deployed ZTE MF833U1 included) are routers
# of their own: they dial LTE themselves (APN + SIM PIN live in the stick's
# web UI) and show up here as a plain USB-Ethernet device. ModemManager never
# sees them as a modem, so generic "gsm connection + ModemManager" 4G guides
# don't apply — the Pi side is just DHCP on that USB-Ethernet link.
#
# Its profile is DEDICATED and matched by USB-modem driver. The netplan
# profile the Imager ships ("netplan-eth0") matches ANY ethernet device and
# was what the dongle ran on until 2026-09-29 — which also meant a cable in
# the Pi's own port at boot could steal it (it outranked eth0-physical),
# leaving the dongle with no profile at all. autoconnect-retries 0 = retry
# forever: this is the only uplink at the site, so NetworkManager must never
# give up on it after a few failed DHCP attempts (e.g. while the stick is
# still booting after a reset). Recovery from the stick itself wedging
# (USB re-plug, redial, reboot via its API) is tools/bot-watchdog.sh's job.
step "Configuring 4G dongle as the primary link..."
systemctl enable --now ModemManager 2>/dev/null || true   # only matters for serial/QMI/MBIM modems (template below)
DONGLE_DRIVERS="cdc_ether,cdc_ncm,rndis_host,huawei_cdc_ncm"   # same list as tools/bot-watchdog.sh
if ! nmcli -t -f NAME connection show 2>/dev/null | grep -qx "4g-dongle"; then
    nmcli connection add type ethernet con-name 4g-dongle match.driver "$DONGLE_DRIVERS" >/dev/null 2>&1 \
        || warn "  Could not create the 4g-dongle connection profile."
fi
# (Re-)apply every run, so an existing profile picks up changes here.
if nmcli connection modify 4g-dongle \
        match.driver "$DONGLE_DRIVERS" \
        ipv4.method auto ipv6.method auto \
        connection.metered yes \
        ipv4.route-metric 100 ipv6.route-metric 100 \
        connection.autoconnect yes connection.autoconnect-priority 20 \
        connection.autoconnect-retries 0 2>/dev/null; then
    ok "  '4g-dongle' → metered primary link (metric 100, priority 20, retries forever), matched by driver."
fi
# Retire catch-all ethernet profiles (no interface name, no match rule) so they
# can't grab the dongle — or the Pi's own port — ahead of the dedicated ones.
# autoconnect off rather than delete: reversible, and the netplan YAML stays intact.
while IFS=: read -r uuid typ; do
    [[ "$typ" == "802-3-ethernet" ]] || continue
    cname=$(nmcli -g connection.id connection show "$uuid" 2>/dev/null || true)
    [[ "$cname" == "4g-dongle" || "$cname" == "eth0-physical" ]] && continue
    bound=$(nmcli -g connection.interface-name,match.interface-name,match.driver,match.path \
                connection show "$uuid" 2>/dev/null | tr -d '[:space:]' || true)
    [[ -z "$bound" ]] || continue
    if [[ "$(nmcli -g connection.autoconnect connection show "$uuid" 2>/dev/null)" == "yes" ]]; then
        nmcli connection modify "$uuid" connection.autoconnect no 2>/dev/null \
            && ok "  '$cname' (matches any ethernet device) → autoconnect off; 4g-dongle / eth0-physical take over."
    fi
done < <(nmcli -t -f UUID,TYPE connection show 2>/dev/null)
# Move a dongle that's plugged in right now onto the new profile (a ~5 s blip).
DONGLE_DEV=""
for n in /sys/class/net/*; do
    drv=$(basename "$(readlink -f "$n/device/driver" 2>/dev/null)" 2>/dev/null || true)
    [[ -n "$drv" && ",$DONGLE_DRIVERS," == *",$drv,"* ]] && { DONGLE_DEV=$(basename "$n"); break; }
done
if [[ -n "$DONGLE_DEV" ]] && [[ "$(nmcli -g GENERAL.CONNECTION device show "$DONGLE_DEV" 2>/dev/null)" != "4g-dongle" ]]; then
    nmcli connection up 4g-dongle ifname "$DONGLE_DEV" >/dev/null 2>&1 \
        && ok "  $DONGLE_DEV now on '4g-dongle'." \
        || warn "  Could not activate 4g-dongle on $DONGLE_DEV — it will on the next replug/reboot."
fi
# Template for serial/QMI/MBIM dongles (ModemManager). Uncomment + set your APN:
#   nmcli connection add type gsm ifname '*' con-name 4g-modem \
#        gsm.apn "<YOUR_APN>" connection.autoconnect yes \
#        connection.autoconnect-priority 20 connection.metered yes \
#        ipv4.route-metric 100
nmcli connection reload 2>/dev/null || true

# Connectivity checking is what makes "Wi-Fi is the fallback" actually true.
# An ECM/HiLink stick keeps its USB link up and hands out a DHCP lease even
# with no LTE at all, so to NetworkManager a dead dongle looked perfectly
# healthy and metric 100 < 700 kept every packet going into it. With a
# per-device check enabled, NetworkManager adds a large penalty to the route
# metric of a device that fails it (including when the stick answers with its
# own "no network" portal page), so a working Wi-Fi takes over by itself and
# hands back once 4G recovers. At the deployment site (no Wi-Fi) it changes
# nothing. 600 s keeps it to ~5 MB/month of mobile data.
# Same ASCII-only + parse-check discipline as the Wi-Fi drop-in (Step 8b).
step "Enabling NetworkManager connectivity checking (makes Wi-Fi fallback real)..."
CONCHECK_CONF=/etc/NetworkManager/conf.d/20-connectivity-check.conf
printf '%s\n' \
    '# Installed by AmonaVision setup.sh - per-device internet check.' \
    '# A device that fails it gets its default-route metric penalised, so a' \
    '# working link (Wi-Fi) takes over from a 4G stick with no LTE.' \
    '[connectivity]' \
    'enabled=true' \
    'uri=http://nmcheck.gnome.org/check_network_status.txt' \
    'response=NetworkManager is online' \
    'interval=600' > "$CONCHECK_CONF"
if command -v NetworkManager &>/dev/null && ! NetworkManager --print-config &>/dev/null; then
    warn "Connectivity drop-in broke NM config parsing — removing it."
    rm -f "$CONCHECK_CONF"
else
    nmcli general reload conf 2>/dev/null || true
    ok "Connectivity check enabled (every 600 s, per device)."
fi

# ── Step 8c-bis: physical Ethernet port as a wired emergency-access fallback ──
# The netplan-rendered profile matching "any ethernet device" above legitimately
# binds to whichever ethernet-type interface NetworkManager finds first — on
# this unit that's the 4G dongle (it enumerates as a USB-Ethernet device too),
# leaving the Pi's own physical Ethernet jack (eth0) with no profile of its own
# at all. Found 2026-09-17 investigating "can I get back in via a cable if the
# dongle stalls": plugging one in did nothing, because nothing was configured
# to claim it. A dedicated profile, matched by exact interface name (not a
# wildcard), fixes that — DHCP, autoconnect, and a route-metric lower than
# both the dongle (100) and Wi-Fi (700) so a cable always wins when physically
# present, the most reliable link available. Purely a maintenance convenience:
# the deployment site has no LAN to plug into, so this only ever matters when
# working on the unit somewhere that does.
step "Configuring the physical Ethernet port as a wired fallback..."
if ! nmcli -t -f NAME connection show 2>/dev/null | grep -qx "eth0-physical"; then
    if nmcli connection add type ethernet con-name eth0-physical ifname eth0 \
        ipv4.method auto ipv4.route-metric 50 \
        connection.autoconnect yes connection.autoconnect-priority 10 2>/dev/null; then
        ok "  'eth0-physical' created — plugging a cable into the Pi's Ethernet port now gets a DHCP lease automatically."
    else
        warn "  Could not create the eth0-physical connection profile."
    fi
else
    ok "  'eth0-physical' already configured."
fi

# ── Step 8d: Clock sanity (no RTC) ───────────────────────────────────────────
# The Pi has no RTC. On a cold boot the clock starts from the last value
# systemd-timesyncd persisted in /var/lib/systemd/timesync/clock and is only
# corrected once NTP is reachable — until then a stale clock breaks TLS to
# api.telegram.org / Tailscale / apt ("certificate is not yet valid").
#   • enable systemd-time-wait-sync so time-sync.target actually gates the units
#     that order after it (firstboot.service does);
#   • bump the persisted clock floor to now so the stale window is seconds;
#   • enable fake-hwclock too where it isn't masked (belt and braces).
# run.py additionally waits (bounded) for a sane clock before the bot polls.
step "Hardening clock / time-sync..."
systemctl enable systemd-time-wait-sync.service 2>/dev/null || true
systemctl enable --now fake-hwclock 2>/dev/null || true   # masked on RPi OS — harmless
fake-hwclock save 2>/dev/null || true
touch -m /var/lib/systemd/timesync/clock 2>/dev/null || true
ok "time-sync.target gated (systemd-time-wait-sync); clock floor advanced."

# ── Step 9: Disable desktop session manager ───────────────────────────────────
step "Disabling lightdm (app uses kmsdrm framebuffer directly)..."
systemctl disable --now lightdm 2>/dev/null && ok "lightdm disabled." || ok "lightdm not installed."

# ── Step 9b: Disable Raspberry Pi Connect ────────────────────────────────────
# The Imager enables it, and its rpi-connect-wayvnc.service then crash-loops
# forever (~every 5s) because it needs a Wayland compositor and this frame
# drives the display via SDL/kmsdrm on the bare framebuffer. Observed on
# the deployed frame: 15000+ restarts, 46k journal lines = 73% of the journal,
# which vacuumed real incident logs (a Tailscale drop) out of history within
# hours. It's also redundant with Tailscale for remote access. Kill the
# whole stack; mask so nothing re-enables it.
step "Disabling Raspberry Pi Connect (crash-loops on a kmsdrm frame; Tailscale covers remote access)..."
RUID=$(id -u "$APP_USER")
sudo -u "$APP_USER" XDG_RUNTIME_DIR=/run/user/$RUID systemctl --user disable --now \
    rpi-connect.service rpi-connect-wayvnc.service rpi-connect-signin.path 2>/dev/null || true
sudo -u "$APP_USER" XDG_RUNTIME_DIR=/run/user/$RUID systemctl --user mask \
    rpi-connect.service rpi-connect-wayvnc.service 2>/dev/null || true
rm -f "/home/$APP_USER/.config/systemd/user/default.target.wants/rpi-connect"*.service \
      "/home/$APP_USER/.config/systemd/user/paths.target.wants/rpi-connect-signin.path"
sudo -u "$APP_USER" XDG_RUNTIME_DIR=/run/user/$RUID systemctl --user daemon-reload 2>/dev/null || true
ok "Raspberry Pi Connect disabled and masked."

# ═══════════════════════════════════════════════════════════════════════════════
hdr "APPLICATION"
# ═══════════════════════════════════════════════════════════════════════════════

# ── Step 9: systemd services ──────────────────────────────────────────────────
step "Installing systemd services..."
# Make sure the project's own scripts are executable (a deploy that lands them
# without +x has 203/EXEC'd amonavision.service into a StartLimit failure).
chmod +x "$APP_DIR"/*.sh "$APP_DIR"/tools/*.sh 2>/dev/null || true
cp "$SCRIPT_DIR/amonavision.service"  /etc/systemd/system/amonavision.service
cp "$SCRIPT_DIR/fan-control.service" /etc/systemd/system/fan-control.service
cp "$SCRIPT_DIR/firstboot.service"   /etc/systemd/system/firstboot.service
systemctl daemon-reload
systemctl enable amonavision.service fan-control.service firstboot.service
ok "Services installed and enabled."

# ── Step 10: User setup ───────────────────────────────────────────────────────
step "Configuring user $APP_USER..."
loginctl enable-linger "$APP_USER"
usermod -aG video "$APP_USER" 2>/dev/null || true
ok "Linger enabled, added to video group."

# ── Step 11: PipeWire user services ──────────────────────────────────────────
step "Enabling PipeWire user services..."
UID_NUM=$(id -u "$APP_USER")
sudo -u "$APP_USER" XDG_RUNTIME_DIR=/run/user/$UID_NUM \
    systemctl --user enable pipewire pipewire-pulse wireplumber
ok "PipeWire user services enabled."

# ── Step 11b: HDMI as default audio output ────────────────────────────────────
# PipeWire defaults to the last-used or alphabetically first sink, which on Pi
# is bcm2835 Headphones (3.5mm jack). This config raises HDMI sink priority so
# WirePlumber always selects it as the default — without this, audio is silent.
step "Setting HDMI as default audio output..."
WPCONF_DIR="/home/$APP_USER/.config/wireplumber/wireplumber.conf.d"
mkdir -p "$WPCONF_DIR"
if [[ -f "$SCRIPT_DIR/config/wireplumber/50-hdmi-default.conf" ]]; then
    cp "$SCRIPT_DIR/config/wireplumber/50-hdmi-default.conf" \
       "$WPCONF_DIR/50-hdmi-default.conf"
    chown -R "$APP_USER:$APP_USER" "/home/$APP_USER/.config"
    ok "HDMI audio config installed — HDMI will be default output after reboot."
else
    warn "config/wireplumber/50-hdmi-default.conf not found — audio may default to headphone jack."
fi

# ── Step 11c: remove the old WiFi/4G dongle dispatcher (superseded) ───────────
# An earlier revision force-disconnected Wi-Fi while the dongle was up (config.txt
# [Network] dongle_only). Removed: this duplicated what route metrics already do
# for free — dongle metric 100 / Wi-Fi metric 700 (Step 8c) means the kernel
# always prefers the 4G dongle (the real connection at the deployment site) and
# falls back to Wi-Fi automatically if it's ever available and the dongle isn't
# working (unplugged, or — via Step 8c's connectivity check — up on USB but
# with no LTE), no dispatcher needed. One less hand-written script touching
# NetworkManager.
# Clean up any copy a previous setup.sh run may have installed.
rm -f /etc/NetworkManager/dispatcher.d/99-amonavision

# ── Step 12: Python venv ──────────────────────────────────────────────────────
step "Setting up Python venv..."
if [[ ! -d "$APP_DIR/venv" ]]; then
    sudo -u "$APP_USER" python3 -m venv --system-site-packages "$APP_DIR/venv"
fi
sed -i 's/^include-system-site-packages = false/include-system-site-packages = true/' \
    "$APP_DIR/venv/pyvenv.cfg" 2>/dev/null || true

step "Installing Python dependencies..."
sudo -u "$APP_USER" "$APP_DIR/venv/bin/pip" install --quiet -r "$APP_DIR/requirements.txt"

# Verify key packages are importable — catches silent pip failures
step "Verifying Python packages..."
if sudo -u "$APP_USER" "$APP_DIR/venv/bin/python3" \
    -c "import telegram, pygame, ffpyplayer, cv2" 2>/dev/null; then
    ok "Python venv ready — all key packages importable."
else
    warn "One or more Python packages failed to import — check pip output above."
fi

# ═══════════════════════════════════════════════════════════════════════════════
hdr "CONNECTIVITY"
# ═══════════════════════════════════════════════════════════════════════════════

# ── Step 13: Samba ────────────────────────────────────────────────────────────
step "Configuring Samba share (\\\\<pi-ip>\\RaspberryPi → /)..."
if ! grep -qF "[RaspberryPi]" /etc/samba/smb.conf; then
    cat >> /etc/samba/smb.conf << 'EOF'

[RaspberryPi]
# Whole filesystem, but as av: Linux permissions still apply (av writes only
# where av can, e.g. /home/av; root-only files stay unreadable).
path = /
browseable = yes
read only = no
create mask = 0664
directory mask = 0775
valid users = av
EOF
fi
SAMBA_PASSWORD=$(grep -m1 '^SAMBA_PASSWORD=' "$APP_DIR/.env" 2>/dev/null | cut -d= -f2- | tr -d '\r\n' || true)
if [[ -n "$SAMBA_PASSWORD" ]]; then
    printf '%s\n%s\n' "$SAMBA_PASSWORD" "$SAMBA_PASSWORD" | smbpasswd -a "$APP_USER" -s
    ok "Samba share ready (user $APP_USER, password from SAMBA_PASSWORD in .env)."
else
    warn "SAMBA_PASSWORD not set in .env — share configured, Samba password for $APP_USER left as-is."
    warn "  Fix: add SAMBA_PASSWORD=<password> to $APP_DIR/.env and re-run setup.sh (or: sudo smbpasswd -a $APP_USER)"
fi
systemctl enable --now smbd nmbd

# ── Step 14: Tailscale ────────────────────────────────────────────────────────
# NOTE: the whole Tailscale block is wrapped so a transient failure (DNS hiccup
# on 4G, repo key fetch timeout, apt error) only skips remote access — it must
# NOT abort setup.sh after cmdline.txt / config.txt / system.conf were already
# modified. `set -e` is disabled here and restored at the end.
set +e
TS_OK=1
step "Installing Tailscale..."
if ! command -v tailscale &>/dev/null; then
    curl -fsSL https://pkgs.tailscale.com/stable/debian/trixie.noarmor.gpg \
        -o /usr/share/keyrings/tailscale-archive-keyring.gpg \
        || { warn "Could not fetch Tailscale repo key — skipping Tailscale."; TS_OK=0; }
    if [[ $TS_OK -eq 1 ]]; then
        echo "deb [signed-by=/usr/share/keyrings/tailscale-archive-keyring.gpg] https://pkgs.tailscale.com/stable/debian trixie main" \
            > /etc/apt/sources.list.d/tailscale.list
        DEBIAN_FRONTEND=noninteractive apt-get update -qq
        DEBIAN_FRONTEND=noninteractive apt-get install -y tailscale \
            || { warn "Tailscale install failed — skipping."; TS_OK=0; }
    fi
fi
if [[ $TS_OK -eq 1 ]] && command -v tailscale &>/dev/null; then
    systemctl enable --now tailscaled
    ok "Tailscale daemon running."

    # --accept-dns=false: do NOT let Tailscale MagicDNS take over /etc/resolv.conf.
    # If it does and tailscaled later stops / the key expires / 100.100.100.100
    # stops answering, the bot can't resolve api.telegram.org even though Wi-Fi
    # and 4G both have full internet. SSH-in still works by 100.x IP.
    # Run unconditionally so re-running setup.sh on an already-linked device
    # (no key passed) still corrects a box that has MagicDNS on.
    tailscale set --accept-dns=false 2>/dev/null || true

    if [[ -n "$TAILSCALE_KEY" ]]; then
        step "Connecting to Tailscale network..."
        tailscale up --authkey="$TAILSCALE_KEY" --hostname=amonavision --ssh --accept-dns=false
        sleep 3
        TS_IP=$(tailscale ip -4 2>/dev/null || echo "")
        if [[ -n "$TS_IP" ]]; then
            ok "Tailscale connected — $TS_IP  (MagicDNS off; SSH via this IP)"
        else
            warn "tailscale up ran but no IP yet — will connect after reboot."
        fi
    else
        warn "No Tailscale key provided — connect after reboot:"
        warn "  sudo tailscale up --authkey=<key> --hostname=amonavision --ssh --accept-dns=false"
        warn "  Generate a key: https://login.tailscale.com/admin/settings/keys"
    fi
else
    warn "Tailscale not configured — device is reachable on the LAN only."
fi
set -e

# ── Step 14b: periodic health check with Telegram alert ──────────────────────
# Unattended device, no physical access after deployment: this is the
# device telling you when something is wrong, instead of you finding out on
# your next visit. tools/health-alert.sh runs test_setup.sh every few hours
# and DMs the admin (TELEGRAM_ADMIN_CHAT_ID — a person, not the family group)
# only on failure/recovery/daily heartbeat — never spams while healthy. Needs
# TELEGRAM_ADMIN_CHAT_ID in .env to actually send (checked in the health check
# below); deliberately separate from TELEGRAM_GROUP_CHAT_ID, which viewer.py
# uses for the voice-reply fallback to the family group — different audience.
step "Installing periodic health-check timer..."
cp "$SCRIPT_DIR/amonavision-healthcheck.service" /etc/systemd/system/amonavision-healthcheck.service
cp "$SCRIPT_DIR/amonavision-healthcheck.timer"   /etc/systemd/system/amonavision-healthcheck.timer
chmod +x "$APP_DIR/tools/health-alert.sh" 2>/dev/null || true
systemctl daemon-reload
systemctl enable --now amonavision-healthcheck.timer
ok "Health-check timer active (first run in 5 min, then every 3h)."

# ── Step 14c: liveness watchdog (bot + Tailscale) ─────────────────────────────
# A wedged-but-alive process needs an OUTSIDE check, not another in-process
# retry — systemd's Restart=always only fires on exit. 2026-09-04/05 incident:
# something (most likely the WiFi driver itself, since both were affected)
# stopped passing traffic; getUpdates polling silently stopped (no crash, no
# error) AND Tailscale dropped off the tailnet, for the same ~10h window, until
# someone rebooted it by hand. This runs every 5 min and checks both
# independently: if the network is reachable but app.log hasn't been written
# to in 10+ min, restart the bot; if Tailscale doesn't report Online, restart
# tailscaled. Each has its own 20-min cooldown to avoid a restart loop.
# It also logs + self-heals the 4G dongle (USB re-plug / redial / dongle
# reboot), which — being the network itself at the site — runs even offline.
step "Installing liveness watchdog (bot + Tailscale + 4G dongle)..."
cp "$SCRIPT_DIR/amonavision-bot-watchdog.service" /etc/systemd/system/amonavision-bot-watchdog.service
cp "$SCRIPT_DIR/amonavision-bot-watchdog.timer"   /etc/systemd/system/amonavision-bot-watchdog.timer
chmod +x "$APP_DIR/tools/bot-watchdog.sh" 2>/dev/null || true
systemctl daemon-reload
systemctl enable --now amonavision-bot-watchdog.timer
ok "Liveness watchdog active (checks every 5 min; self-heals a wedged bot, tailscaled or 4G dongle)."

# ── Step 14c-bis: /screen — remote panel power-cycle ─────────────────────────
# The panel is powered from the Pi's USB, which stays on across a software
# reboot; 2026-10-07 one left it on "No signal" until the frame was unplugged.
# The bot (av) drops a flag, this .path unit runs tools/panel-power-cycle.sh as
# root: USB power off 10 s (panel, mic and 4G dongle -- the Pi 4's ports are
# ganged), back on, viewer restarted so the HDMI signal is re-presented.
step "Installing /screen panel reset (USB power-cycle)..."
cp "$SCRIPT_DIR/amonavision-panel-reset.service" /etc/systemd/system/amonavision-panel-reset.service
cp "$SCRIPT_DIR/amonavision-panel-reset.path"    /etc/systemd/system/amonavision-panel-reset.path
systemctl daemon-reload
systemctl enable --now amonavision-panel-reset.path
ok "/screen panel reset ready (admin chat only)."

# /reboot: same flag + .path pattern (av's sudo wants a password, so the old
# `sudo reboot` never ran). Reboots; amonavision-reboot-panel-cycle.service
# power-cycles the panel once the frame is up after every boot (cold or not).
step "Installing /reboot (reboot + panel power-cycle)..."
cp "$SCRIPT_DIR/amonavision-reboot.service"             /etc/systemd/system/amonavision-reboot.service
cp "$SCRIPT_DIR/amonavision-reboot.path"                /etc/systemd/system/amonavision-reboot.path
cp "$SCRIPT_DIR/amonavision-reboot-panel-cycle.service" /etc/systemd/system/amonavision-reboot-panel-cycle.service
systemctl daemon-reload
systemctl enable --now amonavision-reboot.path
systemctl enable amonavision-reboot-panel-cycle.service
ok "/reboot ready (admin chat only); the panel is power-cycled after every boot."

# ── Step 14d: LAN dashboard ──────────────────────────────────────────────────
# web/app.py behind its own systemd unit, fully isolated from the frame
# (Nice=15, CPUQuota=20%, MemoryMax=128M, OOMPolicy=stop, no ordering vs
# amonavision.service). Read-mostly: only ever writes config.txt and the
# soft-restart flag. Refuses to start unless WEB_PASSWORD is in .env.
step "Installing LAN dashboard (amonavision-web on :8080)..."
cp "$SCRIPT_DIR/amonavision-web.service" /etc/systemd/system/amonavision-web.service
systemctl daemon-reload
if grep -qE '^WEB_PASSWORD=.+' "$APP_DIR/.env" 2>/dev/null; then
    systemctl enable --now amonavision-web.service
    ok "Dashboard active — http://$(hostname -I | awk '{print $1}'):8080"
else
    systemctl enable amonavision-web.service
    warn "WEB_PASSWORD not set in .env — dashboard enabled but NOT started."
    warn "  Add WEB_PASSWORD=<shared-password> to $APP_DIR/.env, then:"
    warn "  sudo systemctl start amonavision-web"
fi

# ═══════════════════════════════════════════════════════════════════════════════
hdr "HEALTH CHECK"
# ═══════════════════════════════════════════════════════════════════════════════

ISSUES=0

# .env
if [[ -f "$APP_DIR/.env" ]]; then
    ok ".env present."
else
    warn ".env MISSING — app will not start without bot token and chat ID."
    (( ISSUES++ )) || true
fi

# Services enabled
for svc in amonavision fan-control smbd tailscaled amonavision-web; do
    if systemctl is-enabled "$svc" &>/dev/null; then
        ok "$svc — enabled."
    else
        warn "$svc — NOT enabled."
        (( ISSUES++ )) || true
    fi
done

# Dashboard responding (only if it was started)
if systemctl is-active amonavision-web &>/dev/null; then
    if curl -sf -m 3 -o /dev/null http://127.0.0.1:8080/healthz; then
        ok "Dashboard responding on :8080."
    else
        warn "amonavision-web active but /healthz did not respond."
        (( ISSUES++ )) || true
    fi
fi

# Bot liveness watchdog
if systemctl is-enabled amonavision-bot-watchdog.timer &>/dev/null; then
    ok "amonavision-bot-watchdog.timer — enabled."
else
    warn "amonavision-bot-watchdog.timer — NOT enabled."
    (( ISSUES++ )) || true
fi

# /screen panel reset
if systemctl is-enabled amonavision-panel-reset.path &>/dev/null && command -v uhubctl &>/dev/null; then
    ok "amonavision-panel-reset.path — enabled (/screen)."
else
    warn "amonavision-panel-reset.path or uhubctl missing — /screen won't do anything."
    (( ISSUES++ )) || true
fi
if systemctl is-enabled amonavision-reboot.path &>/dev/null && systemctl is-enabled amonavision-reboot-panel-cycle.service &>/dev/null; then
    ok "amonavision-reboot.path + amonavision-reboot-panel-cycle.service — enabled (/reboot)."
else
    warn "/reboot units not enabled — /reboot won't do anything."
    (( ISSUES++ )) || true
fi

# Health-check timer + Telegram alert destination
if systemctl is-enabled amonavision-healthcheck.timer &>/dev/null; then
    ok "amonavision-healthcheck.timer — enabled."
else
    warn "amonavision-healthcheck.timer — NOT enabled."
    (( ISSUES++ )) || true
fi
if grep -qE '^TELEGRAM_ADMIN_CHAT_ID=.+' "$APP_DIR/.env" 2>/dev/null; then
    ok "TELEGRAM_ADMIN_CHAT_ID set — health-check alerts will actually send."
else
    warn "TELEGRAM_ADMIN_CHAT_ID missing from .env — health-check timer runs but can't alert."
    warn "  Fix: message the bot privately, check app.log for '... from user id=...' — that IS"
    warn "       your chat id in a 1:1 chat — add TELEGRAM_ADMIN_CHAT_ID=<id> to $APP_DIR/.env"
fi

# cloud-init disabled
if [[ -f /etc/cloud/cloud-init.disabled ]]; then
    ok "cloud-init — disabled."
else
    warn "cloud-init — still active (WiFi and boot delay risk)."
    (( ISSUES++ )) || true
fi

# Wi-Fi power-save off
if [[ -f /etc/NetworkManager/conf.d/00-wifi-powersave-off.conf ]]; then
    ok "Wi-Fi power-save — disabled via NetworkManager drop-in."
else
    warn "Wi-Fi power-save drop-in missing — WiFi may drop intermittently."
    (( ISSUES++ )) || true
fi

# 4G dongle profile + connectivity check (Step 8c)
if [[ "$(nmcli -g connection.autoconnect-retries connection show 4g-dongle 2>/dev/null)" == "0" ]]; then
    ok "4G dongle — dedicated '4g-dongle' profile (matched by driver, retries forever)."
else
    warn "4G dongle — '4g-dongle' profile missing or not set to retry forever."
    (( ISSUES++ )) || true
fi
if [[ -f /etc/NetworkManager/conf.d/20-connectivity-check.conf ]]; then
    ok "Connectivity check — enabled (Wi-Fi takes over from a 4G stick with no LTE)."
else
    warn "Connectivity check drop-in missing — a dongle with no LTE keeps the default route."
    (( ISSUES++ )) || true
fi

# DNS not hijacked by Tailscale MagicDNS
if grep -q "generated by tailscale" /etc/resolv.conf 2>/dev/null; then
    warn "/etc/resolv.conf is owned by Tailscale MagicDNS — DNS dies if tailscaled stops."
    warn "  Fix: sudo tailscale set --accept-dns=false && sudo systemctl restart tailscaled"
    (( ISSUES++ )) || true
else
    ok "DNS — not dependent on Tailscale."
fi

# Clock sync (no RTC — stale clock breaks TLS on cold boot)
if timedatectl show -p NTPSynchronized --value 2>/dev/null | grep -q "yes"; then
    ok "System clock — NTP-synchronised."
else
    warn "System clock not yet NTP-synced — TLS may fail until it is."
fi

# config.txt additions
echo ""
echo "  config.txt additions:"
grep -E "dtoverlay=pwm|dtparam=watchdog" "$CONFIG" \
    && true || echo "  (PWM overlay was skipped — check warn above)"

# Tailscale
[[ -n "$TS_IP" ]] && echo "" && echo "  Tailscale IP: $TS_IP"

# Summary
echo ""
if [[ $ISSUES -eq 0 ]]; then
    echo -e "${GREEN}All checks passed.${NC}"
else
    echo -e "${YELLOW}$ISSUES issue(s) need attention — see warnings above.${NC}"
fi

echo ""
echo "================================================================"
echo " Setup complete — $(date)"
echo " Full log: $LOG"
echo ""
# ── Arm first-boot validator ──────────────────────────────────────────────────
# Creates a flag that firstboot.service checks for on the next reboot.
# firstboot.service runs test_setup.sh after everything has cold-started,
# which is the only honest test of a working installation.
step "Arming first-boot validator..."
mkdir -p /var/lib/amonavision
touch /var/lib/amonavision/.firstboot-pending
# Make setup.log writable by av so firstboot.service (User=av) can append to it
chown "$APP_USER:$APP_USER" "$LOG" 2>/dev/null || true
ok "First-boot validation will run automatically after reboot."
ok "Results: journalctl -u firstboot --no-pager"

step "Running pre-reboot installation tests..."
bash "$SCRIPT_DIR/test_setup.sh" || true
echo ""
echo " Next step:  sudo reboot"
echo " After reboot, first-boot tests run automatically."
[[ -n "$TS_IP" ]] && echo " Then SSH via: ssh av@$TS_IP   (MagicDNS is off; use the IP, or your"
[[ -n "$TS_IP" ]] && echo "               router's own DNS name for the device)"
echo "================================================================"
