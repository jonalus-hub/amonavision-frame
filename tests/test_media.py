"""Photo / video pre-processing canvases per display profile."""
import importlib
import json
import subprocess

import numpy as np
import pytest

from conftest import DISPLAY_IDS, PROJECT_ROOT, _clear_caches


@pytest.fixture
def media(device_conf, monkeypatch, tmp_path):
    """(processor, video_processor) reloaded for a display profile."""
    def load(display_id):
        device_conf(f"[device]\ndisplay = {display_id}\n")
        from app.bot import processor, video_processor
        return importlib.reload(processor), importlib.reload(video_processor)
    yield load
    # back to the defaults for later tests
    monkeypatch.setenv("AMONAVISION_DEVICE_CONF", str(tmp_path / "no-device.conf"))
    _clear_caches()
    from app.bot import processor
    importlib.reload(processor)


def _ffmpeg_cmd(video_processor, monkeypatch):
    captured = {}

    def fake_run(cmd, **kw):
        captured["cmd"] = list(cmd)
        return subprocess.CompletedProcess(cmd, 0, b"", b"")

    monkeypatch.setattr(video_processor.subprocess, "run", fake_run)
    assert video_processor.optimize_video("in.mp4", "out.mp4")
    cmd = captured["cmd"]
    cmd[3] = "<ffmpeg>"
    return cmd


def test_lcdwiki_ffmpeg_command_unchanged(media, monkeypatch):
    _, video_processor = media("lcdwiki-10.1-h")
    expected = json.loads((PROJECT_ROOT / "tests/fixtures/ffmpeg_cmd_lcdwiki-10.1-h.json").read_text())["cmd"]
    assert _ffmpeg_cmd(video_processor, monkeypatch) == expected


@pytest.mark.parametrize("display_id", DISPLAY_IDS)
def test_only_the_canvas_changes(media, monkeypatch, display_id):
    """Encoder settings are identical for every display; only scale/crop sizes differ."""
    from app.hal import profiles
    _, video_processor = media(display_id)
    cmd = _ffmpeg_cmd(video_processor, monkeypatch)
    expected = json.loads((PROJECT_ROOT / "tests/fixtures/ffmpeg_cmd_lcdwiki-10.1-h.json").read_text())["cmd"]
    i = cmd.index("-filter_complex")
    assert cmd[:i] == expected[:i] and cmd[i + 2:] == expected[i + 2:]
    w, h = profiles.load_profile("display", display_id).video_canvas
    assert f"scale={w}:{h}[bg]" in cmd[i + 1]
    assert f"crop={w // 4}:{h // 4}" in cmd[i + 1]


@pytest.mark.parametrize("display_id", DISPLAY_IDS)
@pytest.mark.parametrize("src_size", [(4000, 3000), (1080, 1920), (3000, 800), (640, 640)])
def test_photos_fill_the_canvas(media, tmp_path, display_id, src_size):
    import cv2
    from app.hal import profiles
    processor, _ = media(display_id)
    w, h = src_size
    img = np.zeros((h, w, 3), np.uint8)
    img[:, : w // 2] = (200, 120, 40)
    cv2.imwrite(str(tmp_path / "in.png"), img)
    processor.process_image_sync(tmp_path / "in.png", tmp_path / "out.png")
    out = cv2.imread(str(tmp_path / "out.png"))
    cw, ch = profiles.load_profile("display", display_id).image_canvas
    assert out.shape[:2] == (ch, cw)
