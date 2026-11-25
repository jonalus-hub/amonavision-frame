#!/usr/bin/env python3
"""
The frame's LAN dashboard (web/app.py), served next to the simulator.

It is the same code the frame runs. Only what would act on this machine
instead of on a frame is adapted, from the outside:

  - "Run diagnostics" is blocked: it runs test_setup.sh, which can restart
    services and the network with sudo on whatever machine runs it.
  - Service states report the simulator's own systemd units, since the
    frame's units don't exist here.
  - A banner says which numbers are this machine's (CPU, RAM, disk, Tailscale).

Settings saved from the Config page land in config.txt of the checkout being
run. The password comes from WEB_PASSWORD in the environment (see
dashboard.env.example), exactly as on the frame: it refuses to start without one.
"""
import importlib.util
import os
import re
import sys
from pathlib import Path

from flask import flash, redirect, request, url_for
from markupsafe import escape

SIM_DIR = Path(__file__).resolve().parent
SRC = Path(os.environ.get("AMONA_SRC", SIM_DIR.parent)).resolve()
BIND = os.environ.get("SIM_BIND", "127.0.0.1")

if Path("/etc/systemd/system/amonavision.service").exists() and os.environ.get("SIM_ALLOW_ON_FRAME") != "1":
    sys.exit("This machine runs a real AmonaVision frame, which has its own dashboard. "
             "Run the simulator on another machine (SIM_ALLOW_ON_FRAME=1 overrides this check).")
PORT = int(os.environ.get("SIM_DASHBOARD_PORT", "8080"))
SIM_PORT = int(os.environ.get("SIM_HTTP_PORT", "8099"))

# The frame's unit -> the simulator's unit that plays its part.
SIM_UNITS = {
    "amonavision.service": "amonavision-sim.service",
    "amonavision-web.service": "amonavision-sim-web.service",
}

os.chdir(SRC)
sys.path.insert(0, str(SRC))
# Loaded under its own name: as "app" it would shadow the project's app/ package.
_spec = importlib.util.spec_from_file_location("frame_dashboard", SRC / "web" / "app.py")
dash = importlib.util.module_from_spec(_spec)
sys.modules["frame_dashboard"] = dash        # so Flask finds web/templates next to it
_spec.loader.exec_module(dash)               # exits if WEB_PASSWORD isn't set, like on the frame
app = dash.app


def _service_status(unit):
    sim_unit = SIM_UNITS.get(unit)
    if sim_unit is None:
        return {"active": "n/a", "sub": "not simulated", "restarts": "-", "since": "-"}
    out = dash.run_cmd(["systemctl", "--user", "show", sim_unit, "-p",
                        "ActiveState,SubState,NRestarts,ActiveEnterTimestamp"])
    kv = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
    return {"active": kv.get("ActiveState", "?"), "sub": kv.get("SubState", "?"),
            "restarts": kv.get("NRestarts", "?"), "since": kv.get("ActiveEnterTimestamp", "") or "?"}


dash.service_status = _service_status
_frame_network_link = dash.network_link


def _network_link():
    # The frame's classification would call this machine's wired NIC "the 4G dongle".
    link = _frame_network_link()
    if link.get("kind") != "none":
        link = {**link, "kind": "server", "label": f"this machine ({link.get('iface')})"}
    return link


dash.network_link = _network_link


@app.before_request
def _block_host_actions():
    # Registered after the dashboard's own auth check, so it never runs unauthenticated.
    if request.method == "POST" and request.path == "/debug" and request.form.get("action") == "diag":
        flash("Diagnostics are not available in the simulator: test_setup.sh would run "
              "on this machine, not on a frame.", "error")
        return redirect(url_for("debug_page"))
    return None


@app.after_request
def _simulator_banner(resp):
    if resp.status_code == 200 and resp.mimetype == "text/html" and not resp.direct_passthrough:
        host = escape(request.host.split(":")[0])   # Host header: never trusted into HTML
        banner = (
            '<div class="flash" style="background:#3a321833;color:#f0d58a">'
            "Simulator: CPU, RAM, disk, uptime, network and Tailscale are this machine's, "
            "not a Raspberry Pi's. The fan and the watchdogs are not simulated. "
            f'<a href="http://{host}:{SIM_PORT}/" style="color:#ffd76a">Back to the frame</a></div>'
        )
        page = re.sub(r"<main\b[^>]*>", lambda m: m.group(0) + "\n" + banner, resp.get_data(as_text=True), count=1)
        resp.set_data(page)
    return resp


if __name__ == "__main__":
    from werkzeug.serving import run_simple

    run_simple(BIND, PORT, app, threaded=True, use_reloader=False, use_debugger=False)
