# AmonaVision – Smart Photo Frame

AmonaVision turns a Raspberry Pi into a smart photo frame driven by a Telegram bot. Family or friends send photos, videos, and voice notes to a Telegram group; the frame displays them automatically on a connected screen with audio playback and GPIO button controls.

---

## Hardware Requirements

| Component | Notes |
|-----------|-------|
| Raspberry Pi 4 | 2 GB RAM or more recommended |
| MicroSD card | 16 GB minimum, A1/A2 class |
| HDMI display | Any resolution — content is letterboxed to fit |
| 4-pin PWM fan | Connected to BCM 18 (physical pin 12) and GND |
| GPIO buttons | Left / Play / Record / Right (see `config.txt` for pins) |
| USB 4G dongle | For internet when no Wi-Fi is available |

---

## Project Structure

```
AmonaVision/
├── app/
│   ├── bot/
│   │   ├── bot.py              ← Telegram bot handlers and commands
│   │   ├── grouper.py          ← Turns incoming media into sessions (reply-based pairing)
│   │   ├── processor.py        ← Image letterbox + blur padding
│   │   └── video_processor.py  ← FFmpeg video transcoding
│   ├── db/
│   │   └── database.py         ← SQLite database manager
│   ├── scoring.py              ← Picks the next photo/video for the slideshow
│   ├── usage_log.py            ← Button usage log (app/logs/buttons.csv)
│   ├── logs/                   ← Runtime logs
│   └── media/                  ← Received / processed media, voice replies
├── web/
│   ├── app.py                  ← LAN dashboard (Flask)
│   └── templates/
├── tools/
│   ├── bot-watchdog.sh         ← Liveness watchdog (bot, Tailscale, 4G dongle)
│   ├── health-alert.sh         ← Periodic health check with Telegram alerts
│   ├── hdmi-resync.py          ← Re-presents the HDMI signal once per boot
│   ├── hdmi-diag.sh            ← HDMI / audio diagnostics report
│   ├── panel-power-cycle.sh    ← /screen: power-cycles the panel over USB
│   └── reboot-with-panel-cycle.sh ← /reboot and the after-boot panel power-cycle
├── config/
│   ├── edid/amona.bin          ← The panel's EDID
│   ├── initramfs-tools/hooks/  ← Puts the EDID into the initramfs
│   └── wireplumber/            ← Makes HDMI the default audio output
├── assets/                     ← Logo (splash + boot splash) and viewer icons
├── viewer.py                   ← Pygame display engine
├── start.py                    ← Process supervisor (viewer + bot)
├── run.py                      ← Bot entry point
├── launch.sh                   ← Activates the venv, sets up audio, runs start.py
├── fan_control.py              ← PWM fan controller
├── *.service / *.timer / *.path ← systemd units (installed by setup.sh)
├── setup.sh                    ← One-shot device provisioning script
├── test_setup.sh               ← Full diagnostic suite (/test, health check)
├── config.txt                  ← Display, GPIO, overlay, scoring and reminder settings
├── requirements.txt
└── .env                        ← Secrets (not in git — see .env.example)
```

---

## Deployment Guide

### Step 1 — Create a Telegram bot

1. Open Telegram and message **@BotFather**
2. Send `/newbot` and follow the prompts
3. Copy the **bot token** (looks like `123456:ABC-DEF...`)
4. Add the bot to your Telegram group
5. Send a message in the group, then open:
   ```
   https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates
   ```
   Find `"chat": {"id": ...}` — that number is your **group chat ID** (negative for groups, e.g. `-1001234567890`)

---

### Step 2 — Flash the SD card

Use **Raspberry Pi Imager** and select:

- **OS:** Raspberry Pi OS Lite (64-bit) — no desktop needed
- **Advanced settings (gear icon):**
  - Hostname: `amonavision`
  - Enable SSH with password
  - Username: `av` / Password: one of your choice
  - Set your Wi-Fi or leave blank if using a USB dongle

---

### Step 3 — Copy the project to the Pi

Wait for the Pi to boot (give it ~60 seconds), then copy the project files from your PC.

**Mac/Linux:**
```bash
rsync -av --exclude=venv --exclude=.git \
    /path/to/AmonaVision/ av@<pi-ip>:/home/av/AmonaVision/
```

**Windows:**
```powershell
scp -r C:\path\to\AmonaVision av@<pi-ip>:/home/av/AmonaVision
```

---

### Step 4 — Create the `.env` file

SSH into the Pi and create the secrets file:

```bash
ssh av@<pi-ip>
nano /home/av/AmonaVision/.env
```

