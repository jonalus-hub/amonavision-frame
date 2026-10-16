#!/usr/bin/env bash
# AmonaVision — liveness watchdog for the bot and for Tailscale, with escalation.
#
# systemd's Restart=always only helps when a process actually EXITS. It does
# nothing for a process that's still running but wedged — which is what
# happened overnight on 2026-09-04/05: something (most likely the WiFi
# interface/driver itself, not just an app-level socket) stopped passing
# traffic, python-telegram-bot's getUpdates polling silently stopped (no
# error, no crash, nothing for start.py's backoff to react to), and
# separately Tailscale also dropped off the tailnet for the same window. Both
# staying down for ~10h despite each having its own reconnect/retry logic
# points to the network device itself getting stuck, not a momentary blip
# that either client merely failed to recover from. The frame stayed offline
# until someone rebooted it by hand.
#
# Two tiers, per problem, independently:
#   Tier 1 (first sign of trouble): restart the specific stuck service.
#   Tier 2 (still stuck REBOOT_AFTER_MINUTES later despite that): a plain
#     service restart clearly isn't fixing it -- reboot the whole Pi. This is
#     the one action with direct evidence of working (2026-09-05 recovery),
#     so it's the deliberate ceiling: worst case, any hang like this now
#     self-heals within ~REBOOT_AFTER_MINUTES instead of needing a manual
#     reboot, at the cost of a real ~60s reboot if it comes to that.
#
# The 4G dongle gets its own ladder (see its section below): unlike the bot
# and Tailscale, it IS the network at the deployment site, so it has to be
# handled even -- especially -- when there's no internet at all.
#
# Deliberately lighter than tools/health-alert.sh (a handful of quick checks,
# not the full test_setup.sh suite) so it can run every few minutes without
# being expensive. Installed + scheduled by setup.sh via
# amonavision-bot-watchdog.{service,timer}.

set -uo pipefail

APP_DIR=/home/av/AmonaVision
ENV_FILE="$APP_DIR/.env"
APP_LOG="$APP_DIR/app/logs/app.log"
STATE_FILE=/var/lib/amonavision/watchdog_state
BOT_STALE_MINUTES=10
RESTART_COOLDOWN_MINUTES=20   # don't restart the same service again within this long
REBOOT_AFTER_MINUTES=35       # still broken this long after tier-1 -> escalate to reboot
REBOOT_COOLDOWN_MINUTES=120   # never reboot more than once every 2h, however bad things are

mkdir -p /var/lib/amonavision
NOW=$(date +%s)

state_get() {   # state_get <key> -> value (empty if absent)
    [[ -f "$STATE_FILE" ]] && grep -m1 "^$1=" "$STATE_FILE" | cut -d= -f2
}

# Preserve whichever keys aren't touched this run instead of clobbering them.
BOT_LAST_RESTART=$(state_get last_restart_epoch);       : "${BOT_LAST_RESTART:=0}"
TS_LAST_RESTART=$(state_get last_ts_restart_epoch);     : "${TS_LAST_RESTART:=0}"
TS_DOWN_SINCE=$(state_get ts_down_since_epoch);         : "${TS_DOWN_SINCE:=0}"
LAST_REBOOT=$(state_get last_reboot_trigger_epoch);     : "${LAST_REBOOT:=0}"
DONGLE_BAD_SINCE=$(state_get dongle_bad_since_epoch);           : "${DONGLE_BAD_SINCE:=0}"
DONGLE_LAST_REPLUG=$(state_get dongle_last_replug_epoch);       : "${DONGLE_LAST_REPLUG:=0}"
DONGLE_LAST_REDIAL=$(state_get dongle_last_redial_epoch);       : "${DONGLE_LAST_REDIAL:=0}"
DONGLE_LAST_DEVREBOOT=$(state_get dongle_last_devreboot_epoch); : "${DONGLE_LAST_DEVREBOOT:=0}"
DONGLE_PI_REBOOTS=$(state_get dongle_pi_reboots);               : "${DONGLE_PI_REBOOTS:=0}"
DONGLE_ACTIONS=$(state_get dongle_actions)   # comma list of what this outage has tried so far

# No RTC: after a reboot with no NTP (exactly the "4G is dead" case) the clock
# restarts from timesyncd's saved floor, which can be hours or days behind the
# timestamps saved above. A timestamp "in the future" would freeze every
# cooldown/streak until the clock caught up -- clamp them to now instead.
for _v in BOT_LAST_RESTART TS_LAST_RESTART TS_DOWN_SINCE LAST_REBOOT DONGLE_BAD_SINCE \
          DONGLE_LAST_REPLUG DONGLE_LAST_REDIAL DONGLE_LAST_DEVREBOOT; do
    [[ ${!_v} -gt $NOW ]] && printf -v "$_v" '%s' "$NOW"
