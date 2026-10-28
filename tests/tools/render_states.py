"""Render every viewer UI state and print a sha256 per state (JSON on stdout).

Usage: python tests/tools/render_states.py <project_dir> [W H] [--png DIR]

Used by tests/test_layout.py to compare the UI pixel by pixel with the frame
as it was before the HAL (tests/fixtures/render_1024x600.json). Runs with the
SDL dummy driver, a throw-away database and a stub prefetch cache (the real
one is /dev/shm/amonavision_cache), with time frozen so animations are fixed.
Works on any checkout, before or after the HAL.
"""
import hashlib
import json
import os
import sys
import tempfile
import time
from pathlib import Path

os.environ["SDL_VIDEODRIVER"] = "dummy"
os.environ["SDL_AUDIODRIVER"] = "dummy"
args = sys.argv[1:]
png_dir = None
if "--png" in args:
    i = args.index("--png")
    png_dir = Path(args[i + 1]).resolve()
    png_dir.mkdir(parents=True, exist_ok=True)
    del args[i:i + 2]
src = Path(args[0]).resolve()
W, H = (int(args[1]), int(args[2])) if len(args) > 2 else (1024, 600)
os.chdir(src)
sys.path.insert(0, str(src))

FIXED = 1_800_000_000.0
time.time = lambda: FIXED

import pygame  # noqa: E402
import viewer  # noqa: E402

viewer.SETTINGS.update(fullscreen=False, window_width=W, window_height=H)
viewer.DB_PATH = Path(tempfile.mkdtemp()) / "render.db"


class StubCache:
    def __init__(self, *a, **k): pass
    def get_path(self, p): return viewer.MEDIA_DIR / p
    def prefetch_session(self, *a, **k): pass
    def cleanup(self): pass


viewer.PrefetchCache = StubCache
app = viewer.ViewerApp()


def gradient(w, h):
    s = pygame.Surface((w, h))
    for x in range(0, w, 8):
        pygame.draw.rect(s, ((x * 255) // w, 90, 255 - (x * 255) // w), (x, 0, 8, h))
    pygame.draw.circle(s, (250, 250, 250), (w // 3, h // 2), min(w, h) // 5)
    return s


land = app.scale_surface(gradient(1920, 1080))
port = app.scale_surface(gradient(800, 1200))
results = {}


def reset():
    app.state = "WAITING_ACTION"
    app.current_session = None
    app.current_image = None
    app.img_x = app.img_y = 0
    app.last_viewed_image = None
    app.video_player = None
    app.video_path = None
    app.audio_path = None
    app.current_video_frame = None
    app.send_status = None
    app._no_voice_flash_time = 0.0
    app._last_input_time = 0.0
    app._current_played = False
    app._brightness = 100
    app._spinner_angle = 0.0
    app.is_recording = False


def shot(name, setup):
    reset()
    icons = dict(app._icons)
    logo = app._logo_surf
    setup()
    app.draw()
    raw = pygame.image.tobytes(app.screen, "RGB")
    results[name] = hashlib.sha256(raw).hexdigest()
    if png_dir:
        pygame.image.save(app.screen, str(png_dir / f"{name}.png"))
    app._icons = icons
    app._icon_cache.clear()
    app._logo_surf = logo


def s_splash():
    app.state = "SPLASH"


def s_splash_nologo():
    app.state = "SPLASH"
    app._logo_surf = None


def s_idle_text():
    app.state = "IDLE"


def s_idle_image():
    app.state = "IDLE"
    app.last_viewed_image, app.last_img_x, app.last_img_y = land


def s_loading_cover_badge():
    app._no_voice_flash_time = FIXED


def s_photo_land():
    app.current_image, app.img_x, app.img_y = land


def s_photo_port():
    app.current_image, app.img_x, app.img_y = port


def s_hint_play():
    app.current_image, app.img_x, app.img_y = land
    app.current_session = {"id": 1}
    app.audio_path = "voice.ogg"
    app._last_input_time = FIXED


def s_hint_reply():
    s_hint_play()
    app._current_played = True
    viewer.AUDIO_RECORDING_AVAILABLE = True


def s_recording():
    app.state = "RECORDING"
    app.current_image, app.img_x, app.img_y = port
    app.recording_start_time = FIXED - 75


def s_recording_noicon():
    s_recording()
    app._icons = {}
    app._icon_cache.clear()


def s_recording_text():
    app.state = "RECORDING"
    app.recording_start_time = FIXED - 3


def s_send_error():
    app.current_image, app.img_x, app.img_y = land
    app.send_status = "error"
    app.send_status_time = FIXED


def s_voice_playing():
    app.state = "PLAYING"
    app.current_image, app.img_x, app.img_y = port
    app.audio_path = "voice.ogg"
    app._audio_play_start = FIXED
    app._audio_available = True


def s_voice_playing_noicon():
    s_voice_playing()
    app._icons = {}
    app._icon_cache.clear()


for fn in [s_splash, s_splash_nologo, s_idle_text, s_idle_image, s_loading_cover_badge,
           s_photo_land, s_photo_port, s_hint_play, s_hint_reply, s_recording,
           s_recording_noicon, s_recording_text, s_send_error, s_voice_playing,
           s_voice_playing_noicon]:
    viewer.AUDIO_RECORDING_AVAILABLE = False
    shot(fn.__name__[2:], fn)

print(json.dumps({"size": [app.w, app.h], "pygame": pygame.version.ver,
                  "sdl": ".".join(map(str, pygame.get_sdl_version())), "states": results}))
