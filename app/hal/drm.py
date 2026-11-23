"""What the kernel's DRM layer says about the display, read from /sys/class/drm.

- Which /dev/dri/cardN drives the screen. On the Pi 4 there are two cards: vc4
  (the display, with HDMI connectors) and v3d (render-only, no connectors).
  The kernel numbers them in the order their modules finish probing, which is
  not fixed: on most boots v3d is card0 and vc4 card1, but not on all. A
  hard-coded SDL_KMSDRM_DEVICE_INDEX=1 then points SDL at v3d, and the viewer
  dies at start-up on every retry for that whole boot.
- Which HDMI connector has a panel plugged in (status "connected"), so a panel
  that can sit on either port gets its picture and its sound on the right one.

AMONAVISION_SYS_DRM overrides the sysfs path (tests). Missing or unreadable
sysfs means "unknown" (None / []): callers fall back to the profile values.
"""
from __future__ import annotations

import os
import re
import time
from pathlib import Path

DEFAULT_SYS_DRM = "/sys/class/drm"

# cardN-<connector>, e.g. card1-HDMI-A-2 or card1-Writeback-1
_CONNECTOR_DIR = re.compile(r"^card(?P<card>\d+)-(?P<name>.+)$")


def sys_root() -> Path:
    return Path(os.environ.get("AMONAVISION_SYS_DRM") or DEFAULT_SYS_DRM)


def _connector_dirs(root: Path) -> list[tuple[int, str, Path]]:
    """(card index, connector name, sysfs dir), sorted by card then name."""
    try:
        entries = list(root.iterdir())
    except OSError:
        return []
    out = []
    for path in entries:
        m = _CONNECTOR_DIR.match(path.name)
        if m:
            out.append((int(m.group("card")), m.group("name"), path))
    return sorted(out, key=lambda t: (t[0], t[1]))


def kms_card_index(root: Path | None = None) -> int | None:
    """Index of the lowest-numbered card that has display connectors (the KMS
    device SDL must open), or None if none is found."""
    dirs = _connector_dirs(root or sys_root())
    return dirs[0][0] if dirs else None


def connected_connectors(prefix: str = "HDMI-A-", root: Path | None = None) -> list[str]:
    """Names of connectors starting with `prefix` whose status is 'connected',
    e.g. ['HDMI-A-2']."""
    found = []
    for _card, name, path in _connector_dirs(root or sys_root()):
        if not name.startswith(prefix):
            continue
        try:
            status = (path / "status").read_text(encoding="ascii").strip()
        except OSError:
            continue
        if status == "connected" and name not in found:
            found.append(name)
    return found


def detect_hdmi_connector(preferred: str | None = None, wait_seconds: float = 0.0,
                          root: Path | None = None, poll: float = 1.0) -> str | None:
    """The HDMI connector the panel is plugged into, or None if nothing is
    connected. With several connected, `preferred` wins if it is one of them.
    `wait_seconds` keeps polling for a panel that is still waking up and has
    not raised hot-plug yet (start-up only; 0 = look once)."""
    deadline = time.monotonic() + max(0.0, wait_seconds)
    while True:
        found = connected_connectors(root=root)
        if found:
            return preferred if preferred in found else found[0]
        if time.monotonic() >= deadline:
            return None
        time.sleep(poll)