done

save_state() {
    {
        echo "last_restart_epoch=$BOT_LAST_RESTART"
        echo "last_ts_restart_epoch=$TS_LAST_RESTART"
        echo "ts_down_since_epoch=$TS_DOWN_SINCE"
        echo "last_reboot_trigger_epoch=$LAST_REBOOT"
        echo "dongle_bad_since_epoch=$DONGLE_BAD_SINCE"
        echo "dongle_last_replug_epoch=$DONGLE_LAST_REPLUG"
        echo "dongle_last_redial_epoch=$DONGLE_LAST_REDIAL"
        echo "dongle_last_devreboot_epoch=$DONGLE_LAST_DEVREBOOT"
        echo "dongle_pi_reboots=$DONGLE_PI_REBOOTS"
        echo "dongle_actions=$DONGLE_ACTIONS"
    } > "$STATE_FILE"
}

internet_ok() {   # TCP/443, not 53 -- mobile APNs often block or hijack TCP/53
    python3 -c "import socket; socket.create_connection(('1.1.1.1',443),3)" 2>/dev/null \
    || python3 -c "import socket; socket.create_connection(('8.8.8.8',443),3)" 2>/dev/null
}

send_admin_alert() {   # best-effort; if it fails, the 3h health-check still surfaces things
    [[ -f "$ENV_FILE" ]] || return 0
    local token chat_id
    token=$(grep -m1 '^TELEGRAM_BOT_TOKEN=' "$ENV_FILE" | cut -d= -f2- | tr -d '\r\n')
    chat_id=$(grep -m1 '^TELEGRAM_ADMIN_CHAT_ID=' "$ENV_FILE" | cut -d= -f2- | tr -d '\r\n')
    [[ -n "$token" && -n "$chat_id" ]] || return 0
    curl -sS -m 15 "https://api.telegram.org/bot${token}/sendMessage" \
        --data-urlencode "chat_id=${chat_id}" \
        --data-urlencode "text=$1" >/dev/null 2>&1
}

reboot_cooldown_over() {
    [[ $(( (NOW - LAST_REBOOT) / 60 )) -ge $REBOOT_COOLDOWN_MINUTES ]]
}

# escalate_to_reboot <reason> [what tier 1 tried]: last-resort tier. Persists
# state and alerts BEFORE rebooting (both need to survive the reboot happening
# mid-script).
escalate_to_reboot() {
    local reason="$1" tried="${2:-restarting the service}"
    reboot_cooldown_over || return 0   # already tried this recently -- don't reboot-loop
    logger -t amonavision-watchdog "ESCALATION: $reason still broken ${REBOOT_AFTER_MINUTES}+ min after tier-1 recovery -- rebooting"
    LAST_REBOOT=$NOW
    save_state
    send_admin_alert "AmonaVision: $reason has not recovered for more than ${REBOOT_AFTER_MINUTES} min despite ${tried}. Rebooting the whole Raspberry Pi, then the screen (~3 min)."
    sleep 3   # give the alert a moment to actually leave before the network drops
    # Same as the bot's /reboot: reboot, then power-cycle the USB-powered panel
    # once back up -- a plain reboot can leave it on "No signal" (2026-10-07).
    if [[ -f "$APP_DIR/tools/reboot-with-panel-cycle.sh" ]]; then
        bash "$APP_DIR/tools/reboot-with-panel-cycle.sh" request
    else
        reboot
    fi
    exit 0
}

