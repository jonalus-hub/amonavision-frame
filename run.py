"""
AmonaVision – Entry-point script
=================================
Loads environment variables from .env (if present) and starts the bot.

Usage:
    python run.py
"""

import logging
import os
import time
from pathlib import Path

from dotenv import load_dotenv

# Load .env from project root
env_path = Path(__file__).resolve().parent / ".env"
load_dotenv(dotenv_path=env_path)

logger = logging.getLogger("AmonaVisionRun")

# ---------------------------------------------------------------------------
# Clock sanity gate (Raspberry Pi has no RTC)
# ---------------------------------------------------------------------------
# On a cold boot the clock starts from the last-known time (fake-hwclock) and is
# only corrected once systemd-timesyncd reaches an NTP server. Until then the
# clock can be days/weeks behind, which makes every TLS handshake to
# api.telegram.org fail with "certificate is not yet valid". The display process
# (viewer.py) is separate and unaffected — only the bot needs a correct clock —
# so we wait here, briefly and bounded, instead of blocking the whole unit on
# network-online.target.
#
# Bump MIN_PLAUSIBLE_EPOCH occasionally so a wildly stale clock is caught even
# before NTP; 2026-09-01T00:00:00Z here.
MIN_PLAUSIBLE_EPOCH = 1_756_684_800
_CLOCK_WAIT_MAX_S = 120
_TIMESYNC_FLAG = Path("/run/systemd/timesync/synchronized")


def _wait_for_sane_clock() -> None:
    deadline = time.monotonic() + _CLOCK_WAIT_MAX_S
    warned = False
    while time.monotonic() < deadline:
        synced = _TIMESYNC_FLAG.exists()
        plausible = time.time() >= MIN_PLAUSIBLE_EPOCH
        if synced and plausible:
            if warned:
                # bot.py configures logging on import (which happens after this
                # returns), so emit at WARNING so the recovery is visible now.
                logger.warning("Clock is sane now (%s) — starting bot.", time.strftime("%Y-%m-%d %H:%M:%S"))
            return
        if not warned:
            logger.warning(
                "Clock not trustworthy yet (now=%s, ntp_synced=%s) — waiting up to %ds "
                "before starting the bot to avoid TLS 'certificate not yet valid'.",
                time.strftime("%Y-%m-%d %H:%M:%S"), synced, _CLOCK_WAIT_MAX_S,
            )
            warned = True
        time.sleep(3)
    logger.warning(
        "Proceeding with a possibly-stale clock after %ds — python-telegram-bot "
        "will retry on its own once NTP corrects it.", _CLOCK_WAIT_MAX_S,
    )


if os.getenv("AMONAVISION_SKIP_CLOCK_WAIT") != "1":
    _wait_for_sane_clock()

from app.bot.bot import main  # noqa: E402  (import after env + clock setup)

if __name__ == "__main__":
    main()
