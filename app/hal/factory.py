"""Build the Platform (profiles + board implementation) for this process."""
from __future__ import annotations

import functools
import importlib
import os

from .profiles import DisplayProfile, SbcProfile, Selection, get_selection

# profile `implementation:` -> module in this package exposing COMPONENTS
IMPLEMENTATIONS = {
    "rpi4": "sbc_rpi4",
    "radxa": "sbc_radxa",
    "fake": "fake",
}


class Platform:
    """The selected board + panel. Components are created on first use, so a
    process only touches the hardware it actually needs (the bot never loads
    the GPIO library, for instance)."""

    def __init__(self, selection: Selection, implementation: str | None = None):
        self.selection = selection
        self.implementation = implementation or selection.sbc.implementation
        if self.implementation not in IMPLEMENTATIONS:
            raise ValueError(f"no implementation '{self.implementation}' (known: {', '.join(IMPLEMENTATIONS)})")
        module = importlib.import_module(f".{IMPLEMENTATIONS[self.implementation]}", __package__)
        self._classes = module.COMPONENTS
        self._instances: dict = {}

    @property
    def sbc(self) -> SbcProfile:
        return self.selection.sbc

    @property
    def display(self) -> DisplayProfile:
        return self.selection.display

    def label(self) -> str:
        return self.selection.label()

    def _component(self, name: str):
        obj = self._instances.get(name)
        if obj is None:
            obj = self._instances[name] = self._classes[name](self.sbc)
        return obj

    @property
    def thermal(self):
        return self._component("thermal")

    @property
    def buttons(self):
        return self._component("buttons")

    @property
    def audio(self):
        return self._component("audio")

    @property
    def video(self):
        return self._component("video")

    @property
    def fan(self):
        return self._component("fan")

    @property
    def watchdog(self):
        return self._component("watchdog")

    def shell_env(self) -> dict[str, str | None]:
        """Variables for the shell scripts (python3 -m app.hal env)."""
        env = {
            "AV_SBC": self.sbc.id,
            "AV_DISPLAY": self.display.id,
            "AV_HDMI_CONNECTOR": self.sbc.hdmi_connector,
            "AV_KMSDRM_DEVICE_INDEX": self.sbc.kmsdrm_device_index,
            "AV_PANEL_UHUBCTL_LOCATIONS": " ".join(self.sbc.panel_power.get("uhubctl_locations") or []) or None,
            # Whether this panel needs the LCDwiki-style hand-holding: its mode +
            # EDID forced at boot and the signal re-present / USB power-cycle
            # rescue. Always emitted ("1"/"0") so launch.sh, the panel scripts and
            # setup.sh can branch on it; "0" means the panel negotiates HDMI
            # normally and setup.sh forces nothing and installs no reset units.
            "AV_PANEL_RESET": "1" if self.display.panel_reset else "0",
            # Whether this frame has a 4G dongle (device.conf net). "0" = Wi-Fi
            # only, so the liveness watchdog skips the 4G recovery/reboot ladder.
            "AV_HAS_4G": "1" if self.selection.has_4g else "0",
        }
        env.update(self.audio.shell_env())
        return {k: (None if v is None else str(v)) for k, v in env.items()}


@functools.lru_cache(maxsize=1)
def get_platform() -> Platform:
    """The process-wide platform for the device.conf selection.
    AMONAVISION_HAL_IMPL=fake swaps in simulated hardware (tests, simulator)."""
    return Platform(get_selection(), os.environ.get("AMONAVISION_HAL_IMPL") or None)
