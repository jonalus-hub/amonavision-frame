#!/usr/bin/env python3
"""
AmonaVision simulator
=====================
Runs the real frame code (viewer.py + the bot's handlers) on a machine with no
frame hardware, with a web page standing in for the hardware:

  screen   -> SDL dummy driver; every pygame.display.flip() is streamed as MJPEG
  buttons  -> the browser buttons feed the same queue the GPIO callbacks use
  speaker  -> whatever the viewer plays (voice notes, video audio) plays in the browser
  mic      -> the browser records while ● (record) is held; the PCM is handed to
              the viewer through a stand-in sounddevice.InputStream
  Telegram -> a fake family group in the page; messages go through the real bot
              handlers and MessageGrouper via stand-in Update/Bot objects

The frame code runs unmodified, from this checkout (the folder above this
one) or from AMONA_SRC. The real Telegram bot is never contacted: the token is
forced empty below. The web UI has no login, so it only listens on localhost
unless SIM_BIND says otherwise.
"""
import asyncio
import json
import logging
import logging.handlers
import os
import queue
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
import types
import urllib.request
import zlib
from pathlib import Path
from types import SimpleNamespace

SIM_DIR = Path(__file__).resolve().parent
SRC = Path(os.environ.get("AMONA_SRC", SIM_DIR.parent)).resolve()       # the frame code to run
DATA = Path(os.environ.get("SIM_DATA", SIM_DIR / "data")).resolve()     # fake chat + its files
CHAT_MEDIA = DATA / "chat_media"
CERT_DIR = Path(os.environ.get("SIM_CERTS", SIM_DIR / "certs")).resolve()
SAMPLES_DIR = SIM_DIR / "sample-media"     # what a fresh simulator starts with, see its README
BIND = os.environ.get("SIM_BIND", "127.0.0.1")
HTTP_PORT = int(os.environ.get("SIM_HTTP_PORT", "8099"))
HTTPS_PORT = int(os.environ.get("SIM_HTTPS_PORT", "8443"))
DASHBOARD_PORT = int(os.environ.get("SIM_DASHBOARD_PORT", "8080"))   # dashboard.py, its own process
MAX_UPLOAD = 25 * 1024 * 1024           # Telegram bots get files up to 20 MB

SCREEN_W, SCREEN_H = 1024, 600          # the real panel's resolution
GROUP_CHAT_ID = -1000000000001          # the fake family group
BOT_NAME = "AmonaVision"
# Bot commands that would act on the host machine (or need the frame's
# hardware) instead of the simulated frame: /test runs test_setup.sh, /reboot
# and /screen drop flags that root units on a frame act on, /volume runs wpctl.
BLOCKED_COMMANDS = {"test", "reboot", "screen", "volume"}
# Demuxers ffmpeg may use on uploads: real media only, no playlists or other
# formats that open further files or URLs.
FFMPEG_FORMATS = "mov,mp4,m4a,3gp,3g2,mj2,matroska,webm,ogg,wav,mp3,aac,flac"

# Not on a real frame: the simulator would share its /tmp flags (a /restart here
# would restart the real frame) and, from the same checkout, its photos and database.
if Path("/etc/systemd/system/amonavision.service").exists() and os.environ.get("SIM_ALLOW_ON_FRAME") != "1":
    sys.exit("This machine runs a real AmonaVision frame (amonavision.service is installed). "
             "Run the simulator on another machine (SIM_ALLOW_ON_FRAME=1 overrides this check).")

# Never let the simulator reach the real bot: these win over any .env in the checkout.
os.environ["TELEGRAM_BOT_TOKEN"] = ""
os.environ["TELEGRAM_GROUP_CHAT_ID"] = str(GROUP_CHAT_ID)
os.environ["TELEGRAM_ADMIN_CHAT_ID"] = str(GROUP_CHAT_ID)   # admin commands work in the group
os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")            # paced in real time, so playback ends on time

DATA.mkdir(parents=True, exist_ok=True)
CHAT_MEDIA.mkdir(parents=True, exist_ok=True)
os.chdir(SRC)                     # viewer.py uses some paths relative to the project root
sys.path.insert(0, str(SRC))

import numpy as np  # noqa: E402


