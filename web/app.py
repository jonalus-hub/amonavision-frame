"""
AmonaVision — LAN dashboard
===========================
A small, read-mostly web dashboard for the family to inspect the frame:
project/system state, every media file and its weight, the whole database,
plus a whitelisted config editor and a few debug actions.

Design constraints (see amonavision-web.service):
  * Runs as its OWN systemd service, fully independent of amonavision.service.
    A crash / hang / leak here cannot touch the frame.
  * Read-only by default. The SQLite DB is opened mode=ro + query_only.
  * The ONLY things it ever writes are config.txt (atomic, comment-preserving,
    whitelisted keys only) and /tmp/amonavision_restart (the soft-restart flag
    viewer.py already watches).
  * No pygame / RPi.GPIO / anything the frame owns is imported. The one
    exception is app.scoring — pure math (novelty/anti-repetition/interest
    formulas), zero I/O, no hardware — imported read-only to show the
    slideshow's actual picking logic on /db, so scoring bugs are visible
    from the dashboard instead of requiring an SSH session to diagnose.
  * Every route is wrapped so an error returns a 500 page and the server
    stays up.

Auth: HTTP Basic, one shared password from .env (WEB_PASSWORD). If it is not
set the service refuses to start — config/debug actions are never exposed
without a password.
"""

from __future__ import annotations

import hmac
import html
import logging
import math
import os
import re
import sqlite3
import subprocess
import time
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from flask import (
    Flask,
    Response,
    abort,
    flash,
    redirect,
    render_template,
    request,
    send_from_directory,
    url_for,
)

# ---------------------------------------------------------------------------
# Paths & config
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent.parent
# Load app/scoring.py directly by file path rather than "from app import
# scoring": this script is itself named app.py, and Python's namespace-
# package resolution finds THIS file (web/app.py) as a regular module named
# "app" before it finishes considering the app/ package at the project root
# — even with PROJECT_ROOT first on sys.path — so a plain package import
# silently imports the wrong "app" and fails. Loading by path sidesteps
# package-name resolution entirely. Defensive: a bad deploy of scoring.py
# must not take the whole dashboard down with it (see scoring_table()'s
# try/except and the scoring_engine is None guard).
try:
    import importlib.util as _ilu

    _spec = _ilu.spec_from_file_location(
        "amonavision_scoring", PROJECT_ROOT / "app" / "scoring.py"
    )
    scoring_engine = _ilu.module_from_spec(_spec)
    _spec.loader.exec_module(scoring_engine)
except Exception:  # noqa: BLE001
    scoring_engine = None

DB_PATH = PROJECT_ROOT / "app" / "db" / "amonavision.db"
MEDIA_ROOT = PROJECT_ROOT / "app" / "media"
MEDIA_DIRS = ("received", "processed", "recordings", "thumbnails")
LOG_DIR = PROJECT_ROOT / "app" / "logs"
CONFIG_FILE = PROJECT_ROOT / "config.txt"
RESTART_FLAG = Path("/tmp/amonavision_restart")  # matches viewer.py / bot.py

LOG_FILES = {
    "app": LOG_DIR / "app.log",
    "viewer": LOG_DIR / "viewer.log",
    "startup": LOG_DIR / "startup.log",
    "setup": PROJECT_ROOT / "setup.log",
    "dongle": LOG_DIR / "dongle.log",
}

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
VIDEO_EXT = {".mp4", ".mov", ".mkv", ".webm"}
AUDIO_EXT = {".ogg", ".wav", ".mp3", ".m4a", ".opus"}

# Config keys the dashboard is allowed to change. Anything that could brick
# the frame (GPIO pins, resolution, fullscreen, audio device) is deliberately
# absent. value: ("section", "key", validator, input_type).
CONFIG_WHITELIST = [
    ("Viewer", "slideshow_interval", "float_pos", "number"),
    ("Scoring", "enabled", "bool", "checkbox"),
    ("System", "daily_restart", "bool", "checkbox"),
    ("Overlay", "enabled", "bool", "checkbox"),
    ("Overlay", "button_offset_cm", "float_nonneg", "number"),
    ("Overlay", "bottom_margin_px", "int_nonneg", "number"),
    ("Overlay", "pill_font_size", "int_pos", "number"),
    ("Overlay", "hide_after_seconds", "float_nonneg", "number"),
    ("Overlay", "tutorial_mode", "bool", "checkbox"),
]

