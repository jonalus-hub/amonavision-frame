"""mpv as an external video player, with hardware H.264 decoding on the Pi 4.

mpv runs as a subprocess that draws the video itself, full screen. The viewer
never waits on it: it calls poll() from its main loop, which reads mpv's JSON
IPC socket and checks the process and the time limits:

- no first frame within mpv_start_timeout (default 5 s) -> killed, FAILED;
- still running mpv_end_grace (default 10 s) past the video's duration -> killed, FAILED;
- exit code != 0, or exit before the first frame -> FAILED;
- exit code 0 after playing -> ENDED.

Killing is SIGTERM, then SIGKILL. mpv reads no keyboard or input device of its
own: the viewer drives it over IPC (stop, pause).

Whether the hardware decoder was used comes from the `hwdec-current` property
and from "Using hardware decoding" in mpv's log file (--really-quiet keeps it
off the terminal, the log file still gets it).
"""

from __future__ import annotations

import json
import logging
import shlex
import shutil
import socket
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from .base import VideoBackend, VideoEvent
from .config import VideoConfig

logger = logging.getLogger("AmonaVision.video.mpv")

# Required by the frame: full screen, no on-screen controls, no input of its own,
# quiet, driven over IPC.
FIXED_ARGS = (
    "--fs", "--no-osc", "--no-osd-bar", "--no-input-default-bindings",
    "--input-terminal=no", "--really-quiet",
)
# Also fixed: ignore any mpv.conf (reproducible runs), no keyboard from the
# window, exit at the end of the file, no OSD, no cursor.
EXTRA_FIXED_ARGS = (
    "--no-config", "--input-vo-keyboard=no", "--keep-open=no", "--idle=no",
    "--osd-level=0", "--cursor-autohide=always",
)
GPU_VOS = ("gpu", "gpu-next")
QUIT_WAIT_S = 1.5              # after "quit" over IPC, before SIGTERM
TERM_WAIT_S = 2.0              # after SIGTERM, before SIGKILL
UNKNOWN_DURATION_LIMIT_S = 30 * 60
STATS_EVERY_S = 2.0
HWDEC_LOG_MARK = "Using hardware decoding"

_REQ_DURATION, _REQ_HWDEC, _REQ_DROPS, _REQ_DEC_DROPS = 1, 2, 3, 4


class MpvUnavailable(RuntimeError):
    """The mpv binary is not installed."""


@dataclass(frozen=True)
class DrmTarget:
    """Where mpv draws on a KMS/DRM screen (None = mpv's own choice)."""
    device: str | None = None       # /dev/dri/cardN
    connector: str | None = None    # HDMI-A-1
    mode: str | None = None         # 1920x1080, or None for the connector's preferred mode


@dataclass
class MpvRun:
    """What happened in one playback (for the log and the viewer's fallback)."""
    path: str
    started: bool = False
    start_delay_s: float | None = None
    duration_s: float | None = None
    hwdec_current: str | None = None
    hw_log_mark: bool = False
    exit_code: int | None = None
    end_reason: str | None = None
    failure: str | None = None
    outcome: str | None = None          # ended | failed | stopped
    dropped_frames: int | None = None
    decoder_dropped_frames: int | None = None

    @property
    def hardware_decoding(self) -> bool | None:
        if self.hw_log_mark:
            return True
        if self.hwdec_current is None:
            return None
        return self.hwdec_current not in ("", "no")


def dim_shader_source(factor: float) -> str:
    """mpv user shader that scales the picture like the viewer's dimming overlay."""
    f = max(0.0, min(1.0, factor))
    return (
        "//!HOOK OUTPUT\n"
        "//!BIND HOOKED\n"
        "//!DESC AmonaVision dimming\n"
        "vec4 hook() {\n"
        "    vec4 c = HOOKED_texOff(0);\n"
        f"    return vec4(c.rgb * {f:.4f}, c.a);\n"
        "}\n"
    )


def build_command(cfg: VideoConfig, path: Path | str, *, socket_path: Path, log_path: Path,
                  mute: bool, drm: DrmTarget | None, shader_path: Path | None,
                  ontop: bool) -> list[str]:
    cmd = [cfg.mpv_binary, *FIXED_ARGS, f"--input-ipc-server={socket_path}", *EXTRA_FIXED_ARGS,
           f"--log-file={log_path}", f"--hwdec={cfg.mpv_hwdec}", f"--vo={cfg.mpv_vo}"]
    if drm is not None:
        if cfg.mpv_vo in GPU_VOS:
            cmd.append(f"--gpu-context={cfg.mpv_gpu_context}")
        if drm.device:
            cmd.append(f"--drm-device={drm.device}")
        if drm.connector:
            cmd.append(f"--drm-connector={drm.connector}")
        if drm.mode:
            cmd.append(f"--drm-mode={drm.mode}")
    cmd.append("--no-audio" if mute else f"--audio-device={cfg.mpv_audio_device}")
    if shader_path is not None:
        cmd.append(f"--glsl-shaders={shader_path}")
    if ontop:
        cmd.append("--ontop")
    cmd += ["--", str(path)]
    return cmd


