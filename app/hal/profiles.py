"""Hardware profile selection: device.conf + profiles/{sbc,display}/*.yaml.

device.conf (project root, not in git) names the board and the panel:

    [device]
    sbc = rpi4
    display = lcdwiki-10.1-h

A missing file, section or key falls back to DEFAULT_SBC / DEFAULT_DISPLAY
silently, so frames deployed before the HAL existed need no change. An unknown
profile id is logged as an error and replaced by the default; a board whose
/proc/device-tree/model doesn't match the declared SBC is only a warning. Never
raise out of here for a configuration problem: the frame must keep running.

This package is also loaded by file path from web/app.py (which can't import
the `app` package by name), so imports inside app/hal must stay relative.
"""
from __future__ import annotations

import configparser
import copy
import functools
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import builtin

logger = logging.getLogger("AmonaVision.hal")

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PROFILES_DIR = PROJECT_ROOT / "profiles"
DEVICE_CONF = PROJECT_ROOT / "device.conf"
DT_MODEL_PATH = Path("/proc/device-tree/model")
VERSION_FILE = PROJECT_ROOT / "VERSION"

DEFAULT_SBC = "rpi4"
DEFAULT_DISPLAY = "lcdwiki-10.1-h"
KINDS = ("sbc", "display")


class ProfileError(Exception):
    """A profile file is missing, unreadable or malformed."""


# ---------------------------------------------------------------------------
# Profile data classes
# ---------------------------------------------------------------------------
def _req(data: dict, key: str, where: str) -> Any:
    if key not in data:
        raise ProfileError(f"{where}: missing key '{key}'")
    return data[key]


def _size(value: Any, where: str, even: bool = False) -> tuple[int, int]:
    if (not isinstance(value, (list, tuple)) or len(value) != 2
            or not all(isinstance(v, int) and v > 0 for v in value)):
        raise ProfileError(f"{where}: expected [width, height], got {value!r}")
    if even and any(v % 2 for v in value):
        raise ProfileError(f"{where}: width and height must be even (yuv420p), got {value!r}")
    return int(value[0]), int(value[1])


@dataclass(frozen=True)
class DisplayProfile:
    id: str
    name: str
    status: str
    native_size: tuple[int, int]
    connector: str | None
    kernel_mode: str | None
    edid_file: str | None
    usb_hid_ignore: tuple[str, ...]
    panel_width_mm: float | None
    audio_out: str | None
    image_canvas: tuple[int, int]
    video_canvas: tuple[int, int]
    todo: tuple[str, ...]

    @classmethod
    def from_dict(cls, data: dict) -> "DisplayProfile":
        where = f"display profile '{data.get('id', '?')}'"
        canvas = _req(data, "media_canvas", where) or {}
        width = _req(data, "panel_width_mm", where)
        return cls(
            id=str(_req(data, "id", where)),
            name=str(_req(data, "name", where)),
            status=str(_req(data, "status", where)),
            native_size=_size(_req(data, "native_size", where), f"{where} native_size"),
            connector=_req(data, "connector", where),
            kernel_mode=_req(data, "kernel_mode", where),
            edid_file=_req(data, "edid_file", where),
            usb_hid_ignore=tuple(_req(data, "usb_hid_ignore", where) or ()),
            panel_width_mm=float(width) if width is not None else None,
            audio_out=_req(data, "audio_out", where),
            image_canvas=_size(_req(canvas, "image", where), f"{where} media_canvas.image"),
            video_canvas=_size(_req(canvas, "video", where), f"{where} media_canvas.video", even=True),
            todo=tuple(data.get("todo") or ()),
        )


@dataclass(frozen=True)
class SbcProfile:
    id: str
    name: str
    status: str
    implementation: str
    device_tree_models: tuple[str, ...]
    kmsdrm_device_index: int | None
    hdmi_connector: str | None
    buttons: dict
    video: dict
    audio: dict
    thermal: dict
    fan: dict
    watchdog: dict
    panel_power: dict
    boot: dict
    todo: tuple[str, ...]

    @property
    def button_pins(self) -> dict:
        """Action name ('previous', 'play', 'record', 'next') -> pin (or None)."""
        return dict(self.buttons.get("pins") or {})

    @classmethod
    def from_dict(cls, data: dict) -> "SbcProfile":
        where = f"SBC profile '{data.get('id', '?')}'"
        display = _req(data, "display", where) or {}
        buttons = _req(data, "buttons", where) or {}
        pins = _req(buttons, "pins", where) or {}
        for action in ("previous", "play", "record", "next"):
            _req(pins, action, f"{where} buttons.pins")
        return cls(
            id=str(_req(data, "id", where)),
            name=str(_req(data, "name", where)),
            status=str(_req(data, "status", where)),
            implementation=str(_req(data, "implementation", where)),
            device_tree_models=tuple(_req(data, "device_tree_models", where) or ()),
            kmsdrm_device_index=_req(display, "kmsdrm_device_index", where),
            hdmi_connector=_req(display, "hdmi_connector", where),
            buttons=dict(buttons),
            video=dict(_req(data, "video", where) or {}),
            audio=dict(_req(data, "audio", where) or {}),
            thermal=dict(_req(data, "thermal", where) or {}),
            fan=dict(_req(data, "fan", where) or {}),
            watchdog=dict(_req(data, "watchdog", where) or {}),
            panel_power=dict(_req(data, "panel_power", where) or {}),
            boot=dict(_req(data, "boot", where) or {}),
            todo=tuple(data.get("todo") or ()),
        )


