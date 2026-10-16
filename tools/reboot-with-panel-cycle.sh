#!/usr/bin/env bash
# AmonaVision — /reboot: full Pi reboot followed by a panel power-cycle.
#
# A software reboot keeps the Pi's USB powered, so the panel (powered from it)
# never resets and can come back on "No signal" (2026-10-07); 2026-10-08 it
# also did after a cold start. So once the frame is up after ANY boot, USB
# power is cut for 10 s (tools/panel-power-cycle.sh), which brings the panel
# up again on a signal that is already running, like re-plugging it would.
# /reboot leaves a marker only so the Telegram confirmation says why.
#
#   request     (amonavision-reboot.service, via amonavision-reboot.path when
#                the bot drops REBOOT_FLAG): set the marker and reboot.
#   after-boot  (amonavision-reboot-panel-cycle.service, every boot): consume
#                the marker if any, let the viewer come up, power-cycle.
#
# Revert: git revert the commit that added this file, then on the Pi
#   sudo systemctl disable --now amonavision-reboot.path amonavision-reboot-panel-cycle.service

set -uo pipefail

APP_DIR=/home/av/AmonaVision
REBOOT_FLAG=/tmp/amonavision_reboot_request   # written by the bot (/reboot)
MARKER=/var/lib/amonavision/panel-cycle-on-boot
MIN_UPTIME_S=180

log() { logger -t amonavision-reboot "$*"; echo "$*"; }

case "${1:-request}" in
request)
    rm -f "$REBOOT_FLAG"   # consume first: a leftover flag would re-trigger the .path unit
    # Never reboot a Pi that has only just booted: guards against a reboot loop
    # (a /reboot replayed after boot, a flag that survived somehow).
    if (( $(cut -d. -f1 /proc/uptime) < MIN_UPTIME_S )); then
        log "/reboot ignored: up less than ${MIN_UPTIME_S}s"
        exit 0
    fi
    mkdir -p "$(dirname "$MARKER")"
    touch "$MARKER"
    sync
    log "/reboot: rebooting; the panel will be power-cycled once the frame is back up"
    systemctl reboot
    ;;
after-boot)
    if [[ -f "$MARKER" ]]; then
        rm -f "$MARKER"
        why="/reboot"
        msg="AmonaVision: Raspberry Pi rebooted (/reboot) and screen restarted (USB power cut for 10 s). If there is still no picture, unplug the frame for a few minutes."
    else
        why="boot"
        msg="AmonaVision: frame powered on and screen restarted (USB power cut for 10 s, as on every boot). If there is still no picture, unplug the frame for a few minutes."
    fi
    # launch.sh holds the viewer until 20 s of uptime; let it take the display first.
    while (( $(cut -d. -f1 /proc/uptime) < 45 )); do sleep 5; done
    log "after $why: power-cycling the panel"
    exec /usr/bin/bash "$APP_DIR/tools/panel-power-cycle.sh" "$msg"
    ;;
*)
    echo "usage: $0 [request|after-boot]" >&2
    exit 2
    ;;
esac
