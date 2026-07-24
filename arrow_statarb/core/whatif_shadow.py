"""What-if-held shadow tracker (reference §9).

After every exit that is NOT a clean target hit (stops, trailing, sub-target
reversions, small early wins), keep marking the position's REAL held P&L on the
FROZEN entry fill — immune to z/β drift — for a window (~60 min), and record
whether/when it would have reverted to break-even and to the target, plus the
peak it reached. High revert-rate ⇒ exits are premature (cutting winners / stops
too tight); low ⇒ the exits were right and the move was real. This turns "it
would have reverted, just wait" from an argument into logged data.

Persist each watch the moment it is armed and resume on restart — an in-memory
watch is silently lost every restart, so during a tuning session (frequent
restarts) it never finalizes and looks broken. A watch whose window elapsed
during downtime is finalized (dropped), not left lingering.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional

from loguru import logger


class ShadowTracker:
    def __init__(self, path: Optional[Path] = None, window_sec: float = 3600.0,
                 clock=time.time):
        self.path = Path(path) if path is not None else None
        self.window_sec = float(window_sec)
        self._clock = clock
        self._lock = threading.Lock()
        self._watches: List[Dict] = self._load()

    def _load(self) -> List[Dict]:
        if self.path and self.path.exists():
            try:
                with open(self.path) as f:
                    return json.load(f) or []
            except Exception as exc:                     # noqa: BLE001
                logger.warning("ShadowTracker: could not read {} — {}", self.path, exc)
        return []

    def _save(self) -> None:
        if not self.path:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            with open(tmp, "w") as f:
                json.dump(self._watches, f, indent=2)
            tmp.replace(self.path)
        except Exception as exc:                         # noqa: BLE001
            logger.warning("ShadowTracker: could not write {} — {}", self.path, exc)

    # ── arm ───────────────────────────────────────────────────────────────────
    def arm(self, *, direction: str, entry_spread: float, lots: int,
            lot_mult: float, cost_inr: float, exit_reason: str,
            target_net: Optional[float] = None,
            window_sec: Optional[float] = None) -> Optional[Dict]:
        """Arm a shadow watch on close. Skips a clean TARGET HIT (nothing to learn —
        it already banked). Returns the watch, or None if not armed."""
        if str(exit_reason) == "profit_target":
            return None                                  # clean win — no shadow needed
        now = self._clock()
        w = {
            "armed_ts": now, "armed_time": time.strftime("%H:%M:%S"),
            "direction": direction, "entry_spread": float(entry_spread),
            "lots": int(lots), "lot_mult": float(lot_mult),
            "cost_inr": float(cost_inr or 0.0),
            "target_net": (float(target_net) if target_net else None),
            "exit_reason": str(exit_reason),
            "window_sec": float(window_sec if window_sec is not None else self.window_sec),
            "peak_net": 0.0, "peak_min": 0.0,
            "reverted_be": False, "be_min": None,
            "reverted_target": False, "target_min": None,
            "done": False,
        }
        with self._lock:
            self._watches.append(w)
            self._save()
        logger.info("ShadowTracker: armed what-if watch ({} exit, dir {}) — tracking "
                    "reversion for {:.0f}m", exit_reason, direction, w["window_sec"] / 60.0)
        return w

    # ── update ─────────────────────────────────────────────────────────────────
    def _net(self, w: Dict, spread: float) -> float:
        d = 1.0 if w["direction"] == "LONG_SPREAD" else -1.0
        gross = d * (float(spread) - w["entry_spread"]) * w["lots"] * w["lot_mult"]
        return gross - w["cost_inr"]

    def update(self, spread: Optional[float], now: Optional[float] = None) -> None:
        """Mark every active watch against the live spread; finalize any whose
        window has elapsed (including during downtime, on the first call)."""
        now = self._clock() if now is None else now
        changed = False
        with self._lock:
            for w in self._watches:
                if w.get("done"):
                    continue
                elapsed = now - w["armed_ts"]
                if spread is not None:
                    net = self._net(w, float(spread))
                    if net > w["peak_net"]:
                        w["peak_net"] = round(net, 2)
                        w["peak_min"] = round(elapsed / 60.0, 1)
                    if net >= 0 and not w["reverted_be"]:
                        w["reverted_be"] = True
                        w["be_min"] = round(elapsed / 60.0, 1)
                    if (w["target_net"] and net >= w["target_net"]
                            and not w["reverted_target"]):
                        w["reverted_target"] = True
                        w["target_min"] = round(elapsed / 60.0, 1)
                    changed = True
                if elapsed >= w["window_sec"]:
                    w["done"] = True
                    changed = True
            if changed:
                self._save()

    # ── read ─────────────────────────────────────────────────────────────────
    def summary(self) -> Dict:
        """Active count + completed reversion stats: of finished watches, how many
        would have reverted to break-even / to the target, and how fast."""
        with self._lock:
            watches = [dict(w) for w in self._watches]
        active = [w for w in watches if not w.get("done")]
        done = [w for w in watches if w.get("done")]
        be = [w for w in done if w.get("reverted_be")]
        tgt = [w for w in done if w.get("reverted_target")]
        be_mins = [w["be_min"] for w in be if w.get("be_min") is not None]
        return {
            "active": len(active),
            "completed": len(done),
            "reverted_be": len(be),
            "reverted_target": len(tgt),
            "revert_be_rate": (round(100.0 * len(be) / len(done), 1) if done else None),
            "revert_target_rate": (round(100.0 * len(tgt) / len(done), 1) if done else None),
            "avg_revert_min": (round(sum(be_mins) / len(be_mins), 1) if be_mins else None),
            "watches": list(reversed(watches))[:50],       # newest first
        }

    def clear(self) -> None:
        with self._lock:
            self._watches = []
            self._save()
