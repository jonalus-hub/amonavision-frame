"""The viewer with each video backend: same buttons, same logging, mpv fallbacks.

Runs the real ViewerApp on SDL's dummy driver. ffpyplayer is a fake
MediaPlayer; mpv is the real MpvBackend on a fake subprocess + fake IPC
(see test_mpv_backend.Harness), so no player or Pi is needed.
"""

import sys
from pathlib import Path

import pytest

pygame = pytest.importorskip("pygame")
pytest.importorskip("ffpyplayer")

from app.video import VideoConfig  # noqa: E402
from test_mpv_backend import Harness  # noqa: E402

VIDEO = Path("/media/processed/clip.mp4")
SESSION = {"id": 1, "type": "VIDEO", "status": "READ", "files": []}
OTHER = {"id": 2, "type": "PHOTO", "status": "READ", "files": []}
W, H = 640, 400


class FakeMediaPlayer:
    made = []

    def __init__(self, path, ff_opts):
        self.path, self.ff_opts = path, ff_opts
        self.frames = [(None, 0.0)] * 3 + [(None, 'eof')]
        self.closed = False
        FakeMediaPlayer.made.append(self)

    def get_frame(self):
        return self.frames.pop(0) if self.frames else (None, 'eof')

    def close_player(self):
        self.closed = True


class FakeDB:
    def __init__(self, calls):
        self.calls = calls

    def record_revisit(self, sid):
        self.calls.append(("revisit", sid))

    def record_shown(self, sid, dwell):
        pass   # only after >= 1 s on a session: timing-dependent, kept out of the traces

    def mark_as_read(self, sid):
        self.calls.append(("read", sid))

    def delete_session(self, sid):
        self.calls.append(("delete", sid))

    def get_next_session(self, *a, **k):
        return None

    def get_first_session(self, *a, **k):
        return None


class StubCache:
    cache_dir = Path("/nonexistent-cache")

    def __init__(self, *a, **k):
        pass

    def get_path(self, p):
        return Path(p)

    def prefetch_session(self, *a, **k):
        pass

    def cleanup(self):
        pass


@pytest.fixture(scope="module")
def viewer_mod(tmp_path_factory):
    import viewer
    viewer.SETTINGS.update(fullscreen=False, window_width=W, window_height=H)
    viewer.DB_PATH = tmp_path_factory.mktemp("db") / "viewer.db"
    viewer.PrefetchCache = StubCache
    return viewer


@pytest.fixture
def make_app(viewer_mod, monkeypatch, tmp_path):
    """make_app(backend=..., **video_conf) -> (app, calls, harnesses)."""
    v = viewer_mod
    calls = []
    harnesses = []
    monkeypatch.setenv("AMONAVISION_RUN_DIR", str(tmp_path / "run"))
    monkeypatch.setenv("SDL_KMSDRM_DEVICE_INDEX", "1")
    monkeypatch.setattr(v, "MediaPlayer", FakeMediaPlayer)
    monkeypatch.setattr(v, "log_usage", lambda kind, button, **kw: calls.append((kind, button, kw.get("state"))))
    FakeMediaPlayer.made = []

    def make(backend="ffpyplayer", installed=True, popen_error=None, **conf):
        monkeypatch.setattr(v, "VIDEO_CONF", VideoConfig(backend=backend, source="test", **conf))

        def mpv_factory(cfg, run_dir):
            h = Harness(run_dir, cfg=cfg, installed=installed)
            if popen_error is not None:
                def boom(cmd, **kw):
                    h.cmds.append(cmd)
                    raise popen_error
                h.backend._popen = boom
            harnesses.append(h)
            return h.backend

        monkeypatch.setattr(v, "MpvBackend", mpv_factory)
        app = v.ViewerApp()
        app.db = FakeDB(calls)
        app.current_session = dict(SESSION)
        app.state = "WAITING_ACTION"
        app.video_path = VIDEO
        app.audio_path = None
        app.current_image = pygame.Surface((W, H)).convert()
        app.start_recording = lambda: calls.append(("start_recording",))
        app.load_next_valid_session = lambda: app.load_session(dict(OTHER))
        app.load_previous_valid_session = lambda: app.load_session(dict(OTHER))
        return app, calls, harnesses

    yield make
    pygame.display.quit()


def _press(app, key, via_gpio):
    if via_gpio:
        app._gpio_queue.put((pygame.KEYDOWN, key))
        app._gpio_queue.put((pygame.KEYUP, key))
        app._mpv_tick()
    else:
        app._on_key_down(key)
        app._on_key_up(key)


def _play_ffpyplayer_to_the_end(app):
    while app.state == "PLAYING":
        app.draw()


def _mpv_first_frame(h):
    h.backend.poll()
    h.push({"event": "playback-restart"})


# --- default: ffpyplayer, exactly as before ------------------------------------

