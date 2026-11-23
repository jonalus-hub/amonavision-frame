#!/bin/sh
# ExecCondition for log2ram.service (systemd drop-in written by setup.sh).
#
# log2ram keeps /var/log (the journal included) in RAM and copies it to the SD
# card every 15 min and on a clean shutdown. That saves SD wear on a frame in
# daily use, but while a frame is still being set up and tested it is unplugged
# all the time and every unplug loses the logs of that boot -- exactly the ones
# needed to see why something failed. So log2ram only runs on a deployed frame:
#
#   config.txt  [System]  deployed = True    -> exit 0: log2ram runs
#                         deployed = False   -> exit 1: skipped, logs go
#                                               straight to the SD card
#
# A missing file, section or key counts as deployed (frames installed before
# this flag existed keep their log2ram). Read with plain sh + awk: this runs
# early in boot, before Python is worth starting. Applies on the next boot.
#
# Usage: log2ram-condition.sh [path/to/config.txt]
CONFIG="${1:-$(dirname "$0")/../config.txt}"

value=$(awk '
    /^[[:space:]]*\[/ {
        section = $0
        gsub(/^[[:space:]]*\[|\][[:space:]]*$/, "", section)
        next
    }
    section == "System" {
        line = $0
        if (match(line, /^[[:space:]]*deployed[[:space:]]*[=:]/)) {
            v = substr(line, RLENGTH + 1)
            gsub(/[[:space:]]/, "", v)
            print tolower(v)
            exit
        }
    }
' "$CONFIG" 2>/dev/null)

case "$value" in
    false|no|off|0) exit 1 ;;
    *) exit 0 ;;
esac
