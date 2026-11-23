"""Display detection from /sys/class/drm: which card drives the panel, which
HDMI port it is on, and the sound following the picture."""
import os
import subprocess
import sys
import threading

import pytest

from app import hal
from app.hal import drm, profiles
from app.hal.factory import Platform

from conftest import PROJECT_ROOT

FEF00700 = r"^alsa_output\.platform-fef00700\.hdmi\."
FEF05700 = r"^alsa_output\.platform-fef05700\.hdmi\."


def fake_sysfs(root, kms_card=1, render_card=0, status=None):
    """A Pi 4 /sys/class/drm: v3d (no connectors) and vc4 (two HDMI ports +
    the writeback connector). `status` maps connector -> 'connected'/..."""
    status = {"HDMI-A-1": "disconnected", "HDMI-A-2": "disconnected", **(status or {})}
    root.mkdir(parents=True, exist_ok=True)
    (root / f"card{render_card}").mkdir()
    (root / f"card{kms_card}").mkdir()
    (root / "renderD128").mkdir()
    for name, state in {**status, "Writeback-1": "unknown"}.items():
        d = root / f"card{kms_card}-{name}"
        d.mkdir()
        (d / "status").write_text(state + "\n")
    return root


def _platform(display_id):
    sel = hal.Selection(sbc=profiles.load_profile("sbc", "rpi4"),
                        display=profiles.load_profile("display", display_id),
                        source="test", version="0.0.0")
    return Platform(sel)


@pytest.fixture
def sysfs(tmp_path, monkeypatch):
    def make(**kw):
        root = fake_sysfs(tmp_path / "sys-drm", **kw)
        monkeypatch.setenv("AMONAVISION_SYS_DRM", str(root))
        return root
    return make


# ---------------------------------------------------------- drm module ----
def test_kms_card_is_the_one_with_connectors(sysfs):
    sysfs(kms_card=1, render_card=0)
    assert drm.kms_card_index() == 1


def test_kms_card_follows_a_swapped_probe_order(sysfs):
    """vc4 probed first: it is card0 and v3d card1. A fixed index 1 would open v3d."""
    sysfs(kms_card=0, render_card=1)
    assert drm.kms_card_index() == 0


def test_no_sysfs_means_unknown(tmp_path):
    assert drm.kms_card_index(tmp_path / "missing") is None
    assert drm.connected_connectors(root=tmp_path / "missing") == []


def test_connected_hdmi_ports(sysfs):
    sysfs(status={"HDMI-A-2": "connected"})
    assert drm.connected_connectors() == ["HDMI-A-2"]
    assert drm.detect_hdmi_connector() == "HDMI-A-2"


def test_preferred_port_wins_when_both_are_connected(sysfs):
    sysfs(status={"HDMI-A-1": "connected", "HDMI-A-2": "connected"})
    assert drm.detect_hdmi_connector(preferred="HDMI-A-2") == "HDMI-A-2"
    assert drm.detect_hdmi_connector(preferred="HDMI-A-1") == "HDMI-A-1"


def test_nothing_connected_returns_none_without_waiting(sysfs):
    sysfs()
    assert drm.detect_hdmi_connector(wait_seconds=0) is None


def test_waits_for_a_panel_that_is_still_waking_up(sysfs):
    root = sysfs()
    flip = threading.Timer(0.2, lambda: (root / "card1-HDMI-A-2" / "status").write_text("connected\n"))
    flip.start()
    try:
        assert drm.detect_hdmi_connector(wait_seconds=5, poll=0.05) == "HDMI-A-2"
    finally:
        flip.cancel()


# ------------------------------------------------------- HAL shell env ----
def test_auto_panel_on_hdmi1_gets_picture_and_sound_there(sysfs):
    sysfs(status={"HDMI-A-2": "connected"})
    env = _platform("hgfrtee-12.3").shell_env()
    assert env["AV_HDMI_CONNECTOR"] == "HDMI-A-2"
    assert env["AV_HDMI_SINK_RE"] == FEF05700
    assert env["AV_HDMI_PW_DEVICE"] == "alsa_card.platform-fef05700.hdmi"
    assert env["AV_HDMI_ALSA_CARD"] == "vc4hdmi1"
    assert env["AV_KMSDRM_DEVICE_INDEX"] == "1"