# ---------------------------------------------------------------------------
# Microphone: stand-in for sounddevice, fed with audio recorded in the browser
# ---------------------------------------------------------------------------
class SimInputStream:
    active = None

    def __init__(self, samplerate, channels, dtype="int16", callback=None, **_):
        self.samplerate, self.channels, self.callback = int(samplerate), int(channels), callback

    def start(self):
        SimInputStream.active = self

    def stop(self):
        if SimInputStream.active is self:
            SimInputStream.active = None

    def close(self):
        self.stop()

    def feed(self, pcm):
        for i in range(0, len(pcm), 1024):
            chunk = pcm[i:i + 1024]
            self.callback(chunk, len(chunk), None, None)


_sd = types.ModuleType("sounddevice")
_sd.InputStream = SimInputStream
sys.modules["sounddevice"] = _sd


# ---------------------------------------------------------------------------
# Web-facing state
# ---------------------------------------------------------------------------
class Events:
    """Server-sent events fan-out."""

    def __init__(self):
        self._subs, self._lock = set(), threading.Lock()

    def subscribe(self):
        q = queue.Queue(maxsize=500)
        with self._lock:
            self._subs.add(q)
        return q

    def unsubscribe(self, q):
        with self._lock:
            self._subs.discard(q)

    def publish(self, kind, **data):
        msg = json.dumps({"type": kind, **data})
        with self._lock:
            subs = list(self._subs)
        for q in subs:
            try:
                q.put_nowait(msg)
            except queue.Full:
                pass


class Screen:
    """Latest frame as JPEG. Encoding runs off the viewer's thread."""

    def __init__(self):
        self.cond = threading.Condition()
        self.jpeg, self.seq = None, 0
        self._raw, self._raw_ready = None, threading.Event()
        threading.Thread(target=self._encoder, name="jpeg-encoder", daemon=True).start()

    def capture(self, surf):
        self._raw = (pygame.image.tobytes(surf, "RGB"), surf.get_width(), surf.get_height())
        self._raw_ready.set()

    def _encoder(self):
        import cv2
        while True:
            self._raw_ready.wait()
            self._raw_ready.clear()
            raw, w, h = self._raw
            bgr = np.frombuffer(raw, np.uint8).reshape(h, w, 3)[:, :, ::-1]
            ok, buf = cv2.imencode(".jpg", bgr, [cv2.IMWRITE_JPEG_QUALITY, 82])
            if ok:
                with self.cond:
                    self.jpeg, self.seq = buf.tobytes(), self.seq + 1
                    self.cond.notify_all()
            time.sleep(1 / 30)


class FrameAudio:
    """What the frame's speaker would be playing, mirrored to the browser."""

    def __init__(self):
        self.files, self.playing = {}, False

    def play(self, path):
        token = secrets.token_urlsafe(8)
        self.files = dict(list(self.files.items())[-3:])
        self.files[token] = Path(path)
        self.playing = True
        events.publish("audio", action="play", url=f"/frame-audio/{token}")

    def stop(self):
        if self.playing:
            self.playing = False
            events.publish("audio", action="stop")


class Chat:
    """The fake family Telegram group. Message ids double as Telegram message_ids."""

    def __init__(self, path):
        self.path, self.lock = path, threading.Lock()
        data = json.loads(path.read_text()) if path.exists() else {}
        self.next_id = data.get("next_id", 1001)
        self.messages = data.get("messages", [])

    def add(self, sender, text=None, kind="text", file=None, reply_to=None, from_frame=False, **extra):
        with self.lock:
            msg = {
                "id": self.next_id, "sender": sender, "kind": kind, "text": text,
                "file": file, "reply_to": reply_to, "time": time.strftime("%H:%M"),
                "bot": sender == BOT_NAME, "from_frame": from_frame, **extra,
            }
            self.next_id += 1
            self.messages = (self.messages + [msg])[-500:]
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"next_id": self.next_id, "messages": self.messages}))
            tmp.replace(self.path)
        events.publish("chat", msg=msg)
        return msg


events = Events()
screen = Screen()
frame_audio = FrameAudio()
chat = Chat(DATA / "chat.json")
log = logging.getLogger("sim")


# ---------------------------------------------------------------------------
# Hardware patches applied before viewer.py is imported
# ---------------------------------------------------------------------------
import pygame  # noqa: E402

_orig_flip = pygame.display.flip


