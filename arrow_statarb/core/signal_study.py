"""Signal study — every signal the algo's rules would have taken, followed to
its exit and settled in money. A READING: it never trades.

It is fed the same READY-window z the excursion tally sees (SignalEngine's
observer hook) and applies ARROW's rules, as ``core/algo.py`` writes them:

  entry   the algo's own ``entry_mode`` (core/arming.py, its own instance):
          at the TOUCH — ``confirmation_ticks`` samples at z ≥ +entry (SHORT)
          or z ≤ −entry (LONG) — or ARMED by that stretch and entered on the
          way back inside the band, within the re-entry window. Then the
          algo's own gates, in its order — cooldown after an exit, trading
          hours, the probability/EV filter. One position at a time.
          In re-entry mode every armed stretch gets a row, whether it was
          entered, LOST (through the window floor without an entry — the
          trend the rule exists to avoid) or filtered.
  exit    checked in the algo's order on every sample: STOP (|z| ≥ stop_z)
          FIRST — inside one sample both can look reached, and booking the
          happier one is how a simulation flatters itself — then TARGET
          (reverted through exit_z toward the mean), then TIME-STOP
          (time_stop_half_lives × half-life).

Settled in money, not by which level fired: an exit is WON when its net P&L
is above zero, LOST below zero, FLAT at zero. A row whose P&L cannot be
measured (lot size unknown) counts in none of them.

    gross = Δspread × lot size × lots   (SHORT: entry − exit; LONG: exit − entry)
    net   = gross − brokerage_per_lot × lots × 4
            (the trade journal's own rule: both legs, in and out)

Its limits, which the page states beside every number:
  * fills at the SAMPLED spread (last-traded A − B), so bid-ask and slippage
    are not modelled;
  * z is arrow's own: a time window of samples (window_minutes), sample σ
    (ddof=1), simple mean, half-life in samples — not candles;
  * the window is not persisted: after a restart z restarts with a fresh
    window, and an open study position is followed on that new z (the row
    says so). While the new window warms, nothing is checked;
  * the algo's daily-loss halt is not applied (it depends on real trades).

It holds no order path and writes only its own file — nothing the trading
path reads. Tests hold it to that.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional

from loguru import logger

from arrow_statarb.core.algo import _within_trading_hours
from arrow_statarb.core.arming import REENTRY, Arming, mode_of, reentry_levels
from arrow_statarb.models.probability_filter import ProbabilityFilter

MAX_ROWS = 5000
SAVE_EVERY_SEC = 5.0
RANGES = {"1d": 86400.0, "7d": 7 * 86400.0, "30d": 30 * 86400.0, "all": None}


class SignalStudy:
    def __init__(self, path: Optional[Path], params_provider: Callable[[], Dict],
                 clock: Callable[[], float] = time.time):
        self._path = Path(path) if path else None
        self._params = params_provider
        self._clock = clock
        self._lock = threading.RLock()
        self._rows: List[Dict] = []
        self._next_id = 1
        # state that gates what is recorded — survives a restart with the rows
        # (all but the arming, which never outlives the z window: arming.py)
        self._arming = Arming(default_mode="touch")
        self._armed_id: Optional[int] = None         # the armed stretch's row
        self._cooldown_until = 0.0
        self._open_id: Optional[int] = None          # the study's open position
        self._blocked_id: Optional[int] = None       # the current run's filtered row
        self._restarted = False
        self._saved_at = 0.0
        self._load()

    # ── persistence ─────────────────────────────────────────────────────────
    def _load(self) -> None:
        if not self._path or not self._path.exists():
            return
        try:
            d = json.loads(self._path.read_text())
            st = d["state"]
            rows = [r for r in d["rows"] if isinstance(r, dict)]
            cooldown = float(st["cooldown_until"])
        except Exception as exc:                               # noqa: BLE001
            logger.warning("SignalStudy: {} unreadable ({}) — starting fresh", self._path, exc)
            return
        self._rows = rows
        self._next_id = max([int(r.get("id", 0)) for r in rows] + [0]) + 1
        self._cooldown_until = cooldown
        for r in rows:                            # arming ends with the window
            if r.get("status") == "armed":
                r.update(status="never_back", lost_reason="restart")
        self._open_id = st.get("open_id")
        self._blocked_id = st.get("blocked_id")
        self._restarted = True                    # the z window starts fresh now
        logger.info("SignalStudy: restored {} row(s){}", len(rows),
                    " — one study position still open" if self._open_id else "")

    def _save(self, force: bool = False, now: Optional[float] = None) -> None:
        if not self._path:
            return
        now = self._clock() if now is None else now
        if not force and now - self._saved_at < SAVE_EVERY_SEC:
            return
        body = {"version": 1, "rows": self._rows[-MAX_ROWS:],
                "state": {"cooldown_until": self._cooldown_until,
                          "open_id": self._open_id, "blocked_id": self._blocked_id}}
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(self._path.suffix + ".tmp")
            tmp.write_text(json.dumps(body))
            os.replace(tmp, self._path)
            self._saved_at = now
        except Exception as exc:                               # noqa: BLE001
            logger.warning("SignalStudy: could not save — {}", exc)

    def _row(self, rid: Optional[int]) -> Optional[Dict]:
        if rid is None:
            return None
        for r in reversed(self._rows):
            if r.get("id") == rid:
                return r
        return None

    # ── the feed: one READY sample ──────────────────────────────────────────
    def observe(self, s: Dict) -> None:
        """``s``: ts, z, spread, leg_a, leg_b, std, half_life, sample_interval.
        Never raises into the signal engine."""
        try:
            with self._lock:
                self._observe(s)
        except Exception:                                      # noqa: BLE001
            logger.exception("SignalStudy: observe failed")

    def _observe(self, s: Dict) -> None:
        p = self._params() or {}
        ts, z = float(s["ts"]), float(s["z"])
        changed = False

        pos = self._row(self._open_id)
        if pos is not None:
            self._arming.reset()                  # in a position nothing is armed
            changed = self._follow(pos, s, p)
            self._save(force=changed, now=ts)
            return

        arm = self._arming.step(z, ts, p)
        if arm["confirming"] == 0 and not self._arming.armed:
            self._blocked_id = None               # the run beyond entry ended
        if arm["event"] == "armed":
            row = self._new_row(s, p, self._arming.armed, max(1, int(p.get("lots", 1))), {})
            row.update(status="armed", armed_ts=ts, armed_z=round(z, 4))
            self._armed_id, self._blocked_id = row["id"], None
            changed = True
        elif arm["event"] == "lost":
            r = self._row(self._armed_id)
            if r is not None:
                lost = arm["lost"]
                r.update(status="never_back", lost_reason="through the window floor",
                         lost_ts=ts, lost_z=round(z, 4), peak_z=round(lost["peak_z"], 4))
            self._armed_id = None
            changed = True
        if self._armed_id is not None:
            r = self._row(self._armed_id)
            if r is not None and self._arming.peak_z is not None:
                r["peak_z"] = round(self._arming.peak_z, 4)
        if arm["trigger"]:
            changed = self._try_enter(s, p, arm["trigger"]) or changed
        self._save(force=changed, now=ts)

    def _gate(self, s: Dict, p: Dict, lots: int):
        """The algo's entry gates, in its order. (reason, metrics) or (None, m)."""
        ts = float(s["ts"])
        if ts < self._cooldown_until:
            return "cooldown", {}
        if not _within_trading_hours(p):
            return "outside trading hours", {}
        pf = ProbabilityFilter(
            commission_per_lot=float(p.get("brokerage_per_lot", 10.0)) * 2.0,
            slippage_per_lot=float(p.get("slippage_per_lot", 5.0)) * 2.0,
            commission_basis=str(p.get("commission_basis", "per_lot")),
            lot_multiplier=float(p.get("lot_multiplier") or 1.0),
            min_win_probability=float(p.get("min_win_probability", 0.60)),
            min_expected_value=float(p.get("min_expected_value", 0.0)),
            max_half_lives=float(p.get("time_stop_half_lives", 3.0)),
            exit_zscore=float(p.get("exit_zscore", 0.0)),
            stop_zscore=float(p.get("stop_zscore", 4.0)),
            enabled=bool(p.get("enable_probability_filter", True)))
        allow, reason, metrics = pf.check_entry(float(s["z"]), float(s["std"]),
                                                float(s.get("half_life") or 0.0), contracts=lots)
        return (None if allow else reason.replace("_", " ")), metrics

    def _try_enter(self, s: Dict, p: Dict, side: str) -> bool:
        lots = max(1, int(p.get("lots", 1)))
        blocked, metrics = self._gate(s, p, lots)
        armed = self._row(self._armed_id)
        if blocked:
            if armed is not None:                 # stays armed; may still enter in the window
                armed["filter_reason"] = blocked
                armed["blocked_samples"] = int(armed.get("blocked_samples", 0)) + 1
                return False
            if self._blocked_id is not None:      # one filtered row per run
                r = self._row(self._blocked_id)
                if r is not None:
                    r["blocked_samples"] = int(r.get("blocked_samples", 1)) + 1
                return False
            row = self._new_row(s, p, side, lots, metrics)
            row.update(status="filtered", filter_reason=blocked, blocked_samples=1)
            self._blocked_id = row["id"]
            return True
        if armed is not None:                     # the armed stretch becomes the position
            row = armed
            row.update(self._entry_fields(s, metrics))
            self._armed_id = None
        else:
            row = self._new_row(s, p, side, lots, metrics)
        row.update(status="open", crossed_restart=False, filter_reason=None)
        self._open_id = row["id"]
        self._blocked_id = None
        self._restarted = False                   # opened on this run's window
        self._arming.reset()
        return True

    @staticmethod
    def _entry_fields(s: Dict, metrics: Dict) -> Dict:
        return {"entry_ts": float(s["ts"]), "entry_z": round(float(s["z"]), 4),
                "entry_spread": round(float(s["spread"]), 4),
                "entry_leg_a": s.get("leg_a"), "entry_leg_b": s.get("leg_b"),
                "entry_std": round(float(s["std"]), 6),
                "half_life_sec": (round(float(s["half_life"]) * float(s["sample_interval"]), 1)
                                  if s.get("half_life") else None),
                "win_probability": metrics.get("win_probability"),
                "expected_value": metrics.get("expected_value")}

    def _new_row(self, s: Dict, p: Dict, side: str, lots: int, metrics: Dict) -> Dict:
        mult = p.get("lot_multiplier")
        row = {"id": self._next_id, "source": "live", "side": side, "lots": lots,
               "lot_size": (float(mult) if mult else None),
               "entry_mode": mode_of(p),
               "armed_ts": None, "armed_z": None, "peak_z": None,
               "rules": {k: p.get(k) for k in ("entry_zscore", "exit_zscore", "stop_zscore",
                                               "confirmation_ticks", "time_stop_half_lives",
                                               "brokerage_per_lot", "cooldown", "entry_mode",
                                               "reentry_back", "reentry_window_pct")},
               "exit_ts": None, "exit_reason": None, "exit_z": None, "exit_spread": None,
               "held_sec": None, "mae_pts": 0.0, "mfe_pts": 0.0,
               "gross": None, "costs": None, "net": None, "result": None}
        row.update(self._entry_fields(s, metrics))
        self._next_id += 1
        self._rows.append(row)
        if len(self._rows) > MAX_ROWS:
            del self._rows[:len(self._rows) - MAX_ROWS]
        return row

    def _follow(self, pos: Dict, s: Dict, p: Dict) -> bool:
        ts, z, spread = float(s["ts"]), float(s["z"]), float(s["spread"])
        if self._restarted:
            pos["crossed_restart"] = True
        sign = -1.0 if pos["side"] == "SHORT" else 1.0      # P&L direction
        move = sign * (spread - pos["entry_spread"])
        pos["mfe_pts"] = round(max(pos.get("mfe_pts") or 0.0, move), 4)
        pos["mae_pts"] = round(min(pos.get("mae_pts") or 0.0, move), 4)

        stop_z = float(p.get("stop_zscore", 4.0))
        exit_z = float(p.get("exit_zscore", 0.0))
        # the algo's own test: a LONG (entered on z < 0) reverts UP through
        # exit_z, a SHORT reverts DOWN through it
        reverted = (z >= exit_z) if pos["side"] == "LONG" else (z <= exit_z)
        hl = float(s.get("half_life") or 0.0)
        max_hold = (float(p.get("time_stop_half_lives", 3.0)) * hl
                    * float(s.get("sample_interval", 0.5))) if hl > 0 else 0.0
        held = ts - pos["entry_ts"]
        reason = None
        if abs(z) >= stop_z:                         # STOP first — never the happier one
            reason = "stop"
        elif reverted:
            reason = "target"
        elif max_hold > 0 and held >= max_hold:
            reason = "time_stop"
        if reason is None:
            return False
        self._settle(pos, reason, ts, z, spread)
        self._open_id = None
        self._cooldown_until = ts + float(p.get("cooldown", 300))
        return True

    @staticmethod
    def _settle(pos: Dict, reason: str, ts: float, z: float, spread: float) -> None:
        pos.update(exit_ts=ts, exit_reason=reason, exit_z=round(z, 4),
                   exit_spread=round(spread, 4), held_sec=round(ts - pos["entry_ts"], 1),
                   status="closed")
        if not pos.get("lot_size"):
            return                                   # unmeasured: no money, no result
        sign = -1.0 if pos["side"] == "SHORT" else 1.0
        per_point = float(pos["lot_size"]) * int(pos["lots"])
        gross = sign * (spread - pos["entry_spread"]) * per_point
        costs = float((pos.get("rules") or {}).get("brokerage_per_lot") or 0.0) * int(pos["lots"]) * 4
        net = gross - costs
        pos.update(gross=round(gross, 2), costs=round(costs, 2), net=round(net, 2),
                   mae_inr=round(pos["mae_pts"] * per_point, 2),
                   mfe_inr=round(pos["mfe_pts"] * per_point, 2),
                   result=("won" if net > 0 else "lost" if net < 0 else "flat"))

    # ── reading ─────────────────────────────────────────────────────────────
    def report(self, range_key: str = "1d", now: Optional[float] = None) -> Dict:
        now = self._clock() if now is None else now
        span = RANGES.get(range_key, RANGES["1d"])
        with self._lock:
            rows = [dict(r) for r in self._rows
                    if span is None or r["entry_ts"] >= now - span]
        return {"range": range_key if range_key in RANGES else "1d",
                "rows": list(reversed(rows)), "summary": summarize(rows)}


