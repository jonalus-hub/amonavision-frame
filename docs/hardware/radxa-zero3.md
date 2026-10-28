# SBC: `radxa-zero3` — Radxa ZERO 3 (3W / 3E)

**Status: NOT VERIFIED — no board has been tested.**
Profile: `profiles/sbc/radxa-zero3.yaml` · Code: `app/hal/sbc_radxa.py` (skeleton) · System files: `platform/radxa-zero3/` (README only)

Selecting it (`sbc = radxa-zero3` in `device.conf`) loads the profile, but
every hardware call raises `NotImplementedError` until it is filled in. The
viewer will not run on it yet. A Radxa Cubie A7 would get its own profile and
reuse `sbc_radxa.py` where it can.

Every value below is unknown on purpose. Fill each one in from a real board,
then update the profile, the code and this page together.

## Buttons

| Action | Header pin | gpiochip / line | Status |
|---|---|---|---|
| Previous photo | ? | ? | TODO |
| Play | ? | ? | TODO |
| Record | ? | ? | TODO |
| Next photo | ? | ? | TODO |

TODO: GPIO library (gpiozero pin factories target the Raspberry Pi),
internal pull-ups, debounce.

## Display output

| Item | Status |
|---|---|
| DRM card index for the HDMI port (`SDL_KMSDRM_DEVICE_INDEX`) | TODO |
| HDMI connector name (`/sys/class/drm/card*-*`) | TODO |
| Forced mode / EDID on the kernel command line | TODO |

## Overlays and boot configuration

| Item | Status |
|---|---|
| OS image and where the kernel command line / overlays are configured | TODO |
| Watchdog driver and how to enable it | TODO |
| Fan / PWM (needed at all?) | TODO |

## Audio

| Item | Status |
|---|---|
| PipeWire sink name of the HDMI output, ALSA card name | TODO |
| USB microphone source name | TODO |
| pygame mixer buffer that avoids underruns | TODO |

## Video

| Item | Status |
|---|---|
| Hardware H.264 decoding with ffpyplayer under the chosen kernel; decoder name | TODO |
| Software decoding performance at the display's video canvas | TODO — not measured |

## Thermal

| Item | Status |
|---|---|
| Temperature | TODO — suggestion only: the SoC zone under `/sys/class/thermal/` (check `thermal_zone*/type`) |
| Throttling history | no direct equivalent → reported as "not available" (already implemented) |

## Panel power

TODO: can any USB port be power-switched with `uhubctl`, or is an external
hub with per-port power switching needed for `/screen`?
