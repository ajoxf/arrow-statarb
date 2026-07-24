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


def _outcome_tag(reason: str, net: float, entry_z, exit_z) -> str:
    """Deterministic, rule-based outcome tag for the close report — numbers
    first, no prose. Distinguishes the two very different stop stories:
    'STOPPED IN TREND' (z never came home) vs 'STOPPED AFTER FULL REVERSION'
    (z came home but the price never paid — an execution/slippage story)."""
    r = str(reason or "").lower()
    if r == "profit_target":
        return "TARGET HIT"
    if r == "target":
        return "REVERSION BANKED" if net > 0 else "REVERSION RELEASED"
    if r in ("time_stop", "hard_time_stop"):
        return "TIME EXIT"
    if r == "trailing_stop":
        return "TRAILING BANKED" if net > 0 else "TRAILING STOP"
    if r in ("stop", "dollar_stop"):
        try:
            ez, xz = float(entry_z), float(exit_z)
            reverted = (abs(xz) <= abs(ez) * 0.3) or (ez < 0 <= xz) or (ez > 0 >= xz)
        except (TypeError, ValueError):
            reverted = False
        return ("STOPPED AFTER FULL REVERSION — price never paid" if reverted
                else "STOPPED IN TREND — never reverted")
    return r.upper().replace("_", " ") if r else ""