load_dotenv(PROJECT_ROOT / ".env")
WEB_PASSWORD = (os.getenv("WEB_PASSWORD") or "").strip()
if not WEB_PASSWORD:
    raise SystemExit(
        "WEB_PASSWORD is not set in .env — refusing to start the dashboard "
        "(it would expose config/debug actions without a password)."
    )

# Quieten Werkzeug's per-request access log so it can't spam the journal.
logging.getLogger("werkzeug").setLevel(logging.WARNING)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 64 * 1024  # only tiny form posts, never uploads
app.secret_key = hmac.new(b"amonavision-web", WEB_PASSWORD.encode(), "sha256").digest()
app.jinja_env.trim_blocks = True
app.jinja_env.lstrip_blocks = True

START_TIME = time.time()


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------
@app.before_request
def _require_auth():
    if request.path == "/healthz":
        return None
    auth = request.authorization
    if auth and auth.password and hmac.compare_digest(auth.password, WEB_PASSWORD):
        return None
    return Response(
        "Authentication required.",
        401,
        {"WWW-Authenticate": 'Basic realm="AmonaVision"'},
    )


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def human_size(n: int | float | None) -> str:
    if not n:
        return "0 B"
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def run_cmd(args: list[str], timeout: float = 5.0) -> str:
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return (r.stdout or r.stderr or "").strip()
    except Exception:
        return ""


def read_first_line(path: str, default: str = "") -> str:
    try:
        with open(path, "r", errors="replace") as f:
            return f.readline().strip()
    except Exception:
        return default


def tail(path: Path, n: int) -> str:
    """Last n lines without loading the whole file."""
    try:
        size = path.stat().st_size
        with open(path, "rb") as f:
            f.seek(max(0, size - 256 * 1024))
            data = f.read()
        lines = data.decode("utf-8", "replace").splitlines()
        return "\n".join(lines[-n:])
    except FileNotFoundError:
        return "(log not found yet)"
    except Exception as e:  # noqa: BLE001
        return f"(error reading log: {e})"


# ---------- database (read-only) ----------
def db_query(sql: str, params: tuple = (), *, limit: int | None = None):
    uri = f"file:{DB_PATH.as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=5)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only = 1;")
        cur = conn.execute(sql, params)
        rows = cur.fetchmany(limit) if limit else cur.fetchall()
        cols = [c[0] for c in cur.description] if cur.description else []
        return cols, [dict(r) for r in rows]
    finally:
        conn.close()


def db_stats() -> dict:
    try:
        _, rows = db_query(
            "SELECT status, COUNT(*) c FROM sessions GROUP BY status"
        )
        by_status = {r["status"]: r["c"] for r in rows}
        _, tot = db_query("SELECT COUNT(*) c FROM sessions")
        _, tf = db_query("SELECT COUNT(*) c FROM files")
        _, ty = db_query("SELECT type, COUNT(*) c FROM sessions GROUP BY type")
        by_type = {r["type"]: r["c"] for r in ty}
        return {
            "sessions_total": tot[0]["c"],
            "files_total": tf[0]["c"],
            "by_status": by_status,
            "by_type": by_type,
            "ok": True,
        }
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}


def _scoring_settings() -> dict:
    """Mirror viewer.py's [Scoring] parsing exactly (same keys, same
    fallbacks) so the dashboard scores with the identical settings the
    running frame uses — not a second, potentially-drifted copy of them."""
    import configparser

    cp = configparser.ConfigParser()
    try:
        cp.read(CONFIG_FILE, encoding="utf-8")
    except Exception:
        pass
    g = lambda getter, *a, **kw: getattr(cp, getter)(*a, **kw)  # noqa: E731
    return {
        "enabled": g("getboolean", "Scoring", "enabled", fallback=True),
        "weight_novelty": g("getfloat", "Scoring", "weight_novelty", fallback=1.0),
        "weight_anti_repetition": g("getfloat", "Scoring", "weight_anti_repetition", fallback=1.0),
        "weight_sender_balance": g("getfloat", "Scoring", "weight_sender_balance", fallback=1.0),
        "weight_explicit_interest": g("getfloat", "Scoring", "weight_explicit_interest", fallback=1.0),
        "weight_implicit_interest": g("getfloat", "Scoring", "weight_implicit_interest", fallback=1.0),
        "novelty_halflife_days": g("getfloat", "Scoring", "novelty_halflife_days", fallback=3.0),
        "suppression_hours": g("getfloat", "Scoring", "suppression_hours", fallback=24.0),
        "sender_balance_window": g("getint", "Scoring", "sender_balance_window", fallback=20),
        "explicit_halflife_days": g("getfloat", "Scoring", "explicit_halflife_days", fallback=30.0),
        "implicit_dwell_threshold_seconds": g(
            "getfloat", "Scoring", "implicit_dwell_threshold_seconds", fallback=30.0
        ),
    }


