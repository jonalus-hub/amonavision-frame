"""Shell bridge: `python3 -m app.hal env` (run from the project root).

Prints KEY='value' lines for launch.sh and tools/bot-watchdog.sh to eval.
Unknown values are left out, so each script keeps its own rpi4 fallback;
on any error nothing is printed and the exit code is non-zero.

`env --wait-display N` (launch.sh, at start-up): with a display profile on
hdmi_connector: auto, wait up to N seconds for the panel to show up on an HDMI
port before choosing one.
"""
import shlex
import sys

from .factory import get_platform
from .profiles import get_selection


def _wait_display(args: list[str]) -> float:
    if "--wait-display" in args:
        i = args.index("--wait-display")
        try:
            return max(0.0, float(args[i + 1]))
        except (IndexError, ValueError):
            pass
    return 0.0


def main(argv: list[str]) -> int:
    cmd = argv[1] if len(argv) > 1 else "env"
    if cmd == "env":
        env = get_platform().shell_env(wait_display=_wait_display(argv[2:]))
        lines = [f"{k}={shlex.quote(v)}" for k, v in env.items() if v is not None]
        print("\n".join(lines))
        return 0
    if cmd == "label":
        print(get_selection().label())
        return 0
    print("usage: python3 -m app.hal [env [--wait-display SECONDS]|label]", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
