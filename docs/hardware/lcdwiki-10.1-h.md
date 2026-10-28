# Display: `lcdwiki-10.1-h` — LCDwiki 10.1inch HDMI Display-H

**Status: verified** (panel of the deployed frames).
Profile: `profiles/display/lcdwiki-10.1-h.yaml` · Product page: https://www.lcdwiki.com/10.1inch_HDMI_Display-H

| Item | Value | Status |
|---|---|---|
| Native resolution | 1024 x 600 (~17:10) | verified |
| Input | HDMI | verified |
| Power | USB, from the board (so `/screen` can power-cycle it) | verified |
| Forced mode | `video=HDMI-A-1:1024x600M@60D` (kernel command line) | verified |
| EDID | `config/edid/amona.bin`, baked into the initramfs, `drm.edid_firmware=HDMI-A-1:edid/amona.bin` | verified |
| USB touch controller | QDtech `0484:5750`, ignored with `usbhid.quirks=0x0484:0x5750:0x0004` (it crashes SDL's kmsdrm evdev probe; touch is not used) | verified |
| Picture width | 230 mm (`config.txt [Overlay] panel_width_mm`) — places the "Play"/"Reply" pills over the buttons | value in use |
| Audio | built-in speakers over HDMI; the EDID doesn't advertise audio, hence `hdmi_force_edid_audio=1` on the Pi | verified |
| Known issue | sometimes misses an HDMI signal that was already running when it finished booting → `tools/hdmi-resync.py` once per boot | verified |

## Media canvases

Photos are pre-rendered to **1920x1080** and videos to **1280x720** (16:9,
blurred padding). On this ~17:10 panel the viewer letterboxes them with 12 px
bars top and bottom — that is how the frames look today, so the canvases stay
16:9 rather than matching the panel.

## UI

The on-screen UI was designed at this resolution: `app/hal/layout.py` scale = 1.0.