def scoring_table() -> dict:
    """Score every session currently eligible for auto-advance, using the
    real app.scoring engine — the exact same code and config the viewer
    runs, so a scoring bug (e.g. one session perpetually winning) is visible
    here instead of requiring an SSH session + a throwaway script to catch."""
    if scoring_engine is None:
        return {"ok": False, "error": "app.scoring could not be imported", "rows": []}
    try:
        settings = _scoring_settings()

        # Same eligibility filter as DatabaseManager.get_all_sessions_for_scoring().
        cols, rows = db_query(
            """
            SELECT s.id, s.type, s.status, s.sender_id, s.created_at,
                   s.last_shown_at, s.show_count, s.total_dwell_seconds,
                   s.revisit_count, s.voice_reply_count, s.last_voice_reply_at
            FROM sessions s
            WHERE s.status NOT IN ('PROCESSING', 'HIDDEN')
              AND s.id NOT IN (SELECT session_id FROM files WHERE is_optimized = 0)
            """
        )
        # Same "who counts as recently shown" as DatabaseManager.get_recent_senders().
        _, sender_rows = db_query(
            """
            SELECT sender_id FROM sessions
            WHERE last_shown_at IS NOT NULL AND status NOT IN ('PROCESSING', 'HIDDEN')
            ORDER BY last_shown_at DESC LIMIT ?
            """,
            (settings["sender_balance_window"],),
        )
        recent_senders = [r["sender_id"] for r in sender_rows]

        now_ts = time.time()
        for s in rows:
            try:
                s["created_ts"] = datetime.fromisoformat(s["created_at"]).timestamp()
            except Exception:
                s["created_ts"] = 0.0

        # Same weights + tickets build_round() would deal right now (it
        # additionally excludes whichever session is currently on screen,
        # which this table has no way to know — so this is "if a round were
        # built this instant with nothing excluded").
        weights = scoring_engine.selection_weights(rows, now_ts, recent_senders, settings)
        tickets = scoring_engine.tickets_for(weights)
        total_tickets = sum(tickets)

        scored = []
        for s, w, tk in zip(rows, weights, tickets):
            shows = max(s.get("show_count") or 0, 1)
            breakdown = scoring_engine.score_breakdown(s, now_ts, recent_senders, settings)
            scored.append({
                "id": s["id"], "type": s["type"], "status": s["status"],
                "sender_id": s["sender_id"],
                **breakdown,  # novelty, anti_repetition, sender_balance, explicit, implicit, total
                "tickets": tk,
                "probability": (tk / total_tickets) if total_tickets > 0 else (1 / len(rows) if rows else 0),
                "show_count": s.get("show_count") or 0,
                "avg_dwell_s": round((s.get("total_dwell_seconds") or 0.0) / shows, 1),
            })
        scored.sort(key=lambda r: r["total"], reverse=True)
        return {
            "ok": True, "enabled": settings["enabled"], "weights": settings,
            "recent_senders": recent_senders, "rows": scored,
            "max_tickets": scoring_engine.MAX_TICKETS_PER_SESSION,
        }
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e), "rows": []}


