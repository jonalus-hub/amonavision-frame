# SBC: `rpi4` — Raspberry Pi 4 Model B

**Status: verified** (production board of the deployed frames).
Profile: `profiles/sbc/rpi4.yaml` · Code: `app/hal/sbc_rpi4.py` · System files: `platform/rpi4/`

## Buttons (gpiozero, lgpio pin factory)

Internal pull-up, falling edge, 20 ms bounce time. Pins are BCM numbers;
`config.txt [GPIO]` overrides the profile.

| Action | Key | BCM | Physical pin |
|---|---|---|---|
| Previous photo | ← | 9 | 21 |
| Play voice note / video | Space | 17 | 11 |
| Record voice reply | R | 27 | 13 |
| Next photo | → | 22 | 15 |

Each button goes between its pin and GND.

## Display output

| Item | Value | Status |
|---|---|---|
| DRM device | `/dev/dri/card1` (vc4 KMS; card0 is the v3d render node) → `SDL_KMSDRM_DEVICE_INDEX=1` | verified |
| Connector | `HDMI-A-1` (HDMI0, next to USB-C power) | verified |
| KMS driver | `dtoverlay=vc4-kms-v3d` (full KMS; fake-KMS breaks the forced mode) | verified |
| Panel mode / EDID | forced on the kernel command line, see the display page | verified |

## Overlays and boot configuration

| Where | What | Why |
|---|---|---|
| `/boot/firmware/config.txt` | `hdmi_force_hotplug=1`, `hdmi_force_edid_audio=1`, `display_auto_detect=0` | stable HDMI + HDMI audio on the LCDwiki panel |
| `/boot/firmware/config.txt` | `dtoverlay=pwm,pin=18,func=2` | fan PWM on BCM 18 / pin 12 |
| `/boot/firmware/config.txt` | `dtparam=watchdog=on` | hardware watchdog |
| `/boot/firmware/cmdline.txt` | `usbcore.autosuspend=-1 consoleblank=0 cgroup_enable=memory cgroup_memory=1` | USB hubs awake, no blanking, MemoryMax works |

All applied by `setup.sh`; copies in `platform/rpi4/`.

## Audio

| Item | Value | Status |
|---|---|---|
| Output | PipeWire sink `^alsa_output\.platform-fef00700\.hdmi\.` (ALSA card `vc4hdmi0`) | verified |
| Microphone | USB, PipeWire source `^alsa_input\.usb-` | verified |
| pygame mixer | 48 kHz, 16-bit, stereo, 4096-sample buffer (avoids underruns while decoding video) | verified |
| Recording | 48 kHz mono | verified |

## Video

ffpyplayer with `vd=h264_v4l2m2m` (BCM2835 hardware H.264 decoder), 4 threads.
Videos are pre-transcoded to H.264 Main at the display's video canvas.

## Thermal, fan, watchdog, panel power

| Item | Value | Status |
|---|---|---|
| Temperature | `/sys/class/thermal/thermal_zone0/temp` (m°C) — shown in `/pi` and the dashboard | verified |
| Throttling history | not read (`vcgencmd` is not used) → "not available" | — |
| Fan | `fan_control.py`, `/sys/class/pwm/pwmchip0/pwm0`, 25 kHz, curve 40–75 °C | verified |
| Watchdog | `/dev/watchdog`, systemd `RuntimeWatchdogSec=15`, `RebootWatchdogSec=2min` | verified |
| Panel power (`/screen`) | `uhubctl -l 1-1` and `-l 2` (all four ports ganged; the USB mic and dongle drop too) | verified |
