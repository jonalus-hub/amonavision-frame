"""The resolution-independent layout: identical at 1024x600, fits everywhere."""
import json
import subprocess
import sys

import pytest

from app.hal import layout, profiles

from conftest import DISPLAY_IDS, PROJECT_ROOT

FIXTURES = PROJECT_ROOT / "tests" / "fixtures"


def _fixture(name):
    data = json.loads((FIXTURES / name).read_text())
    data.pop("_source", None)
    return data


def test_1024x600_reproduces_the_original_pixel_values():
    original = _fixture("layout_1024x600.json")
    computed = layout.compute_layout(1024, 600)
    assert computed.scale == 1.0
    assert {k: getattr(computed, k) for k in original} == original
    assert layout.design_values() == original


def test_config_pill_values_pass_through_at_reference_size():
    computed = layout.compute_layout(1024, 600, pill_font=50, pill_bottom_margin=30)
    assert (computed.pill_font, computed.pill_bottom_margin) == (50, 30)


@pytest.mark.parametrize("display_id", DISPLAY_IDS)
def test_layout_fits_each_display(display_id):
    w, h = profiles.load_profile("display", display_id).native_size
    assert layout.overflows(layout.compute_layout(w, h), w, h) == []


@pytest.mark.parametrize("size", [(800, 480), (1280, 720), (1280, 800), (1920, 1080), (1920, 1280), (600, 1024)])
def test_layout_fits_common_sizes(size):
    assert layout.overflows(layout.compute_layout(*size), *size) == []


def test_layout_scales_on_the_short_axis():
    computed = layout.compute_layout(1920, 1280)
    assert computed.scale == pytest.approx(1.875)
    assert computed.rec_radius_base == round(55 * 1.875)


def test_rendered_ui_is_identical_to_pre_hal():
    """Every UI state at 1024x600, byte for byte, against main before the HAL."""
    import pygame

    expected = json.loads((FIXTURES / "render_1024x600.json").read_text())
    if (pygame.version.ver, ".".join(map(str, pygame.get_sdl_version()))) != (expected["pygame"], expected["sdl"]):
        pytest.skip(f"fixture rendered with pygame {expected['pygame']} / SDL {expected['sdl']}")
    out = subprocess.run([sys.executable, "tests/tools/render_states.py", str(PROJECT_ROOT)],
                         cwd=PROJECT_ROOT, capture_output=True, text=True, check=True).stdout
    result = json.loads(out.strip().splitlines()[-1])
    assert result["size"] == [1024, 600]
    differing = sorted(k for k in expected["states"] if result["states"].get(k) != expected["states"][k])
    assert differing == []