# ---------- system stats (no psutil; read /proc like the /pi bot command) ----------
def system_stats() -> dict:
    s: dict = {}

    # CPU temp
    raw = read_first_line("/sys/class/thermal/thermal_zone0/temp", "0")
    s["temp_c"] = round(int(raw) / 1000, 1) if raw.isdigit() else None

    # RAM
    mem = {}
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                k, _, rest = line.partition(":")
                mem[k.strip()] = int(rest.split()[0])  # kB
        total = mem.get("MemTotal", 0) // 1024
        avail = mem.get("MemAvailable", 0) // 1024
        s["ram_used_mb"] = total - avail
        s["ram_total_mb"] = total
        s["ram_pct"] = round((total - avail) / total * 100) if total else 0
    except Exception:
        s["ram_used_mb"] = s["ram_total_mb"] = s["ram_pct"] = None

    # Disk
    try:
        import shutil

        du = shutil.disk_usage("/")
        s["disk_used"] = du.used
        s["disk_total"] = du.total
        s["disk_pct"] = round(du.used / du.total * 100)
    except Exception:
        s["disk_used"] = s["disk_total"] = s["disk_pct"] = None

    # Uptime / load
    up = read_first_line("/proc/uptime", "0 0").split()
    try:
        secs = int(float(up[0]))
        d, rem = divmod(secs, 86400)
        h, rem = divmod(rem, 3600)
        m = rem // 60
        s["uptime"] = (f"{d}d " if d else "") + f"{h}h {m}m"
    except Exception:
        s["uptime"] = "?"
    s["loadavg"] = read_first_line("/proc/loadavg", "? ? ?").split()[:3]

    return s


_PWM_FAN = Path("/sys/class/pwm/pwmchip0/pwm0")


def fan_speed() -> dict:
    """Current PWM fan duty cycle, read straight from the same sysfs files
    fan_control.py itself drives — a percentage, not RPM: this is a 3-wire
    PWM fan with no tachometer line wired up, so duty cycle is the only
    "speed" signal there is. World-readable (0644, group gpio), no root
    needed."""
    try:
        period = int((_PWM_FAN / "period").read_text().strip())
        duty = int((_PWM_FAN / "duty_cycle").read_text().strip())
        enabled = (_PWM_FAN / "enable").read_text().strip() == "1"
        pct = round(duty / period * 100) if period and enabled else 0
        return {"ok": True, "enabled": enabled, "pct": pct}
    except Exception:
        return {"ok": False, "enabled": False, "pct": None}


def service_status(unit: str) -> dict:
    out = run_cmd(
        ["systemctl", "show", unit, "-p",
         "ActiveState,SubState,NRestarts,ActiveEnterTimestamp"]
    )
    kv = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
    return {
        "active": kv.get("ActiveState", "?"),
        "sub": kv.get("SubState", "?"),
        "restarts": kv.get("NRestarts", "?"),
        "since": kv.get("ActiveEnterTimestamp", "") or "?",
    }


BOT_HEARTBEAT = Path("/tmp/amonavision_bot_heartbeat")


def bot_last_seen() -> str:
    """How long since the bot's last successful getUpdates poll.

    Read from the heartbeat file the bot touches on every successful poll (the
    same one tools/bot-watchdog.sh uses). app.log no longer has per-poll
    getUpdates lines since httpx was silenced (they carried the bot token), so
    scanning it for them always ended in "unknown".
    """
    try:
        age = time.time() - BOT_HEARTBEAT.stat().st_mtime
        if age < 90:
            return f"connected ({int(age)}s ago)"
        if age < 3600:
            return f"stale ({int(age // 60)} min ago)"
        return f"OFFLINE ({int(age // 3600)} h ago)"
    except Exception:
        return "unknown"


def tailscale_state() -> str:
    out = run_cmd(["tailscale", "status", "--json"], timeout=6)
    if not out:
        return "unknown"
    try:
        import json

        d = json.loads(out)
        self_ = d.get("Self", {})
        ips = self_.get("TailscaleIPs", [])
        return ("online " + (ips[0] if ips else "")) if self_.get("Online") else "OFFLINE"
    except Exception:
        return "unknown"


