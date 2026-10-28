"""Shared test setup.

Run from the project root: `python -m pytest tests/` (see README, "Tests").
Tests never read the developer's own device.conf, the frame's database or the
real /dev/shm cache.
"""
import os
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")

from app import hal  # noqa: E402
from app.hal import factory, profiles  # noqa: E402

SBC_IDS = profiles.available_profiles("sbc")
DISPLAY_IDS = profiles.available_profiles("display")


def _clear_caches():
    profiles.get_selection.cache_clear()
    factory.get_platform.cache_clear()


@pytest.fixture(autouse=True)
def isolated_selection(tmp_path, monkeypatch):
    """Point device.conf at a file that doesn't exist (= defaults) and reset
    the cached selection around every test."""
    monkeypatch.setenv("AMONAVISION_DEVICE_CONF", str(tmp_path / "no-device.conf"))
    monkeypatch.delenv("AMONAVISION_HAL_IMPL", raising=False)
    _clear_caches()
    yield
    _clear_caches()


@pytest.fixture
def device_conf(tmp_path, monkeypatch):
    """Write a device.conf and select it. Returns a function(text) -> Path."""
    def write(text: str) -> Path:
        path = tmp_path / "device.conf"
        path.write_text(text, encoding="utf-8")
        monkeypatch.setenv("AMONAVISION_DEVICE_CONF", str(path))
        _clear_caches()
        return path
    return write


@pytest.fixture
def sbc_profile():
    """Load an SBC profile by id, optionally overriding top-level sections."""
    def load(profile_id: str, **overrides):
        data = profiles._read_yaml("sbc", profile_id)
        data.update(overrides)
        return hal.SbcProfile.from_dict(data)
    return load
