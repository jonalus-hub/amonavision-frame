import os
import sys
import time
import math
import json
import asyncio
import threading
import queue
import tempfile
import shutil
from pathlib import Path
import logging
import logging.handlers
from dotenv import load_dotenv
from datetime import datetime
from typing import Any, Dict, Optional
import configparser

os.environ['PYGAME_HIDE_SUPPORT_PROMPT'] = "hide"
import pygame
from app import scoring as scoring_engine
import imageio_ffmpeg
import subprocess

from ffpyplayer.player import MediaPlayer

# Project paths
PROJECT_ROOT = Path(__file__).resolve().parent
DB_PATH = PROJECT_ROOT / "app" / "db" / "amonavision.db"
MEDIA_DIR = PROJECT_ROOT / "app" / "media"
RECORDINGS_DIR = PROJECT_ROOT / "app" / "media" / "recordings"
THUMBNAILS_DIR = PROJECT_ROOT / "app" / "media" / "thumbnails"
LOG_DIR = PROJECT_ROOT / "app" / "logs"
RESTART_FLAG = Path("/tmp/amonavision_restart")
# Process start time — the daily restart only fires in a process that has been
# up a while, otherwise the instance systemd brings back at 04:00:2x is still
# inside the 04:00 window and restarts a second time.
_PROCESS_START = time.time()

# A photo the user deliberately navigated to with the arrows and then left on
# screen at least this long counts as "seen" (status UNREAD -> READ). Shorter
# than a slideshow interval so parking on an image and letting it auto-advance
# still marks it; long enough that skimming past with rapid arrow presses does
# not. Videos / voice notes are marked seen separately, when played.
SEEN_DWELL_SECONDS = 3.0
# Session types that are "an image on screen" — the dwell rule above applies to
# these. VIDEO / VIDEO_AUDIO / AUDIO need actual playback to count as seen.
SEEN_ON_DWELL_TYPES = {"PHOTO", "PHOTO_AUDIO"}

# Usage log (app/logs/buttons.csv, read by the bot's /usage): name of each button.
USAGE_BUTTONS = {
    pygame.K_RIGHT: "next",
    pygame.K_LEFT: "previous",
    pygame.K_SPACE: "play",
    pygame.K_r: "record",
}

# Voice replies shorter than this are discarded instead of sent: an accidental
# tap on the record button (or a ghost press on its GPIO) otherwise sends an
# empty voice note to the family (2026-10-02 00:13, 0.1 s).
MIN_RECORDING_SECONDS = 1.0

# Environment variables
load_dotenv(PROJECT_ROOT / ".env")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_GROUP_CHAT_ID = os.getenv("TELEGRAM_GROUP_CHAT_ID", "")