def network_link() -> dict:
    """Which physical link default internet traffic is actually going out on
    right now — 4G is meant to be primary at the deployment site (no Wi-Fi
    there at all), Wi-Fi only a fallback for testing/maintenance (setup.sh
    Step 8b/8c). `ip route get` is a pure routing-table lookup, no packet
    sent, so this is cheap and works even fully offline. Classification
    mirrors setup.sh/test_setup.sh exactly: a Wi-Fi-type NM device is Wi-Fi;
    eth0 is the Pi's built-in wired NIC; any other ethernet-type device (or
    wwan*/ppp*) is the 4G dongle (HiLink/NCM/RNDIS sticks enumerate as plain
    USB-Ethernet, not as a modem)."""
    out = run_cmd(["ip", "route", "get", "8.8.8.8"], timeout=3)
    m = re.search(r"\bdev (\S+)", out)
    dev = m.group(1) if m else None
    if not dev:
        return {"iface": None, "label": "no connection", "kind": "none"}

    nm_out = run_cmd(["nmcli", "-t", "-f", "DEVICE,TYPE", "device", "status"], timeout=3)
    types = dict(line.split(":", 1) for line in nm_out.splitlines() if ":" in line)
    dtype = types.get(dev, "")

    if dtype == "wifi":
        return {"iface": dev, "label": f"WiFi ({dev})", "kind": "wifi"}
    if dev == "eth0":
        return {"iface": dev, "label": f"Ethernet ({dev})", "kind": "ethernet"}
    if dtype == "ethernet" or dtype == "gsm" or dev.startswith(("wwan", "ppp")):
        return {"iface": dev, "label": f"4G ({dev})", "kind": "4g"}
    return {"iface": dev, "label": dev, "kind": "other"}


# ---------- media scan ----------
def _kind(name: str) -> str:
    ext = Path(name).suffix.lower()
    if ext in IMAGE_EXT:
        return "image"
    if ext in VIDEO_EXT:
        return "video"
    if ext in AUDIO_EXT:
        return "audio"
    return "other"


def scan_media() -> dict:
    # session id per relative file path, from the DB
    path_to_session: dict[str, int] = {}
    try:
        _, rows = db_query("SELECT session_id, file_path FROM files")
        for r in rows:
            path_to_session[str(r["file_path"]).replace("\\", "/")] = r["session_id"]
    except Exception:
        pass

    folders = []
    grand_count = grand_bytes = 0
    for d in MEDIA_DIRS:
        base = MEDIA_ROOT / d
        items = []
        tot = 0
        if base.is_dir():
            with os.scandir(base) as it:
                for e in it:
                    if not e.is_file() or e.name.startswith("."):
                        continue
                    st = e.stat()
                    rel = f"{d}/{e.name}"
                    items.append({
                        "name": e.name,
                        "rel": rel,
                        "kind": _kind(e.name),
                        "size": st.st_size,
                        "size_h": human_size(st.st_size),
                        "mtime": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M"),
                        "session": path_to_session.get(rel),
                    })
                    tot += st.st_size
        items.sort(key=lambda x: x["mtime"], reverse=True)
        folders.append({"name": d, "files": items, "count": len(items),
                        "bytes": tot, "bytes_h": human_size(tot)})
        grand_count += len(items)
        grand_bytes += tot
    return {"folders": folders, "count": grand_count,
            "bytes": grand_bytes, "bytes_h": human_size(grand_bytes)}


# ---------- config (whitelisted, comment-preserving, atomic) ----------
_VALIDATORS = {
    # float(...) also parses "inf"/"nan"; reject them — an infinite
    # slideshow_interval or button_offset_cm would hang or crash the viewer.
    "float_pos": lambda v: math.isfinite(float(v)) and float(v) > 0,
    "float_nonneg": lambda v: math.isfinite(float(v)) and float(v) >= 0,
    "int_nonneg": lambda v: int(v) >= 0 and str(int(v)) == v.strip(),
    "int_pos": lambda v: int(v) > 0 and str(int(v)) == v.strip(),
    "bool": lambda v: v in ("True", "False"),
}