def _sim_flip(*a, **k):
    _orig_flip(*a, **k)
    surf = pygame.display.get_surface()
    if surf is not None:
        screen.capture(surf)


pygame.display.flip = _sim_flip

_music = pygame.mixer.music
_orig_music = {n: getattr(_music, n) for n in ("load", "play", "stop")}
_music_loaded = {"path": None}


def _sim_music_load(path, *a, **k):
    _orig_music["load"](path, *a, **k)
    _music_loaded["path"] = path


def _sim_music_play(*a, **k):
    _orig_music["play"](*a, **k)
    if _music_loaded["path"]:
        frame_audio.play(_music_loaded["path"])


def _sim_music_stop(*a, **k):
    _orig_music["stop"](*a, **k)
    frame_audio.stop()


_music.load, _music.play, _music.stop = _sim_music_load, _sim_music_play, _sim_music_stop

import viewer  # noqa: E402  (the real frame code)

viewer.SETTINGS.update(fullscreen=False, window_width=SCREEN_W, window_height=SCREEN_H)

try:
    from ffpyplayer.player import MediaPlayer as _RealMediaPlayer

    def _sim_media_player(filename, *a, ff_opts=None, **k):
        # Video audio is played by the browser instead; no Pi hardware decoder here.
        opts = dict(ff_opts or {})
        opts.pop("vd", None)
        opts["an"] = True
        return _RealMediaPlayer(filename, *a, ff_opts=opts, **k)

    viewer.MediaPlayer = _sim_media_player
except ImportError:
    pass

_orig_play_current = viewer.ViewerApp.play_current_media


def _sim_play_current_media(self):
    _orig_play_current(self)
    # A video with its own soundtrack (no separate voice note): play that track.
    if self.state == "PLAYING" and self.video_path and not self.audio_path and self.video_player is not None:
        frame_audio.play(self.video_path)


def _sim_send_voice_reply(self, voice_path):
    """The frame's voice reply lands in the fake group instead of Telegram."""
    src = Path(voice_path)
    if not src.exists():
        return False
    name = f"{int(time.time())}_{src.name}"
    shutil.copy2(src, CHAT_MEDIA / name)
    chat.add(BOT_NAME, kind="voice", file=name, reply_to=self.current_telegram_msg_id, from_frame=True)
    return True


viewer.ViewerApp.play_current_media = _sim_play_current_media
viewer.ViewerApp.send_voice_reply = _sim_send_voice_reply


# ---------------------------------------------------------------------------
# Fake Telegram: real bot handlers, stand-in Update / Bot objects
# ---------------------------------------------------------------------------
import app.bot.bot as botmod  # noqa: E402

_bot_log = logging.handlers.RotatingFileHandler(botmod.LOG_FILE, maxBytes=1_000_000, backupCount=2, encoding="utf-8")
_bot_log.setFormatter(logging.Formatter(botmod.LOG_FORMAT))
logging.getLogger("AmonaVision").addHandler(_bot_log)   # so /logs bot has something to show
logging.getLogger("werkzeug").setLevel(logging.WARNING)


class _FakeTgFile:
    def __init__(self, file_id, src):
        self.file_id, self._src = file_id, src

    async def download_to_drive(self, custom_path=None):
        shutil.copy2(self._src, custom_path)
        return Path(custom_path)


class FakeBot:
    def __init__(self):
        self.files = {}

    async def get_file(self, file_id):
        return _FakeTgFile(file_id, self.files[file_id])

    async def send_message(self, chat_id=None, text="", parse_mode=None, reply_to_message_id=None, **_):
        m = chat.add(BOT_NAME, text=text, reply_to=reply_to_message_id)
        return SimpleNamespace(message_id=m["id"], chat_id=chat_id)


class FakeMessage:
    def __init__(self, mid, text=None, reply_to=None, photo=None, video=None, voice=None, document=None):
        self.message_id, self.text, self.caption = mid, text, None
        self.reply_to_message = SimpleNamespace(message_id=reply_to) if reply_to else None
        self.photo = [photo] if photo else []
        self.video, self.voice, self.video_note, self.document = video, voice, None, document

    async def reply_text(self, text, parse_mode=None, **_):
        m = chat.add(BOT_NAME, text=text, reply_to=self.message_id)
        return SimpleNamespace(message_id=m["id"])


