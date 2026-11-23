"""Build the Platform (profiles + board implementation) for this process."""
from __future__ import annotations

import functools
import importlib
import os

from . import drm
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

    def hdmi_connector(self, wait_seconds: float = 0.0) -> str | None:
        """The HDMI connector the panel is on. Display profiles that leave it
        unset keep the SBC's fixed connector (the LCDwiki needs that: its mode +
        EDID are forced on it at boot). With "auto", the connected port, waiting
        up to `wait_seconds` for a panel still waking up; the SBC's connector if
        none shows up."""
        wanted = self.display.hdmi_connector
        if wanted is None:
            return self.sbc.hdmi_connector
        if wanted != "auto":
            return wanted
        found = drm.detect_hdmi_connector(preferred=self.sbc.hdmi_connector, wait_seconds=wait_seconds)
        return found or self.sbc.hdmi_connector

    def shell_env(self, wait_display: float = 0.0) -> dict[str, str | None]:
        """Variables for the shell scripts (python3 -m app.hal env)."""
        connector = self.hdmi_connector(wait_display)
        card = drm.kms_card_index()
        env = {
            "AV_SBC": self.sbc.id,
            "AV_DISPLAY": self.display.id,
            "AV_HDMI_CONNECTOR": connector,
            # The card that really has the connectors; the profile's index only
            # if sysfs can't tell (see app/hal/drm.py for why it can move).
            "AV_KMSDRM_DEVICE_INDEX": card if card is not None else self.sbc.kmsdrm_device_index,
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
        # Sound goes out of the same HDMI port as the picture.
        port_audio = (self.sbc.audio.get("hdmi_ports") or {}).get(connector) or {}
        for env_key, audio_key in (("AV_HDMI_SINK_RE", "hdmi_sink_pattern"),
                                   ("AV_HDMI_PW_DEVICE", "hdmi_pipewire_device"),
                                   ("AV_HDMI_ALSA_CARD", "hdmi_alsa_card")):
            if port_audio.get(audio_key):
                env[env_key] = port_audio[audio_key]
        return {k: (None if v is None else str(v)) for k, v in env.items()}


@functools.lru_cache(maxsize=1)
def get_platform() -> Platform:
    """The process-wide platform for the device.conf selection.
    AMONAVISION_HAL_IMPL=fake swaps in simulated hardware (tests, simulator)."""
    return Platform(get_selection(), os.environ.get("AMONAVISION_HAL_IMPL") or None)
