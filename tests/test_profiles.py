"""Profile files, device.conf selection and the board check."""
import builtins
import logging

import pytest

from app import hal
from app.hal import builtin, profiles

from conftest import DISPLAY_IDS, SBC_IDS


def _leaves(data, prefix=""):
    if isinstance(data, dict):
        for k, v in data.items():
            yield from _leaves(v, f"{prefix}{k}.")
    else:
        yield prefix.rstrip("."), data


@pytest.mark.parametrize("profile_id", SBC_IDS)
def test_sbc_profiles_parse(profile_id):
    p = profiles.load_profile("sbc", profile_id)
    assert p.id == profile_id
    assert p.status in ("verified", "unverified")
    assert set(p.button_pins) == {"previous", "play", "record", "next"}


@pytest.mark.parametrize("profile_id", DISPLAY_IDS)
def test_display_profiles_parse(profile_id):
    p = profiles.load_profile("display", profile_id)
    assert p.id == profile_id
    assert p.status in ("verified", "unverified")
    assert all(v % 2 == 0 for v in p.video_canvas)


@pytest.mark.parametrize("kind,profile_id", [("sbc", i) for i in SBC_IDS] + [("display", i) for i in DISPLAY_IDS])
def test_status_matches_todo(kind, profile_id):
    """Verified profiles have every value filled in; unverified ones say what's missing."""
    data = profiles._read_yaml(kind, profile_id)
    if data["status"] == "verified":
        assert data["todo"] == []
        nulls = [k for k, v in _leaves(data) if v is None]
        assert nulls == [], f"verified profile with unknown values: {nulls}"
    else:
        assert data["todo"], "an unverified profile must list its TODOs"


def test_production_profiles_match_builtin():
    assert profiles._read_yaml("sbc", "rpi4") == builtin.RPI4
    assert profiles._read_yaml("display", "lcdwiki-10.1-h") == builtin.LCDWIKI_10_1_H


def test_display_panel_reset_flag():
    """panel_reset drives the forced-mode/EDID + signal-rescue install (setup.sh,
    launch.sh, the panel scripts). The LCDwiki needs it; a standard panel doesn't."""
    assert profiles.load_profile("display", "lcdwiki-10.1-h").panel_reset is True
    assert profiles.load_profile("display", "hgfrtee-12.3").panel_reset is False


def test_missing_device_conf_uses_defaults(caplog):
    caplog.set_level(logging.DEBUG, logger="AmonaVision.hal")
    sel = hal.get_selection()
    assert (sel.sbc.id, sel.display.id, sel.source) == ("rpi4", "lcdwiki-10.1-h", "defaults")
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


@pytest.mark.parametrize("text", [
    "",                                    # empty file
    "[device]\n",                          # section, no keys
    "[device]\nsbc =\ndisplay =\n",        # empty values
    "[other]\nsbc = radxa-zero3\n",        # keys in another section
])
def test_device_conf_without_keys_uses_defaults(device_conf, text, caplog):
    caplog.set_level(logging.DEBUG, logger="AmonaVision.hal")
    device_conf(text)
    sel = hal.get_selection()
    assert (sel.sbc.id, sel.display.id) == ("rpi4", "lcdwiki-10.1-h")
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_device_conf_partial(device_conf):
    device_conf("[device]\ndisplay = hgfrtee-12.3\n")
    sel = hal.get_selection()
    assert (sel.sbc.id, sel.display.id, sel.source) == ("rpi4", "hgfrtee-12.3", "device.conf")


def test_device_conf_selects_profiles(device_conf):
    device_conf("[device]\nsbc = radxa-zero3\ndisplay = hgfrtee-12.3\n")
    sel = hal.get_selection()
    assert (sel.sbc.id, sel.display.id) == ("radxa-zero3", "hgfrtee-12.3")


def test_unknown_profile_falls_back_with_error(device_conf, caplog):
    device_conf("[device]\nsbc = nope\ndisplay = nope-either\n")
    sel = hal.get_selection()
    assert (sel.sbc.id, sel.display.id) == ("rpi4", "lcdwiki-10.1-h")
    errors = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
    assert any("unknown sbc profile 'nope'" in m for m in errors)
    assert any("unknown display profile 'nope-either'" in m for m in errors)


def test_unparseable_device_conf_falls_back(device_conf, caplog):
    device_conf("not an ini file\n")
    sel = hal.get_selection()
    assert (sel.sbc.id, sel.display.id) == ("rpi4", "lcdwiki-10.1-h")
    assert any(r.levelno == logging.ERROR for r in caplog.records)


def test_without_pyyaml_uses_builtin(monkeypatch, caplog):
    real_import = builtins.__import__

    def no_yaml(name, *a, **k):
        if name == "yaml":
            raise ImportError("no yaml")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", no_yaml)
    sel = profiles.load_selection(check_board=False)
    assert (sel.sbc.id, sel.display.id) == ("rpi4", "lcdwiki-10.1-h")
    assert sel.sbc.button_pins == {"previous": 9, "play": 17, "record": 27, "next": 22}
    assert any("PyYAML is not installed" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("model,warns", [
    (b"Raspberry Pi 4 Model B Rev 1.5\x00", False),
    (b"Raspberry Pi 4 Model B Rev 1.1\x00", False),
    (b"Radxa ZERO 3W\x00", True),
    (b"Raspberry Pi 3 Model B Plus Rev 1.3\x00", True),
])
def test_device_tree_check_rpi4(tmp_path, caplog, model, warns):
    path = tmp_path / "model"
    path.write_bytes(model)
    sbc = profiles.load_profile("sbc", "rpi4")
    assert profiles.check_device_tree(sbc, path) == model.rstrip(b"\x00").decode()
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert bool(warnings) == warns


def test_device_tree_check_missing_file_is_silent(tmp_path, caplog):
    sbc = profiles.load_profile("sbc", "rpi4")
    assert profiles.check_device_tree(sbc, tmp_path / "absent") is None
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_unverified_sbc_cannot_be_checked(tmp_path, caplog):
    path = tmp_path / "model"
    path.write_bytes(b"Some Board\x00")
    profiles.check_device_tree(profiles.load_profile("sbc", "radxa-zero3"), path)
    assert any("no verified device-tree model" in r.getMessage() for r in caplog.records)


def test_label_uses_version_file():
    sel = hal.get_selection()
    version = (profiles.PROJECT_ROOT / "VERSION").read_text().strip()
    assert sel.label() == f"v{version} · rpi4 · lcdwiki-10.1-h"
