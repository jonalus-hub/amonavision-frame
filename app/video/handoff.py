"""Handing the screen from pygame to mpv and back.

On KMS/DRM (the frames: SDL_VIDEODRIVER=kmsdrm) only one process can drive
the screen — the DRM master. pygame has to close its display before mpv can
open it, and open it again once mpv has exited, with the same size and flags,
the cursor hidden. On X11/Wayland (a dev machine) nothing has to be released:
mpv simply opens full screen on top of the pygame window.

While the display is closed pygame's event queue is down too (event.get/post
raise "video system not initialized"), so the viewer reads the buttons
straight from its GPIO queue during an mpv video. The mixer keeps working.
"""

from __future__ import annotations

import logging
import time

logger = logging.getLogger("AmonaVision.video")

RELEASE_DRIVERS = ("kmsdrm",)


def _hide_cursor(pg) -> None:
    pg.mouse.set_visible(False)
    pg.mouse.set_cursor(pg.cursors.Cursor((8, 8), (0, 0), b'\x00' * 8, b'\x00' * 8))


class DisplayHandoff:
    def __init__(self, pg=None):
        if pg is None:
            import pygame as pg
        self._pg = pg
        self.driver = (pg.display.get_driver() or "").lower() if pg.display.get_init() else ""
        self.released = False

    def must_release(self) -> bool:
        return self.driver in RELEASE_DRIVERS

    def release(self) -> None:
        """Close pygame's display so mpv can become the DRM master."""
        if self.released or not self.must_release():
            return
        self._pg.display.quit()
        self.released = True
        logger.info("Display released to mpv (%s)", self.driver)

    def restore(self, size: tuple[int, int], flags: int, caption: str = "AmonaVision Viewer",
                attempts: int = 5, delay: float = 0.3):
        """Reopen pygame's display; returns the new screen surface.

        mpv has exited by now, but the kernel may need a moment to hand the DRM
        master back, so a failed open is retried a few times.
        """
        pg = self._pg
        last_error = None
        for attempt in range(1, attempts + 1):
            try:
                pg.display.init()
                screen = pg.display.set_mode(size, flags)
                pg.display.set_caption(caption)
                _hide_cursor(pg)
                self.released = False
                if attempt > 1:
                    logger.info("Display reopened on attempt %d", attempt)
                return screen
            except pg.error as e:
                last_error = e
                logger.warning("Reopening the display failed (attempt %d/%d): %s", attempt, attempts, e)
                try:
                    pg.display.quit()
                except Exception:
                    pass
                time.sleep(delay)
        raise RuntimeError(f"could not reopen the display after mpv: {last_error}")
