"""Interfaces every board implementation provides (see sbc_*.py).

Each component is built from the SBC profile (profiles/sbc/<id>.yaml). Common
code only talks to these; it never opens a /sys path or imports a GPIO library
itself. A board that doesn't support something yet raises NotImplementedError
(or reports it unavailable where that is a normal answer, e.g. throttling).
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Mapping

from .profiles import SbcProfile


class Action(str, Enum):
    """What a physical button does. Order = the order buttons are set up in."""
    PREVIOUS = "previous"   # previous photo
    PLAY = "play"           # play the voice note / video
    RECORD = "record"       # record a voice reply
    NEXT = "next"           # next photo


@dataclass(frozen=True)
class ThrottleInfo:
    available: bool
    reason: str = ""
    flags: Mapping[str, bool] | None = None


THROTTLING_UNAVAILABLE = ThrottleInfo(False, "not available on this board")


class Component:
    def __init__(self, sbc: SbcProfile):
        self.sbc = sbc


class Thermal(Component, ABC):
    @abstractmethod
    def read_celsius(self) -> float | None:
        """SoC temperature in °C, or None if it can't be read."""

    def throttling(self) -> ThrottleInfo:
        """Throttling / under-voltage history. Unavailable unless a board says otherwise."""
        return THROTTLING_UNAVAILABLE


class Buttons(Component, ABC):
    @property
    @abstractmethod
    def available(self) -> bool:
        """True if the button backend could be loaded on this machine."""

    def pin_actions(self, pins: Mapping[str, int | None] | None = None) -> dict[int, Action]:
        """Pin -> Action, in Action order. `pins` (action name -> pin, e.g. from
        config.txt) overrides the profile; a None pin is skipped."""
        source = self.sbc.button_pins if pins is None else pins
        mapping: dict[int, Action] = {}
        for action in Action:
            pin = source.get(action.value)
            if pin is not None:
                mapping[pin] = action
        return mapping

    @abstractmethod
    def attach(self, pin: int, on_press: Callable[[], None], on_release: Callable[[], None]) -> None:
        """Watch one pin; callbacks may run in a backend thread. Raises on failure."""

    @abstractmethod
    def close(self) -> None:
        """Release every attached pin. Raises on failure."""


class Audio(Component, ABC):
    @abstractmethod
    def mixer_params(self) -> dict:
        """Keyword arguments for pygame.mixer.pre_init() / init()."""

    @abstractmethod
    def record_samplerate(self) -> int:
        """Default microphone sample rate (config.txt [Audio] samplerate overrides it)."""

    @abstractmethod
    def record_channels(self) -> int:
        ...

    @abstractmethod
    def shell_env(self) -> dict[str, str | None]:
        """Names the shell scripts need (PipeWire / ALSA), see app/hal/__main__.py."""


class VideoDecode(Component, ABC):
    @abstractmethod
    def ff_opts(self, has_external_voice: bool) -> dict:
        """ffpyplayer MediaPlayer ff_opts for one video."""


class Fan(Component, ABC):
    @abstractmethod
    def read_pwm(self) -> dict:
        """{'period': int|None, 'duty': int|None, 'enabled': bool|None} — None = unreadable."""


class Watchdog(Component, ABC):
    @abstractmethod
    def status(self) -> dict:
        """Read-only description for diagnostics; the watchdog itself is fed by systemd."""