def summarize(rows: List[Dict]) -> Dict:
    """Totals over the rows given. Unmeasured is None, never zero."""
    taken = [r for r in rows if r.get("status") in ("open", "closed")]
    closed = [r for r in taken if r.get("status") == "closed"]
    measured = [r for r in closed if r.get("net") is not None]
    won = [r for r in measured if r["result"] == "won"]
    losers = [r for r in measured if r["result"] == "lost"]
    flat = [r for r in measured if r["result"] == "flat"]
    filtered = [r for r in rows if r.get("status") == "filtered"]
    lost = [r for r in rows if r.get("status") == "never_back"]
    armed_now = [r for r in rows if r.get("status") == "armed"]

    def _sum(rs, k):
        return round(sum(float(r[k]) for r in rs), 2) if rs else None

    def _avg(rs, k):
        vals = [float(r[k]) for r in rs if r.get(k) is not None]
        return round(sum(vals) / len(vals), 2) if vals else None

    gross_win = sum(float(r["net"]) for r in won)
    gross_loss = -sum(float(r["net"]) for r in losers)
    equity, cum, peak, mdd = [], 0.0, 0.0, 0.0
    for r in sorted(measured, key=lambda r: r["exit_ts"]):
        cum += float(r["net"])
        peak = max(peak, cum)
        mdd = max(mdd, peak - cum)
        equity.append({"ts": r["exit_ts"], "cum": round(cum, 2)})

    by_reason: Dict[str, Dict] = {}
    for r in closed:
        b = by_reason.setdefault(r["exit_reason"], {"count": 0, "won": 0, "lost": 0,
                                                     "net": None, "avg_hold_sec": None})
        b["count"] += 1
        if r.get("result") == "won":
            b["won"] += 1
        elif r.get("result") == "lost":
            b["lost"] += 1
    for k, b in by_reason.items():
        rs = [r for r in measured if r["exit_reason"] == k]
        b["net"] = _sum(rs, "net")
        b["avg_hold_sec"] = _avg([r for r in closed if r["exit_reason"] == k], "held_sec")

    reasons: Dict[str, int] = {}
    for r in filtered:
        reasons[r["filter_reason"]] = reasons.get(r["filter_reason"], 0) + 1

    decided = len(won) + len(losers)
    armed_total = [r for r in rows if r.get("armed_ts") is not None]
    return {
        "signals": len(taken) + len(filtered) + len(lost) + len(armed_now),
        "taken": len(taken), "filtered": len(filtered), "filter_reasons": reasons,
        "armed": len(armed_total), "armed_now": len(armed_now), "never_back": len(lost),
        "open": len(taken) - len(closed), "closed": len(closed),
        "won": len(won), "lost": len(losers), "flat": len(flat),
        "unmeasured": len(closed) - len(measured),
        "win_rate": round(100.0 * len(won) / decided, 1) if decided else None,
        "net": _sum(measured, "net"), "gross": _sum(measured, "gross"),
        "costs": _sum(measured, "costs"),
        "avg_win": _avg(won, "net"), "avg_loss": _avg(losers, "net"),
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss > 0 else None,
        "expectancy": _avg(measured, "net"),
        "avg_hold_sec": _avg(closed, "held_sec"),
        "max_drawdown": round(mdd, 2) if measured else None,
        "avg_mae": _avg(measured, "mae_inr"), "avg_mfe": _avg(measured, "mfe_inr"),
        "by_exit": by_reason, "equity": equity,
    }