class TradeLog:
    def __init__(self, path: Optional[Path] = None, brokerage_per_lot: float = 20.0):
        # path=None → in-memory only (no file IO), used by the backtester.
        self.path = Path(path) if path is not None else None
        self.brokerage_per_lot = brokerage_per_lot
        self._lock = threading.Lock()
        self._trades: List[Dict] = self._load()

    def _load(self) -> List[Dict]:
        if self.path and self.path.exists():
            try:
                with open(self.path) as f:
                    return json.load(f) or []
            except Exception as exc:
                logger.warning("TradeLog: could not read {} — {}", self.path, exc)
        return []

    def _save(self) -> None:
        if not self.path:
            return
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
               exit_reason: str = "",
               decision_spread: Optional[float] = None,
               stt_pct: float = 0.0, other_cost_pct: float = 0.0,
               capital_gains_pct: float = 0.0,
               stt_a_pct: Optional[float] = None, stt_b_pct: Optional[float] = None,
               peak_pnl: Optional[float] = None, trough_pnl: Optional[float] = None,
               peak_min: Optional[float] = None, trough_min: Optional[float] = None) -> Dict:
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
            # The signal mid the algo decided on (vs the fill spread above).
            # The gap between them is execution slippage, surfaced on CLOSE.
            "entry_decision_spread": decision_spread if action == "OPEN" else None,
            "exit_decision_spread": decision_spread if action == "CLOSE" else None,
            "spread_pnl": 0.0,
            "edge_pnl": 0.0,         # P&L the signal alone implied (decision spreads)
            "slippage_pnl": 0.0,     # realized − edge: cost paid to execution (≤0 = adverse)
            "brokerage": brokerage,
            "stt": 0.0,              # Securities Transaction Tax (settled on CLOSE)
            "other_cost": 0.0,       # exchange txn + GST + SEBI + stamp
            "cgt": 0.0,              # Capital-Gains Tax (haircut on profit)
            "net_pnl": 0.0,
            "status": status,          # DRY-RUN | LIVE-SIM | LIVE | rejected
            "dry_run": dry_run,
            "exit_reason": exit_reason if action == "CLOSE" else "",  # target|stop|time_stop
            # round-trip detail (filled on CLOSE)
            "entry_zscore": None, "exit_zscore": None,
            "entry_leg_a": None, "entry_leg_b": None,
            "exit_leg_a": None, "exit_leg_b": None,
            "held_sec": None,
            # lifecycle extremes ("Peak/Trough ₹X (Ym) / ₹Z (Wm)") + outcome tag
            "peak_pnl": peak_pnl, "trough_pnl": trough_pnl,
            "peak_min": peak_min, "trough_min": trough_min,
            "outcome": "",
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
                            # STT: one sell per leg over a round trip. Notional is
                            # the CONTRACT leg (leg_b) × its lot size (mult), the
                            # scale the spread is denominated in; per-leg rates
                            # (stt_a/stt_b) fall back to stt_pct so a same-instrument
                            # spread is unchanged. leg_b price ≈ leg_a for a
                            # same-scale pair, so either representative works.
                            pb = (abs(float(prev.get("leg_b_price") or prev.get("leg_a_price") or 0))
                                  + abs(float(leg_b_price or leg_a_price or 0))) / 2.0
                            qty = lots * mult
                            a_pct = stt_a_pct if stt_a_pct is not None else stt_pct
                            b_pct = stt_b_pct if stt_b_pct is not None else stt_pct
                            stt = other = 0.0
                            if (a_pct or b_pct):                           # leg_a sell + leg_b sell
                                stt = round(((a_pct + b_pct) / 100.0) * qty * pb, 2)
                            if other_cost_pct and other_cost_pct > 0:      # 4 leg turnovers
                                other = round((other_cost_pct / 100.0) * 4.0 * qty * pb, 2)
                            rec["stt"] = stt
                            rec["other_cost"] = other
                            pre_tax = (rec["spread_pnl"] - brokerage
                                       - prev.get("brokerage", 0) - stt - other)
                            cgt = 0.0
                            if capital_gains_pct and capital_gains_pct > 0 and pre_tax > 0:
                                cgt = round((capital_gains_pct / 100.0) * pre_tax, 2)
                            rec["cgt"] = cgt
                            rec["net_pnl"] = round(pre_tax - cgt, 2)
                            # Decompose realized P&L into signal "edge" (what the
                            # decision spreads implied) and "slippage" (execution
                            # cost = realized − edge). Only when both decision
                            # spreads are known; else leave at 0.
                            edec = prev.get("entry_decision_spread")
                            xdec = decision_spread
                            rec["entry_decision_spread"] = edec
                            if edec is not None and xdec is not None:
                                paper = (xdec - edec) if direction == "LONG_SPREAD" else (edec - xdec)
                                rec["edge_pnl"] = round(paper * lots * mult, 2)
                                rec["slippage_pnl"] = round(rec["spread_pnl"] - rec["edge_pnl"], 2)
                            # full round-trip detail for the journal
                            rec["entry_zscore"] = prev.get("zscore")
                            rec["exit_zscore"] = zscore
                            rec["entry_leg_a"] = prev.get("leg_a_price")
                            rec["entry_leg_b"] = prev.get("leg_b_price")
                            rec["exit_leg_a"] = leg_a_price
                            rec["exit_leg_b"] = leg_b_price
                            rec["held_sec"] = round(rec["ts"] - float(prev.get("ts", rec["ts"])), 1)
                            rec["name"] = name or prev.get("name", "")
                            rec["outcome"] = _outcome_tag(exit_reason, rec["net_pnl"],
                                                          prev.get("zscore"), zscore)
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

    def loss_streak(self) -> int:
        """Count of consecutive most-recent CLOSED trades with net P&L < 0
        (resets on any non-losing close). Powers the loss-streak circuit
        breaker (size reduction / auto-pause)."""
        n = 0
        with self._lock:
            for r in reversed(self._trades):
                if r.get("action") != "CLOSE":
                    continue
                if float(r.get("net_pnl", 0) or 0) < 0:
                    n += 1
                else:
                    break
        return n

    def cost_audit(self, n: int = 20) -> Dict:
        """Average REALIZED round-trip cost over the last ``n`` closed trades
        (round-trip brokerage + STT + |execution slippage|). Lets the caller
        compare it to the MODELED cost and alarm on miscalibration — an inflated
        model makes every trade look unprofitable and jams the edge filter."""
        with self._lock:
            closes = [r for r in self._trades
                      if r.get("action") == "CLOSE" and r.get("entry_spread") is not None][-n:]
        if not closes:
            return {"n": 0, "avg_realized_cost": 0.0, "avg_brokerage": 0.0,
                    "avg_stt": 0.0, "avg_other": 0.0, "avg_cgt": 0.0, "avg_slippage": 0.0}
        brk = stt = other = cgt = slip = 0.0
        for r in closes:
            brk += float(r.get("brokerage", 0) or 0) * 2.0          # entry + exit
            stt += float(r.get("stt", 0) or 0)
            other += float(r.get("other_cost", 0) or 0)
            cgt += float(r.get("cgt", 0) or 0)
            slip += abs(float(r.get("slippage_pnl", 0) or 0))
        k = len(closes)
        return {"n": k, "avg_brokerage": round(brk / k, 2), "avg_stt": round(stt / k, 2),
                "avg_other": round(other / k, 2), "avg_cgt": round(cgt / k, 2),
                "avg_slippage": round(slip / k, 2),
                "avg_realized_cost": round((brk + stt + other + cgt + slip) / k, 2)}

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

    def expectancy(self) -> Dict:
        """The whole book on one sheet, in R (= one average loss). Ported from the
        reference system's hard-won discipline: chase REWARD:RISK, not win rate —
        a system with RR 1.5 is *allowed* to lose 60% of the time. If the measured
        win rate is below the break-even WR, the book is −EV no matter how any
        single trade 'felt'.

        p = win rate; RR = avg_win/avg_loss; PF = Σwin/Σloss;
        break-even WR = 1/(1+RR); expectancy EV/R = p·(1+RR) − 1; ₹/trade."""
        with self._lock:
            closed = [t for t in self._trades if t.get("action") == "CLOSE"
                      and t.get("entry_spread") is not None]
        n = len(closed)
        pnls = [float(t.get("net_pnl", 0) or 0) for t in closed]
        wins = [x for x in pnls if x > 0]
        losses = [-x for x in pnls if x < 0]        # positive magnitudes
        nw, nl = len(wins), len(losses)
        gross_win, gross_loss = round(sum(wins), 2), round(sum(losses), 2)
        avg_win = round(gross_win / nw, 2) if nw else 0.0
        avg_loss = round(gross_loss / nl, 2) if nl else 0.0
        p = (nw / n) if n else 0.0
        rr = (avg_win / avg_loss) if avg_loss > 0 else None
        pf = (gross_win / gross_loss) if gross_loss > 0 else None
        be_wr = (1.0 / (1.0 + rr)) if rr else None
        ev_r = (p * (1.0 + rr) - 1.0) if rr else None
        ev_inr = round(sum(pnls) / n, 2) if n else 0.0
        return {
            "closed": n, "wins": nw, "losses": nl,
            "win_rate": round(100.0 * p, 1),
            "avg_win": avg_win, "avg_loss": avg_loss,
            "gross_win": gross_win, "gross_loss": gross_loss,
            "reward_risk": (round(rr, 2) if rr is not None else None),
            "profit_factor": (round(pf, 2) if pf is not None else None),
            "breakeven_win_rate": (round(100.0 * be_wr, 1) if be_wr is not None else None),
            "expectancy_r": (round(ev_r, 3) if ev_r is not None else None),
            "expectancy_inr": ev_inr,
            # WR minus break-even WR: >0 = +EV geometry, the number that matters.
            "edge": (round(100.0 * p - 100.0 * be_wr, 1) if be_wr is not None else None),
            "positive": ev_inr > 0,
        }

    def clear(self) -> None:
        with self._lock:
            self._trades = []
            self._save()
