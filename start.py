import subprocess
import sys
import time
import logging
import logging.handlers
from pathlib import Path

LOG_DIR = Path(__file__).resolve().parent / "app" / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

_handler = logging.handlers.RotatingFileHandler(
    LOG_DIR / "startup.log", maxBytes=1_000_000, backupCount=2, encoding="utf-8"
)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | STARTUP | %(message)s",
    handlers=[_handler, logging.StreamHandler()],
)
logger = logging.getLogger("AmonaVisionStartup")

BOT_RESTART_MIN_DELAY = 15    # first retry delay (seconds)
BOT_RESTART_MAX_DELAY = 300   # cap the backoff at 5 min
BOT_HEALTHY_UPTIME = 120      # a run lasting this long is "healthy" → reset backoff


def main():
    logger.info("Starting AmonaVision System...")

    logger.info("Launching Viewer (viewer.py)...")
    viewer_process = subprocess.Popen([sys.executable, "viewer.py"])

    logger.info("Launching Bot (run.py)...")
    bot_process = subprocess.Popen([sys.executable, "run.py"])
    bot_started_at = time.monotonic()
    bot_restart_delay = BOT_RESTART_MIN_DELAY

    try:
        while True:
            # Viewer exit = intentional shutdown
            if viewer_process.poll() is not None:
                logger.info("Viewer closed. Shutting down system.")
                break

            # Bot exit = restart it with exponential backoff. A transient network
            # blip recovers fast; a hard failure (bad token, missing dep) no longer
            # hammers a 15s crash-loop — the delay climbs to BOT_RESTART_MAX_DELAY.
            if bot_process.poll() is not None:
                code = bot_process.returncode
                ran_for = time.monotonic() - bot_started_at
                if ran_for >= BOT_HEALTHY_UPTIME:
                    bot_restart_delay = BOT_RESTART_MIN_DELAY  # healthy run → reset
                logger.warning(
                    "Bot stopped (exit %s) after %.0fs. Restarting in %ds...",
                    code, ran_for, bot_restart_delay,
                )
                time.sleep(bot_restart_delay)
                bot_process = subprocess.Popen([sys.executable, "run.py"])
                bot_started_at = time.monotonic()
                bot_restart_delay = min(bot_restart_delay * 2, BOT_RESTART_MAX_DELAY)
                logger.info("Bot restarted.")

            time.sleep(1)

    except KeyboardInterrupt:
        logger.info("Ctrl+C detected. Shutting down system...")

    finally:
        logger.info("Terminating processes...")
        if bot_process.poll() is None:
            bot_process.terminate()
        if viewer_process.poll() is None:
            viewer_process.terminate()
        bot_process.wait()
        viewer_process.wait()
        logger.info("AmonaVision successfully completely stopped.")


if __name__ == "__main__":
    main()