fake_bot = FakeBot()
botmod.db = botmod.DatabaseManager(botmod.DB_PATH)
botmod.grouper = botmod.MessageGrouper(
    received_dir=botmod.MEDIA_RECEIVED, processed_dir=botmod.MEDIA_PROCESSED,
    on_flush=botmod._on_group_flushed, db=botmod.db,
)
botmod._bot_instance = fake_bot
COMMANDS = {name[4:]: fn for name, fn in vars(botmod).items() if name.startswith("cmd_") and callable(fn)}

bot_loop = asyncio.new_event_loop()
threading.Thread(target=bot_loop.run_forever, name="fake-telegram", daemon=True).start()
FFMPEG = viewer.imageio_ffmpeg.get_ffmpeg_exe()


def _run_handler(handler, msg, sender, args=None):
    uid = zlib.crc32(sender.encode()) % 900_000_000 + 100_000_000
    update = SimpleNamespace(
        message=msg, effective_message=msg,
        effective_user=SimpleNamespace(id=uid, first_name=sender, full_name=sender, username=None),
        effective_chat=SimpleNamespace(id=GROUP_CHAT_ID, type="group"),
    )
    context = SimpleNamespace(bot=fake_bot, args=args or [], job_queue=None, error=None)
    asyncio.run_coroutine_threadsafe(handler(update, context), bot_loop).result(timeout=120)


def _ffmpeg_input(path):
    """ffmpeg input options for an uploaded file: only local files and real media
    demuxers, so an upload can't be a playlist that makes ffmpeg open other files or URLs."""
    return ["-protocol_whitelist", "file", "-format_whitelist", FFMPEG_FORMATS, "-i", str(path)]


def _convert(data, out_args):
    """Run ffmpeg on uploaded bytes. They go through a temporary file rather than a
    pipe: MP4s from phones keep their index at the end, which a pipe can't seek to."""
    tmp = DATA / f".upload-{secrets.token_hex(6)}"
    try:
        tmp.write_bytes(data)
        return subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y", *_ffmpeg_input(tmp), *out_args],
                              capture_output=True, check=True, timeout=300).stdout
    finally:
        tmp.unlink(missing_ok=True)


def receive_text(sender, text, reply_to=None):
    msg = chat.add(sender, text=text, reply_to=reply_to)
    fm = FakeMessage(msg["id"], text=text, reply_to=reply_to)
    if text.startswith("/"):
        cmd, *args = text[1:].split()
        cmd = cmd.split("@")[0].lower()
        if cmd in BLOCKED_COMMANDS:
            chat.add(BOT_NAME, text=f"🔒 /{cmd} is not available in the simulator "
                                    "(it would act on this machine, not on a frame).", reply_to=msg["id"])
        elif cmd in COMMANDS:
            _run_handler(COMMANDS[cmd], fm, sender, args)
        # unknown commands: the real bot has no handler for them either
    elif reply_to and re.fullmatch(r"\s*(delete|recycle)\s*", text, re.IGNORECASE):
        _run_handler(botmod.handle_session_command, fm, sender)
    else:
        _run_handler(botmod.handle_unknown, fm, sender)


