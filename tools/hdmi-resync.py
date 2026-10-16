#!/usr/bin/env python3
"""Re-present the HDMI signal once: DPMS off, wait, DPMS on.

The panel (LCDWiki 10.1" HDMI Display-H, powered from the Pi's USB) sometimes
fails to lock onto an HDMI signal that was already running when its scaler
finished booting, and then shows "No signal" for good — re-plugging the HDMI
cable fixes it (2026-09-30). Turning the output off and on again gives the
panel the same fresh signal. launch.sh runs this once per boot, before the
viewer takes the display: it needs DRM master, which nobody holds then.

Usage: hdmi-resync.py [CONNECTOR] [OFF_SECONDS]   (default HDMI-A-1 3)
Exit 0 = done, 1 = connector/libdrm problem, 2 = display held by another process.
"""
import ctypes
import ctypes.util
import glob
import os
import sys
import time

DRM_MODE_CONNECTOR_HDMIA = 11
DPMS_ON, DPMS_OFF = 0, 3


class DrmRes(ctypes.Structure):
    _fields_ = [("count_fbs", ctypes.c_int), ("fbs", ctypes.POINTER(ctypes.c_uint32)),
                ("count_crtcs", ctypes.c_int), ("crtcs", ctypes.POINTER(ctypes.c_uint32)),
                ("count_connectors", ctypes.c_int), ("connectors", ctypes.POINTER(ctypes.c_uint32)),
                ("count_encoders", ctypes.c_int), ("encoders", ctypes.POINTER(ctypes.c_uint32)),
                ("min_width", ctypes.c_uint32), ("max_width", ctypes.c_uint32),
                ("min_height", ctypes.c_uint32), ("max_height", ctypes.c_uint32)]


class DrmConnector(ctypes.Structure):
    _fields_ = [("connector_id", ctypes.c_uint32), ("encoder_id", ctypes.c_uint32),
                ("connector_type", ctypes.c_uint32), ("connector_type_id", ctypes.c_uint32),
                ("connection", ctypes.c_int), ("mmWidth", ctypes.c_uint32), ("mmHeight", ctypes.c_uint32),
                ("subpixel", ctypes.c_int), ("count_modes", ctypes.c_int), ("modes", ctypes.c_void_p),
                ("count_props", ctypes.c_int), ("props", ctypes.POINTER(ctypes.c_uint32)),
                ("prop_values", ctypes.POINTER(ctypes.c_uint64)),
                ("count_encoders", ctypes.c_int), ("encoders", ctypes.POINTER(ctypes.c_uint32))]


class DrmProperty(ctypes.Structure):
    _fields_ = [("prop_id", ctypes.c_uint32), ("flags", ctypes.c_uint32), ("name", ctypes.c_char * 32),
                ("count_values", ctypes.c_int), ("values", ctypes.c_void_p),
                ("count_enums", ctypes.c_int), ("enums", ctypes.c_void_p),
                ("count_blobs", ctypes.c_int), ("blob_ids", ctypes.c_void_p)]


def log(msg):
    print(f"hdmi-resync: {msg}", file=sys.stderr, flush=True)


def main():
    name = sys.argv[1] if len(sys.argv) > 1 else "HDMI-A-1"
    off_s = float(sys.argv[2]) if len(sys.argv) > 2 else 3.0
    type_id = int(name.rsplit("-", 1)[1])

    sysfs = glob.glob(f"/sys/class/drm/card*-{name}")
    if not sysfs:
        log(f"{name} not found in /sys/class/drm")
        return 1
    card = os.path.basename(sysfs[0]).split("-", 1)[0]

    drm = ctypes.CDLL(ctypes.util.find_library("drm") or "libdrm.so.2")
    drm.drmModeGetResources.restype = ctypes.POINTER(DrmRes)
    drm.drmModeGetConnector.restype = ctypes.POINTER(DrmConnector)
    drm.drmModeGetProperty.restype = ctypes.POINTER(DrmProperty)
    drm.drmModeConnectorSetProperty.argtypes = [ctypes.c_int, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint64]

    fd = os.open(f"/dev/dri/{card}", os.O_RDWR | os.O_CLOEXEC)
    try:
        res = drm.drmModeGetResources(fd)
        if not res:
            log(f"drmModeGetResources failed on /dev/dri/{card}")
            return 1
        conn_id = dpms_id = None
        for i in range(res.contents.count_connectors):
            conn = drm.drmModeGetConnector(fd, res.contents.connectors[i])
            c = conn.contents
            if c.connector_type == DRM_MODE_CONNECTOR_HDMIA and c.connector_type_id == type_id:
                conn_id = c.connector_id
                for j in range(c.count_props):
                    prop = drm.drmModeGetProperty(fd, c.props[j])
                    if prop and prop.contents.name == b"DPMS":
                        dpms_id = prop.contents.prop_id
                    drm.drmModeFreeProperty(prop)
            drm.drmModeFreeConnector(conn)
        drm.drmModeFreeResources(res)
        if conn_id is None or dpms_id is None:
            log(f"{name}: connector or its DPMS property not found")
            return 1

        # Plymouth may still be letting go of the display right after boot.
        for _ in range(10):
            if drm.drmIsMaster(fd):
                break
            time.sleep(1)
        else:
            log(f"{name}: another process holds the display (not DRM master) — skipped")
            return 2

        rc = drm.drmModeConnectorSetProperty(fd, conn_id, dpms_id, DPMS_OFF)
        if rc:
            log(f"{name}: DPMS off failed ({rc})")
            return 1
        time.sleep(off_s)
        rc = drm.drmModeConnectorSetProperty(fd, conn_id, dpms_id, DPMS_ON)
        if rc:
            log(f"{name}: DPMS on failed ({rc})")
            return 1
        with open(f"{sysfs[0]}/dpms") as f:
            state = f.read().strip()
        log(f"{name}: signal re-presented (off {off_s:g}s, now dpms={state})")
        return 0
    finally:
        os.close(fd)


if __name__ == "__main__":
    sys.exit(main())