class MpvIpc:
    """mpv's JSON IPC over its Unix socket: one JSON object per line, non-blocking."""

    def __init__(self, path: Path | str):
        self.path = str(path)
        self._sock: socket.socket | None = None
        self._buf = b""

    @property
    def connected(self) -> bool:
        return self._sock is not None

    def connect(self) -> bool:
        if self._sock is not None:
            return True
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            s.connect(self.path)
        except OSError:
            s.close()
            return False
        s.setblocking(False)
        self._sock = s
        return True

    def send(self, *command, request_id: int | None = None) -> bool:
        if self._sock is None:
            return False
        msg: dict = {"command": list(command)}
        if request_id is not None:
            msg["request_id"] = request_id
        try:
            self._sock.sendall((json.dumps(msg) + "\n").encode())
            return True
        except OSError:
            self.close()
            return False

    def read_messages(self) -> list[dict]:
        if self._sock is None:
            return []
        while True:
            try:
                chunk = self._sock.recv(65536)
            except (BlockingIOError, InterruptedError):
                break
            except OSError:
                self.close()
                break
            if not chunk:
                self.close()
                break
            self._buf += chunk
        out = []
        while b"\n" in self._buf:
            line, self._buf = self._buf.split(b"\n", 1)
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if isinstance(msg, dict):
                out.append(msg)
        return out

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None


