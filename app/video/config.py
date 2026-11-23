"""Video playback settings: the [video] section of device.conf.

device.conf (project root, not in git) can choose how videos are played:

    [video]
    backend = mpv                 # ffpyplayer (default) | mpv
    mpv_hwdec = v4l2m2m-copy      # mpv --hwdec
    mpv_vo = gpu                  # mpv --vo: gpu | drm
    mpv_gpu_context = drm         # mpv --gpu-context, only with vo=gpu on a KMS/DRM screen
    mpv_audio_device = pulse      # mpv --audio-device (pulse = the default sink, like ffpyplayer)
    mpv_drm_mode = auto           # auto = the mode pygame is using | preferred | WxH[@Hz]
    mpv_start_timeout = 5         # s for the first frame before mpv is given up on
    mpv_end_grace = 10            # s past the video's duration before mpv is killed

Without a [video] section (or with backend = ffpyplayer) nothing changes:
ffpyplayer plays the videos as before. Bad values are logged and replaced by
their defaults, so a typo never stops the frame from playing videos.
"""

from __future__ import annotations

import configparser
import logging
import os
from dataclasses import dataclass
from pathlib import Path

from app.hal.profiles import DEVICE_CONF

logger = logging.getLogger("AmonaVision.video")

BACKENDS = ("ffpyplayer", "mpv")


@dataclass(frozen=True)
class VideoConfig:
    backend: str = "ffpyplayer"
    mpv_binary: str = "mpv"
    mpv_hwdec: str = "v4l2m2m-copy"
    mpv_vo: str = "gpu"
    mpv_gpu_context: str = "drm"
    mpv_audio_device: str = "pulse"
    mpv_drm_mode: str = "auto"
    mpv_start_timeout: float = 5.0
    mpv_end_grace: float = 10.0
    source: str = "defaults"      # "device.conf" or "defaults"

    def describe(self) -> str:
        if self.backend != "mpv":
            return f"{self.backend} (from {self.source})"
        return (f"mpv (from {self.source}): hwdec={self.mpv_hwdec}, vo={self.mpv_vo}, "
                f"gpu-context={self.mpv_gpu_context}, audio={self.mpv_audio_device}, "
                f"drm-mode={self.mpv_drm_mode}, start timeout {self.mpv_start_timeout:g} s, "
                f"end grace {self.mpv_end_grace:g} s")


def _token(section: configparser.SectionProxy, key: str, default: str) -> str:
    """One mpv argument value: no spaces (each value is passed as a single argument)."""
    raw = section.get(key, fallback="").strip()
    if not raw:
        return default
    if any(ch.isspace() for ch in raw):
        logger.warning("device.conf [video] %s = %r has spaces — using %r", key, raw, default)
        return default
    return raw


def _seconds(section: configparser.SectionProxy, key: str, default: float) -> float:
    raw = section.get(key, fallback="").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        value = -1.0
    if value <= 0:
        logger.warning("device.conf [video] %s = %r is not a positive number — using %g", key, raw, default)
        return default
    return value


def read_video_config(path: Path | str | None = None) -> VideoConfig:
    """The [video] settings from device.conf (AMONAVISION_DEVICE_CONF overrides the path)."""
    path = Path(path or os.environ.get("AMONAVISION_DEVICE_CONF") or DEVICE_CONF)
    cp = configparser.ConfigParser()
    try:
        found = cp.read(path, encoding="utf-8")
    except (configparser.Error, OSError, UnicodeDecodeError) as e:
        logger.warning("Can't read %s (%s) — videos use ffpyplayer", path, e)
        return VideoConfig()
    if not found or not cp.has_section("video"):
        return VideoConfig()

    sec = cp["video"]
    d = VideoConfig()
    backend = (sec.get("backend", fallback="") or sec.get("video_backend", fallback="")).strip().lower()
    if not backend:
        backend = d.backend
    elif backend not in BACKENDS:
        logger.warning("device.conf [video] backend = %r is not one of %s — using ffpyplayer",
                       backend, ", ".join(BACKENDS))
        backend = d.backend

    return VideoConfig(
        backend=backend,
        mpv_binary=_token(sec, "mpv_binary", d.mpv_binary),
        mpv_hwdec=_token(sec, "mpv_hwdec", d.mpv_hwdec),
        mpv_vo=_token(sec, "mpv_vo", d.mpv_vo),
        mpv_gpu_context=_token(sec, "mpv_gpu_context", d.mpv_gpu_context),
        mpv_audio_device=_token(sec, "mpv_audio_device", d.mpv_audio_device),
        mpv_drm_mode=_token(sec, "mpv_drm_mode", d.mpv_drm_mode),
        mpv_start_timeout=_seconds(sec, "mpv_start_timeout", d.mpv_start_timeout),
        mpv_end_grace=_seconds(sec, "mpv_end_grace", d.mpv_end_grace),
        source="device.conf",
    )