Paste and save (`.env.example` lists every variable):
```
TELEGRAM_BOT_TOKEN=123456:ABC-DEF...
TELEGRAM_GROUP_CHAT_ID=-1001234567890
TELEGRAM_ADMIN_CHAT_ID=123456789
WEB_PASSWORD=choose-a-shared-family-password
SAMBA_PASSWORD=choose-a-samba-password
```

---

### Step 5 — Run the setup script

The script installs all system packages, configures hardware, and sets up services. It only needs to be run once.

**Without Tailscale** (local network access only):
```bash
sudo bash /home/av/AmonaVision/setup.sh
```

**With Tailscale** (access from anywhere — recommended for unattended deployment):
```bash
sudo bash /home/av/AmonaVision/setup.sh --tailscale-key=tskey-auth-xxxx
```

> Generate a reusable auth key at https://login.tailscale.com/admin/settings/keys.  
> Once connected, you can SSH with `ssh av@amonavision` from any device on your Tailscale network.

The script configures:

| Item | Detail |
|------|--------|
| System packages | pipewire, ffmpeg, log2ram, lgpio, python3-dev, modemmanager, fake-hwclock, etc. |
| Hardware PWM | `dtoverlay=pwm,pin=18,func=2` in `/boot/firmware/config.txt` |
| Hardware watchdog | Reboots the Pi automatically if the app hangs |
| journald | persistent (survives reboots), 192 MB cap; log2ram RAM raised to 300 MB to fit |
| Raspberry Pi Connect | disabled — `rpi-connect-wayvnc` crash-loops on a kmsdrm frame; Tailscale covers remote access |
| log2ram | Logs live in RAM, synced to SD card every 15 min (`log2ram-daily.timer` override) — a power cut loses at most 15 min of logs |
| USB autosuspend | Disabled so the 4G dongle never drops |
| HDMI forced on | `video=HDMI-A-1:1024x600M@60D drm.edid_firmware=HDMI-A-1:edid/amona.bin` in `cmdline.txt` (connector set by `HDMI_CONNECTOR` in `setup.sh`, backup taken before any change); the panel's EDID at `/lib/firmware/edid/amona.bin` (repo: `config/edid/amona.bin`) and inside the initramfs via `/etc/initramfs-tools/hooks/amona-edid`. The frame drives the panel (video + HDMI audio) even if the panel wasn't ready when the Pi booted. The Pi 4 can't produce the panel's native 1024x600 timing, so it actually outputs 1920x1080 and the panel scales. The panel sometimes misses a signal that was already running when it finished booting ("No signal" until the cable is re-plugged), so `launch.sh` re-presents it once per boot (`tools/hdmi-resync.py`: output off 3 s, back on). The panel is powered from the Pi's USB, which stays on across a software reboot; if one leaves it on "No signal", `/screen` (admin chat only) cuts USB power for 10 s with `uhubctl` (`tools/panel-power-cycle.sh` via `amonavision-panel-reset.path`) — the 4G dongle and mic drop with it for ~1 min, since the Pi 4's ports are ganged. The same power-cycle runs automatically ~45 s after every boot, cold or not (`amonavision-reboot-panel-cycle.service` → `tools/reboot-with-panel-cycle.sh after-boot`; the panel once came up on "No signal" from a cold start too), so the network takes ~1 min longer to come up after a boot. `/reboot` (admin chat only) reboots through `tools/reboot-with-panel-cycle.sh` via `amonavision-reboot.path`. If the HDMI link drops and PipeWire loses its HDMI sink, the 5-min watchdog re-creates it and makes it the default again. Diagnose with `tools/hdmi-diag.sh`; `/test` fails if the output isn't being driven |
| Wi-Fi power-save | Disabled (`/etc/NetworkManager/conf.d/00-wifi-powersave-off.conf`) — an idle frame otherwise drops/lags |
| 4G dongle | Dedicated `4g-dongle` profile, matched by USB-modem driver (`cdc_ether`/`cdc_ncm`/`rndis_host`/`huawei_cdc_ncm`): **metered primary link** (route-metric 100, priority 20, reconnects forever) — the deployed unit has no Wi-Fi at all; Wi-Fi (route-metric 700, priority −10) is only a fallback for testing/maintenance somewhere that has it. The Imager's catch-all `netplan-eth0` profile is set to autoconnect off so it can't grab the dongle |
| Connectivity check | `/etc/NetworkManager/conf.d/20-connectivity-check.conf` (every 600 s, per device) — a HiLink/ECM stick keeps its USB link up even with no LTE, so this is how NetworkManager knows it's dead and lets Wi-Fi take over |
| Clock | `systemd-time-wait-sync` + timesyncd clock-floor (+ `fake-hwclock` where not masked) — the Pi has no RTC; a stale clock breaks TLS on cold boot |
| Samba | `\\<pi-ip>\RaspberryPi` shares the whole filesystem as user `av` (Linux permissions still apply); password from `SAMBA_PASSWORD` in `.env` |
| lightdm | Disabled — app uses the framebuffer directly |
| systemd services | `amonavision` + `fan-control` installed and enabled |
| Python venv | Created with `--system-site-packages`, requirements installed |
| Tailscale | Installed with `--accept-dns=false` (MagicDNS off) and connected if a key is provided |

