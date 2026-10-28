# Display: `hgfrtee-12.3` — HGFRTEE 12.3" 1920x1280

**Status: NOT VERIFIED — the panel has not been tested.**
Profile: `profiles/display/hgfrtee-12.3.yaml`

| Item | Value | Status |
|---|---|---|
| Native resolution | 1920 x 1280 (15:10) | known |
| Inputs | mini-HDMI, USB-C | known; TODO: is USB-C DisplayPort alt mode? |
| Forced mode / EDID | — | TODO: does it need them? Dump its EDID |
| USB touch controller | — | TODO: does it expose one (needs a `usbhid.quirks` like the LCDwiki)? |
| Picture width | — | TODO: measure; then set `config.txt [Overlay] panel_width_mm` (config.txt overrides the profile) |
| Audio | — | TODO: speakers? (hdmi / none / usb) |
| Power | — | TODO: from the board's USB (for `/screen`) or its own supply? |

## Media canvases (proposed, not validated)

- Photos: **1920x1280**, the panel's own size and aspect ratio.
- Videos: **1080x720** (15:10), fewer pixels than today's 1280x720 so
  decoding is no heavier. Whether the target board decodes and blits this
  smoothly is not measured.

Existing media processed for a 16:9 canvas still displays correctly
(letterboxed, with black bars top and bottom).

## UI

`app/hal/layout.py` scales the 1024x600 design by 1.875 (the shorter axis).
Tested at this size with simulated hardware: every element fits. TODO: the
icons in `assets/icons/` are small bitmaps and look soft when scaled this much;
larger versions would look better.
