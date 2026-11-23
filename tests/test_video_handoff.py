"""DisplayHandoff on a real pygame with SDL's dummy driver."""

import pytest

pygame = pytest.importorskip("pygame")

from app.video import handoff as handoff_mod  # noqa: E402
from app.video.handoff import DisplayHandoff  # noqa: E402


@pytest.fixture
def display(monkeypatch):
    monkeypatch.setenv("SDL_VIDEODRIVER", "dummy")
    monkeypatch.setenv("SDL_AUDIODRIVER", "dummy")
    pygame.display.init()
    pygame.display.set_mode((320, 200))
    yield
    pygame.display.quit()


def test_driver_is_detected_at_runtime(display):
    h = DisplayHandoff()
    assert h.driver == "dummy" and not h.must_release()


def test_x11_or_wayland_keeps_the_display(display):
    h = DisplayHandoff()
    h.driver = "x11"
    h.release()
    assert pygame.display.get_init() and not h.released


def test_kmsdrm_releases_and_restores_with_the_same_size_and_hidden_cursor(display):
    h = DisplayHandoff()
    h.driver = "kmsdrm"
    old = pygame.Surface((10, 10)).convert()
    h.release()
    assert h.released and not pygame.display.get_init()
    h.release()                                   # idempotent
    screen = h.restore((320, 200), 0)
    assert not h.released and pygame.display.get_init()
    assert screen.get_size() == (320, 200)
    assert pygame.mouse.get_visible() is False
    screen.blit(old, (0, 0))                      # surfaces made before still draw


class FlakyPygame:
    """Just enough of pygame for restore(): set_mode fails `failures` times."""

    class error(Exception):
        pass

    def __init__(self, failures):
        self.failures = failures
        self.inits = 0
        self.display = self
        self.mouse = self
        self.cursors = self

    # display
    def get_init(self):
        return True

    def get_driver(self):
        return "KMSDRM"

    def init(self):
        self.inits += 1

    def quit(self):
        pass

    def set_mode(self, size, flags):
        if self.failures:
            self.failures -= 1
            raise self.error("kmsdrm not available")
        return ("screen", size, flags)

    def set_caption(self, caption):
        pass

    # mouse / cursors
    def set_visible(self, v):
        pass

    def set_cursor(self, c):
        pass

    def Cursor(self, *a):
        return a


def test_restore_retries_while_the_drm_master_is_still_held(monkeypatch):
    monkeypatch.setattr(handoff_mod.time, "sleep", lambda s: None)
    pg = FlakyPygame(failures=2)
    h = DisplayHandoff(pg)
    assert h.driver == "kmsdrm" and h.must_release()
    h.release()
    assert h.restore((1920, 1280), 7) == ("screen", (1920, 1280), 7)
    assert pg.inits == 3


def test_restore_gives_up_after_the_attempts(monkeypatch):
    monkeypatch.setattr(handoff_mod.time, "sleep", lambda s: None)
    h = DisplayHandoff(FlakyPygame(failures=99))
    h.release()
    with pytest.raises(RuntimeError, match="could not reopen"):
        h.restore((1024, 600), 0, attempts=3)
