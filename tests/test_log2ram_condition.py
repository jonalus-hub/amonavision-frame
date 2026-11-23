"""tools/log2ram-condition.sh: log2ram only runs on a deployed frame
(config.txt [System] deployed)."""
import configparser
import subprocess

import pytest

from conftest import PROJECT_ROOT

SCRIPT = PROJECT_ROOT / "tools" / "log2ram-condition.sh"


def runs_log2ram(config_path) -> bool:
    """True if the condition lets log2ram start (exit 0)."""
    return subprocess.run(["sh", str(SCRIPT), str(config_path)]).returncode == 0


def _config(tmp_path, system_lines):
    path = tmp_path / "config.txt"
    path.write_text(
        "[Viewer]\nfullscreen = True\ndeployed = True\n\n"
        "[System]\ndaily_restart = True\n" + system_lines +
        "\n[Telegram]\ndeployed = True\n",
        encoding="utf-8",
    )
    return path


@pytest.mark.parametrize("value, expected", [
    ("True", True), ("true", True), ("yes", True), ("on", True), ("1", True),
    ("False", False), ("false", False), ("no", False), ("off", False), ("0", False),
])
def test_deployed_flag(tmp_path, value, expected):
    assert runs_log2ram(_config(tmp_path, f"deployed = {value}\n")) is expected


def test_spacing_variants(tmp_path):
    assert runs_log2ram(_config(tmp_path, "  deployed=False  \n")) is False
    assert runs_log2ram(_config(tmp_path, "deployed: false\n")) is False


def test_only_the_system_section_counts(tmp_path):
    """The other sections say True; [System] says False."""
    assert runs_log2ram(_config(tmp_path, "deployed = False\n")) is False


def test_missing_key_or_file_keeps_log2ram(tmp_path):
    """Frames installed before the flag existed keep their log2ram."""
    assert runs_log2ram(_config(tmp_path, "")) is True
    assert runs_log2ram(tmp_path / "does-not-exist.txt") is True


def test_repo_config_is_a_frame_still_being_set_up():
    """A new install starts with log2ram off, and the viewer's own parser
    reads the same value the shell condition does."""
    assert runs_log2ram(PROJECT_ROOT / "config.txt") is False
    cp = configparser.ConfigParser()
    cp.read(PROJECT_ROOT / "config.txt", encoding="utf-8")
    assert cp.getboolean("System", "deployed") is False


def test_setup_installs_the_condition():
    setup = (PROJECT_ROOT / "setup.sh").read_text(encoding="utf-8")
    assert "log2ram.service.d/amonavision-deployed.conf" in setup
    assert "ExecCondition=/bin/sh ${APP_DIR}/tools/log2ram-condition.sh ${APP_DIR}/config.txt" in setup
    assert "log2ram-daily.service.d/amonavision-deployed.conf" in setup
