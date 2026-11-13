"""Touch study — every σ touch on the Timeline, played out as the trade it
could have been. A READING: it never trades, and the real algo never reads it.

Each live touch (+2σ, +entry σ, +3σ and their − sides) opens one PAPER
position at that touch: the side its band sits on (upper → SHORT the spread
"H→L" on the sell spread; lower → LONG "L→H" on the buy spread), at the
executable spread and both legs' prices of that moment. The position is then
managed by the algo's OWN exit code — a paper ``ArrowAutoTrader`` holding it,
fed the live signal and your real settings — so its target, stop, time stops,
reversion gate and the words it exits with are exactly the algo's.

Recorded per touch: instrument, side, the edge filter's numbers (the entry
gate's capture vs needed), break-even / take-profit / stop-loss as spread
prices (the algo's ``entry_levels``), both legs' prices in and out, z and σ
in and out, status, why (the algo's own status line at the exit), held time
and net P&L (the algo's own cost estimate, as the signal study uses).

Its limits, shown on the page: a paper fill at the touch's executable spread
(no slippage beyond the algo's estimate); entry gates are reported, not
applied — every touch is played out; positions are independent (several can
be open at once on one stretch).
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections import deque
from pathlib import Path
from typing import Callable, Deque, Dict, List, Optional

from loguru import logger

from arrow_statarb.core.algo import ArrowAutoTrader

TOUCH_TYPES = ("touch_2_up", "touch_2_down", "touch_entry_up", "touch_entry_down",
               "touch_3_up", "touch_3_down")
STATUS = {"dollar_stop": "stopped out", "stop": "stopped out (z-stop)",
          "profit_target": "take profit", "target": "back to mean",
          "time_stop": "time stop", "hard_time_stop": "hard time stop",
          "trailing_stop": "trailing stop"}


class _Quiet:
    def _say(self, msg, *a, **kw):
        try:
            logger.debug("[touch study] " + str(msg), *a, **kw)
        except Exception:                                      # noqa: BLE001
            pass

    info = warning = error = exception = debug = _say


def event_key(ev: Dict) -> str:
    return f"{float(ev.get('ts') or 0):.3f}|{ev.get('type')}"


def _r(v, n=2):
    return None if v is None else round(float(v), n)


class TouchStudy:
    def __init__(self, path: Optional[Path], signal_provider: Callable[[], Dict],
                 params_provider: Callable[[], Dict],
                 pair_provider: Optional[Callable[[], Optional[str]]] = None,
                 clock: Callable[[], float] = time.time, max_rows: int = 2000):
        self._path = Path(path) if path else None
        self._signal = signal_provider
        self._params = params_provider
        self._pair = pair_provider or (lambda: None)
        self._clock = clock
        self._max_rows = max_rows
        self._lock = threading.RLock()
        self._queue: Deque[Dict] = deque(maxlen=200)
        self._rows: Dict[str, Dict] = {}
        self._open: Dict[str, ArrowAutoTrader] = {}
        self._sig: Dict = {}
        self._p: Dict = {}
        self._saved_at = 0.0
        self._load()

    # ── intake (called from the sampling thread: quick) ─────────────────────
    def on_event(self, ev: Dict) -> None:
        if ev.get("type") in TOUCH_TYPES and ev.get("spread") is not None and ev.get("std"):
            self._queue.append(dict(ev))

    # ── one tick: open the queued touches, manage the open ones ─────────────
    def tick(self) -> None:
        try:
            with self._lock:
                self._tick()
        except Exception:                                      # noqa: BLE001
            logger.exception("TouchStudy: tick failed")

    def _tick(self) -> None:
        self._sig = dict(self._signal() or {})
        self._p = dict(self._params() or {})
        changed = False
        while self._queue:
            ev = self._queue.popleft()
            if event_key(ev) not in self._rows:
                self._open_touch(ev)
                changed = True
        for key in list(self._open):
            algo = self._open[key]
            algo._tick()
            row = self._rows[key]
            st = algo.get_state() or {}
            if algo._pos is None:                  # it exited this tick:
                row["why"] = st.get("status")       # the algo's own exit line
                self._open.pop(key, None)
                changed = True
            else:
                row["why"] = st.get("status")
                row["net"] = _r(st.get("net_pnl"))
                row["held_sec"] = round(self._clock() - float(row["entry_ts"]), 1)
        if len(self._rows) > self._max_rows:
            for key in sorted(self._rows, key=lambda k: self._rows[k]["entry_ts"])[
                    :len(self._rows) - self._max_rows]:
                if key not in self._open:
                    self._rows.pop(key)
        self._save(force=changed)

    def _paper_algo(self, key: str) -> ArrowAutoTrader:
        return ArrowAutoTrader(
            signal_provider=lambda: self._sig, params_provider=lambda: self._p,
            execute_fn=lambda *a, **k: {"success": False, "error": "touch study never enters"},
            close_fn=lambda direction, lots, **kw: self._closed(key, kw),
            clock=self._clock, log=_Quiet())

    def _open_touch(self, ev: Dict) -> None:
        key = event_key(ev)
        p = self._p
        short = ev.get("side") == "sell"
        direction = "SHORT_SPREAD" if short else "LONG_SPREAD"
        lots = max(1, int(p.get("lots", 1) or 1))
        # SHORT the spread = SELL leg A at its bid, BUY leg B at its ask;
        # LONG = BUY A at its ask, SELL B at its bid. No book → the mids.
        a_px = ev.get("bid_a") if short else ev.get("ask_a")
        b_px = ev.get("ask_b") if short else ev.get("bid_b")
        a_px = a_px if a_px is not None else ev.get("mid_a")
        b_px = b_px if b_px is not None else ev.get("mid_b")
        z = float(ev.get("z_exact", ev.get("z")))
        std = float(ev["std"])
        spread = float(ev["spread"])
        algo = self._paper_algo(key)
        pos = {"direction": direction, "lots": lots, "entry_z": z,
               "entry_spread": round(spread, 4), "entry_fill_spread": spread,
               "entry_std": std, "capital_at_risk_inr": float(p.get("capital_at_risk_inr", 0) or 0),
               "entry_leg_a": a_px, "entry_leg_b": b_px, "entry_time": float(ev["ts"]),
               "band_source": self._sig.get("band_source") or "ticks",
               "band_tf": self._sig.get("band_timeframe"),
               "peak_pnl": 0.0, "trough_pnl": 0.0, "peak_min": 0.0, "trough_min": 0.0,
               "order_ids": [], "dry_run": True}
        sig_at = {"mean": ev.get("mean"), "std": std, "leg_a": ev.get("mid_a"),
                  "leg_b": ev.get("mid_b")}
        edge = algo.edge_numbers(z, std, lots, sig_at, p)
        levels = algo.entry_levels(p, pos, sig_at, auto_exits=True)
        algo._pos = pos
        self._open[key] = algo
        self._rows[key] = {
            "key": key, "type": ev.get("type"), "band": ev.get("band"), "entry": bool(ev.get("entry")),
            "instrument": ev.get("series") or self._pair(),
            "direction": direction, "side": "H→L" if short else "L→H",
            "spread_side": "sell" if short else "buy",
            "lots": lots, "lot_size": float(p.get("lot_multiplier", 1) or 1),
            "entry_ts": float(ev["ts"]), "entry_z": _r(z), "entry_spread": _r(spread),
            "entry_mean": _r(ev.get("mean")), "entry_std": _r(std, 4),
            "leg_a_in": {"side": "sell" if short else "buy", "price": _r(a_px)},
            "leg_b_in": {"side": "buy" if short else "sell", "price": _r(b_px)},
            "edge": {**edge, "ok": edge["reason"] is None},
            "break_even": _r(levels.get("break_even")),
            "take_profit": _r(levels.get("target_spread")), "take_profit_kind": levels.get("target_kind"),
            "stop_loss": _r(levels.get("stop_spread")), "stop_loss_kind": levels.get("stop_kind"),
            "status": "open", "why": "opened at the touch", "net": None, "held_sec": 0.0,
            "exit_ts": None, "exit_z": None, "exit_spread": None, "exit_std": None,
            "leg_a_out": None, "leg_b_out": None, "exit_reason": None,
        }

    def _closed(self, key: str, kw: Dict) -> Dict:
        """close_fn of a touch's paper algo: book the exit (nothing is sent)."""
        row = self._rows.get(key)
        algo = self._open.get(key)
        if row is None or algo is None:
            return {"success": True, "message": "paper"}
        sig, p = self._sig, self._p
        pos = algo._pos or {}
        short = row["direction"] == "SHORT_SPREAD"
        # closing a SHORT buys the spread back: BUY A at ask, SELL B at bid
        a_px = sig.get("ask_a") if short else sig.get("bid_a")
        b_px = sig.get("bid_b") if short else sig.get("ask_b")
        a_px = a_px if a_px is not None else sig.get("leg_a")
        b_px = b_px if b_px is not None else sig.get("leg_b")
        reason = kw.get("reason")
        spread = kw.get("spread")
        net = algo._live_net_pnl(spread, p) if spread is not None else None
        now = self._clock()
        row.update(status=STATUS.get(reason, reason or "closed"), exit_reason=reason,
                   exit_ts=now, exit_z=_r(kw.get("z")), exit_spread=_r(spread),
                   exit_std=_r(sig.get("std"), 4), exit_mean=_r(sig.get("mean")),
                   leg_a_out={"side": "buy" if short else "sell", "price": _r(a_px)},
                   leg_b_out={"side": "sell" if short else "buy", "price": _r(b_px)},
                   net=_r(net), held_sec=round(now - float(row["entry_ts"]), 1),
                   peak_pnl=_r(pos.get("peak_pnl")), trough_pnl=_r(pos.get("trough_pnl")))
        return {"success": True, "message": "paper", "dry_run": True}

    # ── reading ─────────────────────────────────────────────────────────────
    def rows_for(self, keys: List[str]) -> Dict[str, Dict]:
        with self._lock:
            return {k: dict(self._rows[k]) for k in keys if k in self._rows}

    # ── persistence ─────────────────────────────────────────────────────────
    def _save(self, force: bool = False) -> None:
        if not self._path:
            return
        now = time.time()
        if not force and now - self._saved_at < 5.0:
            return
        body = {"version": 1, "rows": list(self._rows.values())}
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(self._path.suffix + ".tmp")
            tmp.write_text(json.dumps(body))
            os.replace(tmp, self._path)
            self._saved_at = now
        except Exception as exc:                               # noqa: BLE001
            logger.warning("TouchStudy: could not save — {}", exc)

    def _load(self) -> None:
        """Rows survive a restart. A touch still open when Nexus stopped is
        not resumed (its paper position would miss the gap): it is closed as
        'interrupted by a restart'."""
        if not self._path or not self._path.exists():
            return
        try:
            rows = json.loads(self._path.read_text() or "{}").get("rows") or []
        except Exception as exc:                               # noqa: BLE001
            logger.warning("TouchStudy: unreadable {} — {}", self._path, exc)
            return
        for r in rows:
            if r.get("status") == "open":
                r.update(status="interrupted", why="Nexus restarted while this touch was open — "
                                                   "not played out across the gap")
            self._rows[r["key"]] = r
