#!/usr/bin/env bash
# AmonaVision — power-cycle the panel through the Pi's USB ports (/screen).
#
# The panel (MPI7002, touch controller QDtech 0484:5750) is powered from the
# Pi's USB, which stays powered across a software reboot. 2026-10-07 a plain
# `reboot` left it on "No signal" while the Pi side was fine (connector on,
# CRTC active, viewer DRM master); neither tools/hdmi-resync.py nor anything
# else short of unplugging the frame brought the picture back. Cutting USB
# power with uhubctl turns the panel fully off (tested the same day), so this
# is the remote equivalent of that unplug.
#
# The Pi 4's USB power is ganged: all four ports go off together, so the USB
# mic and the 4G dongle (= the network at the deployment site) drop too, for
# ~1 min. Hence: a safety timer that switches power back on even if this
# script dies, and a Telegram confirmation once the network is back.
#
# Triggered by the bot's /screen command (runs as av, can't touch USB power)
# through amonavision-panel-reset.path watching PANEL_FLAG; runs as root via
# amonavision-panel-reset.service. Manual run: sudo bash tools/panel-power-cycle.sh
# Also run once after a /reboot (tools/reboot-with-panel-cycle.sh); an optional
# first argument replaces the Telegram confirmation text.

set -uo pipefail

APP_DIR=/home/av/AmonaVision
ENV_FILE="$APP_DIR/.env"
PANEL_FLAG=/tmp/amonavision_panel_reset      # written by the bot (/screen)
RESTART_FLAG=/tmp/amonavision_restart        # viewer's soft-restart flag (/restart)
RESYNC_MARK=/run/user/1000/amona-hdmi-resynced
OFF_SECONDS=10

log() { logger -t amonavision-panel-reset "$*"; echo "$*"; }

send_admin_alert() {   # same as tools/bot-watchdog.sh: best-effort
    [[ -f "$ENV_FILE" ]] || return 0
    local token chat_id
    token=$(grep -m1 '^TELEGRAM_BOT_TOKEN=' "$ENV_FILE" | cut -d= -f2- | tr -d '\r\n')
    chat_id=$(grep -m1 '^TELEGRAM_ADMIN_CHAT_ID=' "$ENV_FILE" | cut -d= -f2- | tr -d '\r\n')
    [[ -n "$token" && -n "$chat_id" ]] || return 0
    curl -sS -m 15 "https://api.telegram.org/bot${token}/sendMessage" \
        --data-urlencode "chat_id=${chat_id}" \
        --data-urlencode "text=$1" >/dev/null 2>&1
}

# Consume the request first: a flag left behind would re-trigger the .path unit.
rm -f "$PANEL_FLAG"

exec 9>/run/amonavision-panel-reset.lock
flock -n 9 || { log "already running -- ignoring this request"; exit 0; }

command -v uhubctl &>/dev/null || { log "uhubctl not installed -- cannot power-cycle the panel"; exit 1; }

# Safety net: whatever happens to this script, USB power comes back in 60 s.
systemd-run --quiet --on-active=60 --unit="amonavision-usb-safety-$(date +%s)" \
    /bin/sh -c 'uhubctl -l 2 -a on; uhubctl -l 1-1 -a on' >/dev/null 2>&1

log "cutting USB power (panel, mic, 4G dongle) for ${OFF_SECONDS}s"
uhubctl -l 1-1 -a off >/dev/null
uhubctl -l 2 -a off >/dev/null
sleep "$OFF_SECONDS"
uhubctl -l 2 -a on >/dev/null
uhubctl -l 1-1 -a on >/dev/null
log "USB power back on"

# Let the panel boot, then restart the viewer with the resync marker gone, so
# launch.sh re-presents the HDMI signal to it exactly as on a cold boot. The
# flag must belong to av: the viewer (av) has to delete it from sticky /tmp.
sleep 15
rm -f "$RESYNC_MARK" "$RESTART_FLAG"
runuser -u av -- touch "$RESTART_FLAG"
log "viewer restart requested (HDMI re-presented on start)"

# Confirm on Telegram once the 4G is back (up to ~3 min).
for _ in $(seq 1 36); do
    if python3 -c "import socket; socket.create_connection(('1.1.1.1',443),3)" 2>/dev/null; then
        send_admin_alert "${1:-AmonaVision: screen restarted (USB power cut for ${OFF_SECONDS} s). If there is still no picture, unplug the frame for a few minutes.}"
        exit 0
    fi
    sleep 5
done
log "network not back after 3 min -- no Telegram confirmation (the watchdog re-plugs a stuck 4G dongle)"
