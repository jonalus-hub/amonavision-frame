"""MpvBackend without mpv: fake subprocess, fake IPC, fake clock (plus one real socket)."""

import json
import socket
import subprocess
import threading

import pytest

from app.video import DrmTarget, MpvBackend, MpvUnavailable, VideoConfig, VideoEvent
from app.video.mpv import (FIXED_ARGS, HWDEC_LOG_MARK, QUIT_WAIT_S, TERM_WAIT_S,
                           UNKNOWN_DURATION_LIMIT_S, MpvIpc, build_command, dim_shader_source)

CFG = VideoConfig(backend="mpv")


class FakeProc:
    def __init__(self, cmd, quits_on_ipc=True, dies_on_term=True):
        self.cmd = cmd
        self.returncode = None
        self.quits_on_ipc = quits_on_ipc
        self.dies_on_term = dies_on_term
        self.terminated = self.killed = False
        self.waits = []

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        self.waits.append(timeout)
        if self.returncode is None:
            raise subprocess.TimeoutExpired("mpv", timeout)
        return self.returncode

    def terminate(self):
        self.terminated = True
        if self.dies_on_term:
            self.returncode = -15

    def kill(self):
        self.killed = True
        self.returncode = -9


class FakeIpc:
    def __init__(self, path, harness):
        self.path = path
        self.h = harness
        self.connected = False
        self.inbox = []

    def connect(self):
        self.connected = self.h.socket_up
        return self.connected

    def send(self, *command, request_id=None):
        if not self.connected:
            return False
        self.h.sent.append((list(command), request_id))
        if command == ("quit",) and self.h.proc.quits_on_ipc:
            self.h.proc.returncode = 0
        return True

    def read_messages(self):
        msgs, self.inbox = self.inbox, []
        return msgs

    def close(self):
        self.connected = False


class Harness:
    """An MpvBackend wired to fakes; `t` is the clock."""

    def __init__(self, tmp_path, cfg=CFG, installed=True, **proc_kw):
        self.t = 0.0
        self.sent = []
        self.socket_up = True
        self.proc = None
        self.ipc = None
        self.cmds = []
        self._proc_kw = proc_kw

        def popen(cmd, **kw):
            assert kw["stdin"] is subprocess.DEVNULL
            self.cmds.append(cmd)
            self.proc = FakeProc(cmd, **self._proc_kw)
            return self.proc

        def ipc_factory(path):
            self.ipc = FakeIpc(path, self)
            return self.ipc

        self.backend = MpvBackend(cfg, tmp_path, popen=popen, ipc_factory=ipc_factory,
                                  which=lambda b: f"/usr/bin/{b}" if installed else None,
                                  clock=lambda: self.t)

    def push(self, *msgs):
        self.ipc.inbox.extend(msgs)

    def start(self, **play_kw):
        self.backend.play("/media/clip.mp4", **play_kw)
        assert self.backend.poll() == []          # connects, nothing yet
        self.t += 0.8
        self.push({"event": "playback-restart"})
        assert self.backend.poll() == [VideoEvent.STARTED]
        self.push({"request_id": 1, "error": "success", "data": 20.0},
                  {"request_id": 2, "error": "success", "data": "v4l2m2m-copy"})
        self.backend.poll()


# --- command line -------------------------------------------------------------

def _cmd(cfg=CFG, **kw):
    args = dict(socket_path="/run/amonavision/mpv.sock", log_path="/run/amonavision/mpv-last.log",
                mute=False, drm=None, shader_path=None, ontop=False)
    args.update(kw)
    return build_command(cfg, "/media/a b.mp4", **args)


def test_command_has_the_required_fixed_options_and_the_configured_ones():
    cmd = _cmd(drm=DrmTarget("/dev/dri/card1", "HDMI-A-2", "1920x1280"))
    assert cmd[0] == "mpv"
    for opt in FIXED_ARGS + ("--input-ipc-server=/run/amonavision/mpv.sock",
                             "--no-config", "--input-vo-keyboard=no", "--keep-open=no", "--idle=no"):
        assert opt in cmd
    for opt in ("--fs", "--no-osc", "--no-osd-bar", "--no-input-default-bindings",
                "--input-terminal=no", "--really-quiet"):
        assert opt in cmd
    assert "--hwdec=v4l2m2m-copy" in cmd and "--vo=gpu" in cmd and "--gpu-context=drm" in cmd
    assert "--audio-device=pulse" in cmd
    assert "--drm-device=/dev/dri/card1" in cmd and "--drm-connector=HDMI-A-2" in cmd
    assert "--drm-mode=1920x1280" in cmd
    assert "--log-file=/run/amonavision/mpv-last.log" in cmd
    assert cmd[-2:] == ["--", "/media/a b.mp4"], "the path is one argument, after --"


def test_paired_voice_note_mutes_mpv():
    cmd = _cmd(mute=True)
    assert "--no-audio" in cmd and not any(c.startswith("--audio-device") for c in cmd)


def test_no_drm_options_on_x11_or_wayland():
    cmd = _cmd(drm=None, ontop=True)
    assert not any(c.startswith(("--drm-", "--gpu-context")) for c in cmd)
    assert "--ontop" in cmd