def receive_file(sender, data, mime, as_voice=False, reply_to=None):
    """Turn an upload into what a Telegram client would send, then hand it to the bot."""
    mime = (mime or "").lower()
    if not data:
        raise ValueError("The file is empty")
    file_id = "sim" + secrets.token_hex(8)
    if as_voice or mime.startswith("audio/"):
        stored = CHAT_MEDIA / f"{file_id}.ogg"     # Telegram voice notes are OGG/Opus
        _convert(data, ["-vn", "-ac", "1", "-c:a", "libopus", "-b:a", "32k", str(stored)])
        kind, (duration, _, _), dims = "voice", _probe(stored), {}
        media = {"voice": SimpleNamespace(file_id=file_id, file_size=stored.stat().st_size,
                                          duration=duration, mime_type="audio/ogg")}
        handler = botmod.handle_voice
    elif mime.startswith("image/"):
        import cv2
        img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            raise ValueError("Could not read the image")
        stored = CHAT_MEDIA / f"{file_id}.jpg"     # Telegram recompresses photos to JPEG
        cv2.imwrite(str(stored), img, [cv2.IMWRITE_JPEG_QUALITY, 90])
        kind, dims = "photo", {"w": img.shape[1], "h": img.shape[0]}
        media = {"photo": SimpleNamespace(file_id=file_id, file_size=stored.stat().st_size,
                                          width=img.shape[1], height=img.shape[0])}
        handler = botmod.handle_photo
    elif mime.startswith("video/"):
        stored = CHAT_MEDIA / f"{file_id}.mp4"     # Telegram delivers videos as MP4
        # Always rewritten by ffmpeg, so what reaches the frame code is a real MP4
        # whatever the upload claimed to be: rewrapped as is when the streams fit, else re-encoded.
        try:
            _convert(data, ["-map", "0:v:0", "-map", "0:a:0?", "-c", "copy", "-movflags", "+faststart",
                            "-f", "mp4", str(stored)])
        except subprocess.CalledProcessError:
            _convert(data, ["-map", "0:v:0", "-map", "0:a:0?", "-c:v", "libx264", "-preset", "veryfast",
                            "-pix_fmt", "yuv420p", "-c:a", "aac", "-movflags", "+faststart", "-f", "mp4", str(stored)])
        duration, w, h = _probe(stored)
        kind, dims = "video", ({"w": w, "h": h} if w else {})
        media = {"video": SimpleNamespace(file_id=file_id, file_size=stored.stat().st_size,
                                          duration=duration, mime_type="video/mp4")}
        handler = botmod.handle_video
    else:
        raise ValueError(f"Unsupported file type: {mime or 'unknown'}")
    fake_bot.files[file_id] = stored
    msg = chat.add(sender, kind=kind, file=stored.name, reply_to=reply_to, **dims)
    _run_handler(handler, FakeMessage(msg["id"], reply_to=reply_to, **media), sender)
    return msg["id"]


def _probe(path):
    """(duration s, width, height) from ffmpeg's banner; 0 where unknown."""
    out = subprocess.run([FFMPEG, "-hide_banner", *_ffmpeg_input(path)],
                         capture_output=True, text=True, timeout=60).stderr
    d = re.search(r"Duration: (\d+):(\d+):(\d+)", out)
    v = re.search(r"Video: .*?, (\d{2,5})x(\d{2,5})", out)
    duration = int(d[1]) * 3600 + int(d[2]) * 60 + int(d[3]) if d else 0
    return duration, (int(v[1]) if v else 0), (int(v[2]) if v else 0)


SAMPLE_MIME = {"photo": "image/jpeg", "video": "video/mp4"}


def _sample_bytes(sample):
    """A sample's file. Items whose license doesn't allow passing the file on
    (the Mixkit video) aren't in the repo: they're fetched from their source once."""
    path = SAMPLES_DIR / sample["file"]
    if not path.exists() and sample.get("download_url"):
        cached = DATA / "sample-cache" / sample["file"]
        if not cached.exists():
            cached.parent.mkdir(parents=True, exist_ok=True)
            req = urllib.request.Request(sample["download_url"], headers={"User-Agent": "amonavision-simulator"})
            with urllib.request.urlopen(req, timeout=60) as r:
                body = r.read(MAX_UPLOAD + 1)
            if len(body) > MAX_UPLOAD:
                raise ValueError("sample download too large")
            cached.write_bytes(body)
        path = cached
    return path.read_bytes()


def seed_if_empty():
    """First run only: the sample media 'sent' by the family so the frame isn't empty."""
    if chat.messages or botmod.db.get_all_sessions_for_scoring():
        return
    for sample in json.loads((SAMPLES_DIR / "media.json").read_text()):
        try:
            mid = receive_file(sample["sender"], _sample_bytes(sample), SAMPLE_MIME[sample.get("kind", "photo")])
            if note := sample.get("voice_note"):
                # As in Telegram: the voice note is a reply to the photo, which pairs them.
                time.sleep(1.5)
                receive_file(sample["sender"], (SAMPLES_DIR / note["file"]).read_bytes(), "audio/ogg",
                             as_voice=True, reply_to=mid)
            time.sleep(1.5)
        except Exception:
            log.exception("Could not seed sample %s", sample["file"])


