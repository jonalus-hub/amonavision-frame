"""
AmonaVision - Smart Photo Frame Telegram Bot
=============================================
Backend bot that receives photos, videos, video notes, and voice notes
via Telegram. Every new message becomes its own session on the frame; a
voice note (or a custom video thumbnail) is paired with a photo/video by
**replying** to it in Telegram (see grouper.py).

Uses python-telegram-bot (async version >= 20.x).
"""

import asyncio
import configparser
import os
import re
import json
import subprocess
import logging
import logging.handlers
import threading
import time
from datetime import datetime, time as dt_time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from telegram import Update
from telegram.request import HTTPXRequest
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

from app.bot.grouper import (
    MessageGrouper,
    FlushedSession,
    FileTooLargeError,
    validate_file_size,
    MEDIA_PHOTO,
    MEDIA_VIDEO,
    MEDIA_VIDEO_NOTE,
    MEDIA_VOICE,
    TELEGRAM_FILE_SIZE_LIMIT,
)

from app.db.database import DatabaseManager
from app.usage_log import read_usage

# ---------------------------------------------------------------------------
# Paths – resolved relative to the project root (two levels up from this file)
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
MEDIA_RECEIVED = PROJECT_ROOT / "app" / "media" / "received"
MEDIA_PROCESSED = PROJECT_ROOT / "app" / "media" / "processed"
LOG_DIR = PROJECT_ROOT / "app" / "logs"
LOG_FILE = LOG_DIR / "app.log"

# Ensure directories exist at import time
for _dir in (MEDIA_RECEIVED, MEDIA_PROCESSED, LOG_DIR, PROJECT_ROOT / "app" / "db"):
    _dir.mkdir(parents=True, exist_ok=True)

DB_PATH = PROJECT_ROOT / "app" / "db" / "amonavision.db"
BRIGHTNESS_FILE = PROJECT_ROOT / "app" / "brightness.json"

RESTART_FLAG = Path("/tmp/amonavision_restart")
# Picked up by amonavision-panel-reset.path (root) -> tools/panel-power-cycle.sh
PANEL_RESET_FLAG = Path("/tmp/amonavision_panel_reset")
# Picked up by amonavision-reboot.path (root) -> tools/reboot-with-panel-cycle.sh
REBOOT_FLAG = Path("/tmp/amonavision_reboot_request")

# Monday reminder to the family group ([Telegram] in config.txt, group from
# TELEGRAM_GROUP_CHAT_ID in .env). Read once at start: /restart to apply edits.
CONFIG_FILE = PROJECT_ROOT / "config.txt"
WEEKLY_REMINDER_JOB = "weekly_reminder"
WEEKLY_REMINDER_DEFAULT_TEXT = (
    "I bet you had a great weekend! 📸 "
    "You must have lots of photos to share with grandma 😉"
)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"