def test_vo_drm_gets_no_gpu_context():
    cmd = _cmd(VideoConfig(backend="mpv", mpv_vo="drm"), drm=DrmTarget("/dev/dri/card1", "HDMI-A-1"))
    assert "--vo=drm" in cmd and not any(c.startswith("--gpu-context") for c in cmd)
    assert not any(c.startswith("--drm-mode") for c in cmd), "mode None = mpv's preferred mode"


def test_dim_shader_scales_rgb_only():
    src = dim_shader_source(0.2)
    assert "//!HOOK OUTPUT" in src and "c.rgb * 0.2000" in src and "c.a" in src


# --- lifecycle ----------------------------------------------------------------

def test_missing_binary_raises_and_launches_nothing(tmp_path):
    h = Harness(tmp_path, installed=False)
    assert not h.backend.available()
    with pytest.raises(MpvUnavailable):
        h.backend.play("/media/clip.mp4")
    assert h.cmds == [] and not h.backend.active


def test_normal_playback_starts_reports_hwdec_and_ends(tmp_path):
    h = Harness(tmp_path)
    h.start()
    sent = [c for c, _ in h.sent]
    assert ["get_property", "duration"] in sent and ["get_property", "hwdec-current"] in sent
    h.t += 20.5
    h.proc.returncode = 0
    assert h.backend.poll() == [VideoEvent.ENDED]
    run = h.backend.last_run
    assert run.outcome == "ended" and run.exit_code == 0
    assert run.duration_s == 20.0 and run.hwdec_current == "v4l2m2m-copy"
    assert run.hardware_decoding is True
    assert not h.backend.active and not h.proc.terminated


def test_hardware_decoding_seen_in_the_log_file(tmp_path):
    h = Harness(tmp_path)
    h.backend.play("/media/clip.mp4")
    h.backend.poll()
    (tmp_path / "mpv-last.log").write_text(f"[vd] {HWDEC_LOG_MARK} (v4l2m2m-copy).\n")
    h.push({"event": "playback-restart"})
    h.backend.poll()
    h.proc.returncode = 0
    h.backend.poll()
    assert h.backend.last_run.hw_log_mark and h.backend.last_run.hardware_decoding


def test_software_decoding_is_reported_as_such(tmp_path):
    h = Harness(tmp_path)
    h.backend.play("/media/clip.mp4")
    h.backend.poll()
    h.push({"event": "playback-restart"}, {"request_id": 2, "error": "success", "data": "no"})
    h.backend.poll()
    h.proc.returncode = 0
    h.backend.poll()
    assert h.backend.last_run.hardware_decoding is False


def test_no_first_frame_within_the_start_timeout_kills_it(tmp_path):
    h = Harness(tmp_path)
    h.backend.play("/media/clip.mp4")
    h.t = 4.9
    assert h.backend.poll() == []
    h.t = 5.1
    assert h.backend.poll() == [VideoEvent.FAILED]
    assert h.proc.terminated and not h.proc.killed
    assert "5 s" in h.backend.last_run.failure and h.backend.last_run.outcome == "failed"


def test_sigkill_when_sigterm_is_ignored(tmp_path):
    h = Harness(tmp_path, dies_on_term=False)
    h.backend.play("/media/clip.mp4")
    h.t = 6
    assert h.backend.poll() == [VideoEvent.FAILED]
    assert h.proc.terminated and h.proc.killed
    assert TERM_WAIT_S in h.proc.waits


def test_no_ipc_socket_counts_as_not_starting(tmp_path):
    h = Harness(tmp_path)
    h.socket_up = False
    h.backend.play("/media/clip.mp4")
    for h.t in (1, 2, 3, 4, 5.5):
        events = h.backend.poll()
    assert events == [VideoEvent.FAILED] and h.proc.terminated


def test_still_running_after_duration_plus_grace_is_killed(tmp_path):
    h = Harness(tmp_path)
    h.start()                       # started at t=0.8, duration 20 s
    h.t = 0.8 + 20 + 10 - 0.1
    assert h.backend.poll() == []
    h.t = 0.8 + 20 + 10 + 0.1
    assert h.backend.poll() == [VideoEvent.FAILED]
    assert h.proc.terminated and "20.0 s video + 10 s" in h.backend.last_run.failure


def test_unknown_duration_uses_the_long_limit(tmp_path):
    h = Harness(tmp_path)
    h.backend.play("/media/clip.mp4")
    h.backend.poll()
    h.push({"event": "playback-restart"})
    h.backend.poll()
    h.t = UNKNOWN_DURATION_LIMIT_S - 1
    assert h.backend.poll() == []
    h.t = UNKNOWN_DURATION_LIMIT_S + 1
    assert h.backend.poll() == [VideoEvent.FAILED]


def test_crash_after_start_is_a_failure(tmp_path):
    h = Harness(tmp_path)
    h.start()
    h.push({"event": "end-file", "reason": "error", "file_error": "decoding failed"})
    h.proc.returncode = 2
    assert h.backend.poll() == [VideoEvent.FAILED]
    assert "decoding failed" in h.backend.last_run.failure


