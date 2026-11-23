"""FfpyplayerBackend: the same MediaPlayer calls the viewer always made."""

from app.video import FfpyplayerBackend, VideoEvent


class FakePlayer:
    def __init__(self, path, ff_opts):
        self.path, self.ff_opts = path, ff_opts
        self.frames = [(None, 0.01), (("img", 0.0), 0.04), (("img", 0.04), 0.04), (None, 'eof'), (None, 'eof')]
        self.closed = False
        self.calls = []

    def get_frame(self):
        return self.frames.pop(0) if self.frames else (None, 'eof')

    def close_player(self):
        self.closed = True

    def toggle_pause(self):
        self.calls.append("toggle")

    def set_pause(self, v):
        self.calls.append(("set", v))


def _opts(has_external_voice):
    return {"an": has_external_voice, "vd": "h264_v4l2m2m", "threads": "4", "paused": False}


def _backend():
    made = []

    def factory(path, ff_opts):
        made.append(FakePlayer(path, ff_opts))
        return made[-1]
    return FfpyplayerBackend(_opts, player_factory=factory), made


def test_opens_media_player_with_the_hal_ff_opts():
    b, made = _backend()
    b.play("/media/v.mp4", mute=True)
    assert made[0].path == "/media/v.mp4"
    assert made[0].ff_opts == _opts(True)
    b.play("/media/w.mp4", mute=False)
    assert made[1].ff_opts["an"] is False
    assert made[0].closed, "the previous player is released before the next one"


def test_get_frame_is_passed_through_and_raises_start_and_end_once():
    b, made = _backend()
    b.play("v.mp4")
    seen = [b.get_frame() for _ in range(5)]
    assert seen == [(None, 0.01), (("img", 0.0), 0.04), (("img", 0.04), 0.04), (None, 'eof'), (None, 'eof')]
    assert b.poll() == [VideoEvent.STARTED, VideoEvent.ENDED]
    assert b.poll() == []


def test_stop_and_close_player_release_it():
    b, made = _backend()
    b.play("v.mp4")
    assert b.active
    b.close_player()
    assert made[0].closed and not b.active
    b.stop()   # idempotent


def test_pause():
    b, made = _backend()
    b.pause()            # nothing loaded: no-op
    b.play("v.mp4")
    b.pause()
    b.pause(True)
    assert made[0].calls == ["toggle", ("set", True)]