def test_default_backend_is_ffpyplayer(make_app, viewer_mod):
    app, calls, harnesses = make_app()
    assert app._mpv_backend is None and harnesses == []
    app._on_key_down(pygame.K_SPACE)
    assert app.state == "PLAYING" and app._mpv is None
    p = FakeMediaPlayer.made[-1]
    assert p.path == str(VIDEO)
    assert p.ff_opts == viewer_mod.HW.video.ff_opts(False)
    _play_ffpyplayer_to_the_end(app)
    assert app.state == "WAITING_ACTION" and p.closed and app.video_player is None
    assert calls == [("press", "play", "WAITING_ACTION")]


def test_ffpyplayer_stop_button(make_app):
    app, calls, _ = make_app()
    app._on_key_down(pygame.K_SPACE)
    app.draw()
    app._on_key_down(pygame.K_SPACE)
    assert app.state == "WAITING_ACTION" and FakeMediaPlayer.made[-1].closed


# --- mpv ------------------------------------------------------------------------

def test_mpv_plays_and_returns_to_the_slideshow_at_the_end(make_app):
    app, calls, (h,) = make_app("mpv")
    app._on_key_down(pygame.K_SPACE)
    assert app.state == "PLAYING" and app._mpv is h.backend and app.video_player is None
    assert h.cmds[-1][-1] == str(VIDEO) and "--audio-device=pulse" in h.cmds[-1]
    assert FakeMediaPlayer.made == []
    _mpv_first_frame(h)
    app._mpv_tick()
    before = app._last_input_time
    h.proc.returncode = 0
    app._mpv_tick()
    assert app.state == "WAITING_ACTION" and app._mpv is None
    assert app._last_input_time >= before
    assert h.backend.last_run.outcome == "ended"


def test_paired_voice_note_mutes_mpv(make_app):
    app, calls, (h,) = make_app("mpv")
    app.audio_path = "/nonexistent/voice.wav"       # the mixer fails to load it: logged, harmless
    app._on_key_down(pygame.K_SPACE)
    assert "--no-audio" in h.cmds[-1]


def test_kmsdrm_hands_the_screen_over_and_takes_it_back(make_app):
    app, calls, (h,) = make_app("mpv")
    app._handoff.driver = "kmsdrm"
    app._on_key_down(pygame.K_SPACE)
    assert not pygame.display.get_init(), "pygame let go of the display"
    cmd = h.cmds[-1]
    assert "--drm-device=/dev/dri/card1" in cmd and f"--drm-mode={W}x{H}" in cmd
    assert any(c.startswith("--drm-connector=HDMI-A-") for c in cmd)
    assert "--ontop" not in cmd
    _mpv_first_frame(h)
    app._mpv_tick()
    h.proc.returncode = 0
    app._mpv_tick()
    assert pygame.display.get_init() and app.screen.get_size() == (W, H)
    assert pygame.mouse.get_visible() is False
    app.draw()                                     # the UI draws again


def test_x11_keeps_the_display_and_puts_mpv_on_top(make_app):
    app, calls, (h,) = make_app("mpv")
    app._on_key_down(pygame.K_SPACE)
    assert pygame.display.get_init()
    assert "--ontop" in h.cmds[-1] and not any(c.startswith("--drm-") for c in h.cmds[-1])


@pytest.mark.parametrize("kmsdrm", [False, True], ids=["x11", "kmsdrm"])
@pytest.mark.parametrize("key,expect", [
    (pygame.K_SPACE, ("WAITING_ACTION", None)),
    (pygame.K_r, ("PLAYING", ("start_recording",))),
    (pygame.K_LEFT, ("WAITING_ACTION", ("revisit", 2))),
    (pygame.K_RIGHT, ("WAITING_ACTION", ("revisit", 2))),
], ids=["play", "record", "previous", "next"])
def test_every_button_during_mpv_quits_it_over_ipc_and_does_its_job(make_app, key, expect, kmsdrm):
    app, calls, (h,) = make_app("mpv")
    if kmsdrm:
        app._handoff.driver = "kmsdrm"
    app._on_key_down(pygame.K_SPACE)
    _mpv_first_frame(h)
    app._mpv_tick()
    calls.clear()
    _press(app, key, via_gpio=True)
    assert (["quit"], None) in h.sent
    assert app._mpv is None and not h.backend.active
    assert pygame.display.get_init(), "the screen is back with pygame"
    state, effect = expect
    assert app.state == state
    button = {pygame.K_SPACE: "play", pygame.K_r: "record", pygame.K_LEFT: "previous",
              pygame.K_RIGHT: "next"}[key]
    assert calls[0] == ("press", button, "PLAYING")
    if effect:
        assert effect in calls
    assert FakeMediaPlayer.made == [], "a button press is not a failure: no ffpyplayer fallback"


def test_same_buttons_same_interaction_log_with_both_backends(make_app):
    """The same presses produce the same log_usage / revisit / record calls."""
    script = [pygame.K_SPACE, pygame.K_RIGHT, pygame.K_SPACE, pygame.K_SPACE,
              pygame.K_SPACE, pygame.K_r]
    traces = {}
    for backend in ("ffpyplayer", "mpv"):
        app, calls, hs = make_app(backend)
        states = []
        for key in script:
            if app.state == "WAITING_ACTION" and app.current_session["id"] == 2:
                app.video_path = VIDEO          # the next session is a video too
            if backend == "mpv" and app._mpv is not None:
                _press(app, key, via_gpio=True)
            else:
                _press(app, key, via_gpio=False)
            if backend == "mpv" and app._mpv is not None:
                _mpv_first_frame(hs[-1])
                app._mpv_tick()
            states.append(app.state)
        traces[backend] = (calls, states)
    assert traces["mpv"] == traces["ffpyplayer"]


