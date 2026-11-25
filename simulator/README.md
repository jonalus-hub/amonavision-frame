# AmonaVision simulator

Try the frame in a browser, with no Raspberry Pi and no Telegram bot. The
simulator runs the real frame code (`viewer.py` and the bot's handlers)
unmodified and puts a web page where the hardware would be: the frame with its
four buttons, its speaker and microphone, and a family group chat that goes
through the real bot code.

## What is real and what is simulated

| Part of the frame | In the simulator |
|---|---|
| `viewer.py` (slideshow, states, hints, scoring, recording) | The real one, untouched |
| Bot (handlers, `MessageGrouper`, photo/video processing, database) | The real one, untouched |
| 1024×600 screen | SDL "dummy" driver; every `flip()` is streamed to the browser as MJPEG |
| GPIO buttons | The buttons on the page (or ← → Space R): they feed the same queue as the GPIO callbacks |
| Speaker | Whatever the frame plays (voice notes, video sound) plays in the browser |
| Microphone | The browser records while you hold ●; the audio reaches the viewer as if it came from `sounddevice` |
| Telegram | A fake group chat on the page; messages go through the bot's real handlers |

The real bot is never contacted: the simulator forces `TELEGRAM_BOT_TOKEN` empty,
whatever `.env` says. `/test`, `/reboot`, `/screen` and `/volume` are blocked,
because they would act on the machine running the simulator.

## Quick start

From the root of a checkout, on Linux (it is not tested elsewhere), with
Python 3.11+ and `openssl` (for the HTTPS certificate):

```bash
python3 -m venv venv
grep -v RPi.GPIO requirements.txt | venv/bin/pip install -r /dev/stdin
venv/bin/python simulator/simulator.py
```

Then open **http://127.0.0.1:8099**. On its first start the simulator "receives"
the sample media in `sample-media/`: 12 photos, 4 of them with a voice note, and
one video.

The microphone (the ● button and voice notes from the chat) only works over
HTTPS: use **https://127.0.0.1:8443** and accept the warning. The certificate
is self-signed and is created in `simulator/certs/` on the first start.

### Opening it from other devices

The page has **no login**. Anyone who can reach it can post to the chat and
press the buttons. So it only listens on localhost unless you say otherwise:

```bash
SIM_BIND=0.0.0.0 venv/bin/python simulator/simulator.py
```

Only do that on a network you trust, for example your home LAN or a Tailscale
tailnet. Never do it on a machine that is reachable from the internet.

## The frame's dashboard

The frame's own LAN dashboard (`web/app.py`) runs next to the simulator,
unmodified, through `dashboard.py`:

```bash
cp simulator/dashboard.env.example simulator/dashboard.env   # then set WEB_PASSWORD
set -a; . simulator/dashboard.env; set +a
venv/bin/python simulator/dashboard.py                       # http://127.0.0.1:8080
```

The "⚙️ Dashboard" button on the simulator's page opens it. The user name can be
anything; the password is `WEB_PASSWORD`. Differences from the frame:

- "Run diagnostics" is blocked: `test_setup.sh` would run on this machine.
- CPU, RAM, disk, network and Tailscale are this machine's. The fan and the
  watchdogs are not simulated.
- Service states are those of the simulator's units (see below).
- Settings saved from the Config page go to `config.txt` **of this checkout**,
  which is tracked by git. Don't commit them by accident. To keep your checkout
  clean, run the simulator on a separate clone (`AMONA_SRC`, below).

## Settings

All optional, as environment variables:

| Variable | Default | What it is |
|---|---|---|
| `SIM_BIND` | `127.0.0.1` | Address the page and the dashboard listen on |
| `SIM_HTTP_PORT` / `SIM_HTTPS_PORT` | `8099` / `8443` | The simulator's page |
| `SIM_DASHBOARD_PORT` | `8080` | The dashboard |
| `AMONA_SRC` | this checkout | Frame code to run: point it at another clone to test a different branch |
| `SIM_DATA` | `simulator/data` | The fake chat and its files |
| `SIM_CERTS` | `simulator/certs` | The self-signed certificate and its key |
| `SIM_ALLOW_ON_FRAME` | unset | See "Not on a real frame" |

The frame's own data (database, photos, voice notes, logs) goes where it goes
on a frame: `app/db`, `app/media` and `app/logs` of the checkout being run. All
of it is git-ignored.

## Running it as a service

`systemd/` has two example user units, one for the simulator and one for the
dashboard. Copy them to `~/.config/systemd/user/`, adjust the paths, and enable
them. As on the frame, the viewer exits on `/restart` and at 04:00, and systemd
starts it again.

## Starting over

```bash
simulator/reset.sh
```

This deletes the photos, voice notes, videos, database, chat and stats. On its
next start the simulator seeds the sample media again. If the simulator runs as
the `amonavision-sim` user service (`SIM_SERVICE` to change the name), the
script stops and restarts it.

## Not on a real frame

The simulator, the dashboard and `reset.sh` refuse to run on a machine where
`amonavision.service` is installed, that is, on a real frame:

- From the frame's checkout they would share its database and photos, and
  `reset.sh` would delete them.
- In any case they would share its `/tmp` flags: a `/restart` in the simulator
  would restart the real frame.

`SIM_ALLOW_ON_FRAME=1` overrides the check.

## Security notes

- The page has no login and listens on localhost by default (`SIM_BIND`).
- Every POST must carry an `X-Requested-With` header that the page sends. A
  form on another website can't send it, so a page someone visits can't drive
  the simulator.
- Uploads are limited to 25 MB. Images are decoded and re-encoded. Audio and
  video always go through ffmpeg, which may only use real media demuxers
  (MP4/MOV, Matroska/WebM, Ogg, WAV, MP3, AAC, FLAC) and local files. So an
  upload can't be a playlist that makes ffmpeg read other files or fetch URLs.
- The dashboard keeps the frame's own HTTP Basic password, and its diagnostics
  button is blocked.
- No Telegram traffic, ever: the token is forced empty, and the frame's one
  outgoing call (sending a voice reply) is replaced by the fake chat.

## Files

- `simulator.py`: the harness that stands in for the hardware and Telegram.
- `dashboard.py`: serves the frame's dashboard with the changes above.
- `static/index.html`: the page.
- `static/frame-wood.jpg`: the frame's surface, from a photo of a real frame
  (perspective corrected, screen and buttons removed). The button positions in
  the page are measured on that photo.
- `sample-media/`: what a new simulator starts with, and the licenses (see its README).
- `reset.sh`, `systemd/`, `dashboard.env.example`: see above.