def test_auto_panel_on_hdmi0(sysfs):
    sysfs(status={"HDMI-A-1": "connected"})
    env = _platform("hgfrtee-12.3").shell_env()
    assert env["AV_HDMI_CONNECTOR"] == "HDMI-A-1"
    assert env["AV_HDMI_SINK_RE"] == FEF00700
    assert env["AV_HDMI_ALSA_CARD"] == "vc4hdmi0"


def test_auto_panel_not_found_falls_back_to_hdmi0(sysfs):
    sysfs()
    env = _platform("hgfrtee-12.3").shell_env()
    assert env["AV_HDMI_CONNECTOR"] == "HDMI-A-1"
    assert env["AV_HDMI_SINK_RE"] == FEF00700


def test_lcdwiki_keeps_its_fixed_connector(sysfs):
    """Its mode + EDID are forced on HDMI-A-1 at boot: never moved by detection."""
    sysfs(status={"HDMI-A-2": "connected"})
    env = _platform("lcdwiki-10.1-h").shell_env()
    assert env["AV_HDMI_CONNECTOR"] == "HDMI-A-1"
    assert env["AV_HDMI_SINK_RE"] == FEF00700


def test_card_index_comes_from_sysfs_for_every_panel(sysfs):
    sysfs(kms_card=0, render_card=1, status={"HDMI-A-1": "connected"})
    assert _platform("lcdwiki-10.1-h").shell_env()["AV_KMSDRM_DEVICE_INDEX"] == "0"
    assert _platform("hgfrtee-12.3").shell_env()["AV_KMSDRM_DEVICE_INDEX"] == "0"


def test_no_sysfs_keeps_the_profile_values():
    """conftest points AMONAVISION_SYS_DRM at nothing: the legacy literals."""
    env = _platform("hgfrtee-12.3").shell_env()
    assert env["AV_KMSDRM_DEVICE_INDEX"] == "1"
    assert env["AV_HDMI_CONNECTOR"] == "HDMI-A-1"


def test_display_profile_hdmi_connector_is_validated():
    data = profiles._read_yaml("display", "hgfrtee-12.3")
    assert profiles.DisplayProfile.from_dict(data).hdmi_connector == "auto"
    assert profiles.DisplayProfile.from_dict({**data, "hdmi_connector": "HDMI-A-2"}).hdmi_connector == "HDMI-A-2"
    with pytest.raises(profiles.ProfileError):
        profiles.DisplayProfile.from_dict({**data, "hdmi_connector": "left"})


def test_every_rpi4_hdmi_port_has_its_audio_names():
    ports = profiles.load_profile("sbc", "rpi4").audio["hdmi_ports"]
    assert set(ports) == {"HDMI-A-1", "HDMI-A-2"}
    for names in ports.values():
        assert set(names) == {"hdmi_sink_pattern", "hdmi_pipewire_device", "hdmi_alsa_card"}


def test_cli_wait_display(tmp_path):
    root = fake_sysfs(tmp_path / "sys-drm", kms_card=0, render_card=1, status={"HDMI-A-2": "connected"})
    conf = tmp_path / "device.conf"
    conf.write_text("[device]\nsbc = rpi4\ndisplay = hgfrtee-12.3\nnet = wifi\n")
    env = {"PATH": "/usr/bin:/bin", "AMONAVISION_DEVICE_CONF": str(conf), "AMONAVISION_SYS_DRM": str(root),
           # same import path as this interpreter (PyYAML may live outside the venv)
           **{k: os.environ[k] for k in ("PYTHONPATH",) if k in os.environ}}
    out = subprocess.run([sys.executable, "-B", "-m", "app.hal", "env", "--wait-display", "1"],
                         cwd=PROJECT_ROOT, env=env, capture_output=True, text=True, check=True).stdout
    script = out + '\nprintf "%s|%s|%s" "$AV_KMSDRM_DEVICE_INDEX" "$AV_HDMI_CONNECTOR" "$AV_HDMI_ALSA_CARD"'
    evaluated = subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=True).stdout
    assert evaluated == "0|HDMI-A-2|vc4hdmi1"
