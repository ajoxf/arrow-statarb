"""In-memory execution telemetry — what the SpreadExecutor actually did.

Records each live / live-sim execution (entry or close) with per-leg detail:
reference price at placement vs the executed fill, the resulting slippage,
order type, amendment count, whether it escalated to market, and whether the
trade orphaned and was recovered — plus the wall-clock time to complete.

Bounded and process-local (cleared on restart); purely for the dashboard's
Execution Monitor, not for accounting.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from typing import Dict, List, Optional


def _slippage(side: str, ref: Optional[float], fill: Optional[float]):
    """Adverse slippage in price units and % (positive = filled worse than the
    reference). Buy worse = paid above ref; sell worse = sold below ref."""
    if not ref or not fill:
        return None, None
    raw = (fill - ref) if side == "buy" else (ref - fill)
    return round(raw, 2), round(100.0 * raw / ref, 3)


class ExecutionLog:
    def __init__(self, maxlen: int = 200):
        self._events = deque(maxlen=maxlen)
        self._lock = threading.Lock()

    def record(self, res: Dict, label: str, mode: str) -> Dict:
        legs = []
        for r in (res.get("results") or []):
            sp, spp = _slippage(r.get("side"), r.get("ref_price"), r.get("avg_price"))
            legs.append({
                "symbol": r.get("symbol"), "side": r.get("side"),
                "order_type": r.get("order_type"), "status": r.get("status"),
                "ref_price": r.get("ref_price"), "fill_price": r.get("avg_price"),
                "slippage": sp, "slippage_pct": spp,
                "amend_count": r.get("amend_count", 0),
                "escalated": bool(r.get("escalated")),
                "unconfirmed": bool(r.get("unconfirmed")),
                "error": r.get("error", ""),
            })
        ev = {
            "ts": time.time(), "time": time.strftime("%H:%M:%S"),
            "label": label, "mode": mode,
            "success": bool(res.get("success")),
            "orphan": bool(res.get("orphan")),
            "recovered": bool(res.get("recovered")),
            "elapsed_sec": res.get("elapsed_sec"),
            "legs": legs,
        }
        with self._lock:
            self._events.append(ev)
        return ev

    def all(self) -> List[Dict]:
        with self._lock:
            return list(reversed(self._events))   # newest first

    def stats(self) -> Dict:
        with self._lock:
            evs = list(self._events)
        n = len(evs)
        orphans = sum(1 for e in evs if e["orphan"])
        slips = [l["slippage_pct"] for e in evs for l in e["legs"] if l["slippage_pct"] is not None]
        times = [e["elapsed_sec"] for e in evs if e.get("elapsed_sec") is not None]
        return {
            "count": n,
            "orphans": orphans,
            "avg_slippage_pct": round(sum(slips) / len(slips), 3) if slips else None,
            "avg_fill_sec": round(sum(times) / len(times), 2) if times else None,
        }

    def clear(self) -> None:
        with self._lock:
            self._events.clear()
