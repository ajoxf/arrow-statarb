"""Execution telemetry — what the SpreadExecutor actually did, order by order.

Records each live / live-sim execution (entry or close) with per-leg detail:

  * the REFERENCE price when the order was fired — the touch for its side
    (ask to buy, bid to sell) at the FIRST placement;
  * the executed average FILL price, and the slippage between them, in price
    points, % and ₹ (points × filled units × ₹ per point per unit);
  * the timeline: when the order left for the broker (sent), when the broker
    returned an order id (acked), when the leg was first seen fully filled —
    and the gap between the two legs filling, where a calendar spread is
    exposed to one-sided moves;
  * order type, amendments, escalation to market, orphan / recovery.

Kept in memory for the dashboard AND appended to a JSON-lines file, so the
history — and the average slippage per order that calibrates
``filters.slippage_per_lot`` — survives a restart.
"""

from __future__ import annotations

import json
import threading
import time
from collections import deque
from pathlib import Path
from typing import Callable, Dict, List, Optional

from loguru import logger


def _slippage(side: str, ref: Optional[float], fill: Optional[float]):
    """Adverse slippage in price units and % (positive = filled worse than the
    reference). Buy worse = paid above ref; sell worse = sold below ref."""
    if not ref or not fill:
        return None, None
    raw = (fill - ref) if side == "buy" else (ref - fill)
    return round(raw, 2), round(100.0 * raw / ref, 3)


def _ms(a: Optional[float], b: Optional[float]) -> Optional[int]:
    return int(round((b - a) * 1000)) if (a and b and b >= a) else None


