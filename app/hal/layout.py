"""Resolution-independent sizes for the viewer's on-screen UI.

The UI was designed on the 1024x600 panel. Every size below is that design
value, scaled by min(width / 1024, height / 600) and rounded. At 1024x600 the
scale is exactly 1.0, so every value — and every drawn pixel — is what the
frame drew before this module existed (tests/fixtures/layout_1024x600.json).
On a bigger panel (e.g. 1920x1280, scale 1.875) the UI grows with it, keeping
its proportions on the panel's shorter axis.

The logo (55% x 40% of the screen) and the button-hint offset (millimetres on
the panel, from panel_width_mm) were already relative and stay in viewer.py.
"""
from __future__ import annotations

from dataclasses import dataclass, fields

REFERENCE_SIZE = (1024, 600)


@dataclass(frozen=True)
class Layout:
    scale: float
    # Splash: logo centre above the middle, spinner below it
    splash_logo_offset_y: int = 55
    splash_spinner_gap: int = 55
    splash_text_font: int = 80        # text instead of the logo
    splash_text_offset_y: int = 40
    splash_spinner_offset_y: int = 40
    spinner_radius: int = 30
    spinner_width: int = 4
    # Recording indicator: pulsing red disc + dark halo
    rec_radius_base: int = 55
    rec_radius_pulse: int = 20
    rec_halo: int = 30
    # Voice reply failed to send: red "!" disc
    send_status_size: int = 100
    send_status_font: int = 64
    send_status_inset: int = 2
    # "No voice note" badge
    no_voice_size: int = 80
    no_voice_pad: int = 24
    # Button-hint pills ("Play" / "Reply"); font and bottom margin come from config.txt
    pill_pad_x: int = 18
    pill_pad_y: int = 9
    pill_screen_margin: int = 4
    pill_font: int = 42
    pill_bottom_margin: int = 48
    # Centered status texts
    idle_font: int = 48
    loading_font: int = 36
    recording_text_font: int = 36
    # Voice note playing: icon at the bottom centre
    voice_icon_size: int = 64
    voice_icon_bottom: int = 20
    voice_icon_pad: int = 20
    # ...or the drawn microphone when the icon is missing
    mic_y_from_bottom: int = 60
    mic_body_dx: int = -10
    mic_body_dy: int = -20
    mic_body_w: int = 20
    mic_body_h: int = 30
    mic_body_radius: int = 10
    mic_arc_dx: int = -18
    mic_arc_dy: int = -10
    mic_arc_size: int = 36
    mic_line_width: int = 3
    mic_stem_top: int = 26
    mic_stem_bottom: int = 36
    mic_base_half: int = 15

    def px(self, value: float) -> int:
        """A design-pixel value at this layout's scale."""
        return int(round(value * self.scale))


def ui_scale(width: int, height: int) -> float:
    return min(width / REFERENCE_SIZE[0], height / REFERENCE_SIZE[1])


def design_values() -> dict[str, int]:
    """The 1024x600 design values (the dataclass defaults)."""
    return {f.name: f.default for f in fields(Layout) if f.name != "scale"}


def compute_layout(width: int, height: int, pill_font: int = 42, pill_bottom_margin: int = 48) -> Layout:
    """Layout for a width x height screen. pill_font / pill_bottom_margin are the
    config.txt [Overlay] values, given in 1024x600 design pixels too."""
    scale = ui_scale(width, height)
    values = design_values()
    values.update(pill_font=pill_font, pill_bottom_margin=pill_bottom_margin)
    return Layout(scale=scale, **{k: int(round(v * scale)) for k, v in values.items()})


def overflows(layout: Layout, width: int, height: int) -> list[str]:
    """Elements that would not fit on a width x height screen (empty = all fit).
    Uses each element's largest extent; fonts are approximated by their size."""
    L = layout
    cx, cy = width // 2, height // 2
    problems = []

    def check(name: str, left: float, top: float, right: float, bottom: float) -> None:
        if left < 0 or top < 0 or right > width or bottom > height:
            problems.append(f"{name}: ({left:.0f},{top:.0f})-({right:.0f},{bottom:.0f}) outside {width}x{height}")

    # Splash: logo at most 40% of the height, centred splash_logo_offset_y above
    # the middle, spinner splash_spinner_gap below the logo.
    logo_h = int(height * 0.40)
    logo_bottom = cy - L.splash_logo_offset_y + logo_h / 2
    check("splash logo", cx - width * 0.55 / 2, cy - L.splash_logo_offset_y - logo_h / 2,
          cx + width * 0.55 / 2, logo_bottom)
    check("splash spinner", cx - L.spinner_radius, logo_bottom + L.splash_spinner_gap - L.spinner_radius,
          cx + L.spinner_radius, logo_bottom + L.splash_spinner_gap + L.spinner_radius)
    r = L.rec_radius_base + L.rec_radius_pulse + L.rec_halo
    check("recording indicator", cx - r, cy - r, cx + r, cy + r)
    s = L.send_status_size / 2
    check("send status", cx - s, cy - s, cx + s, cy + s)
    s = (L.no_voice_size + L.no_voice_pad) / 2
    check("no-voice badge", cx - s, cy - s, cx + s, cy + s)
    pill_h = L.pill_font + 2 * L.pill_pad_y
    check("button pill", L.pill_screen_margin, height - L.pill_bottom_margin - pill_h / 2,
          width - L.pill_screen_margin, height - L.pill_bottom_margin + pill_h / 2)
    top = height - L.voice_icon_size - L.voice_icon_bottom - L.voice_icon_pad // 2
    check("voice icon", cx - L.voice_icon_size, top, cx + L.voice_icon_size,
          top + L.voice_icon_size + L.voice_icon_pad)
    mic_y = height - L.mic_y_from_bottom
    check("drawn microphone", cx - L.mic_base_half, mic_y + L.mic_body_dy,
          cx + L.mic_base_half, mic_y + L.mic_stem_bottom + L.mic_line_width)
    return problems
