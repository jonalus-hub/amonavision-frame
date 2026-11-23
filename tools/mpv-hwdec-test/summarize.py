#!/usr/bin/env python3
"""Builds summary.csv and summary.txt from the raw files bench.sh leaves in each
variant's directory:

    meta.json        variant, player, hwdec, vo, exit_code, throttled_end
    pidstat.txt      LC_ALL=C pidstat -h -u -r -p PID 1
    temps.txt        "<unix time> <°C>" every 5 s
    stats.json       mpv_stats.py (mpv) or ffpyplayer_bench.py (ffpyplayer)
    mpv.log          mpv --log-file (mpv only)

Usage: python3 summarize.py OUT_DIR
Standard library only.
"""

import csv
import json
import sys
from pathlib import Path

COLUMNS = ["variant", "player", "hwdec", "vo", "exit_code", "seconds",
           "cpu_avg_pct", "cpu_max_pct", "rss_max_mb",
           "temp_start_c", "temp_max_c", "temp_end_c", "throttled_end",
           "dropped_frames", "decoder_dropped_frames", "frames_shown",
           "hwdec_current", "hw_decoding_in_log"]
HWDEC_LOG_MARK = "Using hardware decoding"


def parse_pidstat(text: str) -> dict:
    """%CPU and RSS per sample, found by header name (aligned from the right,
    so the time format doesn't matter)."""
    header = None
    cpu, rss = [], []
    for line in text.splitlines():
        parts = line.split()
        if not parts:
            continue
        if parts[0] == "#":
            header = parts[1:]
            continue
        if header is None or len(parts) < 3 or not parts[-1] or parts[0].startswith(("Linux", "Average")):
            continue
        try:
            if "%CPU" in header:
                cpu.append(float(parts[len(parts) - (len(header) - header.index("%CPU"))]))
            if "RSS" in header:
                rss.append(float(parts[len(parts) - (len(header) - header.index("RSS"))]))
        except (ValueError, IndexError):
            continue
    return {
        "samples": len(cpu),
        "cpu_avg_pct": round(sum(cpu) / len(cpu), 1) if cpu else None,
        "cpu_max_pct": round(max(cpu), 1) if cpu else None,
        "rss_max_mb": round(max(rss) / 1024, 1) if rss else None,
    }


def parse_temps(text: str) -> dict:
    temps = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 2:
            try:
                temps.append(float(parts[1]))
            except ValueError:
                pass
    return {
        "temp_start_c": temps[0] if temps else None,
        "temp_max_c": max(temps) if temps else None,
        "temp_end_c": temps[-1] if temps else None,
    }


def _read(path: Path) -> str:
    try:
        return path.read_text(errors="replace")
    except OSError:
        return ""


def _json(path: Path) -> dict:
    for line in reversed(_read(path).splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except ValueError:
                return {}
    return {}


def summarize_variant(d: Path) -> dict:
    meta = _json(d / "meta.json")
    stats = _json(d / "stats.json")
    row = {c: None for c in COLUMNS}
    row.update({k: meta.get(k) for k in ("variant", "player", "hwdec", "vo", "exit_code", "throttled_end")})
    p = parse_pidstat(_read(d / "pidstat.txt"))
    row.update({k: p[k] for k in ("cpu_avg_pct", "cpu_max_pct", "rss_max_mb")})
    row["seconds"] = p["samples"] or None
    row.update(parse_temps(_read(d / "temps.txt")))
    if meta.get("player") == "mpv":
        row["dropped_frames"] = stats.get("frame-drop-count")
        row["decoder_dropped_frames"] = stats.get("decoder-frame-drop-count")
        row["frames_shown"] = stats.get("estimated-frame-number")
        row["hwdec_current"] = stats.get("hwdec-current")
        row["hw_decoding_in_log"] = HWDEC_LOG_MARK in _read(d / "mpv.log")
    else:
        row["frames_shown"] = stats.get("frames_shown")
        if stats.get("seconds"):
            row["seconds"] = stats["seconds"]
    return row


def _fmt(v, unit=""):
    return "—" if v is None else f"{v}{unit}"


def write_summary(out: Path) -> list[dict]:
    rows = [summarize_variant(d) for d in sorted(p for p in out.iterdir() if (p / "meta.json").exists())]
    with open(out / "summary.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        w.writeheader()
        w.writerows(rows)
    lines = [
        "Resumen del benchmark de vídeo (mismo vídeo en cada variante)",
        "%CPU = pidstat del proceso del reproductor: 100 = un núcleo entero (la Pi 4 tiene 4, máx. 400).",
        "get_throttled: bits 0-3 = ahora mismo, bits 16-19 = desde el arranque (0x0 = nunca).",
        "",
    ]
    for r in rows:
        lines += [
            f"[{r['variant']}] {r['player']}  hwdec={_fmt(r['hwdec'])}  vo={_fmt(r['vo'])}  exit={_fmt(r['exit_code'])}",
            f"  CPU media {_fmt(r['cpu_avg_pct'], ' %')}, máx {_fmt(r['cpu_max_pct'], ' %')}  ·  "
            f"RAM (RSS) máx {_fmt(r['rss_max_mb'], ' MB')}  ·  {_fmt(r['seconds'], ' s')}",
            f"  Temperatura {_fmt(r['temp_start_c'], ' °C')} → máx {_fmt(r['temp_max_c'], ' °C')}"
            f" → final {_fmt(r['temp_end_c'], ' °C')}  ·  throttled al final {_fmt(r['throttled_end'])}",
        ]
        if r["player"] == "mpv":
            lines.append(
                f"  Fotogramas perdidos {_fmt(r['dropped_frames'])} (decodificador {_fmt(r['decoder_dropped_frames'])})"
                f"  ·  mostrados ~{_fmt(r['frames_shown'])}  ·  hwdec-current={_fmt(r['hwdec_current'])}"
                f"  ·  'Using hardware decoding' en el log: {'sí' if r['hw_decoding_in_log'] else 'no'}")
        else:
            lines.append(f"  Fotogramas mostrados {_fmt(r['frames_shown'])} (ffpyplayer no cuenta los perdidos)")
        lines.append("")
    (out / "summary.txt").write_text("\n".join(lines))
    return rows


if __name__ == "__main__":
    out = Path(sys.argv[1])
    write_summary(out)
    print((out / "summary.txt").read_text())
