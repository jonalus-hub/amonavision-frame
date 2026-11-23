"""Runtime files of the viewer: the run directory and the heartbeat.

The run directory holds mpv's IPC socket, its last log and the viewer
heartbeat. amonavision.service creates /run/amonavision (RuntimeDirectory=);
where it doesn't exist yet (unit not reinstalled, a dev machine) the files go
to $XDG_RUNTIME_DIR/amonavision, or a private temp directory.
"""

from __future__ import annotations

import logging
import os
import tempfile
import time
from pathlib import Path

logger = logging.getLogger("AmonaVision.video")

SYSTEM_RUN_DIR = Path("/run/amonavision")


def run_dir(env: dict | None = None) -> Path:
    env = os.environ if env is None else env
    override = env.get("AMONAVISION_RUN_DIR")
    candidates = [Path(override)] if override else []
    candidates.append(SYSTEM_RUN_DIR)
    if env.get("XDG_RUNTIME_DIR"):
        candidates.append(Path(env["XDG_RUNTIME_DIR"]) / "amonavision")
    candidates.append(Path(tempfile.gettempdir()) / f"amonavision-{os.getuid()}")
    for path in candidates:
        try:
            if path != SYSTEM_RUN_DIR:
                path.mkdir(mode=0o700, parents=True, exist_ok=True)
            if path.is_dir() and os.access(path, os.W_OK | os.X_OK):
                return path
        except OSError:
            continue
    raise RuntimeError("no writable run directory for the viewer")


class Heartbeat:
    """Rewrites <run_dir>/viewer-heartbeat at most every `interval` seconds.

    The file holds "<unix time> <state>", so `cat` shows both that the main
    loop is turning and what it is doing (e.g. "1795400000 PLAYING/mpv").
    """

    def __init__(self, path: Path, interval: float = 2.0, clock=time.monotonic):
        self.path = Path(path)
        self.interval = interval
        self._clock = clock
        self._last = float("-inf")
        self._warned = False

    def beat(self, state: str) -> None:
        now = self._clock()
        if now - self._last < self.interval:
            return
        self._last = now
        try:
            self.path.write_text(f"{int(time.time())} {state}\n")
        except OSError as e:
            if not self._warned:
                self._warned = True
                logger.warning("Can't write the viewer heartbeat %s: %s", self.path, e)
