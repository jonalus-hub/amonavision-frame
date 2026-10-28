"""Radxa boards (ZERO 3; Cubie A7 later) — SKELETON, nothing verified.

No Radxa board has been tested. Every method that needs real hardware data
raises NotImplementedError with what is missing; fill it in from a real board
and update profiles/sbc/radxa-zero3.yaml + docs/hardware/radxa-zero3.md.
"""
from __future__ import annotations

import logging
from typing import Callable

from .base import (
    THROTTLING_UNAVAILABLE,
    Audio,
    Buttons,
    Fan,
    ThrottleInfo,
    Thermal,
    VideoDecode,
    Watchdog,
)


class RadxaThermal(Thermal):
    def read_celsius(self) -> float | None:
        # TODO: suggestion only — find the SoC zone under /sys/class/thermal/
        # (thermal_zone*/type) on a real board, put it in the profile's
        # thermal.path and read it like Rpi4Thermal does.
        raise NotImplementedError(f"thermal read for SBC '{self.sbc.id}' (TODO: thermal.path)")

    def throttling(self) -> ThrottleInfo:
        # No direct equivalent of the Pi's throttling history: "not available", not an error.
        return THROTTLING_UNAVAILABLE


class RadxaButtons(Buttons):
    _warned = False

    @property
    def available(self) -> bool:
        # TODO: GPIO library and (gpiochip, line) for each header pin.
        if not self._warned:
            self._warned = True
            logging.getLogger("AmonaVisionViewer").warning(
                "Buttons not implemented for SBC '%s' yet (TODO). Hardware buttons disabled.", self.sbc.id)
        return False

    def attach(self, pin: int, on_press: Callable[[], None], on_release: Callable[[], None]) -> None:
        raise NotImplementedError(f"buttons for SBC '{self.sbc.id}' (TODO: GPIO backend and pins)")

    def close(self) -> None:
        pass   # nothing was attached


class RadxaAudio(Audio):
    def mixer_params(self) -> dict:
        raise NotImplementedError(f"mixer parameters for SBC '{self.sbc.id}' (TODO: audio.mixer_*)")

    def record_samplerate(self) -> int:
        raise NotImplementedError(f"record sample rate for SBC '{self.sbc.id}' (TODO: audio.record_samplerate)")

    def record_channels(self) -> int:
        raise NotImplementedError(f"record channels for SBC '{self.sbc.id}' (TODO: audio.record_channels)")

    def shell_env(self) -> dict[str, str | None]:
        # Unknown names stay None: the shell scripts then keep their own fallbacks.
        a = self.sbc.audio
        return {
            "AV_HDMI_SINK_RE": a.get("hdmi_sink_pattern"),
            "AV_MIC_SOURCE_RE": a.get("mic_source_pattern"),
            "AV_HDMI_PW_DEVICE": a.get("hdmi_pipewire_device"),
            "AV_HDMI_ALSA_CARD": a.get("hdmi_alsa_card"),
        }


class RadxaVideoDecode(VideoDecode):
    def ff_opts(self, has_external_voice: bool) -> dict:
        raise NotImplementedError(f"video decoding for SBC '{self.sbc.id}' (TODO: video.decoder)")


class RadxaFan(Fan):
    def read_pwm(self) -> dict:
        raise NotImplementedError(f"fan PWM for SBC '{self.sbc.id}' (TODO: fan.*)")


class RadxaWatchdog(Watchdog):
    def status(self) -> dict:
        raise NotImplementedError(f"watchdog for SBC '{self.sbc.id}' (TODO: watchdog.*)")


COMPONENTS = {
    "thermal": RadxaThermal,
    "buttons": RadxaButtons,
    "audio": RadxaAudio,
    "video": RadxaVideoDecode,
    "fan": RadxaFan,
    "watchdog": RadxaWatchdog,
}
