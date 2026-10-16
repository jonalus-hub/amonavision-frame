#!/usr/bin/env bash
# AmonaVision — periodic health check with Telegram alerting.
#
# Runs test_setup.sh and DMs the admin (TELEGRAM_ADMIN_CHAT_ID — a person, not
# the family group: they don't need "0 failures" pings about the frame's plumbing)
# ONLY when something is actually wrong, when it just recovered, or once a day
# as a quiet "still alive" heartbeat — never on every run. Dependency-free
# (curl only, no venv) so it still works even if the Python app is what's broken.
#
# Installed + scheduled by setup.sh via amonavision-healthcheck.{service,timer}.
# Safe to run manually: bash tools/health-alert.sh

set -uo pipefail   # no -e: one failed check must not abort the whole script

APP_DIR=/home/av/AmonaVision
ENV_FILE="$APP_DIR/.env"
STATE_DIR=/var/lib/amonavision
STATE_FILE="$STATE_DIR/healthcheck_state"
RESULT_FILE=$(mktemp)
trap 'rm -f "$RESULT_FILE"' EXIT

if [[ ! -f "$ENV_FILE" ]]; then
    echo "[health-alert] $ENV_FILE not found — nothing to do" >&2
    exit 0
fi

TOKEN=$(grep -m1 '^TELEGRAM_BOT_TOKEN=' "$ENV_FILE" | cut -d= -f2- | tr -d '\r\n')
CHAT_ID=$(grep -m1 '^TELEGRAM_ADMIN_CHAT_ID=' "$ENV_FILE" | cut -d= -f2- | tr -d '\r\n')

if [[ -z "$TOKEN" || -z "$CHAT_ID" ]]; then
    echo "[health-alert] TELEGRAM_BOT_TOKEN/TELEGRAM_ADMIN_CHAT_ID not set in .env — skipping" >&2
    echo "[health-alert] Get your personal chat id: message the bot privately, then check" >&2
    echo "[health-alert]   app.log for '/<command> from user id=...' — that id IS your chat id" >&2
    echo "[health-alert]   in a 1:1 chat. Add TELEGRAM_ADMIN_CHAT_ID=<id> to $ENV_FILE" >&2
    exit 0
fi

mkdir -p "$STATE_DIR"
PREV_STATUS="unknown"
LAST_HEARTBEAT_DATE=""
if [[ -f "$STATE_FILE" ]]; then
    PREV_STATUS=$(grep -m1 '^status=' "$STATE_FILE" | cut -d= -f2)
    LAST_HEARTBEAT_DATE=$(grep -m1 '^heartbeat_date=' "$STATE_FILE" | cut -d= -f2)
fi

# Run the real test suite — as root (this script runs via a root systemd
# service) it gets full coverage, including the checks that need sudo
# (4G upstream test, Tailscale self-heal).
AMONAVISION_HEALTHCHECK=1 bash "$APP_DIR/test_setup.sh" > "$RESULT_FILE" 2>&1
CLEAN=$(sed -E 's/\x1b\[[0-9;]*m//g' "$RESULT_FILE")

FAILS=$(grep -c '\[FAIL\]' <<<"$CLEAN" || true)
WARNS=$(grep -c '\[WARN\]' <<<"$CLEAN" || true)

send_telegram() {
    # Plain text, no parse_mode: test output contains _ * [ ] which would
    # break Telegram's Markdown parser and silently drop the alert — the
    # one failure mode a monitoring script cannot afford.
    curl -sS -m 20 "https://api.telegram.org/bot${TOKEN}/sendMessage" \
        --data-urlencode "chat_id=${CHAT_ID}" \
        --data-urlencode "text=$1" >/dev/null 2>&1
}

MY_IP=$(hostname -I 2>/dev/null | awk '{print $1}')
TODAY=$(date +%F)
STATUS="ok"

if [[ "$FAILS" -gt 0 ]]; then
    STATUS="fail"
    FAIL_LINES=$(grep '\[FAIL\]' <<<"$CLEAN" | sed -E 's/^\s*\[FAIL\]\s*/- /')
    send_telegram "AmonaVision - PROBLEM DETECTED

${FAILS} failure(s), ${WARNS} warning(s).

${FAIL_LINES}

Check with: ssh av@${MY_IP}  or the /test command in this chat."
elif [[ "$PREV_STATUS" == "fail" ]]; then
    send_telegram "AmonaVision - recovered. Last check: 0 failures, ${WARNS} warning(s)."
elif [[ "$LAST_HEARTBEAT_DATE" != "$TODAY" ]]; then
    send_telegram "AmonaVision still online. 0 failures, ${WARNS} warning(s)."
    LAST_HEARTBEAT_DATE="$TODAY"
fi

{
    echo "status=$STATUS"
    echo "heartbeat_date=$LAST_HEARTBEAT_DATE"
    echo "last_run=$(date -Iseconds)"
    echo "fails=$FAILS"
    echo "warns=$WARNS"
} > "$STATE_FILE"
