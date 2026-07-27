"""External watchdog — a SEPARATE process that restarts the app if it dies OR
its heartbeat goes stale (a hang a crash-only supervisor can't catch).

The app's own timer thread writes data/heartbeat.txt; this process only reads it.
Restart fires on a dead process or a stale heartbeat (a genuine freeze) — NOT on
tick-staleness, which the app fixes in-process with a feed reconnect (see
core/health.should_restart / reference §8).

Run it beside the app:  python scripts/watchdog.py
Keep the relaunch fast — a slow relaunch is unmanaged-position time.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from arrow_statarb.core.health import Heartbeat, should_restart   # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
HEARTBEAT = ROOT / "data" / "heartbeat.txt"
START_CMD = [sys.executable, str(ROOT / "run_arrow.py")]

CHECK_SEC = float(os.environ.get("WATCHDOG_CHECK_SEC", "15"))
MAX_HEARTBEAT_SEC = float(os.environ.get("WATCHDOG_MAX_HEARTBEAT_SEC", "60"))


def _launch() -> subprocess.Popen:
    # Gate on an env flag so a supervised app EXITS (never self-execs) on a
    # remote restart — otherwise two live engines place double orders.
    env = dict(os.environ, ARROW_SUPERVISED="1")
    print(f"[watchdog] launching: {' '.join(START_CMD)}", flush=True)
    return subprocess.Popen(START_CMD, env=env, cwd=str(ROOT))


def main() -> None:
    proc = _launch()
    while True:
        time.sleep(CHECK_SEC)
        alive = proc.poll() is None
        age = Heartbeat.age(HEARTBEAT)
        if should_restart(age, alive, MAX_HEARTBEAT_SEC):
            why = "process died" if not alive else f"heartbeat stale ({age}s)"
            print(f"[watchdog] restarting — {why}", flush=True)
            if alive:
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
            proc = _launch()


if __name__ == "__main__":
    main()
