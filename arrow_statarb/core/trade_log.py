"""Tiny append-only trade log backing the dashboard's Trades table.

Records every manual/algo entry and exit (dry-run included, tagged as such) to a
JSON file so the table survives restarts. P&L is best-effort: an entry stores the
spread it went on at; the matching close computes spread P&L = (entry−exit)×lots
for a LONG_SPREAD (reversed for SHORT), minus estimated brokerage.
"""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional

from loguru import logger

# Trading day boundary is evaluated in IST (the app's exchange-local timezone).
_IST = timezone(timedelta(hours=5, minutes=30))


class TradeLog:
    def __init__(self, path: Path, brokerage_per_lot: float = 20.0):
        self.path = Path(path)
        self.brokerage_per_lot = brokerage_per_lot
        self._lock = threading.Lock()
        self._trades: List[Dict] = self._load()

    def _load(self) -> List[Dict]:
        if self.path.exists():
            try:
                with open(self.path) as f:
                    return json.load(f) or []
            except Exception as exc:
                logger.warning("TradeLog: could not read {} — {}", self.path, exc)
        return []

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "w") as f:
                json.dump(self._trades, f, indent=2)
        except Exception as exc:
            logger.warning("TradeLog: could not write {} — {}", self.path, exc)

    def record(self, *, action: str, direction: str, lots: int,
               spread: Optional[float], dry_run: bool, status: str,
               source: str = "manual", lot_size: int = 1,
               zscore: Optional[float] = None, leg_a_price: Optional[float] = None,
               leg_b_price: Optional[float] = None, name: str = "",
               exit_reason: str = "") -> Dict:
        """Append an OPEN or CLOSE event. On CLOSE, settle against the last
        matching OPEN to fill in spread/net P&L plus full round-trip detail
        (entry/exit z, per-leg prices, spreads, time-in-trade).

        ``lot_size`` is units per contract; spread P&L = Δspread × lots ×
        lot_size (a spread move is in ₹/unit, so it must be scaled by the
        contract's unit count — not just the number of lots)."""
        mult = max(1, int(lot_size or 1))
        brokerage = round(self.brokerage_per_lot * lots * 2, 2)  # both legs, one way
        rec = {
            "ts": time.time(),
            "time": time.strftime("%H:%M:%S"),
            "action": action,          # OPEN | CLOSE
            "source": source,          # manual | algo
            "name": name,
            "direction": direction,
            "lots": lots,
            "lot_size": mult,
            "zscore": zscore,
            "leg_a_price": leg_a_price,
            "leg_b_price": leg_b_price,
            "entry_spread": spread if action == "OPEN" else None,
            "exit_spread": spread if action == "CLOSE" else None,
            "spread_pnl": 0.0,
            "brokerage": brokerage,
            "net_pnl": 0.0,
            "status": status,          # DRY-RUN | LIVE-SIM | LIVE | rejected
            "dry_run": dry_run,
            "exit_reason": exit_reason if action == "CLOSE" else "",  # target|stop|time_stop
            # round-trip detail (filled on CLOSE)
            "entry_zscore": None, "exit_zscore": None,
            "entry_leg_a": None, "entry_leg_b": None,
            "exit_leg_a": None, "exit_leg_b": None,
            "held_sec": None,
        }
        with self._lock:
            if action == "CLOSE" and spread is not None:
                for prev in reversed(self._trades):
                    if prev["action"] == "OPEN" and not prev.get("_closed") \
                            and prev["direction"] == direction:
                        entry = prev.get("entry_spread")
                        if entry is not None:
                            # LONG_SPREAD profits when the spread rises; SHORT when it falls.
                            raw = (spread - entry) if direction == "LONG_SPREAD" else (entry - spread)
                            rec["entry_spread"] = entry
                            rec["spread_pnl"] = round(raw * lots * mult, 2)
                            rec["net_pnl"] = round(rec["spread_pnl"] - brokerage - prev.get("brokerage", 0), 2)
                            # full round-trip detail for the journal
                            rec["entry_zscore"] = prev.get("zscore")
                            rec["exit_zscore"] = zscore
                            rec["entry_leg_a"] = prev.get("leg_a_price")
                            rec["entry_leg_b"] = prev.get("leg_b_price")
                            rec["exit_leg_a"] = leg_a_price
                            rec["exit_leg_b"] = leg_b_price
                            rec["held_sec"] = round(rec["ts"] - float(prev.get("ts", rec["ts"])), 1)
                            rec["name"] = name or prev.get("name", "")
                            prev["_closed"] = True
                        break
            else:
                rec["net_pnl"] = round(-brokerage, 2)
            self._trades.append(rec)
            self._save()
        return rec

    def open_position(self) -> Optional[Dict]:
        """The most recent OPEN that has not been matched by a later CLOSE,
        i.e. a position still believed to be live. Used to restore engine state
        after a restart. Returns ``{direction, lots, entry_spread, ts, dry_run}``
        or ``None`` when flat."""
        with self._lock:
            for rec in reversed(self._trades):
                if rec.get("action") == "OPEN" and not rec.get("_closed"):
                    return {"direction": rec.get("direction"),
                            "lots": int(rec.get("lots", 1)),
                            "entry_spread": rec.get("entry_spread"),
                            "ts": float(rec.get("ts", 0.0)),
                            "dry_run": bool(rec.get("dry_run", False))}
        return None

    def last_close_time(self, source: Optional[str] = None) -> Optional[float]:
        """Timestamp of the most recent CLOSE event (optionally filtered by
        ``source``, e.g. ``"algo"``). Used to restore the algo's entry cooldown
        across a restart. Returns ``None`` if there is no matching close."""
        with self._lock:
            for rec in reversed(self._trades):
                if rec.get("action") == "CLOSE" and (source is None
                                                     or rec.get("source") == source):
                    ts = rec.get("ts")
                    return float(ts) if ts is not None else None
        return None

    def day_pnl(self, now_ts: Optional[float] = None) -> float:
        """Realized net P&L (sum of ``net_pnl``) for the current IST trading day.
        Used to enforce the daily-loss limit. Includes brokerage; counts every
        recorded trade since IST midnight regardless of mode."""
        now_ts = time.time() if now_ts is None else now_ts
        midnight = datetime.fromtimestamp(now_ts, _IST).replace(
            hour=0, minute=0, second=0, microsecond=0)
        start = midnight.timestamp()
        with self._lock:
            return round(sum(float(r.get("net_pnl", 0) or 0)
                             for r in self._trades
                             if float(r.get("ts", 0) or 0) >= start), 2)

    def round_trips(self) -> Dict:
        """Completed trades (each settled CLOSE carries full entry+exit detail)
        with a running cumulative P&L, plus the currently-open trade if any.
        Powers the Analysis 'Trade Journal'."""
        with self._lock:
            closed = [dict(r) for r in self._trades
                      if r.get("action") == "CLOSE" and r.get("entry_spread") is not None]
            open_rec = None
            for r in reversed(self._trades):
                if r.get("action") == "OPEN" and not r.get("_closed"):
                    open_rec = dict(r)
                    break
        cum = 0.0
        for r in closed:                       # oldest → newest for a running total
            cum = round(cum + float(r.get("net_pnl", 0) or 0), 2)
            r["cum_pnl"] = cum
        return {"trips": list(reversed(closed)),   # newest first for display
                "open": open_rec,
                "total_pnl": cum,
                "count": len(closed)}

    def all(self) -> List[Dict]:
        with self._lock:
            return list(reversed(self._trades))   # newest first

    def stats(self) -> Dict:
        with self._lock:
            closed = [t for t in self._trades if t["action"] == "CLOSE"]
            total = round(sum(t.get("net_pnl", 0) for t in self._trades), 2)
            wins = sum(1 for t in closed if t.get("net_pnl", 0) > 0)
            win_rate = round(100.0 * wins / len(closed), 1) if closed else 0.0
            return {"count": len(self._trades), "closed": len(closed),
                    "total_pnl": total, "win_rate": win_rate}

    def clear(self) -> None:
        with self._lock:
            self._trades = []
            self._save()
