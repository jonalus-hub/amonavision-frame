"""Video playback: ffpyplayer (default) or mpv with hardware decoding.

The backend comes from device.conf [video] (see config.py). ffpyplayer stays
the default and is also what a failed mpv playback falls back to.
"""

from .base import FfpyplayerBackend, VideoBackend, VideoEvent
from .config import VideoConfig, read_video_config
from .mpv import DrmTarget, MpvBackend, MpvUnavailable
from .runtime import Heartbeat, run_dir

__all__ = [
    "DrmTarget", "FfpyplayerBackend", "Heartbeat", "MpvBackend", "MpvUnavailable",
    "VideoBackend", "VideoConfig", "VideoEvent", "read_video_config", "run_dir",
]
