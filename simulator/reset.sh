#!/usr/bin/env bash
# Wipe the simulator's photos, voice notes, videos, database, chat and stats.
# On its next start the simulator seeds the sample media again.
#
# Usage: simulator/reset.sh [-y]      (-y: don't ask for confirmation)
# If a user service runs the simulator (SIM_SERVICE, default amonavision-sim),
# it is stopped and started again; otherwise stop the simulator first.
set -euo pipefail
SIM_DIR="$(cd "$(dirname "$0")" && pwd)"
SRC="${AMONA_SRC:-$(dirname "$SIM_DIR")}"
DATA="${SIM_DATA:-$SIM_DIR/data}"
SERVICE="${SIM_SERVICE:-amonavision-sim}"

# On a real frame this would delete the family's photos.
if [[ -e /etc/systemd/system/amonavision.service && "${SIM_ALLOW_ON_FRAME:-}" != "1" ]]; then
    echo "This machine runs a real AmonaVision frame: not wiping anything." >&2
    exit 1
fi
if [[ ! -f "$SRC/viewer.py" ]]; then
    echo "$SRC doesn't look like an AmonaVision checkout." >&2
    exit 1
fi
if [[ "${1:-}" != "-y" ]]; then
    read -r -p "Delete all photos, voice notes, videos and the database in $SRC/app, and the chat in $DATA? [y/N] " ok
    [[ "$ok" == [yY]* ]] || exit 1
fi

has_service() { systemctl --user cat "$SERVICE" >/dev/null 2>&1; }
if has_service; then systemctl --user stop "$SERVICE"; fi

rm -rf "$DATA" "$SRC"/app/db/amonavision.db* "$SRC/app/brightness.json" \
       "$SRC/app/media/thumbnails" "$SRC/app/logs/buttons.csv"
find "$SRC/app/media/received" "$SRC/app/media/processed" "$SRC/app/media/recordings" \
     -type f ! -name .gitkeep -delete
# The viewer caches decoded voice notes and photos by session; ids restart at 1 after a reset.
rm -rf /tmp/av_voice_*.wav /dev/shm/amonavision_cache

if has_service; then
    systemctl --user start "$SERVICE"
    echo "Simulator reset; it is seeding the sample media again."
else
    echo "Simulator data wiped. Start it again to seed the sample media."
fi
