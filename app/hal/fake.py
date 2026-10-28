"""Simulated hardware for tests and the browser simulator.

Selected with AMONAVISION_HAL_IMPL=fake (or Platform(selection, "fake")).
Reads the selected SBC profile where it has a value, otherwise uses neutral
stand-ins — these are NOT hardware data for any real board.
"""
from __future__ import annotations

from typing import Callable

from .base import (
    THROTTLING_UNAVAILABLE,
    Action,
    Audio,
    Buttons,
    Fan,
    ThrottleInfo,
    Thermal,
    VideoDecode,
    Watchdog,
)


class FakeThermal(Thermal):
    celsius: float | None = 45.0

    def read_celsius(self) -> float | None:
        return self.celsius

    def throttling(self) -> ThrottleInfo:
        return THROTTLING_UNAVAILABLE


class FakeButtons(Buttons):
    def __init__(self, sbc):
        super().__init__(sbc)
        self.attached: dict[int, tuple[Callable[[], None], Callable[[], None]]] = {}
        self.closed = False

    @property
    def available(self) -> bool:
        return True

    def pin_actions(self, pins=None):
        mapping = super().pin_actions(pins)
        if not mapping and pins is None:
            # Profile without pins (unverified board): stand-in pins 1..4.
            mapping = {i + 1: action for i, action in enumerate(Action)}
        return mapping

    def attach(self, pin: int, on_press: Callable[[], None], on_release: Callable[[], None]) -> None:
        self.attached[pin] = (on_press, on_release)

    def close(self) -> None:
        self.closed = True

    def press(self, pin: int) -> None:
        """Simulate a full press + release of one pin."""
        on_press, on_release = self.attached[pin]
        on_press()
        on_release()


class FakeAudio(Audio):
    def _get(self, key: str, default):
        value = self.sbc.audio.get(key)
        return default if value is None else value

    def mixer_params(self) -> dict:
        return dict(frequency=self._get("mixer_frequency", 48000), size=self._get("mixer_size", -16),
                    channels=self._get("mixer_channels", 2), buffer=self._get("mixer_buffer", 4096))

    def record_samplerate(self) -> int:
        return self._get("record_samplerate", 48000)

    def record_channels(self) -> int:
        return self._get("record_channels", 1)

    def shell_env(self) -> dict[str, str | None]:
        a = self.sbc.audio
        return {
            "AV_HDMI_SINK_RE": a.get("hdmi_sink_pattern"),
            "AV_MIC_SOURCE_RE": a.get("mic_source_pattern"),
            "AV_HDMI_PW_DEVICE": a.get("hdmi_pipewire_device"),
            "AV_HDMI_ALSA_CARD": a.get("hdmi_alsa_card"),
        }


class FakeVideoDecode(VideoDecode):
    def ff_opts(self, has_external_voice: bool) -> dict:
        # Software decoding: no 'vd' (no hardware decoder in a test machine).
        return {'an': has_external_voice, 'paused': False}


class FakeFan(Fan):
    def read_pwm(self) -> dict:
        return {"period": None, "duty": None, "enabled": None}


class FakeWatchdog(Watchdog):
    def status(self) -> dict:
        return {"device": None, "present": False, "fed_by": "fake"}


COMPONENTS = {
    "thermal": FakeThermal,
    "buttons": FakeButtons,
    "audio": FakeAudio,
    "video": FakeVideoDecode,
    "fan": FakeFan,
    "watchdog": FakeWatchdog,
}
