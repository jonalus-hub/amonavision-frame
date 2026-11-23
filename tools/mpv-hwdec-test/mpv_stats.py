#!/usr/bin/env python3
"""Polls a running mpv over its IPC socket once a second until it exits, then
prints one JSON line with the last values: frame drops, decoder frame drops,
hwdec-current, frames shown, duration. Standard library only.

Usage: python3 mpv_stats.py /path/to/mpv.sock
"""

import json
import socket
import sys
import time

PROPS = ["frame-drop-count", "decoder-frame-drop-count", "hwdec-current",
         "estimated-frame-number", "duration", "playback-time"]


def main() -> int:
    path = sys.argv[1]
    deadline = time.monotonic() + 15
    sock = None
    while time.monotonic() < deadline:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            s.connect(path)
            sock = s
            break
        except OSError:
            s.close()
            time.sleep(0.2)
    last = {p: None for p in PROPS}
    if sock is None:
        print(json.dumps({"error": "no IPC socket", **last}))
        return 1
    sock.settimeout(1.0)
    buf = b""
    while True:
        try:
            for i, prop in enumerate(PROPS):
                sock.sendall((json.dumps({"command": ["get_property", prop], "request_id": i}) + "\n").encode())
        except OSError:
            break
        end = time.monotonic() + 1.0
        closed = False
        while time.monotonic() < end:
            try:
                chunk = sock.recv(65536)
            except socket.timeout:
                break
            except OSError:
                closed = True
                break
            if not chunk:
                closed = True
                break
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                rid = msg.get("request_id")
                if isinstance(rid, int) and 0 <= rid < len(PROPS) and msg.get("error") == "success":
                    last[PROPS[rid]] = msg.get("data")
        if closed:
            break
    print(json.dumps(last))
    return 0


if __name__ == "__main__":
    sys.exit(main())