def read_config_text() -> str:
    try:
        return CONFIG_FILE.read_text(encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        return f"# (could not read config.txt: {e})\n"


def current_config_values() -> dict:
    import configparser

    cp = configparser.ConfigParser()
    try:
        cp.read(CONFIG_FILE, encoding="utf-8")
    except Exception:
        pass
    out = {}
    for section, key, _validator, _typ in CONFIG_WHITELIST:
        try:
            out[(section, key)] = cp.get(section, key)
        except Exception:
            out[(section, key)] = ""
    return out


def _set_line(text: str, section: str, key: str, value: str) -> str:
    """Surgically replace `key = value` inside [section], keeping comments."""
    lines = text.splitlines(keepends=True)
    sec_re = re.compile(r"^\s*\[(?P<name>[^\]]+)\]\s*$")
    key_re = re.compile(rf"^(?P<indent>\s*){re.escape(key)}\s*=", re.I)
    cur = None
    sec_start = None
    for i, ln in enumerate(lines):
        m = sec_re.match(ln)
        if m:
            cur = m.group("name").strip()
            if cur == section:
                sec_start = i
            continue
        if cur == section and key_re.match(ln):
            lines[i] = f"{key} = {value}\n"
            return "".join(lines)
    # key not found
    newline = f"{key} = {value}\n"
    if sec_start is not None:
        lines.insert(sec_start + 1, newline)
        return "".join(lines)
    # section not found
    tail_nl = "" if text.endswith("\n") else "\n"
    return text + f"{tail_nl}\n[{section}]\n{newline}"


def write_config(updates: dict[tuple[str, str], str]) -> None:
    text = read_config_text()
    for (section, key), value in updates.items():
        text = _set_line(text, section, key, value)
    tmp = CONFIG_FILE.with_name(CONFIG_FILE.name + f".tmp.{os.getpid()}")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, CONFIG_FILE)  # atomic on the same filesystem


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.route("/healthz")
def healthz():
    return "ok", 200


@app.route("/")
def overview():
    return render_template(
        "overview.html",
        frame=service_status("amonavision.service"),
        fan=service_status("fan-control.service"),
        fan_speed=fan_speed(),
        web_uptime=int(time.time() - START_TIME),
        bot=bot_last_seen(),
        tailscale=tailscale_state(),
        network=network_link(),
        watchdog=_read_state_file("/var/lib/amonavision/watchdog_state"),
        healthcheck=_read_state_file("/var/lib/amonavision/healthcheck_state"),
        sysinfo=system_stats(),
        dbstats=db_stats(),
        media_summary=_media_summary(),
    )


def _read_state_file(path: str) -> dict:
    d = {}
    try:
        with open(path) as f:
            for line in f:
                k, _, v = line.strip().partition("=")
                if k:
                    d[k] = v
    except Exception:
        pass
    return d


def _media_summary() -> dict:
    try:
        m = scan_media()
        return {"count": m["count"], "bytes_h": m["bytes_h"]}
    except Exception:
        return {"count": "?", "bytes_h": "?"}


@app.route("/media")
def media():
    return render_template("media.html", media=scan_media())


@app.route("/media/file/<path:relpath>")
def media_file(relpath: str):
    # first segment must be a known media dir; send_from_directory rejects ..
    first = relpath.split("/", 1)[0]
    if first not in MEDIA_DIRS:
        abort(404)
    full = (MEDIA_ROOT / relpath).resolve()
    if not str(full).startswith(str(MEDIA_ROOT.resolve()) + os.sep):
        abort(404)
    resp = send_from_directory(MEDIA_ROOT, relpath, max_age=3600)
    return resp


@app.route("/db", methods=["GET", "POST"])
def database():
    sql = ""
    sql_cols: list = []
    sql_rows: list = []
    sql_error = None
    if request.method == "POST":
        sql = (request.form.get("sql") or "").strip()
        try:
            low = sql.lower().lstrip()
            if ";" in sql.rstrip().rstrip(";"):
                raise ValueError("Only a single statement is allowed.")
            if not (low.startswith("select") or low.startswith("with")
                    or low.startswith("pragma")):
                raise ValueError("Only SELECT / WITH / PRAGMA queries are allowed.")
            sql_cols, sql_rows = db_query(sql, limit=500)
        except Exception as e:  # noqa: BLE001
            sql_error = str(e)

    try:
        sess_cols, sessions = db_query(
            "SELECT * FROM sessions ORDER BY id DESC LIMIT 500"
        )
    except Exception as e:  # noqa: BLE001
        sess_cols, sessions = [], []
        flash(f"Could not read 'sessions': {e}", "error")
    try:
        _, files = db_query("SELECT * FROM files ORDER BY session_id DESC, id DESC")
        files_by_session: dict[int, list] = {}
        for f in files:
            files_by_session.setdefault(f["session_id"], []).append(f)
    except Exception:
        files_by_session = {}

    wal = DB_PATH.with_name(DB_PATH.name + "-wal")
    return render_template(
        "db.html",
        stats=db_stats(),
        sess_cols=sess_cols,
        sessions=sessions,
        files_by_session=files_by_session,
        db_size=human_size(DB_PATH.stat().st_size if DB_PATH.exists() else 0),
        wal_size=human_size(wal.stat().st_size if wal.exists() else 0),
        sql=sql, sql_cols=sql_cols, sql_rows=sql_rows, sql_error=sql_error,
        scoring=scoring_table(),
    )