class MpvBackend(VideoBackend):
    name = "mpv"

    def __init__(self, cfg: VideoConfig, run_dir: Path, *, popen=subprocess.Popen,
                 ipc_factory=MpvIpc, which=shutil.which, clock=time.monotonic):
        self.cfg = cfg
        self.run_dir = Path(run_dir)
        self._popen = popen
        self._ipc_factory = ipc_factory
        self._which = which
        self._clock = clock
        self._proc = None
        self._ipc: MpvIpc | None = None
        self._run: MpvRun | None = None
        self.last_run: MpvRun | None = None
        self._t_launch = 0.0
        self._t_started = 0.0
        self._last_stats = 0.0

    @property
    def socket_path(self) -> Path:
        return self.run_dir / "mpv.sock"

    @property
    def log_path(self) -> Path:
        return self.run_dir / "mpv-last.log"

    @property
    def can_dim(self) -> bool:
        """Only the gpu video outputs can run the dimming shader."""
        return self.cfg.mpv_vo in GPU_VOS

    def available(self) -> bool:
        return self._which(self.cfg.mpv_binary) is not None

    @property
    def active(self) -> bool:
        return self._proc is not None

    # ------------------------------------------------------------------
    def play(self, path: Path | str, *, mute: bool = False, drm: DrmTarget | None = None,
             dim_factor: float = 1.0, ontop: bool = False) -> None:
        if self._proc is not None:
            self.stop()
        if not self.available():
            raise MpvUnavailable(f"'{self.cfg.mpv_binary}' not found")
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.socket_path.unlink(missing_ok=True)
        self.log_path.unlink(missing_ok=True)
        shader = None
        if dim_factor < 1.0 and self.can_dim:
            shader = self.run_dir / "dim.glsl"
            shader.write_text(dim_shader_source(dim_factor))
        cmd = build_command(self.cfg, path, socket_path=self.socket_path, log_path=self.log_path,
                            mute=mute, drm=drm, shader_path=shader, ontop=ontop)
        logger.info("mpv: starting %s (hwdec=%s, vo=%s, audio=%s%s%s)", Path(path).name,
                    self.cfg.mpv_hwdec, self.cfg.mpv_vo,
                    "off (paired voice note)" if mute else self.cfg.mpv_audio_device,
                    f", {drm.connector or 'auto'} on {drm.device or 'auto'}, mode {drm.mode or 'preferred'}"
                    if drm else "", f", dimmed x{dim_factor:.2f}" if shader else "")
        logger.debug("mpv command: %s", shlex.join(cmd))
        self._run = MpvRun(path=str(path))
        self._ipc = None
        self._proc = self._popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, close_fds=True)
        self._t_launch = self._clock()
        self._last_stats = self._t_launch

    def pause(self, paused: bool | None = None) -> None:
        if self._ipc is None or not self._ipc.connected:
            return
        if paused is None:
            self._ipc.send("cycle", "pause")
        else:
            self._ipc.send("set_property", "pause", bool(paused))

    def poll(self) -> list[VideoEvent]:
        if self._proc is None:
            return []
        run = self._run
        events: list[VideoEvent] = []
        now = self._clock()

        if self._ipc is None:
            self._ipc = self._ipc_factory(self.socket_path)
        if not self._ipc.connected:
            self._ipc.connect()
        for msg in self._ipc.read_messages():
            ev = self._handle_message(msg, now)
            if ev is not None:
                events.append(ev)
        if run.started and now - self._last_stats >= STATS_EVERY_S:
            self._last_stats = now
            self._request_stats()

        rc = self._proc.poll()
        if rc is not None:
            run.exit_code = rc
            if run.started and rc == 0:
                self._finish("ended")
                events.append(VideoEvent.ENDED)
            else:
                run.failure = run.failure or (
                    f"mpv exited with code {rc}" + ("" if run.started else " before the first frame"))
                self._finish("failed")
                events.append(VideoEvent.FAILED)
            return events

        if not run.started:
            if now - self._t_launch > self.cfg.mpv_start_timeout:
                run.failure = f"no first frame within {self.cfg.mpv_start_timeout:g} s"
                self._kill()
                self._finish("failed")
                events.append(VideoEvent.FAILED)
        else:
            if run.duration_s:
                limit = run.duration_s + self.cfg.mpv_end_grace
                why = f"{run.duration_s:.1f} s video + {self.cfg.mpv_end_grace:g} s"
            else:
                limit = UNKNOWN_DURATION_LIMIT_S
                why = f"unknown duration, {UNKNOWN_DURATION_LIMIT_S} s limit"
            if now - self._t_started > limit:
                run.failure = f"still running after {now - self._t_started:.0f} s ({why})"
                self._kill()
                self._finish("failed")
                events.append(VideoEvent.FAILED)
        return events

    def stop(self) -> None:
        """Stop now: "quit" over IPC, then SIGTERM, then SIGKILL."""
        if self._proc is None:
            return
        if self._proc.poll() is None:
            sent = self._ipc is not None and self._ipc.send("quit")
            if not (sent and self._wait(QUIT_WAIT_S)):
                self._kill()
        self._run.exit_code = self._proc.returncode
        self._finish("stopped")

    # ------------------------------------------------------------------
    def _handle_message(self, msg: dict, now: float) -> VideoEvent | None:
        run = self._run
        event = msg.get("event")
        if event == "playback-restart" and not run.started:
            run.started = True
            run.start_delay_s = now - self._t_launch
            self._t_started = now
            self._ipc.send("get_property", "duration", request_id=_REQ_DURATION)
            self._ipc.send("get_property", "hwdec-current", request_id=_REQ_HWDEC)
            logger.info("mpv: first frame after %.2f s", run.start_delay_s)
            return VideoEvent.STARTED
        if event == "end-file":
            run.end_reason = msg.get("reason")
            if msg.get("file_error"):
                run.failure = f"mpv: {msg['file_error']}"
            return None
        rid = msg.get("request_id")
        if rid is None or msg.get("error") != "success":
            return None
        data = msg.get("data")
        if rid == _REQ_DURATION and isinstance(data, (int, float)) and data > 0:
            run.duration_s = float(data)
        elif rid == _REQ_HWDEC:
            run.hwdec_current = str(data) if data is not None else ""
            logger.info("mpv: hwdec-current=%s (hardware decoding: %s)",
                        run.hwdec_current or "-", "yes" if run.hardware_decoding else "no")
        elif rid == _REQ_DROPS and isinstance(data, int):
            run.dropped_frames = data
        elif rid == _REQ_DEC_DROPS and isinstance(data, int):
            run.decoder_dropped_frames = data
        return None

    def _request_stats(self) -> None:
        self._ipc.send("get_property", "frame-drop-count", request_id=_REQ_DROPS)
        self._ipc.send("get_property", "decoder-frame-drop-count", request_id=_REQ_DEC_DROPS)

    def _wait(self, timeout: float) -> bool:
        try:
            self._proc.wait(timeout=timeout)
            return True
        except subprocess.TimeoutExpired:
            return False

    def _kill(self) -> None:
        proc = self._proc
        if proc.poll() is not None:
            return
        proc.terminate()
        if self._wait(TERM_WAIT_S):
            logger.warning("mpv: stopped with SIGTERM")
            return
        proc.kill()
        self._wait(TERM_WAIT_S)
        logger.warning("mpv: did not stop on SIGTERM — sent SIGKILL")

    def _finish(self, outcome: str) -> None:
        run = self._run
        run.outcome = outcome
        if self._ipc is not None:
            self._ipc.close()
        try:
            run.hw_log_mark = HWDEC_LOG_MARK in self.log_path.read_text(errors="replace")
        except OSError:
            pass
        hw = run.hardware_decoding
        logger.info(
            "mpv: %s %s — exit code %s, %s, hardware decoding: %s (hwdec-current=%s, log %s), "
            "dropped frames: %s (decoder: %s)%s",
            outcome, Path(run.path).name, run.exit_code,
            f"{self._clock() - self._t_started:.1f} s played" if run.started else "never started",
            {True: "yes", False: "no", None: "unknown"}[hw], run.hwdec_current or "-",
            "says so" if run.hw_log_mark else "doesn't say so",
            run.dropped_frames if run.dropped_frames is not None else "?",
            run.decoder_dropped_frames if run.decoder_dropped_frames is not None else "?",
            f" — {run.failure}" if run.failure else "")
        self.socket_path.unlink(missing_ok=True)
        self.last_run = run
        self._proc = None
        self._ipc = None
        self._run = None