---

### Step 6 — Reboot

```bash
sudo reboot
```

The frame starts automatically on boot. The Telegram bot is ready once the Pi has internet access.

---

## Remote Access

If Tailscale was configured during setup. MagicDNS is deliberately **off**
(`--accept-dns=false`) so a tailscaled hiccup can never break DNS on the frame —
so connect by the device's Tailscale **IP** (`tailscale ip -4` on the Pi, or the
Tailscale admin console), not by a MagicDNS name:

```bash
# SSH from anywhere (replace with this device's tailscale IP)
ssh av@100.x.y.z

# Check service status
ssh av@100.x.y.z "systemctl status amonavision"

# Stream logs live
ssh av@100.x.y.z "tail -f /home/av/AmonaVision/app/logs/startup.log"
```

> If `setup.sh --hostname=amonavision` collides with an existing tailnet node the
> new node becomes `amonavision-1` etc. — another reason to use the IP.

---

## LAN Dashboard

A small read-mostly web dashboard for the family (not the person with the
frame) at **`http://<pi-ip>:8080`** — system/project state, every media file
and its weight, the whole database (incl. a read-only SQL box), a whitelisted
config editor, and a soft-restart / diagnostics button.

- **Isolated by design** (`amonavision-web.service`): its own unit, no
  ordering vs `amonavision.service`, `Nice=15`, `CPUQuota=20%`,
  `MemoryMax=128M`, `OOMPolicy=stop`. A crash/hang/leak here cannot touch the
  frame. The DB is opened `mode=ro` + `PRAGMA query_only`; the only things it
  ever writes are `config.txt` (atomic, comment-preserving, whitelisted keys
  only) and `/tmp/amonavision_restart` (the same soft-restart flag `/restart`
  in Telegram uses — no root needed).
- **Auth**: HTTP Basic, one shared password in `.env` as `WEB_PASSWORD`. The
  service **refuses to start** if it isn't set.
- Only reachable on the LAN and over Tailscale — no port is forwarded.

```bash
echo 'WEB_PASSWORD=some-shared-family-password' >> /home/av/AmonaVision/.env
sudo systemctl restart amonavision-web
systemctl status amonavision-web
journalctl -u amonavision-web -n 50
```

Config keys editable from the dashboard: `Viewer.slideshow_interval`,
`Scoring.enabled`, `System.daily_restart`, `Overlay.{enabled,button_offset_cm,bottom_margin_px}`.
Anything that could brick the frame (GPIO pins, resolution, audio) is not.

---

## Service Management

```bash
# Restart the app
sudo systemctl restart amonavision

# Check app status and recent logs
sudo systemctl status amonavision

# Restart the fan controller
sudo systemctl restart fan-control

# View full logs
journalctl -u amonavision -f
```

---

## Supported Media

| Type | Notes |
|------|-------|
| Photos | Displayed letterboxed with blurred background fill |
| Videos | Re-encoded to H.264/AAC 720p at 48 kHz for smooth playback |
| Voice notes | Played back over the current display |
| Documents | Image and video files sent as documents are also accepted |

---

## Bot Commands

| Command | Description |
|---------|-------------|
| `/start` | Welcome message |
| `/help` | Show available commands |
| `/status` | Media count and storage usage |
| `/pi` | CPU temp/usage, RAM, SD, fan, uptime |
| `/test` | Full diagnostic on demand (network, services, disk, clock…) — same suite the periodic health-check timer runs |
| `/logs [viewer\|bot\|startup]` | Last lines of a log file |
| `/volume [0-100]` | Get or set audio output volume |
| `/brightness …` | Screen brightness: `70`, `auto`, `day 90`, `night 15`, `schedule 07:00 22:00` |
| `/restart` | Soft restart (~15s) |
| `/chatid` | This chat's id (to fill in `.env`) |
| `/reboot` | Full Pi reboot, then a panel power-cycle (~3 min, admin chat only) |
| `/screen` | Power-cycle the panel over USB (admin chat only) |
| `/reminder` | State and preview of the Monday reminder (admin chat only) |
| `/usage [yesterday\|N]` | Button usage stats (admin chat only): presses per button, voice replies sent / discarded / failed, bursts of use — today, yesterday or the last N days. Read from `app/logs/buttons.csv` |

