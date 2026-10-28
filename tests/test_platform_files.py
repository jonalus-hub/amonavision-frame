"""Profiles, platform/ fragments and the files that still hard-code rpi4 values
must agree, so nothing drifts while setup.sh & co. are left untouched."""
import configparser
import re

import pytest

from app.hal import profiles

from conftest import PROJECT_ROOT

SETUP = (PROJECT_ROOT / "setup.sh").read_text()
RPI4 = profiles.load_profile("sbc", "rpi4")
LCDWIKI = profiles.load_profile("display", "lcdwiki-10.1-h")


def _fragment_lines(name):
    text = (PROJECT_ROOT / "platform/rpi4" / name).read_text()
    return [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]


def _setup_expanded():
    connector = re.search(r"^HDMI_CONNECTOR=(\S+)", SETUP, re.M).group(1)
    return SETUP.replace("${HDMI_CONNECTOR}", connector)


@pytest.mark.parametrize("line", _fragment_lines("config.txt.fragment"))
def test_config_txt_fragment_is_what_setup_writes(line):
    assert line in SETUP


@pytest.mark.parametrize("param", _fragment_lines("cmdline.txt.fragment"))
def test_cmdline_fragment_is_what_setup_writes(param):
    assert param in _setup_expanded()


@pytest.mark.parametrize("line", _fragment_lines("system.conf.fragment"))
def test_system_conf_fragment_is_what_setup_writes(line):
    assert line in SETUP


def test_display_profile_matches_setup():
    expanded = _setup_expanded()
    assert f"video={RPI4.hdmi_connector}:{LCDWIKI.kernel_mode}" in expanded
    assert f"drm.edid_firmware={RPI4.hdmi_connector}:edid/amona.bin" in expanded
    for quirk in LCDWIKI.usb_hid_ignore:
        assert f"usbhid.quirks={quirk}" in SETUP
    assert (PROJECT_ROOT / LCDWIKI.edid_file).is_file()


def test_shipped_config_txt_matches_profiles():
    cp = configparser.ConfigParser()
    cp.read(PROJECT_ROOT / "config.txt", encoding="utf-8")
    pins = RPI4.button_pins
    assert cp.getint("GPIO", "pin_previous") == pins["previous"]
    assert cp.getint("GPIO", "pin_play_pause") == pins["play"]
    assert cp.getint("GPIO", "pin_record") == pins["record"]
    assert cp.getint("GPIO", "pin_next") == pins["next"]
    assert cp.getint("Audio", "samplerate") == RPI4.audio["record_samplerate"]
    assert cp.getfloat("Overlay", "panel_width_mm") == LCDWIKI.panel_width_mm


def test_service_and_scripts_match_rpi4_profile():
    service = (PROJECT_ROOT / "amonavision.service").read_text()
    assert f"SDL_KMSDRM_DEVICE_INDEX={RPI4.kmsdrm_device_index}" in service
    assert f"GPIOZERO_PIN_FACTORY={RPI4.buttons['gpio_pin_factory']}" in service
    fan = (PROJECT_ROOT / "fan_control.py").read_text()
    assert f'PWM_CHIP    = "{RPI4.fan["pwm_chip"]}"' in fan
    assert f'TEMP_PATH   = "{RPI4.thermal["path"]}"' in fan
    assert f"PERIOD_NS   = {RPI4.fan['period_ns']}" in fan
    cycle = (PROJECT_ROOT / "tools/panel-power-cycle.sh").read_text()
    for loc in RPI4.panel_power["uhubctl_locations"]:
        assert f"uhubctl -l {loc} -a off" in cycle


@pytest.mark.parametrize("script,var,key", [
    ("launch.sh", "AV_HDMI_CONNECTOR", "hdmi_connector"),
    ("launch.sh", "AV_HDMI_SINK_RE", "hdmi_sink_pattern"),
    ("launch.sh", "AV_MIC_SOURCE_RE", "mic_source_pattern"),
    ("tools/bot-watchdog.sh", "AV_HDMI_SINK_RE", "hdmi_sink_pattern"),
    ("tools/bot-watchdog.sh", "AV_HDMI_PW_DEVICE", "hdmi_pipewire_device"),
    ("tools/bot-watchdog.sh", "AV_HDMI_ALSA_CARD", "hdmi_alsa_card"),
])
def test_shell_fallbacks_are_the_rpi4_values(script, var, key):
    """If the HAL can't run, the scripts fall back to exactly the rpi4 values."""
    text = (PROJECT_ROOT / script).read_text()
    fallback = re.search(r"\$\{" + var + r":-([^}]*)\}", text).group(1)
    expected = RPI4.hdmi_connector if key == "hdmi_connector" else RPI4.audio[key]
    assert fallback == expected