# ── 4G dongle: health log + self-healing ──────────────────────────────────────
# The deployed stick (ZTE MF833U1) runs in router/ECM mode: it dials LTE by
# itself and shows up as plain USB-Ethernet (cdc_ether -> eth1) with its own
# NAT and a web API at its gateway (192.168.0.1). ModemManager never sees it
# as a modem, so there's no "mobile connection" for NetworkManager to redial.
# The failures it has actually had all happen below NetworkManager:
#   2026-09-17/20  "NETDEV WATCHDOG: transmit queue timed out" clusters -- the
#                  stick stops taking outgoing USB transfers; link stays "up".
#   2026-09-22/23  three outages of 2h to 11.5h. The stick re-enumerated itself
#                  in bursts and the last attempt failed ("cdc_ether probe
#                  failed with error -71" / "can't set config #1, error -71"):
#                  still on the USB bus, no network interface, nothing retried
#                  until someone rebooted the Pi. Home Wi-Fi hid it -- at the
#                  deployment site that's the frame offline indefinitely.
# So every cycle: classify the link, log a sample, and apply the cheapest fix
# for the layer that's broken (each tested live 2026-09-29; time until the
# upstream worked again in brackets):
#   no_iface / no_ip / gw_dead  -> logical unplug/replug of its USB port  [~9s]
#   no_upstream for 10+ min     -> redial via the dongle's API            [~7s]
#   no_upstream for 25+ min     -> reboot the dongle via its API          [~60s]
#   any of these for 35+ min AND the Pi has no internet at all -> reboot the
#     Pi, at most DONGLE_MAX_PI_REBOOTS times per outage: an empty SIM or a
#     carrier outage isn't fixed by rebooting, and a frame that reboots every
#     2h for days is worse than one that just keeps retrying the above.
# Deliberately placed BEFORE the internet-reachability guard below: "no
# internet" is exactly the case this exists for, and a mid-outage log sample
# is what we most want to keep.
DONGLE_LOG="$APP_DIR/app/logs/dongle.log"
DONGLE_DRIVERS="cdc_ether cdc_ncm rndis_host huawei_cdc_ncm"   # same list as setup.sh's 4g-dongle profile
DONGLE_USB_VENDORS="19d2 12d1"   # ZTE, Huawei: finds the stick on the bus when it has NO interface
DONGLE_REPLUG_COOLDOWN_MINUTES=10
DONGLE_REDIAL_AFTER_MINUTES=10
DONGLE_REDIAL_COOLDOWN_MINUTES=30
DONGLE_DEVREBOOT_AFTER_MINUTES=25
DONGLE_DEVREBOOT_COOLDOWN_MINUTES=60
DONGLE_MAX_PI_REBOOTS=2

