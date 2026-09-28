"""Liveness heartbeat + health verdict (reference §8).

The single biggest source of live 'hangs' is a loop that is up but frozen — a
crash-only supervisor never sees it. Two independent signals catch it:

  • HEARTBEAT — written by a small timer thread, it proves the ASYNC LOOP is
    spinning, NOT that data is flowing. If you write it only from the tick
    callback, a data outage is indistinguishable from a frozen loop, and the
    watchdog kills the whole process for what a feed-reconnect would fix.
  • TICK AGE — how long since the last price sample. A stalled feed (no ticks)
    is a DATA problem: force a reconnect, don't kill the process.

`health_verdict` separates the two so the response is surgical. `should_restart`
is the external watchdog's decision: restart only on a dead process or a stale
heartbeat (a true freeze) — never on tick-staleness alone.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional


class Heartbeat:
    """Writes ``now()`` to a file every ``interval_sec`` from a daemon thread —
    proof the process loop is alive, independent of the data feed."""

    def __init__(self, path, interval_sec: float = 10.0, clock: Callable[[], float] = time.time):
        self.path = Path(path)
        self.interval = float(interval_sec)
        self._clock = clock
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def beat(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(str(self._clock()))
        except Exception:                                # noqa: BLE001 — never crash on IO
            pass

    def start(self) -> None:
        self.beat()

        def loop():
            while not self._stop.wait(self.interval):
                self.beat()
        self._thread = threading.Thread(target=loop, daemon=True, name="Heartbeat")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    @staticmethod
    def age(path, now: Optional[float] = None) -> Optional[float]:
        """Seconds since the heartbeat was last written, or None if unreadable."""
        now = time.time() if now is None else now
        try:
            return now - float(Path(path).read_text().strip())
        except Exception:                                # noqa: BLE001
            return None


def health_verdict(heartbeat_age: Optional[float], tick_age: Optional[float],
                   max_heartbeat_sec: float = 60.0,
                   max_tick_sec: float = 120.0) -> Dict:
    """Two independent problems, reported separately: a frozen loop (heartbeat
    stale) vs a stalled feed (no ticks). ok only when neither trips."""
    problems: List[str] = []
    if heartbeat_age is None or heartbeat_age > max_heartbeat_sec:
        problems.append("heartbeat stale — process loop may be frozen")
    if tick_age is not None and tick_age > max_tick_sec:
        problems.append("no ticks — price feed may be stalled (reconnect, don't kill)")
    return {"ok": not problems, "problems": problems}


def should_restart(heartbeat_age: Optional[float], process_alive: bool,
                   max_heartbeat_sec: float = 60.0) -> bool:
    """External-watchdog decision: restart only on a DEAD process or a STALE
    heartbeat (a genuine freeze). Tick-staleness is handled in-process by a
    feed reconnect — never a reason to kill the whole app."""
    if not process_alive:
        return True
    return heartbeat_age is None or heartbeat_age > max_heartbeat_sec
