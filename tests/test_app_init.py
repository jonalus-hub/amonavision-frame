"""The viewer starts on every SBC x display combination (simulated hardware)."""
import json
import os
import subprocess
import sys

import pytest

from conftest import DISPLAY_IDS, PROJECT_ROOT, SBC_IDS


def _run(tmp_path, sbc, display, impl):
    conf = tmp_path / "device.conf"
    conf.write_text(f"[device]\nsbc = {sbc}\ndisplay = {display}\n")
    env = dict(os.environ, AMONAVISION_DEVICE_CONF=str(conf), PYTHONDONTWRITEBYTECODE="1")
    env.pop("AMONAVISION_HAL_IMPL", None)
    if impl:
        env["AMONAVISION_HAL_IMPL"] = impl
    proc = subprocess.run([sys.executable, "tests/tools/app_init.py", str(PROJECT_ROOT)],
                          cwd=PROJECT_ROOT, env=env, capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr[-3000:]
    return json.loads(proc.stdout.strip().splitlines()[-1])


@pytest.mark.parametrize("display", DISPLAY_IDS)
@pytest.mark.parametrize("sbc", SBC_IDS)
def test_viewer_initialises(tmp_path, sbc, display):
    r = _run(tmp_path, sbc, display, "fake")
    assert r["label"].endswith(f" · {sbc} · {display}")
    assert r["size"] == r["native_size"]
    assert r["overflows"] == []
    assert r["drawn"] == ["SPLASH", "IDLE", "WAITING_ACTION", "RECORDING", "PLAYING"]
    assert r["gpio_available"] is True
    # config.txt pins 9/17/27/22 -> left/space/r/right, a press and a release each
    assert r["keys"] == ["left", "left", "space", "space", "r", "r", "right", "right"]


def test_viewer_initialises_on_real_rpi4_implementation(tmp_path):
    """The production path (no fake) loads; without gpiozero the buttons are off."""
    r = _run(tmp_path, "rpi4", "lcdwiki-10.1-h", None)
    assert r["implementation"] == "rpi4"
    assert r["size"] == [1024, 600]
    assert r["overflows"] == []