# ---------------------------------------------------------------------------
# Frame buttons
# ---------------------------------------------------------------------------
KEYS = {"left": pygame.K_LEFT, "right": pygame.K_RIGHT, "play": pygame.K_SPACE, "rec": pygame.K_r}
frame = SimpleNamespace(app=None)


def press(name, down):
    app = frame.app
    if app is None or name not in KEYS:
        return False
    event = (pygame.KEYDOWN if down else pygame.KEYUP, KEYS[name])
    if hasattr(app, "_gpio_queue"):           # exactly what the GPIO callbacks do
        app._gpio_queue.put(event)
        app._wake_main_loop()
    else:
        pygame.event.post(pygame.event.Event(event[0], key=event[1]))
    return True


def release_record(audio, mime):
    """Release of the record button: feed the browser's recording to the viewer, then KEYUP."""
    stream = SimInputStream.active
    if audio and stream is not None:
        try:
            raw = _convert(audio, ["-vn", "-f", "s16le", "-ac", str(stream.channels),
                                   "-ar", str(stream.samplerate), "pipe:1"])
            stream.feed(np.frombuffer(raw, np.int16).reshape(-1, stream.channels))
        except Exception:
            log.exception("Could not decode the browser recording (%s)", mime)
    press("rec", False)


def _bot_heartbeat():
    # The fake Telegram never drops: keep the poll heartbeat fresh, as the real
    # bot does on every getUpdates, so the dashboard shows the bot as connected.
    beat = getattr(botmod, "HEARTBEAT_FILE", Path("/tmp/amonavision_bot_heartbeat"))
    while True:
        try:
            beat.touch()
        except OSError:
            pass
        time.sleep(20)


def _audio_watchdog():
    # Leaving PLAYING (end of clip, Play pressed again, arrows) silences the browser.
    while True:
        time.sleep(0.2)
        if frame.app is not None and frame.app.state != "PLAYING":
            frame_audio.stop()


# ---------------------------------------------------------------------------
# Web server
# ---------------------------------------------------------------------------
from flask import Flask, Response, abort, jsonify, request, send_file, send_from_directory  # noqa: E402

web = Flask(__name__, static_folder=str(SIM_DIR / "static"), static_url_path="/static")
web.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD
CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


@web.before_request
def _same_page_only():
    # The page sends this header on every POST; a plain form on another site
    # can't, so a page someone visits can't drive the frame or post to the chat.
    if request.method == "POST" and request.headers.get("X-Requested-With") != "amonavision-sim":
        abort(403)


@web.after_request
def _headers(resp):
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("Referrer-Policy", "no-referrer")
    return resp


def _src_info():
    def git(*args):
        r = subprocess.run(["git", "-C", str(SRC), *args], capture_output=True, text=True)
        return r.stdout.strip()
    return {"branch": git("rev-parse", "--abbrev-ref", "HEAD"), "commit": git("log", "-1", "--format=%h"),
            "subject": git("log", "-1", "--format=%s"), "date": git("log", "-1", "--format=%cd", "--date=format:%Y-%m-%d %H:%M"),
            "https_port": HTTPS_PORT, "http_port": HTTP_PORT, "dashboard_port": DASHBOARD_PORT}


SRC_INFO = _src_info()


@web.get("/")
def index():
    return send_from_directory(SIM_DIR / "static", "index.html")


