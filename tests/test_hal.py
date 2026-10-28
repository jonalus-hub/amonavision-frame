"""Board implementations: rpi4 (values unchanged), radxa skeleton, fake."""
import subprocess
import sys
import types

import pytest

from app import hal
from app.hal import profiles
from app.hal.factory import Platform

from conftest import PROJECT_ROOT


def _platform(sbc_id="rpi4", display_id="lcdwiki-10.1-h", impl=None, sbc=None):
    sel = hal.Selection(sbc=sbc or profiles.load_profile("sbc", sbc_id),
                        display=profiles.load_profile("display", display_id),
                        source="test", version="0.0.0")
    return Platform(sel, impl)


# ---------------------------------------------------------------- rpi4 ----
def test_rpi4_video_options_are_the_legacy_ones():
    video = _platform().video
    assert video.ff_opts(False) == {'an': False, 'paused': False, 'vd': 'h264_v4l2m2m', 'threads': '4'}
    assert list(video.ff_opts(True)) == ['an', 'paused', 'vd', 'threads']
    assert video.ff_opts(True)['an'] is True


def test_rpi4_audio_is_the_legacy_one():
    audio = _platform().audio
    assert audio.mixer_params() == dict(frequency=48000, size=-16, channels=2, buffer=4096)
    assert (audio.record_samplerate(), audio.record_channels()) == (48000, 1)


def test_rpi4_thermal(tmp_path, sbc_profile):
    temp = tmp_path / "temp"
    p = _platform(sbc=sbc_profile("rpi4", thermal={"path": str(temp), "throttling": "unavailable"}))
    temp.write_text("48312\n")
    assert p.thermal.read_celsius() == 48.312
    temp.write_text("garbage\n")
    assert p.thermal.read_celsius() is None
    temp.unlink()
    assert p.thermal.read_celsius() is None
    assert p.thermal.throttling().available is False


def test_rpi4_fan(tmp_path, sbc_profile):
    chan = tmp_path / "pwm0"
    chan.mkdir()
    p = _platform(sbc=sbc_profile("rpi4", fan={"pwm_chip": str(tmp_path), "pwm_channel": 0, "period_ns": 40000}))
    assert p.fan.read_pwm() == {"period": None, "duty": None, "enabled": None}
    (chan / "period").write_text("40000\n")
    (chan / "duty_cycle").write_text("12000\n")
    (chan / "enable").write_text("1\n")
    assert p.fan.read_pwm() == {"period": 40000, "duty": 12000, "enabled": True}


def test_rpi4_buttons_with_stub_gpiozero(monkeypatch):
    created = []

    class Button:
        def __init__(self, pin, **kw):
            created.append((pin, kw))
            self.closed = False

        def close(self):
            self.closed = True

    monkeypatch.setitem(sys.modules, "gpiozero", types.SimpleNamespace(Button=Button))
    buttons = _platform().buttons
    assert buttons.available
    mapping = buttons.pin_actions()
    assert mapping == {9: hal.Action.PREVIOUS, 17: hal.Action.PLAY, 27: hal.Action.RECORD, 22: hal.Action.NEXT}
    assert list(mapping) == [9, 17, 27, 22]
    pressed = []
    for pin in mapping:
        buttons.attach(pin, lambda p=pin: pressed.append(p), lambda: None)
    assert created == [(pin, {"pull_up": True, "bounce_time": 0.02}) for pin in (9, 17, 27, 22)]
    buttons._buttons[0].when_pressed()
    assert pressed == [9]
    buttons.close()
    assert all(b.closed for b in buttons._buttons)


def test_rpi4_buttons_without_gpiozero(monkeypatch):
    monkeypatch.setitem(sys.modules, "gpiozero", None)   # import fails
    buttons = _platform().buttons
    assert buttons.available is False
    with pytest.raises(RuntimeError):
        buttons.attach(9, lambda: None, lambda: None)


def test_config_pins_override_profile():
    buttons = _platform(impl="fake").buttons
    mapping = buttons.pin_actions({"previous": 5, "play": 6, "record": None, "next": 13})
    assert mapping == {5: hal.Action.PREVIOUS, 6: hal.Action.PLAY, 13: hal.Action.NEXT}


def test_rpi4_shell_env_matches_legacy_literals():
    env = _platform().shell_env()
    assert env["AV_HDMI_CONNECTOR"] == "HDMI-A-1"
    assert env["AV_HDMI_SINK_RE"] == r"^alsa_output\.platform-fef00700\.hdmi\."
    assert env["AV_MIC_SOURCE_RE"] == r"^alsa_input\.usb-"
    assert env["AV_HDMI_PW_DEVICE"] == "alsa_card.platform-fef00700.hdmi"
    assert env["AV_HDMI_ALSA_CARD"] == "vc4hdmi0"
    assert env["AV_KMSDRM_DEVICE_INDEX"] == "1"


def test_shell_bridge_cli(tmp_path):
    env = {"PATH": "/usr/bin:/bin", "AMONAVISION_DEVICE_CONF": str(tmp_path / "none")}
    out = subprocess.run([sys.executable, "-B", "-m", "app.hal", "env"], cwd=PROJECT_ROOT, env=env,
                         capture_output=True, text=True, check=True).stdout
    script = out + '\nprintf "%s|%s" "$AV_HDMI_SINK_RE" "$AV_HDMI_CONNECTOR"'
    evaluated = subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=True).stdout
    assert evaluated == r"^alsa_output\.platform-fef00700\.hdmi\.|HDMI-A-1"


# --------------------------------------------------------------- radxa ----
def test_radxa_skeleton_raises_not_implemented():
    p = _platform("radxa-zero3")
    for call in (p.thermal.read_celsius, lambda: p.video.ff_opts(False), p.fan.read_pwm,
                 p.watchdog.status, p.audio.mixer_params, p.audio.record_samplerate):
        with pytest.raises(NotImplementedError):
            call()
    with pytest.raises(NotImplementedError):
        p.buttons.attach(1, lambda: None, lambda: None)


def test_radxa_throttling_is_unavailable_not_an_error():
    info = _platform("radxa-zero3").thermal.throttling()
    assert info.available is False and info.reason


def test_radxa_buttons_report_unavailable():
    assert _platform("radxa-zero3").buttons.available is False


def test_radxa_shell_env_leaves_unknowns_out():
    env = _platform("radxa-zero3").shell_env()
    assert env["AV_SBC"] == "radxa-zero3"
    assert env["AV_HDMI_CONNECTOR"] is None and env["AV_HDMI_SINK_RE"] is None


# ---------------------------------------------------------------- fake ----
def test_fake_platform_via_env(monkeypatch):
    monkeypatch.setenv("AMONAVISION_HAL_IMPL", "fake")
    p = hal.get_platform()
    assert p.implementation == "fake"
    assert p.thermal.read_celsius() == 45.0
    assert 'vd' not in p.video.ff_opts(False)


def test_fake_buttons_press():
    buttons = _platform("radxa-zero3", impl="fake").buttons
    events = []
    for pin, action in buttons.pin_actions().items():
        buttons.attach(pin, lambda a=action: events.append(("down", a)), lambda a=action: events.append(("up", a)))
    buttons.press(1)
    assert events == [("down", hal.Action.PREVIOUS), ("up", hal.Action.PREVIOUS)]
