# platform/radxa-zero3 — TODO

No Radxa ZERO 3 has been tested yet. Nothing in this folder is applied by
`setup.sh`, and `setup.sh` itself is written for Raspberry Pi OS.

To be filled in from a real board (and only then):

- [ ] OS image and where its boot configuration lives (kernel command line,
      device-tree overlays).
- [ ] Display: DRM card index and HDMI connector name; whether the panel needs
      a forced `video=` mode / EDID like the Pi does.
- [ ] Buttons: GPIO library, `(gpiochip, line)` for each header pin, pull-ups.
- [ ] Audio: PipeWire names of the HDMI sink and the USB microphone; mixer
      buffer size that avoids underruns.
- [ ] Video: whether hardware H.264 decoding works with ffpyplayer on the
      chosen kernel, and which decoder name to use.
- [ ] Thermal zone; fan (if one is needed at all).
- [ ] Watchdog driver and how to enable it.
- [ ] Panel power switching (uhubctl on the board's ports or an external hub).

Record each finding in `profiles/sbc/radxa-zero3.yaml`,
`app/hal/sbc_radxa.py` and `docs/hardware/radxa-zero3.md`.
