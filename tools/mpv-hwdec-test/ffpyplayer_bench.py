#!/usr/bin/env python3
"""Reference player for bench.sh: one video played the way the viewer plays it
with ffpyplayer.

Same MediaPlayer ff_opts (from the HAL), and the same per-frame work as
viewer.draw() in PLAYING: RGB bytes -> frombuffer -> blit onto a display-format
surface -> pygame.transform.scale to fit the screen -> blit -> flip, sleeping
as ffpyplayer suggests (100 ms max).

Usage (in the AmonaVision directory, with the service stopped):
    venv/bin/python tools/mpv-hwdec-test/ffpyplayer_bench.py VIDEO [--seconds N] [--no-audio]

Prints one JSON line at the end: frames shown, seconds played, fps.
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)
os.environ.setdefault("SDL_VIDEODRIVER", "kmsdrm")
os.environ.setdefault("SDL_AUDIODRIVER", "pulse")
os.environ["PYGAME_HIDE_SUPPORT_PROMPT"] = "hide"

from app import hal  # noqa: E402

HW = hal.get_platform()
_env = HW.shell_env()
os.environ.setdefault("SDL_KMSDRM_DEVICE_INDEX", str(_env.get("AV_KMSDRM_DEVICE_INDEX") or 1))

import pygame  # noqa: E402
from ffpyplayer.player import MediaPlayer  # noqa: E402


def fit(surface, w, h):
    """viewer.scale_surface(smooth=False)."""
    s_w, s_h = surface.get_size()
    if (s_w, s_h) == (w, h):
        return surface, 0, 0
    if s_w / s_h > w / h:
        new_w, new_h = w, int(w / (s_w / s_h))
    else:
        new_w, new_h = int(h * (s_w / s_h)), h
    if (s_w, s_h) != (new_w, new_h):
        surface = pygame.transform.scale(surface, (new_w, new_h))
    return surface, (w - new_w) // 2, (h - new_h) // 2


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("video")
    ap.add_argument("--seconds", type=float, default=0, help="stop after this many seconds (0 = whole video)")
    ap.add_argument("--no-audio", action="store_true")
    args = ap.parse_args()

    pygame.init()
    info = pygame.display.Info()
    w, h = info.current_w, info.current_h
    screen = pygame.display.set_mode((w, h), pygame.FULLSCREEN | pygame.DOUBLEBUF)
    pygame.mouse.set_visible(False)

    player = MediaPlayer(str(args.video), ff_opts=HW.video.ff_opts(args.no_audio))
    frames = 0
    video_surf = current = None
    x = y = 0
    t0 = time.monotonic()
    try:
        while True:
            if args.seconds and time.monotonic() - t0 >= args.seconds:
                break
            pygame.event.pump()
            frame, val = player.get_frame()
            if val == 'eof':
                break
            if frame is not None:
                img, _pts = frame
                fw, fh = img.get_size()
                raw = img.to_bytearray()[0]
                if video_surf is None or video_surf.get_size() != (fw, fh):
                    video_surf = pygame.Surface((fw, fh)).convert()
                video_surf.blit(pygame.image.frombuffer(raw, (fw, fh), 'RGB'), (0, 0))
                current, x, y = fit(video_surf, w, h)
                frames += 1
            screen.fill((0, 0, 0))
            if current is not None:
                screen.blit(current, (x, y))
            pygame.display.flip()
            if isinstance(val, (int, float)):
                if frame is None:
                    time.sleep(0.005)
                elif val > 0:
                    time.sleep(min(val, 0.1))
    finally:
        player.close_player()
        pygame.quit()
    seconds = time.monotonic() - t0
    print(json.dumps({"player": "ffpyplayer", "frames_shown": frames, "seconds": round(seconds, 2),
                      "fps": round(frames / seconds, 2) if seconds else 0, "screen": [w, h],
                      "ff_opts": HW.video.ff_opts(args.no_audio)}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
