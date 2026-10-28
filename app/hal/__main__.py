"""Shell bridge: `python3 -m app.hal env` (run from the project root).

Prints KEY='value' lines for launch.sh and tools/bot-watchdog.sh to eval.
Unknown values are left out, so each script keeps its own rpi4 fallback;
on any error nothing is printed and the exit code is non-zero.
"""
import shlex
import sys

from .factory import get_platform
from .profiles import get_selection


def main(argv: list[str]) -> int:
    cmd = argv[1] if len(argv) > 1 else "env"
    if cmd == "env":
        lines = [f"{k}={shlex.quote(v)}" for k, v in get_platform().shell_env().items() if v is not None]
        print("\n".join(lines))
        return 0
    if cmd == "label":
        print(get_selection().label())
        return 0
    print("usage: python3 -m app.hal [env|label]", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
