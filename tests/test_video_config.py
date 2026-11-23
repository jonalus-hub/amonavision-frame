"""device.conf [video]: backend selection and mpv options."""

import pytest

from app.video import VideoConfig, read_video_config
from app.video.config import BACKENDS


def _conf(tmp_path, text):
    p = tmp_path / "device.conf"
    p.write_text(text)
    return p


def test_no_device_conf_means_ffpyplayer(tmp_path):
    cfg = read_video_config(tmp_path / "missing.conf")
    assert cfg == VideoConfig()
    assert cfg.backend == "ffpyplayer" and cfg.source == "defaults"


def test_device_conf_without_video_section_means_ffpyplayer(tmp_path):
    cfg = read_video_config(_conf(tmp_path, "[device]\nsbc = rpi4\ndisplay = hgfrtee-12.3\n"))
    assert cfg.backend == "ffpyplayer"


def test_env_var_points_at_the_device_conf(tmp_path, monkeypatch):
    monkeypatch.setenv("AMONAVISION_DEVICE_CONF", str(_conf(tmp_path, "[video]\nbackend = mpv\n")))
    assert read_video_config().backend == "mpv"


def test_mpv_with_defaults(tmp_path):
    cfg = read_video_config(_conf(tmp_path, "[video]\nbackend = mpv\n"))
    assert cfg.backend == "mpv" and cfg.source == "device.conf"
    assert (cfg.mpv_hwdec, cfg.mpv_vo, cfg.mpv_gpu_context, cfg.mpv_audio_device) == \
        ("v4l2m2m-copy", "gpu", "drm", "pulse")
    assert (cfg.mpv_start_timeout, cfg.mpv_end_grace, cfg.mpv_drm_mode) == (5.0, 10.0, "auto")


def test_every_mpv_option_is_read(tmp_path):
    cfg = read_video_config(_conf(tmp_path, """
[video]
backend = MPV
mpv_binary = /opt/mpv/bin/mpv
mpv_hwdec = drm-copy
mpv_vo = drm
mpv_gpu_context = auto
mpv_audio_device = pulse/alsa_output.platform-fef05700.hdmi.hdmi-stereo
mpv_drm_mode = preferred
mpv_start_timeout = 8
mpv_end_grace = 3.5
"""))
    assert cfg == VideoConfig(
        backend="mpv", mpv_binary="/opt/mpv/bin/mpv", mpv_hwdec="drm-copy", mpv_vo="drm",
        mpv_gpu_context="auto", mpv_audio_device="pulse/alsa_output.platform-fef05700.hdmi.hdmi-stereo",
        mpv_drm_mode="preferred", mpv_start_timeout=8.0, mpv_end_grace=3.5, source="device.conf")


def test_uppercase_brief_style_key_is_accepted(tmp_path):
    # configparser lower-cases keys: VIDEO_BACKEND works inside [video] too
    assert read_video_config(_conf(tmp_path, "[video]\nVIDEO_BACKEND = mpv\n")).backend == "mpv"


@pytest.mark.parametrize("value", ["vlc", "yes", "mpv2"])
def test_unknown_backend_falls_back_to_ffpyplayer(tmp_path, value):
    assert read_video_config(_conf(tmp_path, f"[video]\nbackend = {value}\n")).backend == "ffpyplayer"


def test_bad_values_fall_back_to_their_defaults(tmp_path):
    cfg = read_video_config(_conf(tmp_path, """
[video]
backend = mpv
mpv_hwdec = v4l2m2m copy
mpv_start_timeout = soon
mpv_end_grace = -1
"""))
    d = VideoConfig()
    assert cfg.backend == "mpv"
    assert (cfg.mpv_hwdec, cfg.mpv_start_timeout, cfg.mpv_end_grace) == \
        (d.mpv_hwdec, d.mpv_start_timeout, d.mpv_end_grace)


def test_broken_device_conf_means_ffpyplayer(tmp_path):
    assert read_video_config(_conf(tmp_path, "this is [not an ini\n")).backend == "ffpyplayer"


def test_device_conf_example_documents_the_video_section():
    from conftest import PROJECT_ROOT
    text = (PROJECT_ROOT / "device.conf.example").read_text()
    assert "[video]" in text
    for key in ("backend", "mpv_hwdec", "mpv_vo", "mpv_audio_device"):
        assert key in text
    assert set(BACKENDS) == {"ffpyplayer", "mpv"}