logging.basicConfig(
    level=logging.INFO,
    format=LOG_FORMAT,
    handlers=[
        logging.handlers.RotatingFileHandler(
            LOG_FILE, maxBytes=1_000_000, backupCount=2, encoding="utf-8"
        ),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger("AmonaVision")
# httpx logs every request at INFO with the full URL — bot token included — once
# per getUpdates poll (~8,600/day). Keep only its warnings/errors.
logging.getLogger("httpx").setLevel(logging.WARNING)

# ---------------------------------------------------------------------------
# Poll-loop liveness
# ---------------------------------------------------------------------------
# Touched on every successful getUpdates; tools/bot-watchdog.sh reads its age.
# On tmpfs (/tmp), so a touch every ~10 s costs no SD writes.
HEARTBEAT_FILE = Path("/tmp/amonavision_bot_heartbeat")
# No getUpdates round-trip at all (not even a failed one) for this long means
# the poll loop is wedged, not offline: during an outage PTB keeps retrying at
# least every ~30 s. Exiting lets start.py restart just the bot, leaving the
# viewer (and the screen) untouched. Video transcodes run in grouper's worker
# thread, so they can't stall the loop this long.
POLL_STALL_SECONDS = 300
_last_poll_done: float | None = None   # monotonic time of the last finished getUpdates


class _PollTrackingRequest(HTTPXRequest):
    """Transport used only for getUpdates: records every poll round-trip."""

    async def do_request(self, *args, **kwargs):
        global _last_poll_done
        try:
            code, payload = await super().do_request(*args, **kwargs)
        finally:
            _last_poll_done = time.monotonic()
        if code == 200:
            try:
                HEARTBEAT_FILE.touch()
            except OSError:
                pass
        return code, payload


def _poll_stall_watchdog() -> None:
    """Exit the process if getUpdates stops completing (armed by the first poll)."""
    while True:
        time.sleep(30)
        last = _last_poll_done
        if last is not None and time.monotonic() - last > POLL_STALL_SECONDS:
            logger.error(
                "No getUpdates round-trip for %d s — poll loop looks wedged; "
                "exiting so start.py restarts the bot", time.monotonic() - last,
            )
            os._exit(3)

# Message Grouper and DB – shared instances (initialised in main())
# ---------------------------------------------------------------------------
grouper: MessageGrouper | None = None
db: DatabaseManager | None = None

# Global reference to the bot for sending notifications from the callback.
_bot_instance = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _is_admin_chat(update: Update) -> bool:
    """True if the update comes from TELEGRAM_ADMIN_CHAT_ID (unset: nobody is admin)."""
    admin_chat = os.getenv("TELEGRAM_ADMIN_CHAT_ID", "").strip()
    return bool(admin_chat) and str(update.effective_chat.id) == admin_chat


ADMIN_ONLY_TEXT = "⛔ This command is only available to the administrator."


async def _download_to_received(
    file_id: str,
    extension: str,
    context: ContextTypes.DEFAULT_TYPE,
) -> Path:
    """Download a file from Telegram directly to the received directory.
    Names the file uniquely based on the current timestamp.
    """
    tg_file = await context.bot.get_file(file_id)
    now = datetime.now()
    file_path = MEDIA_RECEIVED / f"{now.strftime('%Y%m%d_%H%M%S_%f')}_{tg_file.file_id[-6:]}.{extension}"
    await tg_file.download_to_drive(custom_path=file_path)
    logger.info("File downloaded to received: %s (%d bytes)", file_path, file_path.stat().st_size)
    return file_path


def _format_size(size_bytes: int) -> str:
    """Format byte count as a human-readable string."""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    else:
        return f"{size_bytes / (1024 * 1024):.1f} MB"


# ---------------------------------------------------------------------------
# Flush callback – invoked by the grouper after a session is committed
# ---------------------------------------------------------------------------
async def _on_group_flushed(user_id: int, session: FlushedSession) -> None:
    """Called when a group of files has been committed to ``received/``.

    Sends a summary message to the user via the bot instance.
    """
    bot = _bot_instance
    if bot is None:
        logger.warning("Cannot notify user %s – bot instance not available", user_id)
        return

    # Look the session up in the database
    session_data = None
    if session.db_session_id > 0 and db is not None:
        try:
            session_data = db.get_session_by_id(session.db_session_id)
        except Exception:
            logger.exception("Error getting session by id in flush notification")

    session_type = session_data['type'] if session_data else "OTHER"

    # Confirmation: just the headline. The tips (reply with a voice note /
    # "delete"), the post-processing note and the file list + size used to
    # follow; dropped 2026-10-14 to keep the family chat clean.
    if session.media_types == ["thumbnail"]:
        text = "🖼️ *Video thumbnail saved!*"
    elif session_type == "PHOTO_AUDIO":
        text = "✅ *Photo and voice note saved!* 📸🎙️"
    elif session_type == "VIDEO_AUDIO":
        text = "✅ *Video and voice note saved!* 🎬🎙️"
    elif session_type == "PHOTO":
        text = "📸 *Photo saved!*"
    elif session_type == "VIDEO":
        text = "🎬 *Video saved!*"
    else:
        text = "✅ *File saved!*"

    try:
        # Reply in the same chat the media came from (group or private DM)
        reply_id = session.telegram_message_id if session.telegram_message_id > 0 else None
        target_chat = session.telegram_chat_id if session.telegram_chat_id else user_id
        await bot.send_message(
            chat_id=target_chat,
            text=text,
            parse_mode="Markdown",
            reply_to_message_id=reply_id
        )

        # Store the media's ORIGINAL message_id (not the confirmation's) so
        # voice replies from the frame can be linked to it in the chat
        if session.db_session_id > 0 and db is not None and session.telegram_message_id:
            try:
                db.set_telegram_message_id(
                    session.db_session_id,
                    session.telegram_message_id,
                    session.telegram_chat_id or None,
                )
                logger.info(
                    "Stored telegram context for session db_id=%d: message_id=%d, chat_id=%d",
                    session.db_session_id, session.telegram_message_id, session.telegram_chat_id,
                )
            except Exception:
                logger.exception(
                    "Failed to store telegram context for session %d",
                    session.db_session_id,
                )
    except Exception:
        logger.exception("Failed to send flush notification to user %s", user_id)


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle the /start command – greet the user."""
    user = update.effective_user
    logger.info("/start from %s (id=%s)", user.full_name, user.id)
    await update.message.reply_text(
        f"👋 Hi, {user.first_name}!\n\n"
        "I'm *AmonaVision*, your smart photo frame 🖼️\n\n"
        "📸 Send me *photos* and I'll show them on the frame.\n"
        "🎥 I also accept *videos* and *video notes*.\n\n"
        "🔗 *How to pair content:*\n"
        "• To pair a *photo* with a *voice note*, reply to the photo with the voice note.\n"
        "• To give a *video* a custom thumbnail, reply to the video with the *photo* you want to use.\n\n"
        f"⚠️ Limit per file: {TELEGRAM_FILE_SIZE_LIMIT // (1024*1024)} MB\n\n"
        "Start sending memories!",
        parse_mode="Markdown",
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle the /help command."""
    logger.info("/help from user id=%s", update.effective_user.id)
    await update.message.reply_text(
        "ℹ️ *AmonaVision help*\n\n"
        "📸 *Photo* → saved and shown on the frame.\n"
        "🎬 *Video* or *video note* → saved for playback.\n"
        "🎙️ *Voice note* → reply to a photo/video with it to pair them.\n\n"
        "🔗 *Pairing by reply:*\n"
        "• *Photo + voice*: reply to the photo with a voice note. They play together.\n"
        "• *Video thumbnail*: reply to a video with a photo to use it as its cover. Otherwise one is extracted automatically.\n\n"
        "📎 Files sent as a *document* are accepted too.\n\n"
        "🔧 *Commands:*\n"
        "• /status → content and database\n"
        "• /pi → temperature, RAM, SD, uptime\n"
        "• /test → full diagnostic (network, services, disk…)\n"
        "• /logs → last lines of the viewer log\n"
        "• /logs bot | /logs startup → other logs\n"
        "• /volume → current volume\n"
        "• /volume 70 → set the volume (0–100)\n"
        "• /brightness → screen brightness\n"
        "• /restart → restart the app\n"
        "• /help → this help\n\n"
        f"⚠️ Max per file: {TELEGRAM_FILE_SIZE_LIMIT // (1024*1024)} MB",
        parse_mode="Markdown",
    )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle the /status command – show basic stats."""
    uid = update.effective_user.id
    logger.info("/status from user id=%s", uid)

    photos = list(MEDIA_RECEIVED.glob("*.jpg")) + list(MEDIA_RECEIVED.glob("*.jpeg"))
    videos = list(MEDIA_RECEIVED.glob("*.mp4")) + list(MEDIA_RECEIVED.glob("*.mov"))
    voices = list(MEDIA_RECEIVED.glob("*.ogg"))

    total_size = sum(f.stat().st_size for f in MEDIA_RECEIVED.iterdir() if f.is_file())
    size_str = _format_size(total_size)

    # Database stats
    db_text = ""
    if db is not None:
        try:
            stats = db.get_stats()
            db_text = (
                f"\n\n📂 *Database:*\n"
                f"  Total sessions: *{stats['total_sessions']}*\n"
                f"  Not yet seen: *{stats['unread_sessions']}* 🆕\n"
                f"  Seen: *{stats['read_sessions']}* ✅\n"
                f"  Registered files: *{stats['total_files']}*"
            )
        except Exception as e:
            logger.error(f"Error querying db stats: {e}")

    await update.message.reply_text(
        "📊 *AmonaVision status*\n\n"
        f"🖼️ Photos on disk: *{len(photos)}*\n"
        f"🎬 Videos on disk: *{len(videos)}*\n"
        f"🎙️ Voice notes on disk: *{len(voices)}*\n"
        f"💾 Space used: *{size_str}*\n"
        f"🟢 Bot online{db_text}",
        parse_mode="Markdown",
    )


async def cmd_volume(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /volume [0-100] – get or set audio output volume via wpctl (PipeWire)."""
    args = context.args

    if not args:
        result = subprocess.run(
            ["wpctl", "get-volume", "@DEFAULT_AUDIO_SINK@"],
            capture_output=True, text=True
        )
        if result.returncode == 0:
            # Output: "Volume: 0.40" or "Volume: 0.40 [MUTED]"
            match = re.search(r'Volume:\s*([\d.]+)', result.stdout)
            pct = int(float(match.group(1)) * 100) if match else "?"
            muted = "[MUTED]" in result.stdout
            mute_str = " 🔇 *muted*" if muted else ""
            await update.message.reply_text(
                f"🔊 Current volume: *{pct}%*{mute_str}", parse_mode="Markdown"
            )
        else:
            await update.message.reply_text("⚠️ Could not read the volume.")
        return

    try:
        volume = int(args[0].strip('%'))
        if not 0 <= volume <= 100:
            raise ValueError
    except ValueError:
        await update.message.reply_text("⚠️ Usage: `/volume 70` (a value from 0 to 100)", parse_mode="Markdown")
        return

    result = subprocess.run(
        ["wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@", f"{volume / 100:.2f}"],
        capture_output=True, text=True
    )
    if result.returncode == 0:
        logger.info("Volume set to %d%% by user %s", volume, update.effective_user.id)
        bar = "█" * (volume // 10) + "░" * (10 - volume // 10)
        await update.message.reply_text(
            f"🔊 Volume set to *{volume}%*\n`{bar}`",
            parse_mode="Markdown"
        )
    else:
        await update.message.reply_text("⚠️ Error setting the volume.")


# ---------------------------------------------------------------------------
# Brightness control
# ---------------------------------------------------------------------------
_BRIGHTNESS_DEFAULTS = {
    "auto": False, "level": 100,
    "day_level": 100, "night_level": 20,
    "day_start": "07:00", "night_start": "22:00",
}

def _brightness_load() -> dict:
    try:
        return {**_BRIGHTNESS_DEFAULTS, **json.loads(BRIGHTNESS_FILE.read_text())}
    except Exception:
        return dict(_BRIGHTNESS_DEFAULTS)

def _brightness_save(data: dict):
    BRIGHTNESS_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False))

def _brightness_bar(level: int) -> str:
    filled = round(level / 10)
    return "█" * filled + "░" * (10 - filled)

async def cmd_brightness(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /brightness — software brightness control."""
    args = context.args
    data = _brightness_load()

    if not args:
        mode = "auto" if data["auto"] else "manual"
        level = data["level"]
        bar = _brightness_bar(level)
        lines = [
            f"💡 Brightness: *{level}%* `{bar}` ({mode})",
            f"  Day: {data['day_level']}% from {data['day_start']}",
            f"  Night: {data['night_level']}% from {data['night_start']}",
            "",
            "Commands:",
            "/brightness 70 — set manually",
            "/brightness auto — automatic mode by time of day",
            "/brightness day 90 — daytime brightness",
            "/brightness night 15 — night-time brightness",
            "/brightness schedule 07:00 22:00 — change the times",
        ]
        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
        return

    cmd = args[0].lower()

    if cmd == "auto":
        data["auto"] = True
        _brightness_save(data)
        await update.message.reply_text(
            f"🌙 Automatic mode on\n"
            f"  Day: {data['day_level']}% from {data['day_start']}\n"
            f"  Night: {data['night_level']}% from {data['night_start']}"
        )
        return

    if cmd == "day" and len(args) > 1:
        try:
            v = max(0, min(100, int(args[1])))
            data["day_level"] = v
            _brightness_save(data)
            await update.message.reply_text(f"☀️ Daytime brightness → *{v}%* `{_brightness_bar(v)}`", parse_mode="Markdown")
        except ValueError:
            await update.message.reply_text("⚠️ Usage: /brightness day 80")
        return

    if cmd == "night" and len(args) > 1:
        try:
            v = max(0, min(100, int(args[1])))
            data["night_level"] = v
            _brightness_save(data)
            await update.message.reply_text(f"🌙 Night-time brightness → *{v}%* `{_brightness_bar(v)}`", parse_mode="Markdown")
        except ValueError:
            await update.message.reply_text("⚠️ Usage: /brightness night 20")
        return

    if cmd == "schedule" and len(args) >= 3:
        data["day_start"]   = args[1]
        data["night_start"] = args[2]
        _brightness_save(data)
        await update.message.reply_text(
            f"🕐 Schedule updated\n  Day from {args[1]}, night from {args[2]}"
        )
        return

    # Try as a plain number
    try:
        level = max(0, min(100, int(cmd.strip('%'))))
        data["auto"] = False
        data["level"] = level
        _brightness_save(data)
        bar = _brightness_bar(level)
        await update.message.reply_text(f"💡 Brightness → *{level}%* `{bar}`", parse_mode="Markdown")
    except ValueError:
        await update.message.reply_text(
            "⚠️ Usage:\n"
            "/brightness 70\n/brightness auto\n/brightness day 90\n"
            "/brightness night 15\n/brightness schedule 07:00 22:00"
        )


# ---------------------------------------------------------------------------
# Debug commands
# ---------------------------------------------------------------------------

async def cmd_pi(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /pi – show Pi hardware diagnostics."""
    logger.info("/pi from user id=%s", update.effective_user.id)

    def _read(path: str, default: str = "?") -> str:
        try:
            return Path(path).read_text().strip()
        except Exception:
            return default

    def _run(cmd: list[str]) -> str:
        try:
            return subprocess.run(cmd, capture_output=True, text=True, timeout=5).stdout.strip()
        except Exception:
            return "?"

    # CPU temperature
    temp_raw = _read("/sys/class/thermal/thermal_zone0/temp", "0")
    temp_c = int(temp_raw) / 1000 if temp_raw.isdigit() else 0.0
    temp_icon = "🔥" if temp_c > 70 else "🌡️"

    # CPU usage — delta-sample /proc/stat (aggregate of all cores) over a short
    # window. Uses asyncio.sleep so the event loop keeps serving other updates.
    def _cpu_times() -> tuple[int, int] | None:
        try:
            values = [int(x) for x in _read("/proc/stat").splitlines()[0].split()[1:]]
            return sum(values), values[3] + values[4]  # (total, idle + iowait)
        except Exception:
            return None

    cpu_pct = None
    before = _cpu_times()
    if before is not None:
        await asyncio.sleep(0.5)
        after = _cpu_times()
        if after is not None:
            total_delta = after[0] - before[0]
            idle_delta = after[1] - before[1]
            if total_delta > 0:
                cpu_pct = 100 * (1 - idle_delta / total_delta)
    cpu_str = f"{cpu_pct:.0f}%" if cpu_pct is not None else "?"

    # Fan speed (PWM duty cycle as % of period — see fan_control.py)
    PWM_PIN = "/sys/class/pwm/pwmchip0/pwm0"
    duty_raw = _read(f"{PWM_PIN}/duty_cycle", "")
    period_raw = _read(f"{PWM_PIN}/period", "")
    fan_pct = None
    if duty_raw.isdigit() and period_raw.isdigit() and int(period_raw) > 0:
        fan_pct = int(duty_raw) / int(period_raw) * 100
    fan_str = f"{fan_pct:.0f}%" if fan_pct is not None else "?"

    # RAM  (/proc/meminfo in kB)
    meminfo = {}
    for line in _read("/proc/meminfo", "").splitlines():
        parts = line.split()
        if len(parts) >= 2:
            meminfo[parts[0].rstrip(":")] = int(parts[1])
    mem_total = meminfo.get("MemTotal", 0) // 1024
    mem_free  = (meminfo.get("MemAvailable", 0)) // 1024
    mem_used  = mem_total - mem_free
    mem_pct   = int(mem_used / mem_total * 100) if mem_total else 0

    # SD card
    df = _run(["df", "-B1", "/"])
    sd_total = sd_used = sd_pct = 0
    for line in df.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 5:
            sd_total = int(parts[1])
            sd_used  = int(parts[2])
            sd_pct   = int(parts[4].rstrip("%"))

    # Uptime
    uptime_sec = float(_read("/proc/uptime", "0").split()[0])
    h, rem = divmod(int(uptime_sec), 3600)
    m = rem // 60
    uptime_str = f"{h}h {m}m" if h else f"{m}m"

    # Load average
    loadavg = _read("/proc/loadavg", "? ? ?")
    load1, load5, load15 = loadavg.split()[:3]

    # Service state
    svc = _run(["systemctl", "is-active", "amonavision"])
    svc_icon = "🟢" if svc == "active" else "🔴"

    await update.message.reply_text(
        f"🖥️ *Pi Diagnostics*\n\n"
        f"{temp_icon} CPU temp: *{temp_c:.1f}°C*\n"
        f"⚙️ CPU usage: *{cpu_str}*\n"
        f"🌀 Fan: *{fan_str}*\n"
        f"🧠 RAM: *{mem_used} / {mem_total} MB* ({mem_pct}%)\n"
        f"💿 SD: *{_format_size(sd_used)} / {_format_size(sd_total)}* ({sd_pct}%)\n"
        f"⏱️ Uptime: *{uptime_str}*\n"
        f"📊 Load: *{load1}  {load5}  {load15}*\n"
        f"{svc_icon} Service: *{svc}*",
        parse_mode="Markdown",
    )


async def cmd_logs(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /logs [viewer|bot|startup] – show last lines of a log file."""
    logger.info("/logs from user id=%s", update.effective_user.id)

    log_map = {
        "viewer":  LOG_DIR / "viewer.log",
        "bot":     LOG_DIR / "app.log",
        "startup": LOG_DIR / "startup.log",
    }

    arg = (context.args[0].lower() if context.args else "viewer")
    if arg not in log_map:
        await update.message.reply_text(
            "⚠️ Usage: `/logs viewer` | `/logs bot` | `/logs startup`",
            parse_mode="Markdown",
        )
        return

    log_path = log_map[arg]
    try:
        lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
        last = lines[-25:] if len(lines) >= 25 else lines
        text = "\n".join(last)
        # Telegram message limit is 4096 chars
        if len(text) > 3800:
            text = text[-3800:]
        await update.message.reply_text(
            f"📋 *{arg}.log* (last lines)\n```\n{text}\n```",
            parse_mode="Markdown",
        )
    except FileNotFoundError:
        await update.message.reply_text(f"⚠️ Log `{arg}` not found yet.", parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"⚠️ Error reading the log: {e}")


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
_TAG_RE = re.compile(r"^\s*\[(PASS|WARN|FAIL)\]\s*(.*)$")


async def cmd_test(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /test – run test_setup.sh on demand and summarise the result.

    Same suite the periodic health-check timer runs, but triggered from the
    chat. Runs as the bot's own user (av, not root), so the couple of checks
    that need sudo (4G upstream test, Tailscale self-heal) degrade to a WARN
    instead of running — everything else has full coverage.
    """
    logger.info("/test from user id=%s", update.effective_user.id)
    status_msg = await update.message.reply_text("🔍 Running diagnostics… (~15-20 s)")

    def _run() -> str:
        env = os.environ.copy()
        env["AMONAVISION_HEALTHCHECK"] = "1"
        try:
            result = subprocess.run(
                ["bash", str(PROJECT_ROOT / "test_setup.sh")],
                capture_output=True, text=True, timeout=60, env=env,
            )
            return result.stdout + result.stderr
        except subprocess.TimeoutExpired:
            return "TIMEOUT: test_setup.sh did not finish within 60s."
        except Exception as e:
            return f"ERROR running test_setup.sh: {e}"

    try:
        raw = await asyncio.to_thread(_run)
        clean = _ANSI_RE.sub("", raw)

        fails, warns, passes = [], [], 0
        for line in clean.splitlines():
            m = _TAG_RE.match(line)
            if not m:
                continue
            tag, rest = m.groups()
            if tag == "PASS":
                passes += 1
            elif tag == "WARN":
                warns.append(rest)
            elif tag == "FAIL":
                fails.append(rest)

        icon = "🔴" if fails else ("🟡" if warns else "🟢")
        lines = [f"{icon} Diagnostics: {passes} OK · {len(warns)} warnings · {len(fails)} failures\n"]
        if fails:
            lines.append("Failures:")
            lines += [f"  • {f}" for f in fails[:10]]
        if warns:
            lines.append("\nWarnings:")
            lines += [f"  • {w}" for w in warns[:10]]
        if not fails and not warns:
            lines.append("All good.")

        text = "\n".join(lines)
        if len(text) > 3900:
            text = text[:3900] + "\n… (truncated — use /logs or SSH for the full detail)"
        # Plain text (no parse_mode): test output can contain _ * [ ] which
        # would break Telegram's Markdown parser and drop the whole message.
        await status_msg.edit_text(text)
    except Exception as e:
        logger.exception("Error running /test")
        await status_msg.edit_text(f"⚠️ Error running the diagnostics: {e}")


# ---------------------------------------------------------------------------
# Media handlers – all media goes through the grouper
# ---------------------------------------------------------------------------
async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Receive a photo, download to temp, and add to the grouper."""
    user = update.effective_user
    logger.info("Photo received from %s (id=%s)", user.full_name, user.id)

    try:
        photo = update.message.photo[-1]

        # Photos are always small, but validate anyway.
        validate_file_size(photo.file_size)

        temp_path = await _download_to_received(photo.file_id, "jpg", context)

        reply_to_id = update.message.reply_to_message.message_id if update.message.reply_to_message else 0

        await grouper.add(
            user_id=user.id,
            temp_path=temp_path,
            extension="jpg",
            media_type=MEDIA_PHOTO,
            user_name=user.first_name,
            message_id=update.message.message_id,
            chat_id=update.effective_chat.id,
            reply_to_message_id=reply_to_id,
        )

    except FileTooLargeError as e:
        logger.warning("Photo too large from user %s: %s", user.id, e)
        await update.message.reply_text(
            f"⚠️ The photo is too large ({_format_size(e.file_size)}). "
            f"The limit is {_format_size(e.limit)}."
        )
    except Exception as exc:
        logger.exception("Error processing photo from user %s: %s", user.id, exc)
        await update.message.reply_text(
            "⚠️ Something went wrong processing your photo. "
            "Please try again."
        )


async def handle_video(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Receive a video, validate size, download to temp, and add to the grouper."""
    user = update.effective_user
    video = update.message.video
    logger.info(
        "Video received from %s (id=%s, size=%s, duration=%ds)",
        user.full_name, user.id,
        _format_size(video.file_size or 0),
        video.duration or 0,
    )

    try:
        # CRITICAL: Validate size BEFORE downloading to avoid wasted bandwidth.
        validate_file_size(video.file_size)

        temp_path = await _download_to_received(video.file_id, "mp4", context)

        reply_to_id = update.message.reply_to_message.message_id if update.message.reply_to_message else 0

        await grouper.add(
            user_id=user.id,
            temp_path=temp_path,
            extension="mp4",
            media_type=MEDIA_VIDEO,
            user_name=user.first_name,
            message_id=update.message.message_id,
            chat_id=update.effective_chat.id,
            reply_to_message_id=reply_to_id,
        )

    except FileTooLargeError as e:
        logger.warning("Video too large from user %s: %s", user.id, e)
        await update.message.reply_text(
            f"⚠️ The video is too large ({_format_size(e.file_size)}).\n"
            f"Telegram's limit is {_format_size(e.limit)}.\n\n"
            "💡 *Tip*: trim or compress the video before sending it.",
            parse_mode="Markdown",
        )
    except Exception as exc:
        logger.exception("Error processing video from user %s: %s", user.id, exc)
        await update.message.reply_text(
            "⚠️ Something went wrong processing your video. "
            "Please try again."
        )


async def handle_video_note(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Receive a video note (circular video), validate, download, and group.

    Telegram video notes are short circular clips recorded directly in the
    app. They are usually small (< 5 MB) and encoded as MP4.
    """
    user = update.effective_user
    video_note = update.message.video_note
    logger.info(
        "Video note received from %s (id=%s, size=%s, duration=%ds)",
        user.full_name, user.id,
        _format_size(video_note.file_size or 0),
        video_note.duration or 0,
    )

    try:
        validate_file_size(video_note.file_size)

        temp_path = await _download_to_received(video_note.file_id, "mp4", context)

        reply_to_id = update.message.reply_to_message.message_id if update.message.reply_to_message else 0

        await grouper.add(
            user_id=user.id,
            temp_path=temp_path,
            extension="mp4",
            media_type=MEDIA_VIDEO_NOTE,
            user_name=user.first_name,
            message_id=update.message.message_id,
            chat_id=update.effective_chat.id,
            reply_to_message_id=reply_to_id,
        )

    except FileTooLargeError as e:
        logger.warning("Video note too large from user %s: %s", user.id, e)
        await update.message.reply_text(
            f"⚠️ The video note is too large ({_format_size(e.file_size)})."
        )
    except Exception as exc:
        logger.exception("Error processing video note from user %s: %s", user.id, exc)
        await update.message.reply_text(
            "⚠️ Something went wrong processing your video note. "
            "Please try again."
        )


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Receive a voice note (.ogg), download to temp, and add to the grouper.

    Voice notes are only valid when linked to a photo or video session via
    Telegram reply. Standalone voice notes are rejected without saving.
    """
    user = update.effective_user
    voice = update.message.voice
    logger.info(
        "Voice note received from %s (id=%s, size=%s, duration=%ds)",
        user.full_name, user.id,
        _format_size(voice.file_size or 0),
        voice.duration or 0,
    )

    # Reject standalone voice notes (not a reply to any message)
    reply_to_id = update.message.reply_to_message.message_id if update.message.reply_to_message else 0
    if reply_to_id == 0:
        await update.message.reply_text(
            "🎙️ Voice notes must be paired with a photo or video.\n\n"
            "To link this voice note to a photo, reply directly "
            "to that photo with the voice note."
        )
        return

    # Also reject if the replied-to message is not a known photo/video session
    if db is not None:
        parent = db.get_session_by_telegram_message_id(reply_to_id)
        if not parent:
            await update.message.reply_text(
                "🎙️ I couldn't find a photo or video to link this voice note to.\n\n"
                "Reply directly to the photo or video you want to pair it with."
            )
            return

    try:
        validate_file_size(voice.file_size)

        temp_path = await _download_to_received(voice.file_id, "ogg", context)

        await grouper.add(
            user_id=user.id,
            temp_path=temp_path,
            extension="ogg",
            media_type=MEDIA_VOICE,
            user_name=user.first_name,
            message_id=update.message.message_id,
            chat_id=update.effective_chat.id,
            reply_to_message_id=reply_to_id,
        )

    except FileTooLargeError as e:
        logger.warning("Voice too large from user %s: %s", user.id, e)
        await update.message.reply_text(
            f"⚠️ The voice note is too long ({_format_size(e.file_size)})."
        )
    except Exception as exc:
        logger.exception("Error processing voice from user %s: %s", user.id, exc)
        await update.message.reply_text(
            "⚠️ Something went wrong processing your voice note. "
            "Please try again."
        )


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle files sent as documents (uncompressed photos/videos/audio)."""
    user = update.effective_user
    doc = update.message.document

    if doc.mime_type is None:
        await update.message.reply_text(
            "🤔 I don't recognise this type of file. "
            "Please send photos, videos or voice notes."
        )
        return

    mime = doc.mime_type.lower()
    logger.info(
        "Document received from %s (id=%s, mime=%s, size=%s)",
        user.full_name, user.id, mime,
        _format_size(doc.file_size or 0),
    )

    try:
        # Validate size before anything else.
        validate_file_size(doc.file_size)

        if mime.startswith("image/"):
            ext = mime.split("/")[-1]
            if ext == "jpeg":
                ext = "jpg"
            media_type = MEDIA_PHOTO
        elif mime.startswith("video/"):
            ext = mime.split("/")[-1]
            if ext == "quicktime":
                ext = "mov"
            media_type = MEDIA_VIDEO
        elif mime.startswith("audio/"):
            ext = mime.split("/")[-1]
            media_type = MEDIA_VOICE
        else:
            await update.message.reply_text(
                "🤔 I only accept photos, videos and voice notes. "
                "Please send a supported file."
            )
            return

        temp_path = await _download_to_received(doc.file_id, ext, context)

        reply_to_id = update.message.reply_to_message.message_id if update.message.reply_to_message else 0

        await grouper.add(
            user_id=user.id,
            temp_path=temp_path,
            extension=ext,
            media_type=media_type,
            user_name=user.first_name,
            message_id=update.message.message_id,
            chat_id=update.effective_chat.id,
            reply_to_message_id=reply_to_id,
        )

    except FileTooLargeError as e:
        logger.warning("Document too large from user %s: %s", user.id, e)
        await update.message.reply_text(
            f"⚠️ The file is too large ({_format_size(e.file_size)}).\n"
            f"Telegram's limit is {_format_size(e.limit)}.",
        )
    except Exception as exc:
        logger.exception("Error processing document from user %s: %s", user.id, exc)
        await update.message.reply_text(
            "⚠️ Something went wrong processing your file. "
            "Please try again."
        )


async def handle_session_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle 'delete' and 'recycle' text commands sent as replies to media sessions."""
    cmd = update.message.text.strip().lower()
    reply_to_id = update.message.reply_to_message.message_id

    if db is None:
        await update.message.reply_text("⚠️ Database not available.")
        return

    session = db.get_session_by_telegram_message_id(reply_to_id)
    if not session:
        await update.message.reply_text(
            "⚠️ I couldn't find a session for that message.\n"
            "Reply directly to the original photo or video you sent."
        )
        return

    if cmd == "delete":
        db.mark_as_hidden(session['id'])
        logger.info("Session %d hidden by user %s", session['id'], update.effective_user.id)
        await update.message.reply_text(
            "🗑️ Photo/video hidden from the frame.\n"
            "The files are still stored on the Pi.\n"
            "Reply to the same message with *recycle* to show it again.",
            parse_mode="Markdown",
        )
    elif cmd == "recycle":
        if session.get('status') != 'HIDDEN':
            await update.message.reply_text("ℹ️ This content is already visible on the frame.")
            return
        db.mark_as_visible(session['id'])
        logger.info("Session %d restored by user %s", session['id'], update.effective_user.id)
        await update.message.reply_text("♻️ Photo/video restored. It will show up on the frame again.")


async def cmd_restart(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /restart — restart the AmonaVision app (soft reboot, ~15s downtime)."""
    logger.info("/restart from user id=%s", update.effective_user.id)
    await update.message.reply_text(
        "🔄 *Soft restart* — restarting the app...\n"
        "The frame will be back in a few seconds.",
        parse_mode="Markdown",
    )
    RESTART_FLAG.touch()


async def cmd_reboot(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /reboot — full Pi reboot, then a panel power-cycle (admin only).

    `sudo reboot` never worked here (av's sudo wants a password), and a plain
    software reboot can leave the USB-powered panel on "No signal". The bot only
    drops the flag; root's tools/reboot-with-panel-cycle.sh reboots and, once
    the frame is back, cuts USB power for 10 s and confirms on Telegram.
    """
    logger.info("/reboot from user id=%s", update.effective_user.id)
    if not _is_admin_chat(update):
        await update.message.reply_text(ADMIN_ONLY_TEXT)
        return
    await update.message.reply_text(
        "🔴 *Full reboot* — rebooting the Raspberry Pi...\n"
        "Once it's back the screen is power-cycled too (USB power off for 10 s). "
        "The frame will be down for about 3 minutes; I'll let you know when it's back.",
        parse_mode="Markdown",
    )
    await asyncio.sleep(1)  # Let Telegram deliver the message before rebooting
    REBOOT_FLAG.touch()


def _local_tz() -> ZoneInfo:
    """The Pi's own time zone (DST-aware), so "10:00" means 10:00 at the frame."""
    try:
        return ZoneInfo(str(Path("/etc/localtime").resolve()).split("zoneinfo/", 1)[1])
    except Exception:
        return ZoneInfo("Europe/Berlin")


def _weekly_reminder_settings() -> tuple[bool, dt_time, str]:
    """[Telegram] weekly_reminder / weekly_reminder_time / weekly_reminder_text."""
    cfg = configparser.ConfigParser(interpolation=None)
    try:
        cfg.read(CONFIG_FILE, encoding="utf-8")
    except Exception:
        logger.exception("Could not read %s -- weekly reminder uses its defaults", CONFIG_FILE)
    enabled = cfg.getboolean("Telegram", "weekly_reminder", fallback=True)
    try:
        hh, mm = cfg.get("Telegram", "weekly_reminder_time", fallback="10:00").split(":")
        at = dt_time(int(hh), int(mm), tzinfo=_local_tz())
    except ValueError:
        logger.warning("Bad [Telegram] weekly_reminder_time -- using 10:00")
        at = dt_time(10, 0, tzinfo=_local_tz())
    text = cfg.get("Telegram", "weekly_reminder_text", fallback=WEEKLY_REMINDER_DEFAULT_TEXT).strip()
    return enabled, at, text or WEEKLY_REMINDER_DEFAULT_TEXT


async def _send_weekly_reminder(context: ContextTypes.DEFAULT_TYPE) -> None:
    """JobQueue callback: the Monday reminder to the family group."""
    try:
        await context.bot.send_message(chat_id=context.job.chat_id, text=context.job.data)
        logger.info("Weekly reminder sent to chat %s", context.job.chat_id)
    except Exception:
        logger.exception("Weekly reminder to chat %s failed", context.job.chat_id)


async def cmd_reminder(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /reminder — show the Monday reminder's state and a preview (admin only).

    The preview goes to the admin chat that asked, never to the group.
    """
    logger.info("/reminder from user id=%s", update.effective_user.id)
    if not _is_admin_chat(update):
        await update.message.reply_text(ADMIN_ONLY_TEXT)
        return
    jobs = context.job_queue.get_jobs_by_name(WEEKLY_REMINDER_JOB) if context.job_queue else []
    _, _, text = _weekly_reminder_settings()
    if jobs:
        nxt = jobs[0].next_t.astimezone(_local_tz())
        state = f"✅ On — next one: Monday {nxt:%d/%m at %H:%M}\nGroup: {jobs[0].chat_id}"
    elif not os.getenv("TELEGRAM_GROUP_CHAT_ID", "").strip():
        state = "⏸️ Off — TELEGRAM_GROUP_CHAT_ID missing from .env"
    else:
        state = "⏸️ Off — weekly_reminder = False in config.txt ([Telegram])"
    await update.message.reply_text(f"📅 Monday reminder\n{state}\n\nPreview:")
    await update.message.reply_text(text)


USAGE_BUTTONS = [("next", "▶️ Next"), ("previous", "◀️ Previous"),
                 ("play", "⏯️ Play/Pause"), ("record", "⏺️ Record")]
USAGE_RESULTS = [("sent", "sent"), ("discarded", "discarded as too short (<1 s)"),
                 ("send_error", "failed to send"), ("save_error", "failed to save"),
                 ("no_audio", "no audio")]
USAGE_BURST_GAP = timedelta(minutes=10)   # presses further apart than this = separate bursts


def _usage_report(arg: str) -> str:
    """Usage stats from app/logs/buttons.csv: today, "yesterday", or the last N days."""
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    arg = arg.strip().lower()
    if arg == "yesterday":
        since, until, title = today - timedelta(days=1), today, f"yesterday ({today - timedelta(days=1):%d/%m})"
    elif arg.isdigit() and int(arg) >= 1:
        days = min(int(arg), 365)
        since, until = today - timedelta(days=days - 1), None
        title = f"last {days} days ({since:%d/%m}–{today:%d/%m})" if days > 1 else f"today ({today:%d/%m})"
    else:
        since, until, title = today, None, f"today ({today:%d/%m})"

    rows = read_usage(since, until)
    presses = [r for r in rows if r["event"] == "press"]
    recs = [r for r in rows if r["event"] == "recording"]
    lines = [f"📊 Frame usage — {title}", ""]
    if not presses and not recs:
        lines.append("No button presses in this period.")
        return "\n".join(lines)

    lines.append(f"Presses: {len(presses)}")
    for key, label in USAGE_BUTTONS:
        n = sum(1 for r in presses if r["button"] == key)
        if n:
            lines.append(f"  {label}: {n}")

    if recs:
        lines += ["", f"🎙️ Recordings: {len(recs)}"]
        for key, label in USAGE_RESULTS:
            n = sum(1 for r in recs if r["result"] == key)
            if n:
                lines.append(f"  {n} {label}")

    stamps = [datetime.strptime(r["timestamp"], "%Y-%m-%d %H:%M:%S") for r in presses]
    multi_day = len({t.date() for t in stamps}) > 1
    if multi_day:
        lines += ["", "Per day:"]
        per_day: dict = {}
        for t in stamps:
            per_day[t.date()] = per_day.get(t.date(), 0) + 1
        for d in sorted(per_day):
            lines.append(f"  {d:%d/%m}: {per_day[d]}")

    # Bursts of use: runs of presses with no gap longer than USAGE_BURST_GAP.
    bursts = []
    for t in stamps:
        if bursts and t - bursts[-1][1] <= USAGE_BURST_GAP:
            bursts[-1][1] = t
            bursts[-1][2] += 1
        else:
            bursts.append([t, t, 1])
    if bursts:
        lines += ["", f"Bursts of use: {len(bursts)}"]
        shown = bursts[-10:]
        if len(bursts) > len(shown):
            lines.append(f"  (last {len(shown)})")
        for start, end, n in shown:
            day = f"{start:%d/%m} " if multi_day else ""
            span = f"{start:%H:%M}" if end - start < timedelta(minutes=1) else f"{start:%H:%M}–{end:%H:%M}"
            lines.append(f"  {day}{span}: {n} press" + ("es" if n != 1 else ""))
        last = stamps[-1]
        lines += ["", f"Last press: {last:%d/%m %H:%M}"]
    return "\n".join(lines)


async def cmd_usage(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /usage [yesterday|N] — button usage stats from app/logs/buttons.csv (admin only)."""
    logger.info("/usage from user id=%s", update.effective_user.id)
    if not _is_admin_chat(update):
        await update.message.reply_text(ADMIN_ONLY_TEXT)
        return
    try:
        text = _usage_report(" ".join(context.args or []))
    except Exception:
        logger.exception("/usage failed")
        text = "❌ Could not read the usage log."
    await update.message.reply_text(text)


async def cmd_chatid(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /chatid — reply with this chat's id (e.g. to set TELEGRAM_ADMIN_CHAT_ID).

    Read-only, so open to anyone; a group's id is negative (-100... for supergroups).
    """
    chat = update.effective_chat
    logger.info("/chatid from user id=%s in chat id=%s (%s)", update.effective_user.id, chat.id, chat.type)
    is_admin = _is_admin_chat(update)
    await update.message.reply_text(
        f"🆔 Chat ID: `{chat.id}`\n"
        f"Type: {chat.type}\n"
        f"Your user ID: `{update.effective_user.id}`\n"
        f"Admin chat: {'yes' if is_admin else 'no'}",
        parse_mode="Markdown",
    )


async def cmd_screen(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /screen — power-cycle the panel through the Pi's USB (admin only).

    For a panel stuck on "No signal" after a software reboot. USB power on the
    Pi 4 is ganged, so the 4G dongle drops too (~1 min): admin chat only. The
    bot (av) only drops the flag; root's tools/panel-power-cycle.sh does the
    work and confirms on Telegram once the network is back.
    """
    logger.info("/screen from user id=%s", update.effective_user.id)
    if not _is_admin_chat(update):
        await update.message.reply_text(ADMIN_ONLY_TEXT)
        return
    await update.message.reply_text(
        "🖥️ *Screen reset* — cutting USB power for 10 s...\n"
        "The frame and its 4G link will be down for 1-2 minutes. "
        "I'll let you know when it's back.",
        parse_mode="Markdown",
    )
    await asyncio.sleep(1)  # Let Telegram deliver the message before the 4G drops
    PANEL_RESET_FLAG.touch()


async def handle_unknown(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Respond to unrecognised messages."""
    await update.message.reply_text(
        "🤔 I don't understand that message.\n"
        "Send me *photos*, *videos* or *voice notes* "
        "to show them on the frame.\n"
        "Use /help for more information.",
        parse_mode="Markdown",
    )


# ---------------------------------------------------------------------------
# Error handler
# ---------------------------------------------------------------------------
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log errors caused by updates."""
    logger.error("Update caused error: %s", context.error, exc_info=context.error)


# ---------------------------------------------------------------------------
# Application entry-point
# ---------------------------------------------------------------------------
def main() -> None:
    """Build and run the bot application."""
    global grouper, _bot_instance, db

    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        logger.critical(
            "TELEGRAM_BOT_TOKEN not set. "
            "Export it as an environment variable or add it to a .env file."
        )
        raise SystemExit(1)

    logger.info("Starting AmonaVision bot …")
    logger.info("Media dir  : %s", MEDIA_RECEIVED)
    logger.info("Log file   : %s", LOG_FILE)

    # Initialise Database
    db = DatabaseManager(DB_PATH)
    logger.info("DatabaseManager initialised at %s", DB_PATH)

    # Initialise the message grouper with the flush callback.
    grouper = MessageGrouper(
        received_dir=MEDIA_RECEIVED,
        processed_dir=MEDIA_PROCESSED,
        on_flush=_on_group_flushed,
        db=db,
    )
    logger.info("Message grouper initialised")

    app = (
        Application.builder()
        .token(token)
        .connect_timeout(30)
        .read_timeout(30)
        .write_timeout(30)
        .get_updates_request(_PollTrackingRequest(connection_pool_size=1))
        .build()
    )

    # Store bot reference for the flush callback.
    _bot_instance = app.bot

    # Register handlers (order matters for MessageHandler)
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("volume", cmd_volume))
    app.add_handler(CommandHandler("brightness", cmd_brightness))
    app.add_handler(CommandHandler("pi", cmd_pi))
    app.add_handler(CommandHandler("logs", cmd_logs))
    app.add_handler(CommandHandler("test", cmd_test))
    app.add_handler(CommandHandler("restart", cmd_restart))
    app.add_handler(CommandHandler("reboot", cmd_reboot))
    app.add_handler(CommandHandler("screen", cmd_screen))
    app.add_handler(CommandHandler("chatid", cmd_chatid))
    app.add_handler(CommandHandler("reminder", cmd_reminder))
    app.add_handler(CommandHandler("usage", cmd_usage))

    # Monday reminder to the family group (PTB >= 20: days 0-6 = Sunday-Saturday).
    group_chat = os.getenv("TELEGRAM_GROUP_CHAT_ID", "").strip()
    reminder_on, reminder_at, reminder_text = _weekly_reminder_settings()
    if group_chat and reminder_on:
        try:
            app.job_queue.run_daily(
                _send_weekly_reminder, time=reminder_at, days=(1,),
                chat_id=int(group_chat), data=reminder_text, name=WEEKLY_REMINDER_JOB,
            )
            logger.info("Weekly reminder scheduled: Mondays %s to chat %s", reminder_at.strftime("%H:%M"), group_chat)
        except ValueError:
            logger.error("TELEGRAM_GROUP_CHAT_ID is not a number -- weekly reminder disabled")
    else:
        logger.info("Weekly reminder off (TELEGRAM_GROUP_CHAT_ID set: %s, weekly_reminder: %s)", bool(group_chat), reminder_on)
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.VIDEO, handle_video))
    app.add_handler(MessageHandler(filters.VIDEO_NOTE, handle_video_note))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND & filters.REPLY &
        filters.Regex(re.compile(r'^\s*(delete|recycle)\s*$', re.IGNORECASE)),
        handle_session_command,
    ))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_unknown))

    # Global error handler
    app.add_error_handler(error_handler)

    threading.Thread(target=_poll_stall_watchdog, name="poll-stall-watchdog", daemon=True).start()

    # Run with automatic reconnection on network errors
    logger.info("Bot is polling …")
    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
        bootstrap_retries=-1,   # retry indefinitely on startup if no internet yet
    )


if __name__ == "__main__":
    main()