_PARSERS = {"sbc": SbcProfile.from_dict, "display": DisplayProfile.from_dict}


@dataclass(frozen=True)
class Selection:
    sbc: SbcProfile
    display: DisplayProfile
    source: str          # "device.conf" or "defaults"
    version: str

    def label(self) -> str:
        """e.g. 'v1.0.0 · rpi4 · lcdwiki-10.1-h' — logs and /pi."""
        return f"v{self.version} · {self.sbc.id} · {self.display.id}"


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------
def available_profiles(kind: str) -> list[str]:
    return sorted(p.stem for p in (PROFILES_DIR / kind).glob("*.yaml"))


def _read_yaml(kind: str, profile_id: str) -> dict:
    import yaml  # ImportError handled by the caller

    path = PROFILES_DIR / kind / f"{profile_id}.yaml"
    if not path.is_file():
        raise ProfileError(
            f"unknown {kind} profile '{profile_id}' "
            f"(available: {', '.join(available_profiles(kind)) or 'none'})"
        )
    try:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except (OSError, yaml.YAMLError) as e:
        raise ProfileError(f"cannot read {path}: {e}") from e
    if not isinstance(data, dict):
        raise ProfileError(f"{path}: not a mapping")
    if data.get("id") != profile_id:
        raise ProfileError(f"{path}: id is {data.get('id')!r}, expected '{profile_id}'")
    return data


def load_profile(kind: str, profile_id: str):
    """Parse profiles/<kind>/<profile_id>.yaml into its dataclass.

    The production profiles also exist built in (app/hal/builtin.py): if PyYAML
    is missing (a deploy that skipped `pip install -r requirements.txt`) or the
    YAML is broken, those are used instead so the frame still starts.
    """
    try:
        data = _read_yaml(kind, profile_id)
        return _PARSERS[kind](data)
    except ImportError:
        fallback = builtin.PROFILES[kind].get(profile_id)
        if fallback is None:
            raise ProfileError(f"PyYAML is not installed and there is no built-in {kind} profile '{profile_id}'")
        logger.warning("PyYAML is not installed — using the built-in %s profile '%s'", kind, profile_id)
        return _PARSERS[kind](copy.deepcopy(fallback))
    except ProfileError as e:
        fallback = builtin.PROFILES[kind].get(profile_id)
        if fallback is None:
            raise
        logger.error("%s — using the built-in %s profile '%s'", e, kind, profile_id)
        return _PARSERS[kind](copy.deepcopy(fallback))


def read_device_conf(path: Path | None = None) -> tuple[str, str, str]:
    """(sbc id, display id, source). Defaults for anything not set."""
    path = Path(path or os.environ.get("AMONAVISION_DEVICE_CONF") or DEVICE_CONF)
    cp = configparser.ConfigParser()
    try:
        found = cp.read(path, encoding="utf-8")
    except (configparser.Error, OSError, UnicodeDecodeError) as e:
        logger.error("Cannot parse %s (%s) — using the default hardware profiles", path, e)
        return DEFAULT_SBC, DEFAULT_DISPLAY, "defaults"
    if not found:
        return DEFAULT_SBC, DEFAULT_DISPLAY, "defaults"
    sbc = cp.get("device", "sbc", fallback="").strip() or DEFAULT_SBC
    display = cp.get("device", "display", fallback="").strip() or DEFAULT_DISPLAY
    return sbc, display, "device.conf"


def read_version() -> str:
    try:
        return VERSION_FILE.read_text(encoding="utf-8").strip() or "unknown"
    except OSError:
        return "unknown"


def check_device_tree(sbc: SbcProfile, model_path: Path | None = None) -> str | None:
    """Compare the declared SBC with /proc/device-tree/model. Logs only."""
    model_path = Path(model_path or DT_MODEL_PATH)
    try:
        model = model_path.read_bytes().rstrip(b"\x00").decode("utf-8", "replace").strip()
    except OSError:
        logger.debug("No %s — SBC profile '%s' not checked against the board", model_path, sbc.id)
        return None
    if not sbc.device_tree_models:
        logger.warning(
            "SBC profile '%s' has no verified device-tree model yet — cannot check it against this board ('%s')",
            sbc.id, model,
        )
    elif not any(model.startswith(prefix) for prefix in sbc.device_tree_models):
        logger.warning(
            "Hardware mismatch: device.conf declares SBC '%s' but this board reports '%s' — continuing anyway",
            sbc.id, model,
        )
    return model


def _load_or_default(kind: str, profile_id: str, default_id: str):
    try:
        return load_profile(kind, profile_id)
    except ProfileError as e:
        if profile_id == default_id:
            raise
        logger.error("%s — falling back to the default %s profile '%s'", e, kind, default_id)
        return load_profile(kind, default_id)


def load_selection(device_conf: Path | None = None, dt_model_path: Path | None = None,
                   check_board: bool = True) -> Selection:
    sbc_id, display_id, source = read_device_conf(device_conf)
    sbc = _load_or_default("sbc", sbc_id, DEFAULT_SBC)
    display = _load_or_default("display", display_id, DEFAULT_DISPLAY)
    if check_board:
        check_device_tree(sbc, dt_model_path)
    return Selection(sbc=sbc, display=display, source=source, version=read_version())


@functools.lru_cache(maxsize=1)
def get_selection() -> Selection:
    """The process-wide selection, loaded (and checked) once."""
    return load_selection()
