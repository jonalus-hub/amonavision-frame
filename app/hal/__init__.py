"""Hardware abstraction layer.

Common code asks this package for anything board- or panel-specific instead of
hard-coding it. The board and panel come from device.conf (see profiles.py);
without one, the frame runs as rpi4 + lcdwiki-10.1-h, as it always has.
"""
from .profiles import (  # noqa: F401
    DEFAULT_DISPLAY,
    DEFAULT_SBC,
    DisplayProfile,
    ProfileError,
    SbcProfile,
    Selection,
    get_selection,
    load_selection,
)