# --- fallbacks to ffpyplayer ------------------------------------------------------

def test_mpv_not_installed_uses_ffpyplayer(make_app):
    app, calls, (h,) = make_app("mpv", installed=False)
    app._on_key_down(pygame.K_SPACE)
    assert h.cmds == [] and app._mpv is None
    assert FakeMediaPlayer.made[-1].path == str(VIDEO) and app.state == "PLAYING"


@pytest.mark.parametrize("kmsdrm", [False, True], ids=["x11", "kmsdrm"])
def test_mpv_launch_error_uses_ffpyplayer_and_gives_the_screen_back(make_app, kmsdrm):
    app, calls, (h,) = make_app("mpv", popen_error=OSError("exec format error"))
    if kmsdrm:
        app._handoff.driver = "kmsdrm"
    app._on_key_down(pygame.K_SPACE)
    assert pygame.display.get_init()
    assert app._mpv is None and FakeMediaPlayer.made[-1].path == str(VIDEO)
    assert str(VIDEO) in app._mpv_failed_videos


@pytest.mark.parametrize("failure", ["no_start", "overrun", "crash", "killed_by_hand"])
def test_mpv_failure_kills_it_recovers_the_ui_and_plays_that_video_with_ffpyplayer(make_app, failure):
    app, calls, (h,) = make_app("mpv")
    app._handoff.driver = "kmsdrm"
    app._on_key_down(pygame.K_SPACE)
    proc = h.proc
    if failure == "no_start":
        h.t += 5.5
    else:
        _mpv_first_frame(h)
        app._mpv_tick()
        h.push({"request_id": 1, "error": "success", "data": 20.0})
        app._mpv_tick()
        if failure == "overrun":
            h.t += 31
        elif failure == "crash":
            proc.returncode = 2
        else:
            proc.returncode = -9
    app._mpv_tick()
    if failure in ("no_start", "overrun"):
        assert proc.terminated, "SIGTERM"
    assert app._mpv is None and pygame.display.get_init()
    assert app.state == "PLAYING" and FakeMediaPlayer.made[-1].path == str(VIDEO)
    _play_ffpyplayer_to_the_end(app)
    assert app.state == "WAITING_ACTION"
    # that video stays on ffpyplayer; mpv is not launched for it again
    n = len(h.cmds)
    app._on_key_down(pygame.K_SPACE)
    assert len(h.cmds) == n and app.video_player is not None


def test_dimmed_screen_uses_the_shader_on_gpu_and_ffpyplayer_on_drm(make_app):
    app, calls, (h,) = make_app("mpv")
    app._brightness = 20
    app._on_key_down(pygame.K_SPACE)
    assert any(c.startswith("--glsl-shaders=") for c in h.cmds[-1])

    app2, _, hs = make_app("mpv", mpv_vo="drm")
    app2._brightness = 20
    app2._on_key_down(pygame.K_SPACE)
    assert hs[-1].cmds == [] and app2.video_player is not None


def test_dim_factor_matches_the_overlay(make_app):
    app, _, _ = make_app()
    for level in (100, 80, 20, 0):
        app._brightness = level
        alpha = int((1 - level / 100) * 255)
        assert app._dim_factor() == pytest.approx(1 - alpha / 255)


# --- the main loop keeps turning during mpv ---------------------------------------

def test_main_loop_keeps_beating_and_reading_buttons_during_mpv(make_app, tmp_path):
    app, calls, (h,) = make_app("mpv")
    app._handoff.driver = "kmsdrm"
    app._heartbeat.interval = 0
    beats = []
    real_beat = app._heartbeat.beat
    app._heartbeat.beat = lambda state: (beats.append(state), real_beat(state))
    app._on_key_down(pygame.K_SPACE)
    _mpv_first_frame(h)

    ticks = []
    real_tick = app._mpv_tick

    def tick():
        ticks.append(1)
        if len(ticks) == 3:
            app._gpio_queue.put((pygame.KEYDOWN, pygame.K_RIGHT))   # a press mid-video
        if len(ticks) == 6:
            return False                                             # end the test
        return real_tick()

    app._mpv_tick = tick
    app.load_next_valid_session = lambda: (app.load_session(dict(OTHER)), app.__setattr__("video_path", VIDEO),
                                           app.play_current_media())   # next is a video: mpv again
    app.run()
    assert len(ticks) == 6 and beats.count("PLAYING/mpv") >= 5
    assert (["quit"], None) in h.sent and ("revisit", 2) in calls
    hb = (tmp_path / "run" / "viewer-heartbeat").read_text().split()
    assert hb[1] == "PLAYING/mpv"