class ExecutionLog:
    def __init__(self, maxlen: int = 200, path: Optional[Path] = None,
                 leg_scale: Optional[Callable[[Dict], Optional[Dict]]] = None):
        """``path`` — JSON-lines file the log is persisted to (None = memory only).
        ``leg_scale(leg)`` → {"inr_per_point": ₹ a 1-point move is worth per
        filled UNIT (the P&L multiplier ÷ the broker lot size), "lot_size":
        broker units per lot}; None when unresolvable, in which case the ₹
        figures are left blank rather than guessed."""
        self._events = deque(maxlen=maxlen)
        self._lock = threading.Lock()
        self._path = Path(path) if path else None
        self._leg_scale = leg_scale
        self._load()

    # ── persistence ───────────────────────────────────────────────────────────
    def _load(self) -> None:
        if not self._path or not self._path.exists():
            return
        try:
            with open(self._path) as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            self._events.append(json.loads(line))
                        except ValueError:
                            continue
        except Exception as exc:                     # never block startup
            logger.warning("ExecutionLog: could not read {} — {}", self._path, exc)

    def _append(self, ev: Dict) -> None:
        if not self._path:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with open(self._path, "a") as f:
                f.write(json.dumps(ev, default=str) + "\n")
            # Keep the file bounded: rewrite it from the in-memory window when it
            # has grown well past it.
            if self._path.stat().st_size > 2_000_000:
                with self._lock:
                    evs = list(self._events)
                tmp = self._path.with_suffix(".tmp")
                with open(tmp, "w") as f:
                    for e in evs:
                        f.write(json.dumps(e, default=str) + "\n")
                tmp.replace(self._path)
        except Exception as exc:                     # telemetry must never break a trade
            logger.warning("ExecutionLog: could not persist — {}", exc)

    # ── recording ─────────────────────────────────────────────────────────────
    def record(self, res: Dict, label: str, mode: str) -> Dict:
        legs = []
        for r in (res.get("results") or []):
            sp, spp = _slippage(r.get("side"), r.get("ref_price"), r.get("avg_price"))
            filled = r.get("filled") or 0
            scale = None
            if self._leg_scale is not None:
                try:
                    scale = self._leg_scale(r) or None
                except Exception:
                    scale = None
            per_pt = (scale or {}).get("inr_per_point")
            lot_size = (scale or {}).get("lot_size")
            lots = (round(filled / lot_size, 4) if lot_size and filled else None)
            legs.append({
                "symbol": r.get("symbol"), "side": r.get("side"),
                "order_type": r.get("order_type"), "status": r.get("status"),
                "units": r.get("units"), "filled": filled, "lots": lots,
                "ref_price": r.get("ref_price"), "fill_price": r.get("avg_price"),
                "slippage": sp, "slippage_pct": spp,
                "slippage_inr": (round(sp * filled * per_pt, 2)
                                 if sp is not None and per_pt and filled else None),
                "sent_at": r.get("sent_at"), "acked_at": r.get("acked_at"),
                "filled_at": r.get("filled_at"),
                "ack_ms": _ms(r.get("sent_at"), r.get("acked_at")),
                "fill_ms": _ms(r.get("sent_at"), r.get("filled_at")),
                "amend_count": r.get("amend_count", 0),
                "escalated": bool(r.get("escalated")),
                "unconfirmed": bool(r.get("unconfirmed")),
                "error": r.get("error", ""),
            })
        fills = [l["filled_at"] for l in legs if l.get("filled_at")]
        sends = [l["sent_at"] for l in legs if l.get("sent_at")]
        slips_inr = [l["slippage_inr"] for l in legs if l.get("slippage_inr") is not None]
        ev = {
            "ts": time.time(), "time": time.strftime("%H:%M:%S"),
            "date": time.strftime("%Y-%m-%d"),
            "label": label, "mode": mode,
            "success": bool(res.get("success")),
            "orphan": bool(res.get("orphan")),
            "recovered": bool(res.get("recovered")),
            "elapsed_sec": res.get("elapsed_sec"),
            # first order sent → last leg filled: the whole execution
            "total_ms": (_ms(min(sends), max(fills))
                         if sends and len(fills) == len(legs) and legs else None),
            # first leg filled → second leg filled: one-sided exposure
            "leg_gap_ms": (_ms(min(fills), max(fills)) if len(fills) >= 2 else None),
            "slippage_inr": round(sum(slips_inr), 2) if slips_inr else None,
            "legs": legs,
        }
        with self._lock:
            self._events.append(ev)
        self._append(ev)
        return ev

    def all(self) -> List[Dict]:
        with self._lock:
            return list(reversed(self._events))   # newest first

    def stats(self) -> Dict:
        with self._lock:
            evs = list(self._events)
        n = len(evs)
        orphans = sum(1 for e in evs if e.get("orphan"))
        legs = [l for e in evs for l in e.get("legs", [])]
        slips = [l["slippage_pct"] for l in legs if l.get("slippage_pct") is not None]
        inr = [l["slippage_inr"] for l in legs
               if l.get("slippage_inr") is not None and l.get("filled")]
        # ₹ per ORDER per LOT — directly comparable with filters.slippage_per_lot
        per_lot = [l["slippage_inr"] / float(l["lots"]) for l in legs
                   if l.get("slippage_inr") is not None and l.get("lots")]
        times = [e["elapsed_sec"] for e in evs if e.get("elapsed_sec") is not None]
        gaps = [e["leg_gap_ms"] for e in evs if e.get("leg_gap_ms") is not None]
        fill_ms = [l["fill_ms"] for l in legs if l.get("fill_ms") is not None]
        return {
            "count": n,
            "orphans": orphans,
            "orders": len(legs),
            "avg_slippage_pct": round(sum(slips) / len(slips), 3) if slips else None,
            "avg_slippage_inr_per_order": round(sum(inr) / len(inr), 2) if inr else None,
            "avg_slippage_inr_per_lot_order": (round(sum(per_lot) / len(per_lot), 2)
                                               if per_lot else None),
            "avg_fill_sec": round(sum(times) / len(times), 2) if times else None,
            "avg_fill_ms": int(sum(fill_ms) / len(fill_ms)) if fill_ms else None,
            "avg_leg_gap_ms": int(sum(gaps) / len(gaps)) if gaps else None,
            "max_leg_gap_ms": max(gaps) if gaps else None,
        }

    def clear(self) -> None:
        with self._lock:
            self._events.clear()
        if self._path:
            try:
                self._path.unlink(missing_ok=True)
            except Exception:
                pass
