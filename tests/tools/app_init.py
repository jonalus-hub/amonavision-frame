"""Start the viewer for the device.conf / HAL in the environment; report as JSON.

Usage: AMONAVISION_DEVICE_CONF=... [AMONAVISION_HAL_IMPL=fake] \
       python tests/tools/app_init.py <project_dir>

Creates ViewerApp at the display profile's native size (SDL dummy driver,
throw-away database, stub prefetch cache), draws every screen state once,
presses each button through the HAL and checks the layout fits.
"""
import json
import os
import sys
import tempfile
import time
from pathlib import Path

os.environ["SDL_VIDEODRIVER"] = "dummy"
os.environ["SDL_AUDIODRIVER"] = "dummy"
src = Path(sys.argv[1]).resolve()
os.chdir(src)
sys.path.insert(0, str(src))

import pygame  # noqa: E402
import viewer  # noqa: E402
from app.hal.layout import overflows  # noqa: E402

W, H = viewer.HW.display.native_size
viewer.SETTINGS.update(fullscreen=False, window_width=W, window_height=H)
viewer.DB_PATH = Path(tempfile.mkdtemp()) / "init.db"


class StubCache:
    def __init__(self, *a, **k): pass
    def get_path(self, p): return viewer.MEDIA_DIR / p
    def prefetch_session(self, *a, **k): pass
    def cleanup(self): pass


viewer.PrefetchCache = StubCache
app = viewer.ViewerApp()

photo = pygame.Surface((1920, 1080))
photo.fill((40, 90, 160))
img, x, y = app.scale_surface(photo)
now = time.time()
drawn = []
for state, setup in [
    ("SPLASH", {}),
    ("IDLE", {}),
    ("WAITING_ACTION", dict(current_image=img, img_x=x, img_y=y, current_session={"id": 1},
                            audio_path="v.ogg", _last_input_time=now, _no_voice_flash_time=now)),
    ("RECORDING", dict(recording_start_time=now - 5)),
    ("PLAYING", dict(audio_path="v.ogg", _audio_play_start=now, send_status="error", send_status_time=now)),
]:
    for k, v in setup.items():
        setattr(app, k, v)
    app.state = state
    app.draw()
    drawn.append(state)

keys = []
buttons = viewer.HW.buttons
for pin in list(getattr(buttons, "attached", {})):
    buttons.press(pin)
while not app._gpio_queue.empty():
    keys.append(pygame.key.name(app._gpio_queue.get()[1]))

print(json.dumps({
    "label": viewer.HW.label(),
    "implementation": viewer.HW.implementation,
    "size": [app.w, app.h],
    "native_size": list(viewer.HW.display.native_size),
    "overflows": overflows(app._ui, app.w, app.h),
    "drawn": drawn,
    "gpio_available": viewer.GPIO_AVAILABLE,
    "keys": keys,
}))