Replying `delete` to a photo/video you sent hides it from the frame (the files stay on the Pi); replying `recycle` shows it again.

---

## Logs

The physical buttons also have their own usage log, `app/logs/buttons.csv` (on the SD card, never rotated): one row per press (button, viewer state, session and photo/video on screen) and one per voice reply with its duration and result (`sent` / `discarded` if shorter than 1 s / `send_error` / …). `/usage` summarises it.

Logs are stored in RAM (`/var/log` via log2ram) and synced to the SD card every 15 minutes to minimise write wear. To read them:

```bash
# Application log
tail -f /home/av/AmonaVision/app/logs/startup.log

# systemd journal
journalctl -u amonavision -n 100
```

---

## Monitoring (unattended deployments)

For a frame with no physical access (e.g. at a relative's house), several layers to check its health without visiting:

- **`/test` in Telegram** — runs the full diagnostic suite on demand and replies with a summary, in whichever chat you send it from (group or your private DM with the bot).
- **Liveness watchdog** (`amonavision-bot-watchdog.timer`, every 5 min) — `Restart=always` only helps when a process *exits*; it does nothing for one that's still running but wedged. On 2026-09-04/05 the bot's `getUpdates` polling silently stopped (no crash, no error) **and** Tailscale dropped off the tailnet, for the same ~10h window, until a manual reboot — both staying down that long despite each having its own reconnect logic points at the WiFi driver itself getting stuck, not a brief blip either failed to shrug off. Two tiers, per problem, independently:
  - **Tier 1**: internet reachable but neither the bot's poll heartbeat (`/tmp/amonavision_bot_heartbeat`, touched on every successful `getUpdates`) nor `app/logs/app.log` updated for 10+ min → restart `amonavision.service`; internet reachable but `tailscale status --json`'s `Self.Online` isn't true → restart `tailscaled`.
  - **Tier 2 (escalation)**: still broken 35+ minutes after tier 1 fired — a plain service restart clearly isn't fixing it — reboots the whole Pi. This is the one action with direct proof of working (the 2026-09-05 recovery), so it's the deliberate ceiling: worst case, a hang like this now self-heals within ~35 min instead of needing someone to notice and reboot it by hand. Capped at once per 2h so a reboot that somehow doesn't help can't loop.

  Each tier DMs the admin before acting.

  It also watches the **4G dongle** — and unlike the two above, it does so even with no internet at all, because at the deployment site the dongle *is* the internet. On 2026-09-22/23 the stick re-enumerated itself in bursts and the last attempt failed (`cdc_ether probe failed with error -71`): still on the USB bus, no network interface, nothing retried — three outages of 2–11.5 h that only home Wi-Fi hid. Every cycle it classifies the link and applies the cheapest fix for the broken layer (all tested live):
  - **no interface / no IP / gateway not answering** → logical unplug/replug of the dongle's USB port (`/sys/bus/usb/devices/<dev>/port/disable`) — back in ~9 s.
  - **no upstream for 10+ min** (USB fine, LTE/carrier side not) → redial via the dongle's own web API (ZTE `DISCONNECT_NETWORK`/`CONNECT_NETWORK`) — ~7 s; **25+ min** → reboot the dongle via its API (`REBOOT_DEVICE`) — ~60 s.
  - **35+ min and the Pi has no internet at all** → reboot the Pi, at most twice per outage (an empty SIM or a carrier outage isn't fixed by rebooting).

  One DM on the first action of an outage and one on recovery ("4G recovered after ~X min (automatic actions: …)"). Every cycle appends a line to `app/logs/dongle.log` (also on the dashboard's `/debug`): interface + USB path, error counters, kernel re-enumerations / USB errors / TX-queue timeouts in the last 5 min, and the dongle's own view — `lte=ppp_connected net=LTE band=… rsrp=… rsrq=… cell=… conn_uptime_s=…` — ending in `status=… action=…`.
- **Automatic health report**: `amonavision-healthcheck.timer` runs the full suite every 3 hours (`tools/health-alert.sh`) and DMs an admin — only on failure, on recovery, or once a day as a heartbeat, never while healthy and nothing changed. Deliberately goes to a *person*, not the family group (`TELEGRAM_GROUP_CHAT_ID`, used elsewhere for the voice-reply fallback) — the family doesn't need "0 failures" pings about the frame's plumbing. Requires `TELEGRAM_ADMIN_CHAT_ID` in `.env`:
  ```bash
  # Message the bot privately (not the group), then check app.log:
  grep "from user id=" /home/av/AmonaVision/app/logs/app.log | tail -1
  # that id IS your chat id for a 1:1 chat — add it to .env:
  echo 'TELEGRAM_ADMIN_CHAT_ID=123456789' >> /home/av/AmonaVision/.env
  ```
- Check either timer directly: `systemctl status amonavision-healthcheck.timer amonavision-bot-watchdog.timer` · `journalctl -u amonavision-healthcheck -u amonavision-bot-watchdog -n 50`
- Run either by hand: `sudo bash /home/av/AmonaVision/tools/health-alert.sh` · `sudo bash /home/av/AmonaVision/tools/bot-watchdog.sh`
- **Diagnosing an overnight outage after the fact**: `journalctl` now persists across reboots (`Storage=persistent`, synced to the SD by log2ram) specifically so a NetworkManager/kernel trail survives a recovery reboot — `journalctl -b -1` shows the previous boot.

---

## Connectivity Troubleshooting

The frame reaches the internet over **the USB 4G dongle first, Wi-Fi as fallback**
— the deployed unit lives somewhere with no Wi-Fi at all, so the dongle IS the
connection; Wi-Fi only matters for testing/maintenance somewhere that has it,
and is used automatically whenever the dongle can't reach the internet.
Run `bash /home/av/AmonaVision/test_setup.sh` for a full check. Common issues:

| Symptom | Likely cause | Fix |
|---|---|---|
| Bot offline right after a power cut, recovers after ~1 min | Cold-boot clock was stale (no RTC) → TLS "certificate not yet valid" | Expected; `fake-hwclock` + NTP fix it. `run.py` also waits for a sane clock before polling. Persistent → check UDP/123 isn't blocked. |
| Bot offline but `ping 1.1.1.1` works | `/etc/resolv.conf` hijacked by Tailscale MagicDNS and tailscaled is unhealthy | `sudo tailscale set --accept-dns=false && sudo systemctl restart tailscaled` |
| Wi-Fi drops / high latency when idle | brcmfmac power-save | `sudo` re-run `setup.sh` (installs the power-save-off drop-in), or `sudo iw dev wlan0 set power_save off` |
| No internet at all (no Wi-Fi at the site, dongle plugged) | 4G SIM has no data (no credit / not registered) — a HiLink dongle still hands out a LAN lease | `tail app/logs/dongle.log` — `status=no_upstream` with `lte=ppp_disconnected` means the radio side; the watchdog already redials / reboots the stick. Otherwise check the dongle web UI at its gateway IP (`nmcli -g IP4.GATEWAY device show eth1`); top up / activate the SIM; make sure the SIM PIN is disabled (a PIN-locked SIM never redials after a power cut) |
| Dongle on the USB bus (`lsusb` shows ZTE `19d2:1706`) but no `eth1` | Failed re-enumeration (`error -71` in `journalctl -k`) — the 2026-09-22/23 outages | Self-heals: the watchdog re-plugs the USB port within ~5 min (`status=no_iface action=usb_replug->ok` in `dongle.log`). Now: `sudo bash /home/av/AmonaVision/tools/bot-watchdog.sh`. If it keeps recurring: powered USB hub, or a mains-powered 4G router |
| Weak 4G signal (`rsrp=` below about −105 in `dongle.log`, or a WARN in `/test`) | Poor coverage where the Pi sits — the modem then transmits near full power, retransmits more and drops more often | Move the stick toward a window (USB extension); if it can't move, a mains-powered 4G router at the window, ideally cabled to the Pi's Ethernet port (`eth0-physical` already prefers it) |
| `setup.sh` said "No internet" on a 4G-only box | Old probe used TCP/53, which mobile APNs block | Fixed — probe is now TCP/443 and non-fatal |
| Testing at home shows everything routing over Wi-Fi, not the dongle | Fine — Wi-Fi has priority-20/metric-700 fallback status, so it only wins when the dongle has no internet; check `/` on the dashboard to see which link is actually active | If the dongle DOES have signal/data but Wi-Fi still wins, check `nmcli -f NAME,DEVICE connection show --active` — the dongle should be on `4g-dongle`; re-run `setup.sh` if not |
| Everything routes into the dead dongle instead of falling back to Wi-Fi | NetworkManager connectivity check missing — without it a stick with no LTE still looks healthy to NetworkManager | Re-run `setup.sh` (installs `20-connectivity-check.conf`); the SIM itself may still need fixing |

---

## License

GNU General Public License v3.0 — see [LICENSE](LICENSE).
