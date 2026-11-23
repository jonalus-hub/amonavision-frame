"""tools/mpv-hwdec-test/summarize.py: parsing what bench.sh collects."""

import csv
import importlib.util
import json

from conftest import PROJECT_ROOT

_spec = importlib.util.spec_from_file_location(
    "bench_summarize", PROJECT_ROOT / "tools" / "mpv-hwdec-test" / "summarize.py")
summarize = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(summarize)

PIDSTAT_24H = """Linux 6.12.47+rpt-rpi-v8 (AmonaVision3) \t11/23/2026 \t_aarch64_\t(4 CPU)

# Time        UID       PID    %usr %system  %guest   %wait    %CPU   CPU  minflt/s  majflt/s     VSZ     RSS   %MEM  Command
17:00:01     1000      4242   80.00   20.00    0.00    0.00  100.00     1   1500.00      0.00  812345  204800  10.85  python
17:00:02     1000      4242  150.00   30.00    0.00    0.00  180.00     2   1200.00      0.00  812345  262144  13.90  python
"""

# Same data with a 12-hour clock (two tokens for the time): parsing is aligned from the right
PIDSTAT_12H = PIDSTAT_24H.replace("17:00:01", "05:00:01 PM").replace("17:00:02", "05:00:02 PM")


def test_pidstat_cpu_and_rss():
    for text in (PIDSTAT_24H, PIDSTAT_12H):
        assert summarize.parse_pidstat(text) == {
            "samples": 2, "cpu_avg_pct": 140.0, "cpu_max_pct": 180.0, "rss_max_mb": 256.0}


def test_pidstat_empty_or_garbage():
    assert summarize.parse_pidstat("")["cpu_avg_pct"] is None
    assert summarize.parse_pidstat("Linux x\n\nnot pidstat\n")["samples"] == 0


def test_temps():
    t = summarize.parse_temps("1 50.1\n6 61.3\n11 58.0\nbad line\n")
    assert t == {"temp_start_c": 50.1, "temp_max_c": 61.3, "temp_end_c": 58.0}


def test_summary_files(tmp_path):
    a = tmp_path / "a-ffpyplayer"
    a.mkdir()
    (a / "meta.json").write_text(json.dumps({"variant": "a", "player": "ffpyplayer", "hwdec": "", "vo": "",
                                             "exit_code": 0, "throttled_end": "0x0"}))
    (a / "pidstat.txt").write_text(PIDSTAT_24H)
    (a / "temps.txt").write_text("1 50\n6 70\n")
    (a / "stats.json").write_text('{"player": "ffpyplayer", "frames_shown": 700, "seconds": 30.1}\n')
    c = tmp_path / "c-mpv-v4l2m2m-copy-gpu"
    c.mkdir()
    (c / "meta.json").write_text(json.dumps({"variant": "c", "player": "mpv", "hwdec": "v4l2m2m-copy",
                                             "vo": "gpu", "exit_code": 0, "throttled_end": "0x0"}))
    (c / "pidstat.txt").write_text(PIDSTAT_24H.replace("100.00", "12.00").replace("180.00", "14.00"))
    (c / "temps.txt").write_text("1 50\n6 55\n")
    (c / "stats.json").write_text(json.dumps({"frame-drop-count": 3, "decoder-frame-drop-count": 0,
                                              "hwdec-current": "v4l2m2m-copy", "estimated-frame-number": 750}))
    (c / "mpv.log").write_text("[vd] Using hardware decoding (v4l2m2m-copy).\n")

    rows = summarize.write_summary(tmp_path)
    assert [r["variant"] for r in rows] == ["a", "c"]
    with open(tmp_path / "summary.csv") as f:
        got = list(csv.DictReader(f))
    assert got[0]["cpu_avg_pct"] == "140.0" and got[0]["frames_shown"] == "700" and got[0]["seconds"] == "30.1"
    assert got[1]["cpu_avg_pct"] == "13.0" and got[1]["dropped_frames"] == "3"
    assert got[1]["hwdec_current"] == "v4l2m2m-copy" and got[1]["hw_decoding_in_log"] == "True"
    txt = (tmp_path / "summary.txt").read_text()
    assert "[a] ffpyplayer" in txt and "[c] mpv" in txt and "Using hardware decoding' en el log: sí" in txt
