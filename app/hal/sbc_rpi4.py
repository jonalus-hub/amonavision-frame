"""Raspberry Pi 4 — the code that used to live in viewer.py / bot.py / web/app.py,
moved here unchanged (same reads, same parsing, same log lines)."""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable

from .base import Audio, Buttons, Fan, Thermal, VideoDecode, Watchdog

# The button messages keep the viewer's logger name, as before the HAL.
_viewer_log = logging.getLogger("AmonaVisionViewer")


def _read(path: Path) -> str | None:
    try:
        return path.read_text().strip()
    except Exception:
        return None


class Rpi4Thermal(Thermal):
    def read_celsius(self) -> float | None:
        raw = _read(Path(self.sbc.thermal["path"]))
        return int(raw) / 1000 if raw is not None and raw.isdigit() else None


class Rpi4Buttons(Buttons):
    """gpiozero (Pi OS Bookworm+, lgpio pin factory set in amonavision.service)."""

    def __init__(self, sbc):
        super().__init__(sbc)
        self._button_cls = None
        self._loaded = False
        self._buttons: list = []   # keep the Button objects referenced so they aren't destroyed

    @property
    def available(self) -> bool:
        if not self._loaded:
            self._loaded = True
            try:
                from gpiozero import Button
                self._button_cls = Button
                _viewer_log.info("gpiozero library available. Hardware buttons enabled.")
            except (ImportError, Exception) as e:
                self._button_cls = None
                _viewer_log.warning("gpiozero library NOT available (%s). Hardware buttons disabled.", e)
        return self._button_cls is not None

    def attach(self, pin: int, on_press: Callable[[], None], on_release: Callable[[], None]) -> None:
        if not self.available:
            raise RuntimeError("gpiozero not available")
        # gpiozero.Button enables the internal pull-up and detects the press (falling edge).
        # bounce_time in seconds (Cherry MX: <5ms bounce; 20ms is safe and responsive)
        btn = self._button_cls(pin, pull_up=self.sbc.buttons["pull_up"],
                               bounce_time=self.sbc.buttons["bounce_time"])
        # when_pressed runs in a gpiozero thread
        btn.when_pressed = on_press
        btn.when_released = on_release
        self._buttons.append(btn)  # keep it alive

    def close(self) -> None:
        for btn in self._buttons:
            btn.close()


class Rpi4Audio(Audio):
    def mixer_params(self) -> dict:
        a = self.sbc.audio
        return dict(frequency=a["mixer_frequency"], size=a["mixer_size"],
                    channels=a["mixer_channels"], buffer=a["mixer_buffer"])

    def record_samplerate(self) -> int:
        return self.sbc.audio["record_samplerate"]

    def record_channels(self) -> int:
        return self.sbc.audio["record_channels"]

    def shell_env(self) -> dict[str, str | None]:
        a = self.sbc.audio
        return {
            "AV_HDMI_SINK_RE": a.get("hdmi_sink_pattern"),
            "AV_MIC_SOURCE_RE": a.get("mic_source_pattern"),
            "AV_HDMI_PW_DEVICE": a.get("hdmi_pipewire_device"),
            "AV_HDMI_ALSA_CARD": a.get("hdmi_alsa_card"),
        }


class Rpi4VideoDecode(VideoDecode):
    def ff_opts(self, has_external_voice: bool) -> dict:
        return {
            'an': has_external_voice,
            'paused': False,
            'vd': self.sbc.video["decoder"],  # BCM2835 hardware H.264 decoder on Pi 4
            'threads': str(self.sbc.video["threads"]),
        }


class Rpi4Fan(Fan):
    """Reads the PWM channel fan_control.py drives (fan_control.py itself is an
    rpi4-only service and keeps its own constants)."""

    def read_pwm(self) -> dict:
        chan = Path(self.sbc.fan["pwm_chip"]) / f"pwm{self.sbc.fan['pwm_channel']}"
        period = _read(chan / "period")
        duty = _read(chan / "duty_cycle")
        enable = _read(chan / "enable")
        return {
            "period": int(period) if period is not None and period.isdigit() else None,
            "duty": int(duty) if duty is not None and duty.isdigit() else None,
            "enabled": (enable == "1") if enable is not None else None,
        }


class Rpi4Watchdog(Watchdog):
    def status(self) -> dict:
        w = self.sbc.watchdog
        return {
            "device": w["device"],
            "present": Path(w["device"]).exists(),
            "runtime_sec": w["runtime_sec"],
            "reboot_sec": w["reboot_sec"],
            "fed_by": "systemd (RuntimeWatchdogSec, see setup.sh)",
        }


COMPONENTS = {
    "thermal": Rpi4Thermal,
    "buttons": Rpi4Buttons,
    "audio": Rpi4Audio,
    "video": Rpi4VideoDecode,
    "fan": Rpi4Fan,
    "watchdog": Rpi4Watchdog,
}