@app.route("/config", methods=["GET", "POST"])
def config_page():
    if request.method == "POST":
        updates: dict[tuple[str, str], str] = {}
        errors = []
        for section, key, validator, typ in CONFIG_WHITELIST:
            if typ == "checkbox":
                raw = "True" if request.form.get(f"{section}.{key}") == "on" else "False"
            else:
                raw = (request.form.get(f"{section}.{key}") or "").strip()
            try:
                if not _VALIDATORS[validator](raw):
                    raise ValueError
                updates[(section, key)] = raw
            except Exception:
                errors.append(f"{section}.{key}: invalid value \"{raw}\"")
        if errors:
            for e in errors:
                flash(e, "error")
        else:
            try:
                write_config(updates)
                flash("config.txt updated.", "ok")
                if request.form.get("apply") == "on":
                    RESTART_FLAG.touch()
                    flash("Soft restart of the frame requested (~15 s).", "ok")
            except Exception as e:  # noqa: BLE001
                flash(f"Could not write config.txt: {e}", "error")
        return redirect(url_for("config_page"))

    return render_template(
        "config.html",
        whitelist=CONFIG_WHITELIST,
        values=current_config_values(),
        raw=read_config_text(),
    )


@app.route("/debug", methods=["GET", "POST"])
def debug_page():
    diag_output = None
    if request.method == "POST":
        action = request.form.get("action")
        if action == "restart":
            try:
                RESTART_FLAG.touch()
                flash("Soft restart of the frame requested (~15 s).", "ok")
            except Exception as e:  # noqa: BLE001
                flash(f"Could not request the restart: {e}", "error")
            return redirect(url_for("debug_page"))
        if action == "diag":
            try:
                # test_setup.sh probes things this hardened service can't reach
                # on its own: the venv (needs it on PATH) and av's `systemctl
                # --user` units (need XDG_RUNTIME_DIR + the session bus address).
                # Without this the diagnostic reports false FAILs — "missing
                # packages", "PipeWire not enabled" — that would alarm the family.
                env = {
                    **os.environ,
                    "AMONAVISION_HEALTHCHECK": "1",
                    "PATH": f"{PROJECT_ROOT / 'venv' / 'bin'}:/usr/sbin:/sbin:"
                            + os.environ.get("PATH", "/usr/bin:/bin"),
                    "XDG_RUNTIME_DIR": f"/run/user/{os.getuid()}",
                    "DBUS_SESSION_BUS_ADDRESS":
                        f"unix:path=/run/user/{os.getuid()}/bus",
                }
                r = subprocess.run(
                    ["bash", str(PROJECT_ROOT / "test_setup.sh")],
                    capture_output=True, text=True, timeout=90, env=env,
                )
                diag_output = re.sub(r"\x1b\[[0-9;]*m", "", r.stdout + r.stderr)
            except subprocess.TimeoutExpired:
                diag_output = "test_setup.sh did not finish within 90 s."
            except Exception as e:  # noqa: BLE001
                diag_output = f"error: {e}"

    which = request.args.get("log", "viewer")
    if which not in LOG_FILES:
        which = "viewer"
    try:
        n = max(20, min(int(request.args.get("n", 200)), 1000))
    except Exception:
        n = 200
    return render_template(
        "debug.html",
        logs=list(LOG_FILES),
        which=which, n=n,
        log_text=tail(LOG_FILES[which], n),
        diag_output=diag_output,
    )


# ---------------------------------------------------------------------------
# Error handling — a bug shows an error page, the server keeps running
# ---------------------------------------------------------------------------
@app.errorhandler(Exception)
def _any_error(e):
    code = getattr(e, "code", 500) or 500
    return render_template("error.html", code=code, message=html.escape(str(e))), code


# ---------------------------------------------------------------------------
if __name__ == "__main__":
    from werkzeug.serving import run_simple

    run_simple(
        "0.0.0.0", 8080, app,
        threaded=True, use_reloader=False, use_debugger=False,
    )