def test_exit_zero_before_the_first_frame_is_a_failure(tmp_path):
    h = Harness(tmp_path)
    h.backend.play("/media/clip.mp4")
    h.proc.returncode = 0
    assert h.backend.poll() == [VideoEvent.FAILED]
    assert "before the first frame" in h.backend.last_run.failure


def test_killed_by_hand_is_a_failure(tmp_path):
    h = Harness(tmp_path)
    h.start()
    h.proc.returncode = -9        # kill -9 from a shell
    assert h.backend.poll() == [VideoEvent.FAILED]


def test_stop_quits_over_ipc(tmp_path):
    h = Harness(tmp_path)
    h.start()
    h.backend.stop()
    assert (["quit"], None) in h.sent
    assert not h.proc.terminated and QUIT_WAIT_S in h.proc.waits
    assert h.backend.last_run.outcome == "stopped" and not h.backend.active
    assert h.backend.poll() == []


def test_stop_escalates_when_quit_is_ignored(tmp_path):
    h = Harness(tmp_path, quits_on_ipc=False)
    h.start()
    h.backend.stop()
    assert h.proc.terminated


def test_stop_before_the_socket_exists_terminates(tmp_path):
    h = Harness(tmp_path)
    h.socket_up = False
    h.backend.play("/media/clip.mp4")
    h.backend.stop()
    assert h.proc.terminated and h.backend.last_run.outcome == "stopped"


def test_pause_commands(tmp_path):
    h = Harness(tmp_path)
    h.start()
    h.sent.clear()
    h.backend.pause()
    h.backend.pause(True)
    h.backend.pause(False)
    assert [c for c, _ in h.sent] == [["cycle", "pause"], ["set_property", "pause", True],
                                       ["set_property", "pause", False]]


def test_frame_drop_counters_are_polled(tmp_path):
    h = Harness(tmp_path)
    h.start()
    h.sent.clear()
    h.t += 2.1
    h.backend.poll()
    assert (["get_property", "frame-drop-count"], 3) in h.sent
    h.push({"request_id": 3, "error": "success", "data": 4},
           {"request_id": 4, "error": "success", "data": 1})
    h.backend.poll()
    h.proc.returncode = 0
    h.backend.poll()
    assert (h.backend.last_run.dropped_frames, h.backend.last_run.decoder_dropped_frames) == (4, 1)


def test_dim_shader_only_for_gpu_vo(tmp_path):
    h = Harness(tmp_path)
    h.backend.play("/media/clip.mp4", dim_factor=0.2)
    assert f"--glsl-shaders={tmp_path / 'dim.glsl'}" in h.cmds[-1]
    assert "0.2000" in (tmp_path / "dim.glsl").read_text()
    h.backend.stop()

    d = Harness(tmp_path / "drm", cfg=VideoConfig(backend="mpv", mpv_vo="drm"))
    assert not d.backend.can_dim
    d.backend.play("/media/clip.mp4", dim_factor=0.2)
    assert not any(c.startswith("--glsl-shaders") for c in d.cmds[-1])


def test_playing_again_stops_the_previous_mpv(tmp_path):
    h = Harness(tmp_path)
    h.start()
    first = h.proc
    h.backend.play("/media/other.mp4")
    assert first.returncode is not None and h.proc is not first


# --- the real IPC client against a real socket --------------------------------

def test_ipc_client_talks_json_lines_over_a_unix_socket(tmp_path):
    path = tmp_path / "s"
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(str(path))
    srv.listen(1)
    got = []

    def serve():
        conn, _ = srv.accept()
        f = conn.makefile("rb")
        got.append(json.loads(f.readline()))
        # one message split across two writes, then two in one write
        conn.sendall(b'{"event": "playback-res')
        conn.sendall(b'tart"}\n{"request_id": 2, "error": "success", "data": "no"}\nnot json\n')
        conn.close()

    t = threading.Thread(target=serve)
    t.start()
    ipc = MpvIpc(path)
    assert ipc.connect()
    assert ipc.send("get_property", "hwdec-current", request_id=2)
    t.join(timeout=5)
    msgs = []
    for _ in range(50):
        msgs += ipc.read_messages()
        if not ipc.connected:
            break
    assert got == [{"command": ["get_property", "hwdec-current"], "request_id": 2}]
    assert msgs == [{"event": "playback-restart"}, {"request_id": 2, "error": "success", "data": "no"}]
    assert not ipc.connected, "EOF from mpv closes the client"
    assert not ipc.send("quit")
    srv.close()


def test_ipc_connect_fails_quietly_without_a_socket(tmp_path):
    ipc = MpvIpc(tmp_path / "nothing-here")
    assert not ipc.connect() and not ipc.connected and ipc.read_messages() == []


def test_a_too_long_run_dir_gets_a_short_socket_path(tmp_path):
    deep = tmp_path / ("x" * 120)
    b = MpvBackend(CFG, deep)
    assert len(str(b.socket_path).encode()) <= 100
    from pathlib import Path
    assert MpvBackend(CFG, Path("/run/amonavision")).socket_path == Path("/run/amonavision/mpv.sock")