# Required directories
RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR.mkdir(parents=True, exist_ok=True)
THUMBNAILS_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    handlers=[
        logging.handlers.RotatingFileHandler(
            LOG_DIR / "viewer.log", maxBytes=1_000_000, backupCount=2, encoding="utf-8"
        ),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger("AmonaVisionViewer")
# telegram.Bot (voice replies) goes through httpx, which logs every request at
# INFO with the bot token in the URL. Keep its warnings/errors only.
logging.getLogger("httpx").setLevel(logging.WARNING)

# Settings from config.txt; any missing key falls back to its default here.
def _parse_settings(config: configparser.ConfigParser) -> dict:
    return {
        'fullscreen':         config.getboolean('Viewer', 'fullscreen',         fallback=True),
        'window_width':       config.getint    ('Viewer', 'window_width',       fallback=1280),
        'window_height':      config.getint    ('Viewer', 'window_height',      fallback=720),
        'slideshow_interval': config.getfloat  ('Viewer', 'slideshow_interval', fallback=10.0),

        'pin_previous':   config.getint('GPIO', 'pin_previous',   fallback=9),
        'pin_play_pause': config.getint('GPIO', 'pin_play_pause', fallback=17),
        'pin_record':     config.getint('GPIO', 'pin_record',     fallback=27),
        'pin_next':       config.getint('GPIO', 'pin_next',       fallback=22),

        'samplerate':    config.getint    ('Audio',  'samplerate',    fallback=48000),
        'daily_restart': config.getboolean('System', 'daily_restart', fallback=True),

        'scoring_enabled':                  config.getboolean('Scoring', 'enabled',                          fallback=True),
        'weight_novelty':                   config.getfloat  ('Scoring', 'weight_novelty',                   fallback=1.0),
        'weight_anti_repetition':           config.getfloat  ('Scoring', 'weight_anti_repetition',           fallback=1.0),
        'weight_sender_balance':            config.getfloat  ('Scoring', 'weight_sender_balance',            fallback=1.0),
        'weight_explicit_interest':         config.getfloat  ('Scoring', 'weight_explicit_interest',         fallback=1.0),
        'weight_implicit_interest':         config.getfloat  ('Scoring', 'weight_implicit_interest',         fallback=1.0),
        'novelty_halflife_days':            config.getfloat  ('Scoring', 'novelty_halflife_days',            fallback=3.0),
        'suppression_hours':                config.getfloat  ('Scoring', 'suppression_hours',                fallback=24.0),
        'sender_balance_window':            config.getint    ('Scoring', 'sender_balance_window',            fallback=20),
        'explicit_halflife_days':           config.getfloat  ('Scoring', 'explicit_halflife_days',           fallback=30.0),
        'implicit_dwell_threshold_seconds': config.getfloat  ('Scoring', 'implicit_dwell_threshold_seconds', fallback=30.0),

        'overlay_enabled':            config.getboolean('Overlay', 'enabled',            fallback=True),
        'overlay_panel_width_mm':     config.getfloat  ('Overlay', 'panel_width_mm',     fallback=230.0),
        'overlay_button_offset_cm':   config.getfloat  ('Overlay', 'button_offset_cm',   fallback=1.0),
        'overlay_bottom_margin_px':   config.getint    ('Overlay', 'bottom_margin_px',   fallback=48),
        'overlay_hide_after_seconds': config.getfloat  ('Overlay', 'hide_after_seconds', fallback=10.0),
        'overlay_pill_font_size':     config.getint    ('Overlay', 'pill_font_size',     fallback=42),
        'overlay_tutorial_mode':      config.getboolean('Overlay', 'tutorial_mode',      fallback=False),
    }


def load_config() -> dict:
    config = configparser.ConfigParser()
    try:
        config.read(PROJECT_ROOT / "config.txt", encoding='utf-8')
        logger.info("Configuration file 'config.txt' loaded successfully.")
    except Exception as e:
        logger.error(f"Error reading 'config.txt', using defaults: {e}")
    try:
        return _parse_settings(config)
    except Exception as e:
        logger.error(f"Error parsing configuration values, using default values. Error: {e}")
        return _parse_settings(configparser.ConfigParser())

SETTINGS = load_config()


sys.path.insert(0, str(PROJECT_ROOT))
from app.db.database import DatabaseManager
from app.usage_log import log_usage

# sounddevice/soundfile record the voice replies
try:
    import sounddevice as sd
    import soundfile as sf
    import numpy as np
    AUDIO_RECORDING_AVAILABLE = True
    logger.info("Audio recording available (sounddevice + soundfile)")
except Exception as e:
    AUDIO_RECORDING_AVAILABLE = False
    logger.warning(f"Audio recording NOT available. Error: {e}. Make sure sounddevice and soundfile are installed.")


# gpiozero drives the physical buttons (the recommended library on Pi OS
# Bookworm+, uses lgpio underneath)
try:
    from gpiozero import Button
    GPIO_AVAILABLE = True
    logger.info("gpiozero library available. Hardware buttons enabled.")
except (ImportError, Exception) as e:
    GPIO_AVAILABLE = False
    logger.warning("gpiozero library NOT available (%s). Hardware buttons disabled.", e)


# ---------------------------------------------------------------------------
# Send a voice note as a linked reply
# ---------------------------------------------------------------------------
async def _send_voice_note_async(
    bot_token: str,
    chat_id: str | int,
    voice_path: str,
    reply_to_message_id: int | None = None,
) -> int | None:
    """
    Send a voice note to a Telegram chat, optionally as a reply to a
    specific message (linked reply).

    Returns:
        The message_id of the sent message, or None on failure.
    """
    from telegram import Bot

    bot = Bot(token=bot_token)
    try:
        with open(voice_path, 'rb') as voice_file:
            msg = await bot.send_voice(
                chat_id=chat_id,
                voice=voice_file,
                reply_to_message_id=reply_to_message_id,
            )
        logger.info(
            "Voice note sent to chat %s (msg_id=%d, reply_to=%s)",
            chat_id, msg.message_id, reply_to_message_id,
        )
        return msg.message_id
    except Exception:
        logger.exception(
            "Failed to send voice note to chat %s (reply_to=%s)",
            chat_id, reply_to_message_id,
        )
        return None


class PrefetchCache:
    def __init__(self, media_dir: Path):
        self.media_dir = media_dir
        # RAM-backed, so prefetching never touches the SD card
        self.cache_dir = Path("/dev/shm/amonavision_cache")
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._prefetch_thread: threading.Thread | None = None
        self._prefetched_session_id: int | None = None
        logger.info(f"PrefetchCache initialized. Cache directory: {self.cache_dir}")

    def get_path(self, file_path_str: str) -> Path:
        """
        Return the file's path in the RAM cache if it is there (complete),
        otherwise its original path on the SD card.
        """
        p = Path(file_path_str)
        cached = self.cache_dir / p.name
        if cached.exists():
            return cached
        return self.media_dir / file_path_str

    def prefetch_session(self, session_data: Optional[Dict[str, Any]], current_session_data: Optional[Dict[str, Any]] = None):
        """
        Prefetch the given session's files into the RAM cache in a background thread.
        """
        if not session_data:
            return

        session_id = session_data.get('id')
        if session_id == self._prefetched_session_id:
            # Already prefetched / being prefetched
            return

        self._prefetched_session_id = session_id

        def _do_prefetch():
            try:
                files = session_data.get('files', [])
                target_names = {Path(f['file_path']).name for f in files}

                # Keep the next session's thumbnails
                for f in files:
                    target_names.add(f"thumb_{Path(f['file_path']).name}.jpg")

                # Keep the files and thumbnails of the session on screen
                if current_session_data:
                    current_files = current_session_data.get('files', [])
                    for f in current_files:
                        target_names.add(Path(f['file_path']).name)
                        target_names.add(f"thumb_{Path(f['file_path']).name}.jpg")

                # Drop old files
                for cached_file in self.cache_dir.iterdir():
                    if cached_file.is_file() and cached_file.name not in target_names:
                        # Skip temp files that are still being written
                        if cached_file.name.endswith('_tmp') or '_tmp.' in cached_file.name:
                            continue
                        try:
                            cached_file.unlink(missing_ok=True)
                            logger.debug(f"Removed old cache file: {cached_file.name}")
                        except Exception as e:
                            logger.warning(f"Error removing cached file {cached_file.name}: {e}")

                # Copy the new files to the RAM cache
                for f in files:
                    src = self.media_dir / f['file_path']
                    dest = self.cache_dir / Path(f['file_path']).name
                    if src.exists():
                        if not dest.exists():
                            logger.debug(f"Prefetching {src.name} to RAM cache...")
                            dest_tmp = dest.with_name(dest.name + "_tmp")
                            try:
                                shutil.copy2(str(src), str(dest_tmp))
                                os.replace(str(dest_tmp), str(dest))
                                logger.debug(f"Prefetched {src.name} successfully.")
                            except Exception as e:
                                logger.error(f"Error prefetching {src.name}: {e}")
                                if dest_tmp.exists():
                                    try:
                                        dest_tmp.unlink(missing_ok=True)
                                    except Exception:
                                        pass

                        # For a video, also prefetch its persistent thumbnail if there is one
                        if f.get('file_type') == 'VIDEO':
                            thumb_name = f"thumb_{src.name}.jpg"
                            persistent_thumb = THUMBNAILS_DIR / thumb_name
                            ram_thumb = self.cache_dir / thumb_name
                            if persistent_thumb.exists() and not ram_thumb.exists():
                                logger.debug(f"Prefetching persistent thumbnail {thumb_name} to RAM cache...")
                                try:
                                    shutil.copy2(str(persistent_thumb), str(ram_thumb))
                                    logger.debug(f"Prefetched thumbnail {thumb_name} successfully.")
                                except Exception as e:
                                    logger.error(f"Error prefetching thumbnail {thumb_name}: {e}")
                    else:
                        logger.warning(f"Source file for prefetch does not exist: {src}")
            except Exception as e:
                logger.error(f"Error in prefetch thread: {e}")

        self._prefetch_thread = threading.Thread(target=_do_prefetch, daemon=True)
        self._prefetch_thread.start()

    def cleanup(self):
        """Delete the cache directory and everything in it."""
        logger.info("Cleaning up PrefetchCache...")
        try:
            if self.cache_dir.exists():
                shutil.rmtree(str(self.cache_dir), ignore_errors=True)
                logger.info("PrefetchCache cleaned up successfully.")
        except Exception as e:
            logger.error(f"Error during PrefetchCache cleanup: {e}")


class ViewerApp:
    def __init__(self):
        # Large buffer (4096 samples = ~85ms at 48kHz) prevents underruns during
        # CPU-heavy video frame decode+blit on Pi 4. pre_init must come before
        # pygame.init(), which opens the mixer itself — any later init() is a no-op.
        pygame.mixer.pre_init(frequency=48000, size=-16, channels=2, buffer=4096)
        pygame.init()
        try:
            pygame.display.init()
        except pygame.error as e:
            raise RuntimeError(f"Display init failed (SDL_VIDEODRIVER={os.environ.get('SDL_VIDEODRIVER','unset')}): {e}") from e
        try:
            pygame.mixer.init(frequency=48000, size=-16, channels=2, buffer=4096)
            self._audio_available = True
        except pygame.error as e:
            logger.warning("Audio init failed: %s. Running without audio.", e)
            self._audio_available = False

        # Display set up from config.txt.
        # In kmsdrm fullscreen mode, let SDL use the display's native EDID resolution
        # to avoid unreliable DRM mode-switching on warm restarts.
        if SETTINGS['fullscreen']:
            info = pygame.display.Info()
            self.w, self.h = info.current_w, info.current_h
            flags = pygame.FULLSCREEN | pygame.DOUBLEBUF
        else:
            self.w = SETTINGS['window_width']
            self.h = SETTINGS['window_height']
            flags = pygame.DOUBLEBUF

        self.screen = pygame.display.set_mode((self.w, self.h), flags)
        pygame.display.set_caption("AmonaVision Viewer")
        pygame.mouse.set_visible(False)
        pygame.mouse.set_cursor(pygame.cursors.Cursor((8, 8), (0, 0), b'\x00'*8, b'\x00'*8))
        self.clock = pygame.time.Clock()
        self._dim_overlay = pygame.Surface((self.w, self.h))
        self._dim_overlay.fill((0, 0, 0))

        # Show splash immediately — before DB/GPIO init runs
        self.bg_color = (0, 0, 0)
        self.send_status = None
        self._spinner_angle = 0.0
        self._splash_start = time.time()
        self._icons = {}
        self._logo_surf = self._load_logo()
        self._load_icons()
        self.state = "SPLASH"  # transitions once ready
        self.screen.fill(self.bg_color)
        self._draw_splash()
        pygame.display.flip()

        self.db = DatabaseManager(DB_PATH)

        # RAM prefetch cache
        self.cache = PrefetchCache(MEDIA_DIR)

        # --- Idle-render optimization ---
        # We only redraw/flip when something actually changed, instead of blitting
        # and flipping at 30 FPS forever. Any code path that mutates what should be
        # on screen (new image, state change, badge, brightness) MUST set
        # self._needs_redraw = True. See _is_animating() for the "always redraw"
        # states. A periodic forced redraw (see run()) self-heals if a future change
        # ever forgets to set the flag.
        self._needs_redraw = True
        self._last_forced_redraw = time.time()
        self._font_cache: dict[int, "pygame.font.Font"] = {}
        self._icon_cache: dict[tuple[str, int], pygame.Surface] = {}

        # --- Button-hint overlays ("Play" / "Reply") ---
        # Offset of each pill from screen centre, in render pixels: the whole
        # width self.w maps to the whole panel (panel_width_mm), so
        # px_per_cm = self.w / panel_width_mm * 10, whatever the resolution.
        self._overlay_enabled = bool(SETTINGS.get('overlay_enabled', True))
        _pw_mm = SETTINGS.get('overlay_panel_width_mm', 230.0) or 230.0
        self._btn_offset_px = int(round(
            SETTINGS.get('overlay_button_offset_cm', 1.0) * 10.0 * (self.w / _pw_mm)
        ))
        self._overlay_bottom_margin = SETTINGS.get('overlay_bottom_margin_px', 48)
        # The "Play" / "Reply" hints only show for this many seconds
        # after a real button press — during untouched idle slideshow the
        # screen shows the photo alone, nothing else. 0.0 so they start
        # hidden; a KEYDOWN from an actual button sets _last_input_time.
        self._overlay_hide_after = SETTINGS.get('overlay_hide_after_seconds', 10.0)
        self._overlay_pill_font_size = SETTINGS.get('overlay_pill_font_size', 42)
        # Tutorial mode: hints stay up permanently, ignoring _overlay_hide_after —
        # for someone still learning the buttons. Toggle in config.txt / the
        # dashboard, not meant to be left on for normal day-to-day use.
        self._tutorial_mode = bool(SETTINGS.get('overlay_tutorial_mode', False))
        self._last_input_time = 0.0
        self._overlay_was_visible = False  # tracked to redraw on the hide transition
        # True once the current session has been played this time it was shown
        # (reset in load_session). Drives "Play" -> "Reply".
        self._current_played = False
        # True when the current session was reached by a manual arrow press
        # rather than the slideshow / scoring auto-advance. Consumed in
        # load_session() to decide whether leaving it should mark it seen.
        self._nav_was_manual = False
        # Shuffled ticket bag for scored auto-advance (see load_scored_session
        # / scoring_engine.build_round) — consumed one session per cycle and
        # rebuilt with fresh scores once empty.
        self._score_round: list = []

        self.current_session = None
        self.current_image = None
        self.img_x = 0
        self.img_y = 0
        self.video_player: MediaPlayer | None = None  # ffpyplayer — handles video + audio itself
        self.video_path = None           # Path of the active .mp4
        self.audio_path = None           # Path of a paired (external) .ogg voice note
        self.current_video_frame = None  # Last rendered frame
        self.video_x = 0
        self.video_y = 0
        self._video_surf: pygame.Surface | None = None  # Pre-allocated surface reused every frame

        # Brightness overlay (software dimming, 0-100)
        self._brightness = 100
        self._brightness_file = Path("app/brightness.json")
        self._brightness_mtime = 0.0
        self._brightness_last_check = 0.0

        self.last_viewed_image = None
        self.last_img_x = 0
        self.last_img_y = 0
        self.slideshow_interval = SETTINGS['slideshow_interval']  # Seconds before moving to the next photo
        self.slide_start_time = time.time()

        # --- Linked reply state ---
        self.current_telegram_msg_id = None   # message_id of the original message
        self.current_telegram_chat_id = None  # chat_id of the original chat

        # --- Scoring / dwell tracking ---
        self._session_display_start = time.time()

        # --- Recording state ---
        self._audio_stream = None
        self._audio_play_start = 0.0
        self._no_voice_flash_time = 0.0
        self.is_recording = False
        self.recording_start_time = 0.0
        self.recorded_frames = []     # Captured audio frames
        self.recording_samplerate = SETTINGS['samplerate']

        self.recording_channels = 1
        self.current_recording_path = None  # Path of the recorded OGG

        # --- Send feedback state ---
        self.send_status = None       # None, "sending", "sent", "error"
        self.send_status_time = 0.0   # When it last changed (for auto-hide)

        # --- GPIO Buttons Setup ---
        self._gpio_queue = queue.Queue()  # Thread-safe queue for GPIO → main-thread events
        self._gpio_buttons = []  # Keep the Button objects referenced so they aren't destroyed
        if GPIO_AVAILABLE:
            self.setup_gpio_buttons()

    def _load_logo(self) -> pygame.Surface | None:
        logo_path = PROJECT_ROOT / "assets" / "logo.png"
        if not logo_path.exists():
            return None
        try:
            img = pygame.image.load(str(logo_path)).convert_alpha()
            max_w = int(self.w * 0.55)
            max_h = int(self.h * 0.40)
            scale = min(max_w / img.get_width(), max_h / img.get_height(), 1.0)
            new_size = (int(img.get_width() * scale), int(img.get_height() * scale))
            return pygame.transform.smoothscale(img, new_size)
        except Exception as e:
            logger.warning("Could not load logo.png: %s", e)
            return None

    def _load_icons(self):
        icons_dir = PROJECT_ROOT / "assets" / "icons"
        for name in ("rec", "voice", "no_voice"):
            path = icons_dir / f"{name}.png"
            if path.exists():
                try:
                    self._icons[name] = pygame.image.load(str(path)).convert_alpha()
                except Exception as e:
                    logger.warning("Could not load icon '%s': %s", name, e)
        logger.info("Icons loaded: %s", list(self._icons.keys()))

    def _draw_splash(self):
        cx, cy = self.w // 2, self.h // 2

        if self._logo_surf:
            logo_rect = self._logo_surf.get_rect(center=(cx, cy - 55))
            self.screen.blit(self._logo_surf, logo_rect)
            spinner_cy = logo_rect.bottom + 55
        else:
            font = self._get_font(80)
            txt = font.render("AmonaVision", True, (220, 220, 220))
            self.screen.blit(txt, txt.get_rect(center=(cx, cy - 40)))
            spinner_cy = cy + 40

        r = 30
        arc_rect = pygame.Rect(cx - r, spinner_cy - r, r * 2, r * 2)
        a = math.radians(self._spinner_angle)
        pygame.draw.arc(self.screen, (200, 200, 200), arc_rect, a, a + math.radians(290), 4)
        self._spinner_angle = (self._spinner_angle + 6) % 360

    def setup_gpio_buttons(self):
        """Set up the GPIO buttons with gpiozero (Pi OS Bookworm+)."""
        try:
            # GPIO (BCM) pin -> Pygame key, from config.txt
            gpio_mapping = {
                SETTINGS['pin_previous']: pygame.K_LEFT,
                SETTINGS['pin_play_pause']: pygame.K_SPACE,
                SETTINGS['pin_record']: pygame.K_r,
                SETTINGS['pin_next']: pygame.K_RIGHT,
            }

            configured_count = 0
            for pin, key in gpio_mapping.items():
                try:
                    # gpiozero.Button enables the internal pull-up and detects the press (falling edge).
                    # bounce_time in seconds (Cherry MX: <5ms bounce; 20ms is safe and responsive)
                    btn = Button(pin, pull_up=True, bounce_time=0.02)

                    # when_pressed runs in a gpiozero thread: queue the key so the
                    # main Pygame thread injects it as an event.
                    def on_press(k=key, p=pin):
                        logger.info("GPIO: Button on pin %d pressed -> Enqueuing key (%s)", p, pygame.key.name(k))
                        self._gpio_queue.put((pygame.KEYDOWN, k))
                        self._wake_main_loop()

                    def on_release(k=key, p=pin):
                        self._gpio_queue.put((pygame.KEYUP, k))
                        self._wake_main_loop()

                    btn.when_pressed = on_press
                    btn.when_released = on_release
                    self._gpio_buttons.append(btn)  # keep it alive
                    configured_count += 1
                    logger.info("GPIO pin %d configured OK (-> %s)", pin, pygame.key.name(key))
                except Exception as e:
                    logger.error("Failed to configure GPIO pin %d: %s", pin, e)

            logger.info("GPIO buttons: %d/%d configured successfully.", configured_count, len(gpio_mapping))
        except Exception as e:
            logger.error("Error setting up GPIO buttons: %s", e)

    def scale_surface(self, surface, smooth=True):
        """Scale a surface to the screen size, keeping its aspect ratio (letterboxing)."""
        s_w, s_h = surface.get_size()
        if s_w == self.w and s_h == self.h:
            return surface, 0, 0

        aspect_ratio = s_w / s_h
        screen_ratio = self.w / self.h

        if aspect_ratio > screen_ratio:
            new_w = self.w
            new_h = int(self.w / aspect_ratio)
        else:
            new_h = self.h
            new_w = int(self.h * aspect_ratio)

        if s_w == new_w and s_h == new_h:
            x = (self.w - new_w) // 2
            y = (self.h - new_h) // 2
            return surface, x, y

        if smooth:
            scaled = pygame.transform.smoothscale(surface, (new_w, new_h))
        else:
            scaled = pygame.transform.scale(surface, (new_w, new_h))
        x = (self.w - new_w) // 2
        y = (self.h - new_h) // 2
        return scaled, x, y

    def load_session(self, session_data) -> bool:
        # Any successful or failed navigation changes what belongs on screen.
        self._needs_redraw = True
        # New session on screen -> back to the "Play" hint.
        self._current_played = False

        # Record dwell time for the session we are leaving
        if self.current_session:
            dwell = time.time() - self._session_display_start
            if dwell >= 1.0:
                try:
                    self.db.record_shown(self.current_session['id'], dwell)
                except Exception:
                    pass
            # A photo counts as "seen" once the user has arrowed to it on
            # purpose and stayed on it (rather than skimming past). Videos and
            # voice notes need actual playback — they are marked seen in
            # play_current_media() instead.
            if (self._nav_was_manual and dwell >= SEEN_DWELL_SECONDS
                    and self.current_session.get('type') in SEEN_ON_DWELL_TYPES
                    and self.current_session.get('status') == 'UNREAD'):
                try:
                    self.db.mark_as_read(self.current_session['id'])
                except Exception as e:
                    logger.error(f"Error marking as read (manual nav): {e}")

        self._session_display_start = time.time()
        self.slide_start_time = time.time()
        if not session_data:
            self.state = "IDLE"
            self.current_session = None
            return False

        self.current_session = session_data
        self.state = "WAITING_ACTION"
        self.current_image = None
        self.current_video_frame = None
        self.video_path = None
        self.audio_path = None
        self._close_video_player()   # Release the previous player, if any
        self.current_telegram_msg_id = session_data.get('telegram_message_id')
        self.current_telegram_chat_id = session_data.get('telegram_chat_id')

        files = session_data.get('files', [])

        photo_path = None
        video_path = None
        audio_path = None

        for f in files:
            p = self.cache.get_path(f['file_path'])
            if not p.exists():
                logger.warning(f"File not found: {p}")
                continue
            if f['file_type'] == 'IMAGE':
                photo_path = p
            elif f['file_type'] == 'VIDEO':
                video_path = p
            elif f['file_type'] == 'AUDIO':
                audio_path = p

        # If no visual file was found, the media was deleted from disk.
        # Remove the session from the database so it stops being retried every cycle.
        if not photo_path and not video_path:
            session_id = session_data['id']
            missing = [f['file_path'] for f in files
                       if not self.cache.get_path(f['file_path']).exists()]
            if missing:
                logger.warning(
                    "Session %d: media files deleted from disk (%s) — removing from database.",
                    session_id, ", ".join(missing)
                )
                try:
                    self.db.delete_session(session_id)
                except Exception:
                    logger.exception("Failed to delete orphaned session %d from database.", session_id)
            else:
                logger.warning("Session %d has no photo or video — skipping.", session_id)
            return False

        # Load the media into memory
        if photo_path:
            try:
                img = pygame.image.load(str(photo_path)).convert()
                self.current_image, self.img_x, self.img_y = self.scale_surface(img, smooth=True)
                self.last_viewed_image = self.current_image
                self.last_img_x, self.last_img_y = self.img_x, self.img_y
            except Exception as e:
                logger.error(f"Error loading image {photo_path}: {e}")
                self.current_image = None

        if video_path:
            self.video_path = video_path
            if not photo_path:
                # Is there already a thumbnail, in the RAM cache or on disk?
                ram_thumb_path = self.cache.cache_dir / f"thumb_{video_path.name}.jpg"
                persistent_thumb_path = THUMBNAILS_DIR / f"thumb_{video_path.name}.jpg"

                loaded = False
                for path in (ram_thumb_path, persistent_thumb_path):
                    if path.exists():
                        try:
                            img = pygame.image.load(str(path)).convert()
                            self.current_image, self.img_x, self.img_y = self.scale_surface(img, smooth=True)
                            self.last_viewed_image = self.current_image
                            self.last_img_x, self.last_img_y = self.img_x, self.img_y
                            logger.debug(f"Loaded cached thumbnail from {path.name} ({'RAM' if path == ram_thumb_path else 'Persistent'})")
                            loaded = True

                            # Loaded from disk but not in RAM yet: copy it to the RAM cache
                            if path == persistent_thumb_path and not ram_thumb_path.exists():
                                try:
                                    shutil.copy2(str(persistent_thumb_path), str(ram_thumb_path))
                                    logger.debug(f"Copied persistent thumbnail to RAM cache: {ram_thumb_path.name}")
                                except Exception as e:
                                    logger.warning(f"Error copying thumbnail to RAM cache: {e}")
                            break
                        except Exception as e:
                            logger.error(f"Error loading thumbnail from {path}: {e}")
                            try:
                                path.unlink(missing_ok=True)
                            except Exception:
                                pass

                if not loaded:
                    self.current_image = None
                    self._start_bg_thumbnail_extraction(video_path, persistent_thumb_path, ram_thumb_path)

        if audio_path:
            # Voice note paired with the video or photo
            self.audio_path = str(audio_path)

        if not self.audio_path and not self.video_path and not self.current_image:
            logger.warning(f"Session {session_data['id']} loaded but has no valid media.")
            return False
        else:
            # Prefetch the next optimized session
            next_session = self.db.get_next_session(self.current_session['id'], only_optimized=True)
            if not next_session:
                next_session = self.db.get_first_session(only_optimized=True)
            if next_session and next_session['id'] != self.current_session['id']:
                self.cache.prefetch_session(next_session, self.current_session)
            return True

    def load_next_valid_session(self) -> bool:
        """
        Find and load the next session that has valid media files.
        Returns True if one was loaded, False if none is valid.
        """
        curr_id = self.current_session['id'] if self.current_session else None

        next_session = None
        if curr_id is not None:
            next_session = self.db.get_next_session(curr_id, only_optimized=True)
        if not next_session:
            next_session = self.db.get_first_session(only_optimized=True)

        if not next_session:
            self.state = "IDLE"
            self.current_session = None
            return False

        visited_ids = set()
        current_to_try = next_session

        while current_to_try and current_to_try['id'] not in visited_ids:
            visited_ids.add(current_to_try['id'])
            if self.load_session(current_to_try):
                return True

            current_to_try = self.db.get_next_session(current_to_try['id'], only_optimized=True)
            if not current_to_try:
                current_to_try = self.db.get_first_session(only_optimized=True)

        logger.warning("No sessions with valid media files found in database/disk.")
        self.state = "IDLE"
        self.current_session = None
        return False

    def load_previous_valid_session(self) -> bool:
        """
        Find and load the previous session that has valid media files.
        Returns True if one was loaded, False if none is valid.
        """
        curr_id = self.current_session['id'] if self.current_session else None

        prev_session = None
        if curr_id is not None:
            prev_session = self.db.get_previous_session(curr_id, only_optimized=True)
        if not prev_session:
            prev_session = self.db.get_previous_session(None, only_optimized=True)

        if not prev_session:
            self.state = "IDLE"
            self.current_session = None
            return False

        visited_ids = set()
        current_to_try = prev_session

        while current_to_try and current_to_try['id'] not in visited_ids:
            visited_ids.add(current_to_try['id'])
            if self.load_session(current_to_try):
                return True

            current_to_try = self.db.get_previous_session(current_to_try['id'], only_optimized=True)
            if not current_to_try:
                current_to_try = self.db.get_previous_session(None, only_optimized=True)

        logger.warning("No sessions with valid media files found in database/disk.")
        self.state = "IDLE"
        self.current_session = None
        return False

    def load_scored_session(self) -> bool:
        """Auto-advance using the scoring engine. Draws from self._score_round
        — a shuffled ticket bag built by scoring_engine.build_round() and
        refilled whenever it runs dry — rather than re-scoring from scratch
        every single cycle. Falls back to sequential if disabled, if no
        session is eligible, or on any scoring error."""
        if not SETTINGS.get('scoring_enabled'):
            self._score_round = []
            return self.load_next_valid_session()

        try:
            current_id = self.current_session['id'] if self.current_session else None

            if not self._score_round:
                candidates = self.db.get_all_sessions_for_scoring()
                recent_senders = self.db.get_recent_senders(SETTINGS['sender_balance_window'])
                self._score_round = scoring_engine.build_round(
                    candidates, current_id, recent_senders, SETTINGS
                )

            if self._score_round:
                chosen = self._score_round.pop(0)
                full_session = self.db.get_session_by_id(chosen['id'])
                if full_session and self.load_session(full_session):
                    logger.debug(
                        "Scored next: session %d (sender=%s, %d left in round)",
                        chosen['id'], chosen.get('sender_id'), len(self._score_round),
                    )
                    return True
        except Exception:
            logger.exception("Scoring error — falling back to sequential")
            self._score_round = []

        return self.load_next_valid_session()

    # ------------------------------------------------------------------
    # Brightness control
    # ------------------------------------------------------------------
    def _update_brightness(self):
        """Read app/brightness.json and apply dimming overlay alpha."""
        DEFAULT = {"auto": False, "level": 100,
                   "day_level": 100, "night_level": 20,
                   "day_start": "07:00", "night_start": "22:00"}
        try:
            mtime = self._brightness_file.stat().st_mtime
        except Exception:
            mtime = 0.0

        data = DEFAULT
        if mtime != self._brightness_mtime:
            self._brightness_mtime = mtime
            try:
                data = {**DEFAULT, **json.loads(self._brightness_file.read_text())}
            except Exception:
                pass

        if data.get("auto"):
            now = datetime.now().strftime("%H:%M")
            day_start   = data.get("day_start",   "07:00")
            night_start = data.get("night_start", "22:00")
            # Night if before day_start or at/after night_start
            if now >= night_start or now < day_start:
                level = int(data.get("night_level", 20))
            else:
                level = int(data.get("day_level", 100))
        else:
            level = int(data.get("level", 100))

        level = max(0, min(100, level))
        if level != self._brightness:
            self._brightness = level
            if self._dim_overlay is not None:
                self._dim_overlay.set_alpha(int((1 - level / 100) * 255))
            self._needs_redraw = True

    # ------------------------------------------------------------------
    # Video player (ffpyplayer) helpers
    # ------------------------------------------------------------------
    def _close_video_player(self):
        """Close and release the current MediaPlayer, if any."""
        if self.video_player is not None:
            try:
                self.video_player.close_player()
                logger.debug("MediaPlayer closed and resources released.")
            except Exception as e:
                logger.warning(f"Error closing MediaPlayer: {e}")
            finally:
                self.video_player = None

    def _start_bg_thumbnail_extraction(self, video_path: Path, persistent_thumb_path: Path, ram_thumb_path: Path):
        """Extract a thumbnail in a background thread."""
        threading.Thread(
            target=self._bg_extract_thumbnail,
            args=(video_path, persistent_thumb_path, ram_thumb_path),
            daemon=True
        ).start()

    def _bg_extract_thumbnail(self, video_path: Path, persistent_thumb_path: Path, ram_thumb_path: Path):
        """Grab a video's first frame and save it to disk (runs in the background)."""
        try:
            logger.info(f"Starting background thumbnail extraction for: {video_path.name}")
            thumb_player = MediaPlayer(
                str(video_path),
                ff_opts={
                    'an': True,
                    'vf': '',
                    'fast': True,
                },
            )
            frame = None
            t_start = time.time()
            while time.time() - t_start < 4.0:
                f, val = thumb_player.get_frame()
                if val == 'eof':
                    break
                if f is not None:
                    frame = f
                    break
                time.sleep(0.03)
            thumb_player.close_player()

            if frame is not None:
                img_array, _ = frame
                w, h = img_array.get_size()
                raw = img_array.to_bytearray()[0]

                # Build the Pygame surface
                surf = pygame.image.frombuffer(raw, (w, h), 'RGB')

                # Write a temp file, then rename it atomically into persistent storage
                tmp_path = persistent_thumb_path.with_name(persistent_thumb_path.stem + "_tmp" + persistent_thumb_path.suffix)
                pygame.image.save(surf, str(tmp_path))
                os.replace(str(tmp_path), str(persistent_thumb_path))
                logger.info(f"Successfully saved extracted thumbnail to persistent storage: {persistent_thumb_path.name}")

                # Copy it to the RAM cache
                try:
                    shutil.copy2(str(persistent_thumb_path), str(ram_thumb_path))
                    logger.debug(f"Copied new thumbnail to RAM cache: {ram_thumb_path.name}")
                except Exception as e:
                    logger.warning(f"Error copying new thumbnail to RAM: {e}")
            else:
                logger.warning(f"Could not extract frame for thumbnail from {video_path.name}")
        except Exception as e:
            logger.error(f"Error in background thumbnail extraction for {video_path.name}: {e}")


    # ------------------------------------------------------------------
    # Audio Recording
    # ------------------------------------------------------------------
    def start_recording(self):
        """Start recording audio from the microphone."""
        if not AUDIO_RECORDING_AVAILABLE:
            logger.error("Cannot record: sounddevice/soundfile not installed")
            return
        if self.is_recording:
            return

        # The slideshow pauses while recording
        self._needs_redraw = True
        self.is_recording = True
        self.recording_start_time = time.time()
        self.recorded_frames = []
        self.state = "RECORDING"

        # Stop any audio that is playing
        if self._audio_available and pygame.mixer.music.get_busy():
            pygame.mixer.music.stop()

        logger.info("Recording started for session %s",
                     self.current_session['id'] if self.current_session else '?')

        # Capture through a callback stream (doesn't block the game loop)
        try:
            self._audio_stream = sd.InputStream(
                samplerate=self.recording_samplerate,
                channels=self.recording_channels,
                dtype='int16',
                callback=self._audio_callback,
            )
            self._audio_stream.start()
        except Exception:
            logger.exception("Failed to start audio recording")
            self.is_recording = False
            self.state = "WAITING_ACTION"

    def _audio_callback(self, indata, frames, time_info, status):
        """sounddevice callback — runs in an internal thread."""
        if status:
            logger.warning("Audio callback status: %s", status)
        self.recorded_frames.append(indata.copy())

    def stop_recording(self):
        """Stop recording and save the file."""
        if not self.is_recording:
            return

        self._needs_redraw = True
        self.is_recording = False

        # Stop the stream
        try:
            if self._audio_stream is not None:
                self._audio_stream.stop()
                self._audio_stream.close()
                self._audio_stream = None
        except Exception:
            logger.exception("Error stopping audio stream")

        duration = time.time() - self.recording_start_time
        logger.info("Recording stopped (%.1fs)", duration)

        if not self.recorded_frames:
            logger.warning("No audio frames captured")
            log_usage("recording", "record", state="RECORDING", session=self.current_session,
                      duration_s=duration, result="no_audio")
            self.state = "WAITING_ACTION"
            self.slide_start_time = time.time()
            return

        if duration < MIN_RECORDING_SECONDS:
            logger.info("Recording too short (%.1fs < %.0fs) — discarded, not sent",
                        duration, MIN_RECORDING_SECONDS)
            log_usage("recording", "record", state="RECORDING", session=self.current_session,
                      duration_s=duration, result="discarded")
            self.recorded_frames = []
            self.state = "WAITING_ACTION"
            self.slide_start_time = time.time()
            return

        # Concatenate the frames and save as WAV
        audio_data = np.concatenate(self.recorded_frames, axis=0)
        self.recorded_frames = []

        now_str = datetime.now().strftime("%Y%m%d_%H%M%S")
        session_id = self.current_session['id'] if self.current_session else 0
        wav_path = RECORDINGS_DIR / f"reply_{now_str}_s{session_id}.wav"
        ogg_path = RECORDINGS_DIR / f"reply_{now_str}_s{session_id}.ogg"

        try:
            sf.write(str(wav_path), audio_data, self.recording_samplerate)
            logger.info("WAV saved: %s (%.1fs)", wav_path.name, duration)
        except Exception:
            logger.exception("Failed to save WAV recording")
            log_usage("recording", "record", state="RECORDING", session=self.current_session,
                      duration_s=duration, result="save_error")
            self.state = "WAITING_ACTION"
            self.slide_start_time = time.time()
            return

        # Convert WAV → OGG with ffmpeg
        try:
            ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
            subprocess.run(
                [ffmpeg_exe, "-y", "-i", str(wav_path),
                 "-acodec", "libvorbis", "-q:a", "4",
                 str(ogg_path)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=True,
            )
            logger.info("OGG converted: %s", ogg_path.name)
            # Delete the temporary WAV
            wav_path.unlink(missing_ok=True)
        except Exception:
            logger.exception("Failed to convert WAV to OGG, sending WAV instead")
            ogg_path = wav_path  # Fallback: send the WAV

        self.current_recording_path = str(ogg_path)

        # Record explicit interest: user replied with a voice note to this session
        if self.current_session:
            try:
                self.db.record_voice_reply(self.current_session['id'])
            except Exception:
                pass

        self.state = "WAITING_ACTION"
        self.slide_start_time = time.time()

        # Send the voice note to Telegram
        self._send_recording_async(duration)

    def _send_recording_async(self, duration: float | None = None):
        """Send the recording from a separate thread so Pygame doesn't block."""
        self.send_status = "sending"
        self.send_status_time = time.time()
        session = self.current_session   # the slide may change while sending

        def _do_send():
            send_start = time.time()
            success = self.send_voice_reply(self.current_recording_path)
            log_usage("recording", "record", state="RECORDING", session=session,
                      duration_s=duration, result="sent" if success else "send_error")
            # Keep "sending" visible for at least 1.5 s so it doesn't flash
            remaining = 1.5 - (time.time() - send_start)
            if remaining > 0:
                time.sleep(remaining)
            if success:
                self.send_status = "sent"
            else:
                self.send_status = "error"
            self.send_status_time = time.time()
            self.state = "WAITING_ACTION"
            self.slide_start_time = time.time()
            self._needs_redraw = True

        thread = threading.Thread(target=_do_send, daemon=True)
        thread.start()

    def send_voice_reply(self, voice_path: str) -> bool:
        """
        Send a voice note to Telegram as a reply linked to the original
        message of the session currently on screen.

        Target chat_id:
        1. The session's telegram_chat_id, if it has one (reply in context)
        2. Otherwise TELEGRAM_GROUP_CHAT_ID from .env

        Args:
            voice_path: path of the audio file (.ogg) to send.

        Returns:
            True if it was sent, False otherwise.
        """
        if not TELEGRAM_BOT_TOKEN:
            logger.error("Cannot send voice reply: TELEGRAM_BOT_TOKEN not configured")
            return False

        # Target chat_id
        target_chat_id = self.current_telegram_chat_id or TELEGRAM_GROUP_CHAT_ID
        if not target_chat_id:
            logger.error("Cannot send voice reply: no chat_id available (neither from session nor .env)")
            return False

        reply_to = self.current_telegram_msg_id
        if reply_to:
            logger.info(
                "Sending voice reply to chat=%s, reply_to_message_id=%d (session %s)",
                target_chat_id, reply_to,
                self.current_session['id'] if self.current_session else '?',
            )
        else:
            logger.warning(
                "No telegram_message_id for current session - sending without reply link to chat=%s",
                target_chat_id,
            )

        try:
            loop = asyncio.new_event_loop()
            try:
                result = loop.run_until_complete(
                    _send_voice_note_async(
                        bot_token=TELEGRAM_BOT_TOKEN,
                        chat_id=target_chat_id,
                        voice_path=voice_path,
                        reply_to_message_id=reply_to,
                    )
                )
                return result is not None
            finally:
                loop.close()
        except Exception:
            logger.exception("Failed to send voice reply")
            return False

    def play_current_media(self):
        if not self.current_session:
            return

        self._needs_redraw = True
        self._current_played = True   # "Play" hint -> "Reply" hint
        self.state = "PLAYING"

        # Mark it as read in the database
        if self.current_session.get('status') == 'UNREAD':
            try:
                self.db.mark_as_read(self.current_session['id'])
                self.current_session['status'] = 'READ'
            except Exception as e:
                logger.error(f"Error marking as read: {e}")

        # Nothing loaded: straight back to WAITING_ACTION
        if not self.audio_path and not self.video_path and not self.current_image:
            self.state = "WAITING_ACTION"
            return

        if self.video_path:
            has_external_voice = bool(self.audio_path)
            ff_opts = {
                'an': has_external_voice,
                'paused': False,
                'vd': 'h264_v4l2m2m',  # BCM2835 hardware H.264 decoder on Pi 4
                'threads': '4',
            }
            try:
                self._close_video_player()
                self._video_surf = None  # Reset pre-allocated surface for new video
                self.video_player = MediaPlayer(str(self.video_path), ff_opts=ff_opts)
                self.current_video_frame = None
                logger.info(
                    f"MediaPlayer opened: {self.video_path.name} "
                    f"(audio={'external .ogg' if has_external_voice else 'internal'})"
                )
            except Exception as e:
                logger.error(f"Error opening MediaPlayer for {self.video_path}: {e}")
                self.video_player = None
                self.state = "WAITING_ACTION"
                return

        if self.audio_path:
            # Paired voice note (.ogg) — convert to a temporary WAV for pygame.mixer
            try:
                if self.audio_path.lower().endswith(".wav"):
                    wav_path = self.audio_path
                else:
                    tmp_dir = Path(tempfile.gettempdir())
                    tmp_wav = tmp_dir / f"av_voice_{self.current_session['id']}.wav"
                    # Discard cached WAV if empty (e.g. from a prior failed ffmpeg run)
                    if tmp_wav.exists() and tmp_wav.stat().st_size == 0:
                        tmp_wav.unlink()
                    if not tmp_wav.exists():
                        ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
                        subprocess.run(
                            [ffmpeg_exe, "-y", "-i", self.audio_path,
                             "-acodec", "pcm_s16le", str(tmp_wav)],
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL
                        )
                    wav_path = str(tmp_wav)

                # Re-init mixer if it lost its audio device (e.g. after long uptime)
                if not pygame.mixer.get_init():
                    logger.warning("Mixer not initialized — attempting re-init")
                    try:
                        pygame.mixer.quit()
                        pygame.mixer.init(frequency=48000, size=-16, channels=2, buffer=4096)
                        self._audio_available = True
                        logger.info("Mixer re-initialized successfully")
                    except pygame.error as reinit_err:
                        logger.error(f"Mixer re-init failed: {reinit_err}")
                        self._audio_available = False
                        raise

                pygame.mixer.music.load(wav_path)
                pygame.mixer.music.play()
                self._audio_play_start = time.time()
                logger.info(f"Playing paired voice note: {Path(self.audio_path).name}")
            except Exception as e:
                logger.error(f"Error playing voice note audio: {e}")

        if not self.video_path and not self.audio_path and not self.current_image:
            self.state = "WAITING_ACTION"

    # ------------------------------------------------------------------
    # Drawing
    # ------------------------------------------------------------------
    def _get_font(self, size: int) -> "pygame.font.Font":
        """Cached SysFont lookup — creating a Font every frame is a real cost at 30 FPS."""
        font = self._font_cache.get(size)
        if font is None:
            font = pygame.font.SysFont(None, size)
            self._font_cache[size] = font
        return font

    def _scaled_icon(self, name: str, size: int) -> pygame.Surface | None:
        """Cached smoothscale of a loaded icon — icons are re-scaled on demand, once per size."""
        icon = self._icons.get(name)
        if icon is None:
            return None
        key = (name, size)
        surf = self._icon_cache.get(key)
        if surf is None:
            surf = pygame.transform.smoothscale(icon, (size, size))
            self._icon_cache[key] = surf
        return surf

    def _wake_main_loop(self):
        """
        Posts a no-op event to unblock a pygame.event.wait() call that the main loop
        may currently be sleeping in (idle-power mode). Called from the gpiozero
        callback thread — pygame.event.post() is documented thread-safe. Swallow any
        error so a posting hiccup can never take down the GPIO callback thread.
        """
        try:
            pygame.event.post(pygame.event.Event(pygame.USEREVENT))
        except Exception:
            pass

    def _is_animating(self) -> bool:
        """States/overlays that must keep redrawing every tick (real motion on screen)."""
        return (
            self.state in ("SPLASH", "PLAYING", "RECORDING")
            or self.send_status is not None
            or (time.time() - self._no_voice_flash_time) <= 5.0
        )

    def _draw_recording_indicator(self):
        """Pulsing red circle centered on screen with elapsed-seconds counter."""
        elapsed = time.time() - self.recording_start_time
        pulse = abs(math.sin(time.time() * 2.5))           # smooth 0→1→0
        radius = int(55 + pulse * 20)
        alpha  = int(160 + pulse * 95)

        cx, cy = self.w // 2, self.h // 2

        # Red circle on a semi-transparent dark disc so it reads on any background
        bg = pygame.Surface((self.w, self.h), pygame.SRCALPHA)
        pygame.draw.circle(bg, (0, 0, 0, 80), (cx, cy), radius + 30)
        self.screen.blit(bg, (0, 0))

        sz = radius * 2
        scaled = self._scaled_icon('rec', sz)
        if scaled:
            scaled.set_alpha(alpha)
            self.screen.blit(scaled, (cx - radius, cy - radius))
        else:
            surf = pygame.Surface((radius * 2, radius * 2), pygame.SRCALPHA)
            pygame.draw.circle(surf, (255, 40, 40, alpha), (radius, radius), radius)
            self.screen.blit(surf, (cx - radius, cy - radius))

        mm, ss = divmod(int(elapsed), 60)
        timer_str = f"{mm:02d}:{ss:02d}"
        font = self._get_font(int(radius * 1.1))
        txt = font.render(timer_str, True, (255, 255, 255))
        self.screen.blit(txt, (cx - txt.get_width() // 2, cy - txt.get_height() // 2))

    def _draw_send_status(self):
        """Red "!" in the centre for a few seconds when a voice reply failed to send.
        "sending" / "sent" draw nothing but keep the loop redrawing until the
        status clears."""
        if self.send_status is None:
            return

        if self.send_status in ("sent", "error") and time.time() - self.send_status_time > 3.0:
            self.send_status = None
            return

        cx, cy = self.w // 2, self.h // 2
        sz = 100
        if self.send_status == "error":
            bg = pygame.Surface((sz, sz), pygame.SRCALPHA)
            pygame.draw.circle(bg, (180, 0, 0, 200), (sz // 2, sz // 2), sz // 2 - 2)
            self.screen.blit(bg, (cx - sz // 2, cy - sz // 2))
            font = self._get_font(64)
            txt = font.render("!", True, (255, 255, 255))
            self.screen.blit(txt, (cx - txt.get_width() // 2, cy - txt.get_height() // 2))

    def _draw_no_voice_badge(self):
        """Centered mute icon shown for 5 s after pressing play with no voice note."""
        if time.time() - self._no_voice_flash_time > 5.0:
            return
        sz = 80
        scaled = self._scaled_icon('no_voice', sz)
        if not scaled:
            return
        cx, cy = self.w // 2, self.h // 2
        bg = pygame.Surface((sz + 24, sz + 24), pygame.SRCALPHA)
        pygame.draw.circle(bg, (0, 0, 0, 180), ((sz + 24) // 2, (sz + 24) // 2), (sz + 24) // 2)
        self.screen.blit(bg, (cx - (sz + 24) // 2, cy - (sz + 24) // 2))
        self.screen.blit(scaled, (cx - sz // 2, cy - sz // 2))

    def _draw_pill(self, text: str, center_x: int, center_y: int):
        """A single semi-transparent rounded text label."""
        font = self._get_font(self._overlay_pill_font_size)
        txt = font.render(text, True, (255, 255, 255))
        pad_x, pad_y = 18, 9
        w = txt.get_width() + pad_x * 2
        h = txt.get_height() + pad_y * 2
        x = int(center_x - w / 2)
        y = int(center_y - h / 2)
        x = max(4, min(x, self.w - w - 4))          # keep on-screen
        y = max(4, min(y, self.h - h - 4))
        pill = pygame.Surface((w, h), pygame.SRCALPHA)
        pygame.draw.rect(pill, (0, 0, 0, 150), (0, 0, w, h), border_radius=h // 2)
        self.screen.blit(pill, (x, y))
        self.screen.blit(txt, (x + pad_x, y + pad_y))

    def _overlays_visible(self) -> bool:
        """True for _overlay_hide_after seconds after a real button press.
        During untouched idle slideshow this is False, so the screen shows
        only the photo — hints appear only once the user actually engages.
        Always True in tutorial mode."""
        if self._tutorial_mode:
            return True
        return (time.time() - self._last_input_time) < self._overlay_hide_after

    def _draw_button_hints(self):
        """Text pill above the physical PLAY / RECORD button while a video or
        voice-note session is on screen. 'Play' until it's been played,
        then 'Reply'. The two are mutually exclusive so they never overlap
        despite sitting close to the centre."""
        if not self._overlay_enabled or not self.current_session:
            return
        if not self._overlays_visible():
            return
        if not (self.video_path or self.audio_path):
            return   # nothing to play (photo-only) -> no hints
        cx = self.w // 2
        y = self.h - self._overlay_bottom_margin
        if not self._current_played:
            self._draw_pill("Play", cx - self._btn_offset_px, y)
        elif AUDIO_RECORDING_AVAILABLE:
            self._draw_pill("Reply", cx + self._btn_offset_px, y)

    def draw(self):
        if self.state != "PLAYING" or self.current_video_frame is None:
            self.screen.fill(self.bg_color)

        if self.state == "SPLASH":
            self._draw_splash()

        elif self.state == "IDLE":
            if self.last_viewed_image:
                self.screen.blit(self.last_viewed_image, (self.last_img_x, self.last_img_y))
            else:
                font = self._get_font(48)
                txt = font.render("AmonaVision - Waiting for memories...", True, (150, 150, 150))
                self.screen.blit(txt, ((self.w - txt.get_width()) // 2, (self.h - txt.get_height()) // 2))

        elif self.state == "WAITING_ACTION":
            if self.current_image:
                self.screen.blit(self.current_image, (self.img_x, self.img_y))
            else:
                font = self._get_font(36)
                txt = font.render("Loading cover...", True, (100, 100, 100))
                self.screen.blit(txt, ((self.w - txt.get_width()) // 2, (self.h - txt.get_height()) // 2))
            self._draw_no_voice_badge()
            self._draw_button_hints()

        elif self.state == "RECORDING":
            # Keep the image on screen while recording
            if self.current_image:
                self.screen.blit(self.current_image, (self.img_x, self.img_y))
            else:
                font = self._get_font(36)
                txt = font.render("Recording...", True, (100, 100, 100))
                self.screen.blit(txt, ((self.w - txt.get_width()) // 2, (self.h - txt.get_height()) // 2))
            self._draw_recording_indicator()

        elif self.state == "PLAYING":
            if self.video_player is not None:
                # --- ffpyplayer handles A/V timing itself ---
                # get_frame() returns (frame, val):
                #   frame: ImageFrame with pixel data, or None if no frame is ready yet
                #   val  : seconds until the next frame (>0: wait), 'eof': finished
                try:
                    frame, val = self.video_player.get_frame()
                except Exception as e:
                    logger.error(f"ffpyplayer get_frame error: {e}")
                    self._close_video_player()
                    self.state = "WAITING_ACTION"
                    self.slide_start_time = time.time()
                    self._last_input_time = time.time()  # reveal the hint — see below
                    frame, val = None, 'eof'

                if val == 'eof':
                    # Video finished — treated as a fresh interaction so its hint
                    # ("Reply", since _current_played has been True since playback
                    # started) shows again even if the video ran longer than
                    # overlay_hide_after_seconds.
                    self._close_video_player()
                    self.state = "WAITING_ACTION"
                    self.slide_start_time = time.time()
                    self._last_input_time = time.time()
                elif frame is not None:
                    img_array, _pts = frame
                    w, h = img_array.get_size()
                    raw = img_array.to_bytearray()[0]
                    # Reuse a display-format surface to avoid a 6MB alloc+convert per frame
                    if self._video_surf is None or self._video_surf.get_size() != (w, h):
                        self._video_surf = pygame.Surface((w, h)).convert()
                    src = pygame.image.frombuffer(raw, (w, h), 'RGB')
                    self._video_surf.blit(src, (0, 0))
                    self.current_video_frame, self.video_x, self.video_y = self.scale_surface(self._video_surf, smooth=False)

                # Draw the latest available frame (keeps the picture up between frames)
                if self.current_video_frame is not None:
                    self.screen.blit(self.current_video_frame, (self.video_x, self.video_y))
                elif self.current_image is not None:
                    self.screen.blit(self.current_image, (self.img_x, self.img_y))

                # Video/audio sync with ffpyplayer
                if val != 'eof' and isinstance(val, (int, float)):
                    if frame is None:
                        # No frame ready yet: wait a little
                        time.sleep(0.005)
                    elif val > 0:
                        # Sleep as long as ffpyplayer suggests (100ms max)
                        time.sleep(min(val, 0.1))

            else:
                # Photo with a paired voice note (no video)
                if self.current_image:
                    self.screen.blit(self.current_image, (self.img_x, self.img_y))

                    # Voice/audio playing indicator (centered bottom)
                    sz = 64
                    scaled = self._scaled_icon('voice', sz)
                    if scaled:
                        ix = (self.w - sz) // 2
                        iy = self.h - sz - 20
                        bg = pygame.Surface((sz + 20, sz + 20), pygame.SRCALPHA)
                        pygame.draw.circle(bg, (0, 0, 0, 140), ((sz + 20) // 2, (sz + 20) // 2), (sz + 20) // 2)
                        self.screen.blit(bg, (ix - 10, iy - 10))
                        self.screen.blit(scaled, (ix, iy))
                    else:
                        mic_x, mic_y = self.w // 2, self.h - 60
                        pygame.draw.rect(self.screen, (255, 255, 255), (mic_x - 10, mic_y - 20, 20, 30), border_radius=10)
                        pygame.draw.arc(self.screen, (255, 255, 255), (mic_x - 18, mic_y - 10, 36, 36), 3.14, 0, 3)
                        pygame.draw.line(self.screen, (255, 255, 255), (mic_x, mic_y + 26), (mic_x, mic_y + 36), 3)
                        pygame.draw.line(self.screen, (255, 255, 255), (mic_x - 15, mic_y + 36), (mic_x + 15, mic_y + 36), 3)

                # No video and no audio → back
                if not self.audio_path and not self.video_path:
                    self.state = "WAITING_ACTION"
                    self.slide_start_time = time.time()
                # Voice note finished → back to the slideshow (same as the end of a
                # video: the hint shows again even if its timeout has passed)
                elif (not self._audio_available or not pygame.mixer.music.get_busy()) and self.audio_path:
                    if time.time() - self._audio_play_start > 0.5:
                        self.state = "WAITING_ACTION"
                        self.slide_start_time = time.time()
                        self._last_input_time = time.time()

        # Send-status overlay (drawn on top of any state)
        self._draw_send_status()

        # Software brightness dimming overlay (scheduling/refresh happens in run())
        if self._brightness < 100 and self._dim_overlay is not None:
            self.screen.blit(self._dim_overlay, (0, 0))

        pygame.display.flip()

    def run(self):
        try:
            running = True
            while running:
                # Everything in this block used to run unguarded: an uncaught exception
                # here (e.g. a transient DB error during slideshow advance) would crash
                # the whole process. start.py treats any viewer.py exit as an intentional
                # shutdown and brings down the bot with it — so a hiccup here shouldn't
                # get to take out message-receiving too. Log and skip to the next tick
                # instead, matching the recovery pattern already used for frame errors.
                try:
                    # Exit splash once minimum duration has passed
                    if self.state == "SPLASH" and time.time() - self._splash_start >= 3.0:
                        self.load_next_valid_session()
                        self._nav_was_manual = False

                    # Auto-advance in WAITING_ACTION or IDLE;
                    # never while recording or sending
                    if self.state in ("IDLE", "WAITING_ACTION"):
                        if time.time() - self.slide_start_time > self.slideshow_interval:
                            self.slide_start_time = time.time()
                            self.load_scored_session()
                            self._nav_was_manual = False

                    # Telegram /restart command — the bot writes this flag, we exit cleanly
                    if RESTART_FLAG.exists():
                        RESTART_FLAG.unlink(missing_ok=True)
                        logger.info("Restart requested via Telegram — exiting for systemd to restart.")
                        running = False

                    # Daily restart at 04:00 — exit cleanly; systemd Restart=always brings it back
                    if SETTINGS['daily_restart']:
                        _now = datetime.now()
                        if (_now.hour == 4 and _now.minute == 0 and _now.second < 30
                                and time.time() - _PROCESS_START > 120):
                            logger.info("Daily restart at 04:00 — exiting for systemd to restart.")
                            running = False

                    # Pick up the thumbnail once the background extraction is done
                    if self.state == "WAITING_ACTION" and self.current_image is None and self.video_path:
                        thumb_path = self.cache.cache_dir / f"thumb_{self.video_path.name}.jpg"
                        if thumb_path.exists():
                            try:
                                img = pygame.image.load(str(thumb_path)).convert()
                                self.current_image, self.img_x, self.img_y = self.scale_surface(img, smooth=True)
                                self.last_viewed_image = self.current_image
                                self.last_img_x, self.last_img_y = self.img_x, self.img_y
                                self._needs_redraw = True
                                logger.info(f"Loaded dynamically extracted thumbnail for {self.video_path.name}")
                            except Exception as e:
                                logger.error(f"Error loading dynamic thumbnail: {e}")
                                try:
                                    thumb_path.unlink(missing_ok=True)
                                except Exception:
                                    pass

                    # Software brightness dimming — check for schedule/level changes every 2 s.
                    # Runs every iteration regardless of redraw state so day/night dimming
                    # stays on time even while idle; marks self._needs_redraw on real change.
                    now_t = time.time()
                    if now_t - self._brightness_last_check >= 2.0:
                        self._brightness_last_check = now_t
                        self._update_brightness()

                    # Nothing changes on screen while the hints sit there, so the
                    # only redraw this needs is at the shown<->hidden transition —
                    # in particular the disappearance at the 10 s mark, which
                    # otherwise wouldn't repaint until some unrelated redraw came
                    # along (up to 30 s later, via the safety net below). Checked
                    # at the same ~1 s cadence as the other periodic checks, so
                    # the hide lands within ~1 s of the deadline. Far cheaper than
                    # treating this like real animation (_is_animating()), which
                    # would busy-poll at 30 FPS for the whole window.
                    overlay_now_visible = (
                        self.state == "WAITING_ACTION" and self._overlays_visible()
                    )
                    if overlay_now_visible != self._overlay_was_visible:
                        self._needs_redraw = True
                    self._overlay_was_visible = overlay_now_visible

                    # Self-healing safety net: force a redraw at least every 30 s even if
                    # nothing flagged a change, in case a future code path forgets to set
                    # self._needs_redraw. Cheap insurance against a frozen/stale screen.
                    if now_t - self._last_forced_redraw >= 30.0:
                        self._last_forced_redraw = now_t
                        self._needs_redraw = True

                    # Drain the GPIO queue into Pygame events, from the main thread
                    while not self._gpio_queue.empty():
                        try:
                            event_type, gpio_key = self._gpio_queue.get_nowait()
                            pygame.event.post(pygame.event.Event(event_type, key=gpio_key))
                        except queue.Empty:
                            break
                except Exception:
                    logger.exception("Periodic-check error — skipping to next tick")
                    self.clock.tick(5)
                    continue

                # Idle-power optimization: while nothing is animating on screen, block on
                # pygame.event.wait() instead of busy-polling at 30 FPS. GPIO presses wake
                # this almost instantly via _wake_main_loop(), so the timeout only bounds
                # the periodic checks above (slideshow/brightness/restart-flag) — 1 s is
                # imperceptible for all of them.
                animating_before = self._is_animating()
                try:
                    if animating_before:
                        events = pygame.event.get()
                    else:
                        first_event = pygame.event.wait(timeout=1000)
                        events = pygame.event.get()
                        if first_event.type != pygame.NOEVENT:
                            events.insert(0, first_event)
                        if events:
                            self._needs_redraw = True

                    for event in events:
                        if event.type == pygame.QUIT:
                            if self.is_recording:
                                self.stop_recording()
                            running = False
                        elif event.type == pygame.KEYDOWN:
                            if self.state != "SPLASH" and event.key in (
                                pygame.K_SPACE, pygame.K_r, pygame.K_LEFT, pygame.K_RIGHT
                            ):
                                # A real button press — reveal the Play/Reply
                                # hints for a while, whatever else this key
                                # ends up doing in the current state.
                                self._last_input_time = time.time()
                            if event.key in USAGE_BUTTONS:
                                log_usage("press", USAGE_BUTTONS[event.key],
                                          state=self.state, session=self.current_session)
                            if self.state == "SPLASH":
                                pass  # ignore all input during startup screen
                            elif event.key == pygame.K_SPACE:
                                if self.state == "WAITING_ACTION":
                                    if not self.audio_path and not self.video_path:
                                        self._no_voice_flash_time = time.time()
                                    else:
                                        self.play_current_media()
                                elif self.state == "PLAYING":
                                    if self._audio_available:
                                        pygame.mixer.music.stop()
                                    self._close_video_player()
                                    self.state = "WAITING_ACTION"
                                    self.slide_start_time = time.time()

                            elif event.key == pygame.K_r:
                                if not self.is_recording and self.state in ("WAITING_ACTION", "PLAYING"):
                                    if self.state == "PLAYING":
                                        if self._audio_available:
                                            pygame.mixer.music.stop()
                                        self._close_video_player()
                                    self.start_recording()

                            elif event.key == pygame.K_LEFT:
                                if not self.is_recording and self.state not in ("SPLASH", "RECORDING"):
                                    self._no_voice_flash_time = 0.0
                                    self.load_previous_valid_session()
                                    if self.current_session:
                                        self._nav_was_manual = True
                                        try:
                                            self.db.record_revisit(self.current_session['id'])
                                        except Exception:
                                            pass

                            elif event.key == pygame.K_RIGHT:
                                if not self.is_recording and self.state not in ("SPLASH", "RECORDING"):
                                    self._no_voice_flash_time = 0.0
                                    self.load_next_valid_session()
                                    if self.current_session:
                                        self._nav_was_manual = True
                                        try:
                                            self.db.record_revisit(self.current_session['id'])
                                        except Exception:
                                            pass

                        elif event.type == pygame.KEYUP:
                            if event.key == pygame.K_r and self.is_recording:
                                self.stop_recording()

                    if animating_before or self._needs_redraw:
                        self.draw()
                        self._needs_redraw = False
                        # Dropped out of an animating state (video/voice ended, a badge
                        # expired, recording stopped) without an external event to catch
                        # it next loop — force one more redraw so the static view settles
                        # instead of freezing on the last animated frame.
                        if animating_before and not self._is_animating():
                            self._needs_redraw = True

                except Exception:
                    logger.exception("Frame error — recovering to safe state")
                    try:
                        if self.state == "PLAYING":
                            self._close_video_player()
                        if self.is_recording:
                            self.is_recording = False
                        if self.state not in ("IDLE", "WAITING_ACTION", "SPLASH"):
                            self.state = "WAITING_ACTION"
                            self.slide_start_time = time.time()
                        self.screen.fill(self.bg_color)
                        pygame.display.flip()
                    except Exception:
                        pass

                if animating_before and (self.state != "PLAYING" or self.video_player is None):
                    self.clock.tick(30)
        finally:
            self._close_video_player()
            if self._audio_available:
                pygame.mixer.music.stop()
            if hasattr(self, 'cache'):
                self.cache.cleanup()
            if GPIO_AVAILABLE:
                try:
                    for btn in self._gpio_buttons:
                        btn.close()
                    logger.info("GPIO buttons closed successfully.")
                except Exception as e:
                    logger.error("Error closing GPIO buttons: %s", e)
            pygame.quit()

if __name__ == "__main__":
    app = ViewerApp()
    app.run()