@web.get("/stream.mjpg")
def stream():
    def frames():
        last = -1
        while True:
            with screen.cond:
                screen.cond.wait_for(lambda: screen.seq != last, timeout=5)
                jpeg, last = screen.jpeg, screen.seq
            if jpeg:
                yield (b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: %d\r\n\r\n" % len(jpeg)) + jpeg + b"\r\n"
    return Response(frames(), mimetype="multipart/x-mixed-replace; boundary=frame",
                    headers={"Cache-Control": "no-store"})


@web.get("/api/events")
def sse():
    def gen():
        q = events.subscribe()
        try:
            yield "retry: 2000\n\n"
            while True:
                try:
                    yield f"data: {q.get(timeout=15)}\n\n"
                except queue.Empty:
                    yield ": ping\n\n"
        finally:
            events.unsubscribe(q)
    return Response(gen(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


@web.get("/api/state")
def state():
    session = getattr(frame.app, "current_session", None) or {}
    return jsonify(messages=chat.messages, info=SRC_INFO,
                   frame_state=getattr(frame.app, "state", None),
                   frame_session=session.get("id"), frame_session_type=session.get("type"))


@web.post("/api/button")
def button():
    body = request.get_json(force=True, silent=True)
    if not isinstance(body, dict):
        abort(400)
    return jsonify(ok=press(body.get("name"), bool(body.get("down"))))


@web.post("/api/rec-release")
def rec_release():
    release_record(request.get_data(), request.content_type)
    return jsonify(ok=True)


@web.post("/api/send")
def send():
    sender = CONTROL_CHARS.sub("", request.form.get("sender") or "").strip()[:32] or "Family"
    try:
        reply_to = int(request.form["reply_to"]) if request.form.get("reply_to") else None
    except ValueError:
        return jsonify(ok=False, error="Bad reply_to"), 400
    try:
        if "file" in request.files:
            f = request.files["file"]
            receive_file(sender, f.read(), f.mimetype, as_voice=request.form.get("voice") == "1", reply_to=reply_to)
        elif (text := (request.form.get("text") or "").strip()[:4000]):
            receive_text(sender, text, reply_to)
        else:
            return jsonify(ok=False, error="Empty message"), 400
    except ValueError as e:                 # our own messages: safe to show
        return jsonify(ok=False, error=str(e)), 400
    except Exception:
        log.exception("send failed")         # details (paths, ffmpeg output) stay in the log
        return jsonify(ok=False, error="Could not process that file"), 500
    return jsonify(ok=True)


@web.get("/chat-media/<path:name>")
def chat_media(name):
    return send_from_directory(CHAT_MEDIA, name, conditional=True)


@web.get("/frame-audio/<token>")
def frame_audio_file(token):
    path = frame_audio.files.get(token)
    if path is None or not path.exists():
        abort(404)
    return send_file(path, conditional=True)


def _ensure_cert():
    cert, key = CERT_DIR / "sim.crt", CERT_DIR / "sim.key"
    if not cert.exists():
        CERT_DIR.mkdir(parents=True, exist_ok=True)
        CERT_DIR.chmod(0o700)                # holds the private key
        names = {"localhost", os.uname().nodename}
        ips = {"127.0.0.1", *subprocess.run(["hostname", "-I"], capture_output=True, text=True).stdout.split()}
        try:
            ts = json.loads(subprocess.run(["tailscale", "status", "--json"], capture_output=True, text=True).stdout)
            names.add(ts["Self"]["DNSName"].rstrip("."))
        except Exception:
            pass
        san = ",".join([f"DNS:{n}" for n in names if n] + [f"IP:{i}" for i in ips if ":" not in i])
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "3650",
                        "-subj", "/CN=AmonaVision simulator", "-addext", f"subjectAltName={san}",
                        "-keyout", str(key), "-out", str(cert)], check=True, capture_output=True)
        key.chmod(0o600)
    return str(cert), str(key)


def start_web():
    from werkzeug.serving import make_server
    servers = [make_server(BIND, HTTP_PORT, web, threaded=True)]
    try:
        servers.append(make_server(BIND, HTTPS_PORT, web, threaded=True, ssl_context=_ensure_cert()))
    except Exception:
        log.exception("HTTPS disabled (the browser mic needs it)")
    for srv in servers:
        threading.Thread(target=srv.serve_forever, name=f"web-{srv.server_port}", daemon=True).start()
    log.info("Simulator UI: http://%s:%d  https://%s:%d", BIND, HTTP_PORT, BIND, HTTPS_PORT)
    if BIND not in ("127.0.0.1", "localhost", "::1"):
        log.warning("The simulator has no login: anyone who can reach %s can use it", BIND)


def main():
    start_web()
    frame.app = viewer.ViewerApp()
    threading.Thread(target=_audio_watchdog, name="audio-watchdog", daemon=True).start()
    threading.Thread(target=_bot_heartbeat, name="bot-heartbeat", daemon=True).start()
    threading.Thread(target=seed_if_empty, name="seed", daemon=True).start()
    frame.app.run()
    # /restart, the 04:00 daily restart, Esc...: the real frame exits and systemd
    # brings it back. Same here.
    log.info("Viewer exited — exiting so systemd restarts the simulator")
    os._exit(0)


if __name__ == "__main__":
    main()
