"""
Usage log: one CSV row per physical button press and per voice-reply outcome.

Written by the viewer, read by the bot's /usage. Lives in app/logs/buttons.csv
(on the SD card, not in RAM) and is never rotated: ~100 bytes a row, so even
a few hundred presses a day is only a few MB a year.

Columns:
    timestamp     local time, "YYYY-MM-DD HH:MM:SS"
    event         "press" (a button went down) or "recording" (outcome of
                  a voice reply, logged when it's known)
    button        next / previous / play / record
    state         viewer state when the button was pressed (WAITING_ACTION,
                  PLAYING, RECORDING, SPLASH)
    session_id    session on screen, if any
    session_type  PHOTO / PHOTO_AUDIO / VIDEO / ...
    media         file name of the photo/video on screen
    duration_s    "recording" only: how long the record button was held
    result        "recording" only: discarded (too short) / sent /
                  send_error / save_error / no_audio
"""

from __future__ import annotations

import csv
import logging
import threading
from datetime import datetime
from pathlib import Path

USAGE_LOG = Path(__file__).resolve().parent / "logs" / "buttons.csv"
FIELDS = ["timestamp", "event", "button", "state", "session_id",
          "session_type", "media", "duration_s", "result"]

_lock = threading.Lock()   # the voice send outcome comes from another thread
logger = logging.getLogger(__name__)


def log_usage(event: str, button: str, *, state: str = "", session: dict | None = None,
              duration_s: float | None = None, result: str = "") -> None:
    """Append one row. Never raises: usage stats must not break the viewer."""
    try:
        media = ""
        for f in (session or {}).get("files", []):
            if f.get("file_type") in ("IMAGE", "VIDEO"):
                media = Path(f["file_path"]).name
                break
        row = {
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "event": event,
            "button": button,
            "state": state,
            "session_id": (session or {}).get("id", ""),
            "session_type": (session or {}).get("type", ""),
            "media": media,
            "duration_s": "" if duration_s is None else f"{duration_s:.1f}",
            "result": result,
        }
        with _lock:
            new = not USAGE_LOG.exists()
            USAGE_LOG.parent.mkdir(parents=True, exist_ok=True)
            with open(USAGE_LOG, "a", newline="", encoding="utf-8") as fh:
                w = csv.DictWriter(fh, fieldnames=FIELDS)
                if new:
                    w.writeheader()
                w.writerow(row)
    except Exception:
        logger.exception("Could not write usage log row")


def read_usage(since: datetime, until: datetime | None = None) -> list[dict]:
    """Rows with since <= timestamp < until (until=None: up to now)."""
    lo = since.strftime("%Y-%m-%d %H:%M:%S")
    hi = until.strftime("%Y-%m-%d %H:%M:%S") if until else "9999"
    if not USAGE_LOG.exists():
        return []
    with open(USAGE_LOG, newline="", encoding="utf-8") as fh:
        return [r for r in csv.DictReader(fh) if lo <= r.get("timestamp", "") < hi]