dongle_iface() {   # by USB-modem driver, not NM state: must still find it after NM gave up on it
    local n drv
    for n in /sys/class/net/*; do
        drv=$(basename "$(readlink -f "$n/device/driver" 2>/dev/null)" 2>/dev/null)
        [[ -n "$drv" && " $DONGLE_DRIVERS " == *" $drv "* ]] && { basename "$n"; return 0; }
    done
    ip -o link show 2>/dev/null | grep -oE '(wwan[0-9]+|ppp[0-9]+)' | head -1
}

dongle_usb() {   # dongle_usb [iface] -> sysfs name of the stick's USB device (e.g. 1-1.3), or empty
    local d
    if [[ -n "${1:-}" && -e "/sys/class/net/$1/device" ]]; then
        d=$(dirname "$(readlink -f "/sys/class/net/$1/device")")   # .../1-1.3/1-1.3:1.0 -> .../1-1.3
        [[ -f "$d/idVendor" ]] && { basename "$d"; return 0; }
    fi
    for d in /sys/bus/usb/devices/*; do
        [[ -f "$d/idVendor" && " $DONGLE_USB_VENDORS " == *" $(cat "$d/idVendor") "* ]] && { basename "$d"; return 0; }
    done
}

dongle_replug() {   # dongle_replug <usbdev>: logical unplug/replug of its port -> fresh enumeration + driver probe
    local usb="$1" port i
    port=$(readlink -f "/sys/bus/usb/devices/$usb/port" 2>/dev/null)
    if [[ -n "$port" && -w "$port/disable" ]]; then
        echo 1 > "$port/disable" || return 1
        sleep 3
        # Re-enabling is what brings it back -- never leave the port disabled.
        for i in 1 2 3 4 5; do echo 0 > "$port/disable" 2>/dev/null && return 0; sleep 2; done
        return 1
    fi
    [[ -w "/sys/bus/usb/devices/$usb/authorized" ]] || return 1   # no port control: deauthorize/reauthorize
    echo 0 > "/sys/bus/usb/devices/$usb/authorized" || return 1
    sleep 3
    for i in 1 2 3 4 5; do echo 1 > "/sys/bus/usb/devices/$usb/authorized" 2>/dev/null && return 0; sleep 2; done
    return 1
}

zte_get() {   # zte_get <gw> <cmd,...> -> JSON (empty/HTML if the gateway isn't a ZTE web API)
    curl -s -m 5 -H "Referer: http://$1/index.html" \
        "http://$1/goform/goform_get_cmd_process?isTest=false&multi_data=1&cmd=$2" 2>/dev/null
}

zte_set() {   # zte_set <gw> <goformId>: succeeds iff the dongle answers "success" (the MF833U1 needs no login)
    curl -s -m 8 -H "Referer: http://$1/index.html" \
        --data "isTest=false&notCallback=true&goformId=$2" \
        "http://$1/goform/goform_set_cmd_process" 2>/dev/null | grep -q '"result":"success"'
}

upstream_via() {   # TCP/443 handshake forced out of <iface> (SO_BINDTODEVICE, root): ~200 B of mobile data, no TLS
    python3 - "$1" 2>/dev/null <<'EOF'
import socket, sys
dev = sys.argv[1].encode()
for host in ("1.1.1.1", "8.8.8.8"):
    try:
        s = socket.socket()
        s.setsockopt(socket.SOL_SOCKET, getattr(socket, "SO_BINDTODEVICE", 25), dev)
        s.settimeout(5)
        s.connect((host, 443))
        s.close()
        sys.exit(0)
    except OSError:
        pass
sys.exit(1)
EOF
}

dongle_classify() {   # sets DONGLE_DEV DONGLE_USB DONGLE_GW DONGLE_STATUS
    DONGLE_DEV=$(dongle_iface)
    DONGLE_USB=$(dongle_usb "$DONGLE_DEV")
    DONGLE_GW=""
    if [[ -z "$DONGLE_DEV" ]]; then
        if [[ -n "$DONGLE_USB" ]]; then DONGLE_STATUS=no_iface; else DONGLE_STATUS=absent; fi
        return
    fi
    if [[ "$DONGLE_DEV" == wwan* || "$DONGLE_DEV" == ppp* ]]; then   # point-to-point modem: no LAN gateway to test
        if upstream_via "$DONGLE_DEV"; then DONGLE_STATUS=ok; else DONGLE_STATUS=no_upstream; fi
        return
    fi
    DONGLE_GW=$(ip -4 route show dev "$DONGLE_DEV" 2>/dev/null | awk '$1=="default"{print $3; exit}')
    [[ -z "$DONGLE_GW" ]] && DONGLE_GW=$(nmcli -g IP4.GATEWAY device show "$DONGLE_DEV" 2>/dev/null)
    if [[ -z "$DONGLE_GW" ]]; then
        DONGLE_STATUS=no_ip
    elif ! ping -c 2 -W 2 -I "$DONGLE_DEV" "$DONGLE_GW" &>/dev/null \
      && ! curl -s -m 4 -o /dev/null --interface "$DONGLE_DEV" "http://$DONGLE_GW/"; then
        DONGLE_STATUS=gw_dead
    elif ! upstream_via "$DONGLE_DEV"; then
        DONGLE_STATUS=no_upstream
    else
        DONGLE_STATUS=ok
    fi
}

dongle_problem() {   # human description for the admin alert (ASCII, like the other alerts)
    case "$1" in
        no_iface)    echo "is on USB but has no network interface (enumeration failed)" ;;
        no_ip)       echo "hands out no IP address" ;;
        gw_dead)     echo "does not answer over USB" ;;
        no_upstream) echo "has no internet access over the mobile network" ;;
        *)           echo "is not working ($1)" ;;
    esac
}

dongle_classify
# A stick resetting itself takes ~1-5 s: don't mistake one caught mid-flight
# for a stuck one -- look again before acting on a USB-side status.
if [[ "$DONGLE_STATUS" == no_iface || "$DONGLE_STATUS" == no_ip || "$DONGLE_STATUS" == gw_dead ]]; then
    sleep 20
    dongle_classify
fi

# Snapshot for the log line, taken BEFORE any action so it shows what the
# action was reacting to. Kernel counts cover the last 5 min (the timer
# period) rather than tracking a cursor -- a missed event at the boundary just
# shows up in the next line instead.
KLOG=$(journalctl -k --since "5 min ago" --no-pager -q 2>/dev/null)
WD_TIMEOUTS=$(grep "NETDEV WATCHDOG" <<<"$KLOG" | grep -cE "cdc_ether|cdc_ncm|rndis_host|huawei_cdc_ncm")
REENUMS=$(grep -cE "(cdc_ether|cdc_ncm|rndis_host|huawei_cdc_ncm) .*: unregister" <<<"$KLOG")
USB_ERRS=$(grep -cE "error -(71|110|32)|can't set config|device descriptor read|unable to enumerate|not accepting address" <<<"$KLOG")
KERNEL_PART="watchdog_timeouts_last5min=$WD_TIMEOUTS reenumerations_last5min=$REENUMS usb_errors_last5min=$USB_ERRS"

if [[ -n "$DONGLE_DEV" && -d "/sys/class/net/$DONGLE_DEV" ]]; then
    STATS="/sys/class/net/$DONGLE_DEV/statistics"
    TX_ERR=$(cat "$STATS/tx_errors" 2>/dev/null || echo "?")
    TX_DROP=$(cat "$STATS/tx_dropped" 2>/dev/null || echo "?")
    TX_CARRIER=$(cat "$STATS/tx_carrier_errors" 2>/dev/null || echo "?")
    RX_ERR=$(cat "$STATS/rx_errors" 2>/dev/null || echo "?")
    RX_DROP=$(cat "$STATS/rx_dropped" 2>/dev/null || echo "?")
    CARRIER_CHANGES=$(cat "/sys/class/net/$DONGLE_DEV/carrier_changes" 2>/dev/null || echo "?")
    OPERSTATE=$(cat "/sys/class/net/$DONGLE_DEV/operstate" 2>/dev/null || echo "?")
    DEFAULT_DEV=$(ip route get 8.8.8.8 2>/dev/null | grep -oE ' dev [^ ]+' | awk '{print $2}')
    LINK_PART="iface=$DONGLE_DEV usb=${DONGLE_USB:-?} state=$OPERSTATE default_route=${DEFAULT_DEV:-?} | tx_errors=$TX_ERR tx_dropped=$TX_DROP tx_carrier_errors=$TX_CARRIER rx_errors=$RX_ERR rx_dropped=$RX_DROP carrier_changes=$CARRIER_CHANGES"
elif [[ -n "$DONGLE_USB" ]]; then
    LINK_PART="iface=none usb=$DONGLE_USB (on the USB bus, no network interface)"
else
    LINK_PART="iface=none (no 4G/cellular device detected)"
fi

# The dongle's own view (radio state + signal), from its web API. Local USB
# traffic only -- it costs no mobile data. "lte=n/a" for non-ZTE sticks.
RADIO_PART="lte=n/a"
if [[ -n "$DONGLE_GW" && "$DONGLE_STATUS" != gw_dead ]]; then
    RADIO_PART=$(python3 -c '
import json, sys
try:
    d = json.loads(sys.argv[1])
except Exception:
    print("lte=n/a"); sys.exit(0)
g = lambda k: (str(d.get(k, "")).strip() or "?").replace(" ", "_")
print("lte=%s net=%s band=%s rsrp=%s rsrq=%s sinr=%s cell=%s conn_uptime_s=%s" % tuple(
    g(k) for k in ("ppp_status", "network_type", "wan_active_band", "lte_rsrp",
                   "lte_rsrq", "lte_snr", "cell_id", "realtime_time")))
' "$(zte_get "$DONGLE_GW" "ppp_status,network_type,wan_active_band,lte_rsrp,lte_rsrq,lte_snr,cell_id,realtime_time")" 2>/dev/null)
    [[ -z "$RADIO_PART" ]] && RADIO_PART="lte=n/a"
fi
IS_ZTE=0; [[ "$RADIO_PART" != "lte=n/a" ]] && IS_ZTE=1
SNAP_GW=${DONGLE_GW:--}

# ── the ladder ──
DONGLE_ACTION=none
PI_REBOOT_NEEDED=0
if [[ "$DONGLE_STATUS" == ok ]]; then
    if [[ $DONGLE_BAD_SINCE -gt 0 && -n "$DONGLE_ACTIONS" ]]; then
        DOWN_MIN=$(( (NOW - DONGLE_BAD_SINCE) / 60 ))
        logger -t amonavision-watchdog "4G dongle recovered after ~${DOWN_MIN}min (actions: $DONGLE_ACTIONS)"
        send_admin_alert "AmonaVision: 4G recovered after ~${DOWN_MIN} min (automatic actions: ${DONGLE_ACTIONS})."
    fi
    DONGLE_BAD_SINCE=0; DONGLE_ACTIONS=""; DONGLE_PI_REBOOTS=0
else
    [[ $DONGLE_BAD_SINCE -eq 0 ]] && DONGLE_BAD_SINCE=$NOW
    BAD_MIN=$(( (NOW - DONGLE_BAD_SINCE) / 60 ))
    case "$DONGLE_STATUS" in
        no_iface|no_ip|gw_dead)
            if [[ -n "$DONGLE_USB" ]] && (( (NOW - DONGLE_LAST_REPLUG) / 60 >= DONGLE_REPLUG_COOLDOWN_MINUTES )); then
                if dongle_replug "$DONGLE_USB"; then DONGLE_ACTION=usb_replug; else DONGLE_ACTION=usb_replug_failed; fi
                DONGLE_LAST_REPLUG=$NOW
            fi
            ;;
        no_upstream)
            if (( BAD_MIN >= DONGLE_DEVREBOOT_AFTER_MINUTES && (NOW - DONGLE_LAST_DEVREBOOT) / 60 >= DONGLE_DEVREBOOT_COOLDOWN_MINUTES )); then
                if [[ $IS_ZTE -eq 1 ]] && zte_set "$DONGLE_GW" REBOOT_DEVICE; then
                    DONGLE_ACTION=dongle_reboot
                elif [[ -n "$DONGLE_USB" ]] && dongle_replug "$DONGLE_USB"; then
                    DONGLE_ACTION=usb_replug   # no API: re-enumerating is the closest thing to a modem restart
                fi
                DONGLE_LAST_DEVREBOOT=$NOW
            elif [[ $IS_ZTE -eq 1 ]] && (( BAD_MIN >= DONGLE_REDIAL_AFTER_MINUTES && (NOW - DONGLE_LAST_REDIAL) / 60 >= DONGLE_REDIAL_COOLDOWN_MINUTES )); then
                zte_set "$DONGLE_GW" DISCONNECT_NETWORK
                sleep 3
                if zte_set "$DONGLE_GW" CONNECT_NETWORK; then DONGLE_ACTION=redial; else DONGLE_ACTION=redial_failed; fi
                DONGLE_LAST_REDIAL=$NOW
            fi
            ;;
    esac

    if [[ "$DONGLE_ACTION" != none ]]; then
        logger -t amonavision-watchdog "4G dongle $DONGLE_STATUS for ${BAD_MIN}min -- $DONGLE_ACTION"
        # Alert once per outage (on its first action), not on every retry; the
        # recovery alert above lists everything that was tried.
        [[ -z "$DONGLE_ACTIONS" ]] && send_admin_alert "AmonaVision: the 4G modem $(dongle_problem "$DONGLE_STATUS"). Automatic action: $DONGLE_ACTION."
        [[ ",$DONGLE_ACTIONS," == *",$DONGLE_ACTION,"* ]] || DONGLE_ACTIONS="${DONGLE_ACTIONS:+$DONGLE_ACTIONS,}$DONGLE_ACTION"
        # Record whether it worked (a replug/redial takes ~10 s; a dongle
        # reboot ~60 s, so that one just shows up in the next cycle's line).
        if [[ "$DONGLE_ACTION" == usb_replug || "$DONGLE_ACTION" == redial ]]; then
            sleep 25
            PREV_STATUS=$DONGLE_STATUS
            dongle_classify
            DONGLE_ACTION="$DONGLE_ACTION->$DONGLE_STATUS"
            DONGLE_STATUS=$PREV_STATUS
        fi
    elif (( BAD_MIN >= REBOOT_AFTER_MINUTES && DONGLE_PI_REBOOTS < DONGLE_MAX_PI_REBOOTS )) \
         && reboot_cooldown_over && ! internet_ok; then
        # Only when nothing else was tried this cycle, and only if the frame
        # is fully offline -- at home with Wi-Fi a dead dongle isn't worth a reboot.
        PI_REBOOT_NEEDED=1
        DONGLE_ACTION=pi_reboot
        DONGLE_PI_REBOOTS=$(( DONGLE_PI_REBOOTS + 1 ))
        [[ ",$DONGLE_ACTIONS," == *",pi_reboot,"* ]] || DONGLE_ACTIONS="${DONGLE_ACTIONS:+$DONGLE_ACTIONS,}pi_reboot"
    fi
fi

printf '%s | %s | %s | gw=%s %s | status=%s action=%s\n' \
    "$(date '+%Y-%m-%d %H:%M:%S')" "$LINK_PART" "$KERNEL_PART" "$SNAP_GW" "$RADIO_PART" \
    "$DONGLE_STATUS" "$DONGLE_ACTION" >> "$DONGLE_LOG"
# Keep it bounded -- a single unbounded file under app/logs/ could still crowd
# out app.log/viewer.log over months of 5-minute samples.
if [[ -f "$DONGLE_LOG" ]] && [[ $(wc -l < "$DONGLE_LOG") -gt 20000 ]]; then
    tail -n 10000 "$DONGLE_LOG" > "${DONGLE_LOG}.tmp" && mv "${DONGLE_LOG}.tmp" "$DONGLE_LOG"
fi

[[ $PI_REBOOT_NEEDED -eq 1 ]] && escalate_to_reboot "the 4G link ($DONGLE_STATUS)" "re-plugging the modem over USB and through its API"

# ── Wi-Fi fallback: re-activate a saved profile NM gave up on ─────────────────
# A few rejected handshakes in a row (e.g. a phone hotspot still holding the
# association the Pi dropped when it rebooted: ASSOC-REJECT status 16) end in
# "no-secrets", and NM then blocks autoconnect for that profile until someone
# activates it by hand -- 2026-10-07 the Pi came back from a reboot with the
# hotspot in range and stayed off it. With wlan0 idle, bring up any saved
# autoconnect Wi-Fi profile whose SSID is in range. Wi-Fi stays the fallback
# (route metric 700), so this never takes over from the 4G. Local-only, so it
# runs before the internet guard.
if [[ "$(nmcli -g GENERAL.STATE device show wlan0 2>/dev/null)" == 30* ]]; then   # 30 = disconnected
    VISIBLE_SSIDS=$(nmcli -g SSID device wifi list ifname wlan0 2>/dev/null)
    while IFS=: read -r wname wtype; do
        [[ "$wtype" == "802-11-wireless" ]] || continue
        [[ "$(nmcli -g connection.autoconnect connection show "$wname" 2>/dev/null)" == "yes" ]] || continue
        wssid=$(nmcli -g 802-11-wireless.ssid connection show "$wname" 2>/dev/null)
        grep -qxF "$wssid" <<< "$VISIBLE_SSIDS" || continue
        if timeout 45 nmcli --wait 40 connection up "$wname" ifname wlan0 >/dev/null 2>&1; then
            logger -t amonavision-watchdog "wlan0 was idle with saved Wi-Fi '$wname' in range -- re-activated it"
            break
        fi
        logger -t amonavision-watchdog "wlan0 idle, saved Wi-Fi '$wname' in range but activation failed"
    done < <(nmcli -t -f NAME,TYPE connection show 2>/dev/null)
fi

# ── HDMI audio: re-create PipeWire's HDMI sink if it went missing ─────────────
# When the panel's HDMI link drops and comes back (cable re-plug, panel power
# glitch), PipeWire can re-open the HDMI device in the instant the kernel still
# refuses audio ("playback open failed: Unknown error 524") and never retries:
# no HDMI sink, and the frame's audio silently goes to the analog jack until
# the next restart (2026-09-30). With the panel connected (HDMI Jack on) but no
# HDMI sink, cycle the card's profile off -> output:hdmi-stereo so WirePlumber
# creates the sink again, then make it the default so streams move back to it.
# No service restart. Local-only, so it runs before the internet guard.
hdmi_audio_state() {   # -> "<device id> <off idx> <hdmi-stereo idx> <sink id or ->"
    sudo -u av XDG_RUNTIME_DIR=/run/user/1000 pw-dump 2>/dev/null | python3 -c '
import json, sys
dev = off = hdmi = ""; sink = "-"
try:
    objs = json.load(sys.stdin)
except Exception:
    objs = []
for o in objs:
    info = o.get("info") or {}
    p = info.get("props") or {}
    if p.get("device.name") == "alsa_card.platform-fef00700.hdmi":
        dev = o["id"]
        for e in (info.get("params") or {}).get("EnumProfile", []):
            if e.get("name") == "off": off = e.get("index")
            if e.get("name") == "output:hdmi-stereo": hdmi = e.get("index")
    if p.get("media.class") == "Audio/Sink" and str(p.get("node.name", "")).startswith("alsa_output.platform-fef00700.hdmi."):
        sink = o["id"]
print(dev, off, hdmi, sink)' 2>/dev/null
}
if amixer -c vc4hdmi0 cget iface=CARD,name='HDMI Jack' 2>/dev/null | grep -q 'values=on'; then
    read -r HA_DEV HA_OFF HA_HDMI HA_SINK <<<"$(hdmi_audio_state)"
    if [[ -n "${HA_DEV:-}" && -n "${HA_OFF:-}" && -n "${HA_HDMI:-}" && "${HA_SINK:-}" == "-" ]]; then
        sudo -u av XDG_RUNTIME_DIR=/run/user/1000 wpctl set-profile "$HA_DEV" "$HA_OFF" 2>/dev/null
        sleep 2
        sudo -u av XDG_RUNTIME_DIR=/run/user/1000 wpctl set-profile "$HA_DEV" "$HA_HDMI" 2>/dev/null
        sleep 3
        read -r _ _ _ HA_SINK <<<"$(hdmi_audio_state)"
        if [[ -n "${HA_SINK:-}" && "$HA_SINK" != "-" ]]; then
            sudo -u av XDG_RUNTIME_DIR=/run/user/1000 wpctl set-default "$HA_SINK" 2>/dev/null
            logger -t amonavision-watchdog "HDMI audio sink was missing with the panel connected -- recreated it (profile off -> output:hdmi-stereo) and made it the default"
        else
            logger -t amonavision-watchdog "HDMI audio sink missing with the panel connected and could not be recreated -- the 3h health check reports it"
        fi
    fi
fi

# If the network itself is down, silence from the bot/Tailscale is expected --
# tools/health-alert.sh (every 3h) already reports real outages. Only act on
# those when the network is fine but a specific piece isn't using it.
if ! internet_ok; then
    save_state
    exit 0
fi

# ── Bot liveness ──────────────────────────────────────────────────────────────
# Alive = whichever is newer: the heartbeat the bot touches on every successful
# getUpdates poll, or app.log (errors, messages). Since httpx's per-poll lines
# were silenced (they carried the bot token), an idle-but-healthy bot can go
# 10+ min without writing app.log — 2026-10-05 it got restarted every 20 min.
BOT_HEARTBEAT=/tmp/amonavision_bot_heartbeat
LIVE_MTIME=$(stat -c %Y "$APP_LOG" 2>/dev/null || echo 0)
HB_MTIME=$(stat -c %Y "$BOT_HEARTBEAT" 2>/dev/null || echo 0)
[[ $HB_MTIME -gt $LIVE_MTIME ]] && LIVE_MTIME=$HB_MTIME
if [[ $LIVE_MTIME -gt 0 ]]; then
    AGE_MIN=$(( (NOW - LIVE_MTIME) / 60 ))
    if [[ $AGE_MIN -ge $REBOOT_AFTER_MINUTES ]]; then
        escalate_to_reboot "the bot (getUpdates)"
    elif [[ $AGE_MIN -ge $BOT_STALE_MINUTES ]]; then
        SINCE_LAST_MIN=$(( (NOW - BOT_LAST_RESTART) / 60 ))
        if [[ $SINCE_LAST_MIN -ge $RESTART_COOLDOWN_MINUTES ]]; then
            logger -t amonavision-watchdog "bot heartbeat and app.log stale for ${AGE_MIN}min with internet reachable -- bot looks wedged, restarting amonavision.service"
            systemctl restart amonavision
            BOT_LAST_RESTART=$NOW
            send_admin_alert "AmonaVision: the bot had been inactive for ${AGE_MIN} min with internet available. Restarted automatically."
        fi
    fi
fi

# ── Tailscale liveness ────────────────────────────────────────────────────────
# `tailscale status --json`'s Self.Online reflects the control plane's view of
# this node, not just tailscaled's own opinion of itself -- and a `timeout` on
# the command itself (tailscaled wedged badly enough to not even answer its
# local CLI) counts as unhealthy too.
if command -v tailscale &>/dev/null && systemctl is-enabled tailscaled &>/dev/null; then
    TS_JSON=$(timeout 10 tailscale status --json 2>/dev/null)
    TS_ONLINE=$(python3 -c "
import json,sys
try:
    print(bool(json.loads(sys.argv[1]).get('Self',{}).get('Online')))
except Exception:
    print('False')
" "${TS_JSON:-}" 2>/dev/null)

    if [[ "$TS_ONLINE" == "True" ]]; then
        TS_DOWN_SINCE=0   # healthy -- clear the streak
    else
        [[ "$TS_DOWN_SINCE" -eq 0 ]] && TS_DOWN_SINCE=$NOW   # first time noticing
        TS_DOWN_MIN=$(( (NOW - TS_DOWN_SINCE) / 60 ))
        if [[ $TS_DOWN_MIN -ge $REBOOT_AFTER_MINUTES ]]; then
            escalate_to_reboot "Tailscale"
        else
            SINCE_LAST_TS_MIN=$(( (NOW - TS_LAST_RESTART) / 60 ))
            if [[ $SINCE_LAST_TS_MIN -ge $RESTART_COOLDOWN_MINUTES ]]; then
                logger -t amonavision-watchdog "tailscale not Online for ${TS_DOWN_MIN}min with internet reachable -- restarting tailscaled"
                systemctl restart tailscaled
                TS_LAST_RESTART=$NOW
                send_admin_alert "AmonaVision: Tailscale had not been connected for ${TS_DOWN_MIN} min with internet available. tailscaled restarted automatically."
            fi
        fi
    fi
fi

save_state
