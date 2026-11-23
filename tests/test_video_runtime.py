"""Run directory and viewer heartbeat."""

import os

from app.video import Heartbeat, run_dir
from app.video import runtime


def test_override_wins(tmp_path):
    d = tmp_path / "rd"
    assert run_dir({"AMONAVISION_RUN_DIR": str(d)}) == d and d.is_dir()


def test_falls_back_to_xdg_runtime_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime, "SYSTEM_RUN_DIR", tmp_path / "missing-run-amonavision")
    got = run_dir({"XDG_RUNTIME_DIR": str(tmp_path)})
    assert got == tmp_path / "amonavision" and oct(got.stat().st_mode & 0o777) == "0o700"


def test_uses_the_systemd_runtime_directory_when_it_exists(tmp_path, monkeypatch):
    sysdir = tmp_path / "run-amonavision"
    sysdir.mkdir()
    monkeypatch.setattr(runtime, "SYSTEM_RUN_DIR", sysdir)
    assert run_dir({"XDG_RUNTIME_DIR": str(tmp_path / "x")}) == sysdir


def test_last_resort_is_a_private_temp_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime, "SYSTEM_RUN_DIR", tmp_path / "missing")
    monkeypatch.setattr(runtime.tempfile, "gettempdir", lambda: str(tmp_path))
    assert run_dir({}) == tmp_path / f"amonavision-{os.getuid()}"


def test_heartbeat_is_rate_limited_and_carries_the_state(tmp_path):
    t = [0.0]
    hb = Heartbeat(tmp_path / "viewer-heartbeat", interval=2.0, clock=lambda: t[0])
    hb.beat("WAITING_ACTION")
    first = (tmp_path / "viewer-heartbeat").read_text()
    assert first.split()[1] == "WAITING_ACTION" and first.split()[0].isdigit()
    t[0] = 1.0
    hb.beat("PLAYING/mpv")
    assert (tmp_path / "viewer-heartbeat").read_text() == first
    t[0] = 2.0
    hb.beat("PLAYING/mpv")
    assert (tmp_path / "viewer-heartbeat").read_text().split()[1] == "PLAYING/mpv"


def test_heartbeat_write_errors_are_not_fatal(tmp_path):
    hb = Heartbeat(tmp_path / "no-such-dir" / "hb")
    hb.beat("IDLE")
    hb.beat("IDLE")
