from __future__ import annotations

import sys

from tw_watchdesk.app import main
from tw_watchdesk.live_check import run_live_check_cli


if __name__ == "__main__":
    if "--live-check" in sys.argv:
        raise SystemExit(run_live_check_cli(sys.argv[1:]))
    main()
