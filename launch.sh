#!/bin/bash

# Always run from the project directory, wherever this is called from
cd "$(dirname "$0")"

source venv/bin/activate

# Re-present the HDMI signal once per boot, before the viewer takes the display.
# The panel sometimes misses a signal that was already running when it finished
# booting and shows "No signal" until the cable is re-plugged; output off/on
# does the same. Wait until 20 s of uptime so the panel (powered from the Pi's
# USB, up ~2 s after the kernel) has surely finished booting. The marker lives
# in /run/user/1000, so it's gone after a reboot but survives service restarts.
RESYNC_MARK="${XDG_RUNTIME_DIR:-/run/user/1000}/amona-hdmi-resynced"
if [[ ! -e "$RESYNC_MARK" ]]; then
    while (( $(cut -d. -f1 /proc/uptime) < 20 )); do sleep 1; done
    python3 tools/hdmi-resync.py HDMI-A-1 3 && touch "$RESYNC_MARK"
fi

# Route audio: HDMI out, USB mic in
export PULSE_RUNTIME_PATH=/run/user/1000/pulse
export PULSE_LATENCY_MSEC=150   # larger PipeWire audio buffer — prevents underruns during video decode

# PipeWire node id of the first node with media.class $1 whose node.name
# matches regex $2 (empty if none). pactl is not installed on this image.
pw_node_id() {
    pw-dump 2>/dev/null | python3 -c '
import json, re, sys
cls, pat = sys.argv[1], re.compile(sys.argv[2])
for o in json.load(sys.stdin):
    p = (o.get("info") or {}).get("props") or {}
    if p.get("media.class") == cls and pat.search(p.get("node.name", "")):
        print(o["id"])
        break
' "$1" "$2" 2>/dev/null
}
HDMI_SINK_RE='^alsa_output\.platform-fef00700\.hdmi\.'   # HDMI0 = HDMI-A-1 = the panel
USB_MIC_RE='^alsa_input\.usb-'

# The HDMI sink can disappear from PipeWire if the display re-reads its EDID
# (e.g. after standby). Give WirePlumber a few seconds at boot, then restart
# it to force re-detection if the sink still isn't there.
for _ in 1 2 3 4 5; do
    [[ -n "$(pw_node_id Audio/Sink "$HDMI_SINK_RE")" ]] && break
    sleep 1
done
for _attempt in 1 2; do
    [[ -n "$(pw_node_id Audio/Sink "$HDMI_SINK_RE")" ]] && break
    echo "HDMI sink not found (attempt $_attempt) — restarting WirePlumber..." >&2
    systemctl --user restart wireplumber
    sleep 4
done

# WirePlumber persists these as the configured defaults across reboots.
SINK_ID=$(pw_node_id Audio/Sink "$HDMI_SINK_RE")
[[ -n "$SINK_ID" ]] && wpctl set-default "$SINK_ID" 2>/dev/null
MIC_ID=$(pw_node_id Audio/Source "$USB_MIC_RE")
[[ -n "$MIC_ID" ]] && wpctl set-default "$MIC_ID" 2>/dev/null

echo "Starting AmonaVision..."
python start.py
