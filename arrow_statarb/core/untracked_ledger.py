"""Untracked-close ledger.

Money that moves OUTSIDE a recorded trade — an orphan cleanup, a reconcile
auto-close, a force-flatten — must not vanish into the broker statement. Each
such event is appended here with its estimated cost, charged to the daily-loss
tracker, and surfaced in the UI. Silent cleanup costs are how accounts quietly
bleed; this makes them visible.
"""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional

from loguru import logger

_IST = timezone(timedelta(hours=5, minutes=30))


class UntrackedLedger:
    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path) if path is not None else None
        self._lock = threading.Lock()
        self._events: List[Dict] = self._load()

    def _load(self) -> List[Dict]:
        if self.path and self.path.exists():
            try:
                with open(self.path) as f:
                    return json.load(f) or []
            except Exception as exc:
                logger.warning("UntrackedLedger: could not read {} — {}", self.path, exc)
        return []

    def _save(self) -> None:
        if not self.path:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            with open(tmp, "w") as f:
                json.dump(self._events, f, indent=2)
            tmp.replace(self.path)
        except Exception as exc:
            logger.warning("UntrackedLedger: could not write {} — {}", self.path, exc)

    def record(self, *, reason: str, symbol: str = "", qty: int = 0,
               est_cost: float = 0.0, detail: str = "") -> Dict:
        """Append an untracked money-movement event. ``est_cost`` is the ₹ cost
        charged to the day's P&L (positive = a loss)."""
        ev = {"ts": time.time(), "time": time.strftime("%H:%M:%S"),
              "reason": reason, "symbol": str(symbol), "qty": int(qty),
              "est_cost": round(float(est_cost), 2), "detail": str(detail)}
        with self._lock:
            self._events.append(ev)
            self._save()
        logger.warning("UntrackedLedger: {} {} qty={} est_cost=₹{:.2f} — {}",
                       reason, symbol, qty, est_cost, detail)
        return ev

    def day_cost(self, now_ts: Optional[float] = None) -> float:
        """Sum of estimated costs since IST midnight — added to the daily-loss
        tracker so cleanup costs count against the limit."""
        now_ts = time.time() if now_ts is None else now_ts
        start = datetime.fromtimestamp(now_ts, _IST).replace(
            hour=0, minute=0, second=0, microsecond=0).timestamp()
        with self._lock:
            return round(sum(float(e.get("est_cost", 0) or 0)
                             for e in self._events
                             if float(e.get("ts", 0) or 0) >= start), 2)

    def all(self, limit: int = 100) -> List[Dict]:
        with self._lock:
            return list(reversed(self._events))[:limit]     # newest first

    def total(self) -> float:
        with self._lock:
            return round(sum(float(e.get("est_cost", 0) or 0) for e in self._events), 2)

    def clear(self) -> None:
        with self._lock:
            self._events = []
            self._save()
