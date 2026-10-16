#!/usr/bin/env bash
# AmonaVision — HDMI / display diagnostics. Read-only: changes nothing on the
# system, only writes its own report to /home/av/hdmi-diag/ (on the SD card,
# NOT on log2ram's RAM-backed /var/log, so reports survive a power cut).
#
# Run on the Pi:      bash /home/av/AmonaVision/tools/hdmi-diag.sh
# Run remotely:       ssh av@<tailscale-ip> 'bash -s' < tools/hdmi-diag.sh
# dmesg, the debugfs mode and other units' journal need root: they use
# `sudo -n` when it works without a password and are marked "(needs root)"
# otherwise. To include them remotely, cache sudo in the same session first:
#   ssh av@<tailscale-ip> 'sudo -S -v 2>/dev/null <<< "<password>"; bash -s' < tools/hdmi-diag.sh

set -uo pipefail
OUT_DIR=/home/av/hdmi-diag
mkdir -p "$OUT_DIR"
OUT="$OUT_DIR/hdmi-diag-$(date +%Y%m%d-%H%M%S).txt"

# Run as root when sudo works without a prompt, otherwise as ourselves.
R() { if sudo -n true 2>/dev/null; then sudo -n "$@"; else "$@"; fi; }
ROOT=no; sudo -n true 2>/dev/null && ROOT=yes

{
echo "=== AmonaVision HDMI diagnostics ==="
echo "host=$(hostname)  now=$(date '+%F %T')  up_since=$(uptime -s)  kernel=$(uname -r)  root=$ROOT"
echo "boot_id=$(cat /proc/sys/kernel/random/boot_id)"
if [[ -f /proc/device-tree/chosen/bootloader/rsts ]]; then
    RSTS=$(od -An -tx4 --endian=big /proc/device-tree/chosen/bootloader/rsts | tr -d ' ')
    case "$RSTS" in
        00001000) WHY="power-on (power cut / re-plug)" ;;
        00000020) WHY="watchdog/software reboot" ;;
        *)        WHY="see PM_RSTS docs" ;;
    esac
    echo "last reset: rsts=0x$RSTS -> $WHY"
fi

echo; echo "=== kernel cmdline (display params) ==="
tr ' ' '\n' < /proc/cmdline | grep -E '^(video|drm\.edid_firmware|consoleblank)=' || echo "(none)"

echo; echo "=== DRM connectors ==="
ls /sys/class/drm/
for c in /sys/class/drm/card*-HDMI-A-*; do
    [[ -d "$c" ]] || continue
    n=${c##*/}
    size=$(wc -c < "$c/edid" 2>/dev/null || echo 0)
    echo "--- $n: status=$(cat "$c/status") enabled=$(cat "$c/enabled") dpms=$(cat "$c/dpms" 2>/dev/null) edid_bytes=$size"
    [[ "$size" -gt 0 ]] && echo "    edid_sha256=$(sha256sum < "$c/edid" | cut -d' ' -f1)"
    echo "    modes: $(tr '\n' ' ' < "$c/modes")"
done
[[ -f /lib/firmware/edid/amona.bin ]] && echo "EDID override file: $(sha256sum /lib/firmware/edid/amona.bin | cut -d' ' -f1)  /lib/firmware/edid/amona.bin"
echo "mode driven by the CRTC:"
R cat /sys/kernel/debug/dri/1/state 2>/dev/null | grep -E '^\s+mode:' | sed 's/^/  /' || true
[[ $ROOT == no ]] && echo "  (needs root)"
echo "HDMI audio ELD:"
for e in /proc/asound/card*/eld*; do [[ -f "$e" ]] && echo "  $e: $(grep -E 'monitor_name|connection_type' "$e" | awk '{print $2}' | tr '\n' ' ')"; done

echo; echo "=== dmesg: hdmi / vc4 / drm / edid ==="
if D=$(R dmesg 2>/dev/null); then grep -iE 'hdmi|vc4|drm|edid' <<<"$D" | tail -40; else echo "(needs root)"; fi

echo; echo "=== power / temperature ==="
echo "$(vcgencmd get_throttled 2>/dev/null)  $(vcgencmd measure_temp 2>/dev/null)"

echo; echo "=== amonavision.service ==="
systemctl show amonavision -p ActiveState -p SubState -p NRestarts -p ActiveEnterTimestamp -p Result | sed 's/^/  /'

echo; echo "=== previous boots: display / app failures ==="
if B=$(R journalctl --list-boots --no-pager 2>/dev/null); then
    echo "$B" | tail -10
    echo "(boot start times are the stale fake-hwclock time until NTP syncs — see boot_id, not the date)"
    printf '  %-4s %-8s %-8s %-10s %-9s %-11s %s\n' idx starts limit display_err kmsdrm edid_fail last_entry
    while read -r idx id _; do
        [[ "$idx" =~ ^-?[0-9]+$ ]] || continue
        J=$(R journalctl -b "$id" --no-pager -o short-iso -u amonavision 2>/dev/null)
        K=$(R journalctl -b "$id" --no-pager -k 2>/dev/null)
        last=$(R journalctl -b "$id" --no-pager -o short-iso -n 1 2>/dev/null | tail -1 | cut -d' ' -f1)
        printf '  %-4s %-8s %-8s %-10s %-9s %-11s %s\n' "$idx" \
            "$(grep -c 'Started amonavision.service' <<<"$J")" \
            "$(grep -ciE 'start request repeated|start-limit' <<<"$J")" \
            "$(grep -c 'Display init failed' <<<"$J")" \
            "$(grep -ciE 'kmsdrm' <<<"$J")" \
            "$(grep -c 'Requesting EDID firmware' <<<"$K")" \
            "$last"
    done <<<"$(echo "$B" | tail -10)"
    echo "--- display/SDL errors in the last 5 boots (newest last) ---"
    for i in -4 -3 -2 -1 0; do
        R journalctl -b "$i" --no-pager -o short-iso -u amonavision 2>/dev/null \
            | grep -iE 'Display init failed|kmsdrm|video system|No available video device|Could not initialize' | tail -3
    done
else
    echo "(needs root)"
fi

echo; echo "=== log persistence ==="
echo "log2ram: enabled=$(systemctl is-enabled log2ram 2>/dev/null) active=$(systemctl is-active log2ram 2>/dev/null)  /var/log on: $(findmnt -no FSTYPE -T /var/log)"
echo "this report: $OUT (on $(findmnt -no FSTYPE -T "$OUT_DIR"))"
} 2>&1 | tee "$OUT"
