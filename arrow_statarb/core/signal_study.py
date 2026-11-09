"""Signal study — what the algo WOULD have done, settled in money. A READING:
it never trades, and it does not need the algo to be ON.

It runs a PAPER copy of the live algo (``ArrowAutoTrader``) on the very signal
the real one reads. Every rule therefore comes from the algo's own code, not a
re-telling of it: executable sell/buy sides, trade direction, candle or tick
bands (a position keeps its own), confirmation and armed re-entry
(``entry_mode``), every entry gate, rupee and z stops/targets, time stops,
cooldowns. Its "orders" are recordings: ``execute_fn`` / ``close_fn`` write a
row here, nothing else. It logs nowhere the trading log is read.

    every signal       a row: armed (re-entry mode), filtered (a gate blocked
                       it, with the gate's own words), entered, or "never came
                       back" (armed, then fell through the re-entry window
                       floor without an entry — the trend the rule avoids)
    every exit         settled in money, from the exit the paper algo chose:
        gross  = Δspread × lot multiplier × lots  (SHORT: entry − exit)
        costs  = the algo's own round-trip cost estimate
        net    = gross − costs  →  WON > 0, LOST < 0, FLAT = 0

The paper algo's daily-loss halt and loss-streak gates read the STUDY's own
results, not the real trades — it is the algo run on its own.

Its limits, stated on the page: the paper fills at the decision spread the
algo saw (bid/ask sides when there is a book, else the mid) — slippage beyond
the algo's own cost estimate is not modelled; an arming does not survive a
restart; an open paper position does (as does its cooldown).
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional

from loguru import logger

_NUM = re.compile(r"[-+−]?₹?\d+(?:\.\d+)?")


def reason_key(reason: Optional[str]) -> str:
    """A gate's reason with its numbers taken out — "buy spread at z -2.51 —
    LONG entries are off" and "... z -2.58 ..." are the same gate."""
    text = str(reason or "blocked").split(" (")[0]
    return " ".join(_NUM.sub("…", text).split())[:60]

from arrow_statarb.core.algo import ArrowAutoTrader

MAX_ROWS = 5000
SAVE_EVERY_SEC = 5.0
MERGE_BLOCKED_SEC = 120.0                 # a refused stretch is one row, not one per tick
# The OUTCOME study: the same paper algo with every entry GATE switched off —
# trade direction, entry rule and exits kept — so each stretch that reaches the
# entry z answers one question: did its exit make or lose money?
OUTCOME_GATES_OFF = {
    "enable_probability_filter": False, "min_edge_multiple": 0.0, "cost_floor_mult": 0.0,
    "half_life_min_sec": 0.0, "half_life_max_sec": 0.0, "max_entry_zscore": 0.0,
    "cooldown": 0.0, "stop_cooldown": 0.0, "z_reset_after_stop": False,
    "max_daily_loss": 0.0, "loss_streak_pause_at": 0, "loss_streak_reduce_at": 0,
    "regime_enabled": False, "max_entry_z_divergence": 0.0, "max_entry_spread_divergence": 0.0,
}


def outcome_params(p: Dict) -> Dict:
    out = dict(p or {})
    out.update(OUTCOME_GATES_OFF)
    return out


def _verdict(r: Dict) -> str:
    """What the real algo's rules did with a stretch they did not trade."""
    reason = str(r.get("filter_reason") or "").strip()
    if reason.lower().startswith("blocked:"):
        reason = reason[8:].strip()
    if r.get("status") == "never_back":
        return "never came back" + (f" · held back by {reason}" if reason else "")
    if r.get("status") == "armed":
        return "armed, waiting" + (f" · {reason}" if reason else "")
    return "blocked: " + (reason or "a gate")


def with_algo_verdict(outcomes: List[Dict], faithful: List[Dict]) -> List[Dict]:
    """Say, for each stretch the outcome study traded, what the REAL algo's
    rules did with it: took it (a faithful paper entry at the same moment), or
    the gate that blocked it (a faithful refusal while the stretch was on)."""
    taken = [r for r in faithful if r.get("status") in ("open", "closed", "running")]
    refused = [r for r in faithful if r.get("status") in ("filtered", "never_back", "armed")]
    out = []
    for o in outcomes:
        o = dict(o)
        t0 = float(o.get("armed_ts") or o.get("entry_ts") or 0)
        t1 = float(o.get("exit_ts") or o.get("entry_ts") or t0)
        hit = next((r for r in taken if r.get("side") == o.get("side")
                    and abs(float(r.get("entry_ts") or 0) - float(o.get("entry_ts") or 0)) <= 5), None)
        if hit is not None:
            o["algo"], o["algo_net"] = "took it", hit.get("net")
        else:
            why = [r for r in refused if r.get("side") == o.get("side")
                   and t0 - 120 <= float(r.get("armed_ts") or r.get("entry_ts") or 0) <= t1 + 5]
            o["algo"] = _verdict(why[-1]) if why else None
        out.append(o)
    return out


RANGES = {"1d": 86400.0, "7d": 7 * 86400.0, "30d": 30 * 86400.0, "all": None}
IST_OFFSET = 5.5 * 3600


class _Quiet:
    """The paper algo's logger: everything at DEBUG, labelled, so its
    "ENTER"/"EXIT" lines can never be read as real trades in app.log."""

    def _say(self, msg, *a, **kw):
        try:
            logger.debug("[study paper algo] " + str(msg), *a, **kw)
        except Exception:                                      # noqa: BLE001
            pass

    info = warning = error = exception = debug = _say


class SignalStudy:
    def __init__(self, path: Optional[Path], signal_provider: Callable[[], Dict],
                 params_provider: Callable[[], Dict],
                 prices_provider: Optional[Callable] = None,
                 clock: Callable[[], float] = time.time,
                 pair_provider: Optional[Callable[[], Optional[str]]] = None,
                 source: str = "live"):
        self._path = Path(path) if path else None
        self._pair = pair_provider or (lambda: None)
        self._source = source                  # "live" | "replayed"
        self._live_since: Optional[float] = None
        self._signal = signal_provider
        self._params = params_provider
        self._clock = clock
        self._lock = threading.RLock()
        self._rows: List[Dict] = []
        self._next_id = 1
        self._open_id: Optional[int] = None
        self._armed_id: Optional[int] = None
        self._blocked_id: Optional[int] = None
        self._last_blocked_ts: Optional[int] = None
        self._saved_at = 0.0
        self._restarted = False
        self._algo = ArrowAutoTrader(
            signal_provider=self._signal, params_provider=self._paper_params,
            execute_fn=self._paper_open, close_fn=self._paper_close,
            clock=clock, prices_provider=prices_provider, log=_Quiet(),
            on_entered=self._paper_entered)
        self._load()
        if self._live_since is None:
            self._live_since = clock()        # the live record starts now

    # ── the paper algo's world ──────────────────────────────────────────────
    def _paper_params(self) -> Dict:
        p = dict(self._params() or {})
        p["day_pnl"] = self._day_net()            # its OWN results, not the real ones
        p["loss_streak"] = self._loss_streak()
        return p

    def _day_net(self) -> float:
        day = int((self._clock() + IST_OFFSET) // 86400)
        return round(sum(float(r.get("net") or 0.0) for r in self._rows
                         if r.get("exit_ts") and int((r["exit_ts"] + IST_OFFSET) // 86400) == day), 2)

    def _loss_streak(self) -> int:
        n = 0
        for r in reversed(self._rows):
            if r.get("result") == "lost":
                n += 1
            elif r.get("result") in ("won", "flat"):
                break
        return n

    def _paper_open(self, direction: str, lots: int, z: Optional[float] = None,
                    spread: Optional[float] = None, band_source: Optional[str] = None,
                    band_tf: Optional[str] = None, **_kw) -> Dict:
        sig = self._signal() or {}
        p = self._params() or {}
        now = self._clock()
        side = "LONG" if direction == "LONG_SPREAD" else "SHORT"
        row = self._row(self._armed_id)
        if row is None or row.get("side") != side:
            row = self._new_row(side, p)
        mult = p.get("lot_multiplier")
        row.update(status="open", lots=int(lots), filter_reason=None,
                   lot_size=(float(mult) if mult else None),
                   entry_ts=now, entry_z=_r(z), entry_spread=_r(spread),
                   entry_leg_a=sig.get("leg_a"), entry_leg_b=sig.get("leg_b"),
                   band_source=band_source or "ticks", band_tf=band_tf,
                   crossed_restart=False, mae_pts=0.0, mfe_pts=0.0)
        self._open_id, self._armed_id, self._blocked_id = row["id"], None, None
        self._restarted = False
        self._save(force=True)
        return {"success": True, "dry_run": True, "results": [], "message": "paper",
                "fill_spread": spread}

    def _paper_entered(self, levels: Dict) -> None:
        """The paper position's break-even / target / stop, as the algo set them."""
        row = self._row(self._open_id)
        if row is not None:
            row.update({k: (_r(v) if isinstance(v, (int, float)) else v)
                        for k, v in (levels or {}).items()})

    def _paper_close(self, direction: str, lots: int, reason: str = "",
                     z: Optional[float] = None, spread: Optional[float] = None,
                     **_kw) -> Dict:
        row = self._row(self._open_id)
        now = self._clock()
        if row is not None:
            row.update(status="closed", exit_ts=now, exit_reason=reason or "exit",
                       exit_z=_r(z), exit_spread=_r(spread),
                       held_sec=round(now - float(row["entry_ts"]), 1))
            self._settle(row)
        self._open_id = None
        self._save(force=True)
        return {"success": True, "dry_run": True, "results": [], "message": "paper"}

    def _settle(self, row: Dict) -> None:
        if not row.get("lot_size") or row.get("exit_spread") is None or row.get("entry_spread") is None:
            return                                   # unmeasured: no money, no result
        sign = -1.0 if row["side"] == "SHORT" else 1.0
        per_point = float(row["lot_size"]) * int(row["lots"])
        gross = sign * (float(row["exit_spread"]) - float(row["entry_spread"])) * per_point
        try:
            costs = float(self._algo._round_trip_cost(
                self._paper_params(), lots=int(row["lots"]),
                ref_price=row.get("entry_leg_a"), ref_b=row.get("entry_leg_b")))
        except Exception:                                      # noqa: BLE001
            costs = None
        net = gross - costs if costs is not None else None
        row.update(gross=round(gross, 2), costs=_r(costs, 2), net=_r(net, 2),
                   mae_inr=round(float(row.get("mae_pts") or 0) * per_point, 2),
                   mfe_inr=round(float(row.get("mfe_pts") or 0) * per_point, 2),
                   result=(None if net is None else
                           "won" if net > 0 else "lost" if net < 0 else "flat"))

    # ── one tick: the same cadence as the live algo ─────────────────────────
    def tick(self) -> None:
        """Run the paper algo once on the live signal, then book what it did.
        Never raises into the caller."""
        try:
            with self._lock:
                self._tick()
        except Exception:                                      # noqa: BLE001
            logger.exception("SignalStudy: tick failed")

    def _tick(self) -> None:
        sig = self._signal() or {}
        now = self._clock()
        # follow the open paper position on the side that would close it —
        # BEFORE the paper algo acts, so the exit sample counts in MAE / MFE
        row = self._row(self._open_id)
        if row is not None:
            close = sig.get("buy_spread") if row["side"] == "SHORT" else sig.get("sell_spread")
            if close is None:
                close = sig.get("spread")
            if close is not None and row.get("entry_spread") is not None:
                sign = -1.0 if row["side"] == "SHORT" else 1.0
                move = sign * (float(close) - float(row["entry_spread"]))
                row["mfe_pts"] = round(max(float(row.get("mfe_pts") or 0), move), 4)
                row["mae_pts"] = round(min(float(row.get("mae_pts") or 0), move), 4)
            if self._restarted:
                row["crossed_restart"] = True

        had_pos = self._algo._pos is not None
        self._algo._tick()
        st = self._algo.get_state()
        # the paper algo records its position only AFTER the paper "order"
        # returns — save once it has, so a restart resumes it
        changed = (self._algo._pos is not None) != had_pos

        ev = st.get("arm_event")
        if ev == "armed" and self._open_id is None:
            row = self._new_row(st.get("armed"), self._params() or {})
            row.update(status="armed", armed_ts=now, armed_z=_r(st.get("armed_z")),
                       peak_z=_r(st.get("armed_peak_z")))
            self._armed_id, self._blocked_id = row["id"], None
            changed = True
        elif ev == "lost":
            row = self._row(self._armed_id)
            if row is not None:
                lost = st.get("arm_lost") or {}
                row.update(status="never_back", lost_reason="through the window floor",
                           lost_ts=now, peak_z=_r(lost.get("peak_z")))
            self._armed_id = None
            changed = True
        if self._armed_id is not None and st.get("armed_peak_z") is not None:
            row = self._row(self._armed_id)
            if row is not None:
                row["peak_z"] = _r(st.get("armed_peak_z"))

        # a signal the paper algo wanted but a gate refused
        lb = st.get("last_blocked_signal")
        if lb and lb.get("timestamp") != self._last_blocked_ts and self._open_id is None:
            self._last_blocked_ts = lb.get("timestamp")
            changed = self._blocked(lb, now) or changed
        elif self._blocked_id is not None and (not lb or lb.get("timestamp") == self._last_blocked_ts):
            self._blocked_id = None                  # the blocked run ended
        self._save(force=changed, now=now)

    def _blocked(self, lb: Dict, now: float) -> bool:
        reason = str(lb.get("reason") or "blocked")
        if "entries are off" in reason:
            return False                  # the side the trade direction switched off: not a signal
        armed = self._row(self._armed_id)
        if armed is not None:                         # stays armed; may still enter
            armed["filter_reason"] = reason
            armed["blocked_samples"] = int(armed.get("blocked_samples", 0)) + 1
            return False
        row = self._row(self._blocked_id)
        side = lb.get("would_be_signal") or "SHORT"
        if row is None:                   # the same stretch, blocked again a little later
            last = next((r for r in reversed(self._rows) if r.get("status") == "filtered"), None)
            if last is not None and last.get("side") == side \
                    and now - float(last.get("last_blocked_ts") or last.get("entry_ts") or 0) < MERGE_BLOCKED_SEC:
                row, self._blocked_id = last, last["id"]
        if row is not None:
            # One row per blocked RUN. Its entry z / spread / reason are all
            # the FIRST sample's, so the row reads as one moment; the run's
            # deepest z goes to peak_z. A later sample only replaces the reason
            # when a DIFFERENT gate took over (not merely a new z in the text).
            row["blocked_samples"] = int(row.get("blocked_samples", 1)) + 1
            z = lb.get("zscore")
            if z is not None and (row.get("peak_z") is None
                                  or abs(float(z)) > abs(float(row["peak_z"]))):
                row["peak_z"] = _r(z)
            if reason_key(reason) != reason_key(row.get("filter_reason")):
                row["filter_reason"] = reason
            row["last_blocked_ts"] = now
            return False
        row = self._new_row(side, self._params() or {})
        row.update(status="filtered", filter_reason=reason, blocked_samples=1,
                   entry_ts=now, entry_z=_r(lb.get("zscore")),
                   entry_spread=_r(lb.get("spread")), peak_z=_r(lb.get("zscore")),
                   last_blocked_ts=now)
        self._blocked_id = row["id"]
        return True

    # ── rows ────────────────────────────────────────────────────────────────
    def _new_row(self, side: Optional[str], p: Dict) -> Dict:
        now = self._clock()
        row = {"id": self._next_id, "source": self._source, "side": side or "SHORT",
               "pair": self._pair(),
               "entry_mode": str(p.get("entry_mode") or "touch"),
               "lots": max(1, int(p.get("lots", 1) or 1)), "lot_size": None,
               "armed_ts": None, "armed_z": None, "peak_z": None,
               "entry_ts": now, "entry_z": None, "entry_spread": None,
               "rules": {k: p.get(k) for k in (
                   "entry_zscore", "exit_zscore", "stop_zscore", "confirmation_ticks",
                   "entry_mode", "reentry_back", "reentry_window_pct", "cooldown",
                   "band_source", "trade_direction")},
               "exit_ts": None, "exit_reason": None, "exit_z": None, "exit_spread": None,
               "held_sec": None, "mae_pts": 0.0, "mfe_pts": 0.0,
               "gross": None, "costs": None, "net": None, "result": None}
        self._next_id += 1
        self._rows.append(row)
        if len(self._rows) > MAX_ROWS:
            del self._rows[:len(self._rows) - MAX_ROWS]
        return row

    def _row(self, rid: Optional[int]) -> Optional[Dict]:
        if rid is None:
            return None
        for r in reversed(self._rows):
            if r.get("id") == rid:
                return r
        return None

    # ── persistence ─────────────────────────────────────────────────────────
    def _load(self) -> None:
        if not self._path or not self._path.exists():
            return
        try:
            d = json.loads(self._path.read_text())
            rows = [r for r in d["rows"] if isinstance(r, dict)]
            st = d["state"]
        except Exception as exc:                               # noqa: BLE001
            logger.warning("SignalStudy: {} unreadable ({}) — starting fresh", self._path, exc)
            return
        self._rows = rows
        self._next_id = max([int(r.get("id", 0)) for r in rows] + [0]) + 1
        try:
            self._live_since = float(st.get("live_since")) if st.get("live_since") else None
        except (TypeError, ValueError):
            self._live_since = None
        for r in rows:                                # an arming ends with the process
            if r.get("status") == "armed":
                r.update(status="never_back", lost_reason="restart")
        self._open_id = st.get("open_id")
        pos = st.get("paper_position")
        if self._open_id is not None and isinstance(pos, dict):
            self._algo._pos = dict(pos)               # the paper algo resumes managing it
            self._restarted = True
        else:
            self._open_id = None
        try:
            self._algo._cooldown_until = float(st.get("cooldown_until") or 0.0)
        except (TypeError, ValueError):
            pass
        logger.info("SignalStudy: restored {} row(s){}", len(rows),
                    " — one paper position still open" if self._open_id else "")

    def _save(self, force: bool = False, now: Optional[float] = None) -> None:
        if not self._path:
            return
        now = self._clock() if now is None else now
        if not force and now - self._saved_at < SAVE_EVERY_SEC:
            return
        body = {"version": 2, "rows": self._rows[-MAX_ROWS:],
                "state": {"open_id": self._open_id, "live_since": self._live_since,
                          "paper_position": self._algo._pos,
                          "cooldown_until": self._algo._cooldown_until}}
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(self._path.suffix + ".tmp")
            tmp.write_text(json.dumps(body, default=str))
            os.replace(tmp, self._path)
            self._saved_at = now
        except Exception as exc:                               # noqa: BLE001
            logger.warning("SignalStudy: could not save — {}", exc)

    # ── reading ─────────────────────────────────────────────────────────────
    def report(self, range_key: str = "1d", now: Optional[float] = None,
               day: Optional[str] = None, pair: Optional[str] = None) -> Dict:
        """Rows for a rolling range (1d | 7d | 30d | all), or one IST trading
        DAY ('YYYY-MM-DD', which wins over the range), optionally one pair."""
        now = self._clock() if now is None else now
        span = RANGES.get(range_key, RANGES["1d"])
        lo = hi = None
        if day:
            try:
                y, m, d = (int(x) for x in day.split("-"))
                import calendar
                lo = calendar.timegm((y, m, d, 0, 0, 0)) - IST_OFFSET
                hi = lo + 86400
            except Exception:                              # noqa: BLE001
                lo = hi = None
        with self._lock:
            rows = [dict(r) for r in self._rows
                    if (lo is not None and lo <= float(r.get("entry_ts") or 0) < hi)
                    or (lo is None and (span is None or float(r.get("entry_ts") or 0) >= now - span))]
            pairs = sorted({str(r.get("pair")) for r in self._rows if r.get("pair")})
            status = self._algo.get_state().get("status")
            derived = [r for r in self._rows if r.get("source") == "replayed"]
        if pair:
            rows = [r for r in rows if r.get("pair") == pair]
        rows.sort(key=lambda r: float(r.get("entry_ts") or 0))
        return {"range": range_key if range_key in RANGES else "1d", "day": day if lo else None,
                "rows": list(reversed(rows)), "summary": summarize(rows), "pairs": pairs,
                "paper_status": status, "live_since": self._live_since,
                "derived": {"count": len(derived),
                            "from": min((r["entry_ts"] for r in derived), default=None),
                            "to": max((r["entry_ts"] for r in derived), default=None)}}

    @property
    def live_since(self) -> float:
        """Where the live record begins: a derived row is only ever earlier."""
        live = [float(r["entry_ts"]) for r in self._rows
                if r.get("source") != "replayed" and r.get("entry_ts")]
        return min([self._live_since or self._clock()] + live)

    def replace_derived(self, rows: List[Dict]) -> int:
        """Swap in a fresh set of replayed rows: every earlier replayed row
        goes, every live row stays untouched, and only rows from BEFORE the
        live record began are kept (the two never overlap or double-count)."""
        with self._lock:
            until = self.live_since
            keep = [r for r in self._rows if r.get("source") != "replayed"]
            new = []
            for r in rows:
                if float(r.get("entry_ts") or 0) >= until:
                    continue
                r = dict(r, source="replayed", id=self._next_id)
                if r.get("status") in ("open", "armed"):
                    r["status"] = "running" if r["status"] == "open" else "never_back"
                self._next_id += 1
                new.append(r)
            self._rows = sorted(keep + new, key=lambda r: float(r.get("entry_ts") or 0))
            self._save(force=True)
        return len(new)


def replay_candles(bars: List, params: Dict, tf: str, tf_sec: int, length: int,
                   k: float = 1.0, pair: Optional[str] = None) -> List[Dict]:
    """Run the paper algo over candle history, one look per closed candle.

    ``bars``: [(bucket_start, close_a, close_b, ...)] from candles.align_legs.
    The bands are the algo's own candle bands — BB(length) with an EMA basis
    and population σ, the current candle included — evaluated at each candle's
    close; the paper algo's clock is the candle's close time. What it cannot
    see: anything inside a candle (a stop touched and recovered within one is
    missed), bid/ask (it trades at the candle close), the trading-hours and
    regime gates (candles exist only while the market is open)."""
    from arrow_statarb.core.candles import pine_stdev
    n = max(2, int(length))
    closes = [k * float(b[1]) - float(b[2]) for b in bars]
    alpha = 2.0 / (n + 1.0)
    means: List[Optional[float]] = []
    stds: List[Optional[float]] = []
    ema = None
    for i, c in enumerate(closes):
        if i + 1 >= n:
            ema = sum(closes[:n]) / n if ema is None else alpha * c + (1.0 - alpha) * ema
            means.append(ema)
            stds.append(pine_stdev(closes[:i + 1], n))
        else:
            means.append(None)
            stds.append(None)
    cur = {"i": 0}

    def sig() -> Dict:
        i = cur["i"]
        m, sd, c = means[i], stds[i], closes[i]
        ready = m is not None and sd is not None and sd > 1e-12
        out = {"spread": c, "leg_a": float(bars[i][1]), "leg_b": float(bars[i][2]),
               "ready": ready, "band_source": "candles", "band_timeframe": tf,
               "band_length": n, "half_life": 0.0, "half_life_sec": 0.0,
               "sample_interval_sec": float(tf_sec), "regime": None, "regime_detail": {},
               "candles_have": i + 1, "candles_need": n}
        if ready:
            out.update(zscore=(c - m) / sd, mean=m, std=sd,
                       bands={tf: {"mean": m, "std": sd}})
        else:
            out.update(zscore=None, mean=None, std=None)
        return out

    p = dict(params)
    # Live, confirmation is a few SAMPLES (seconds); a candle is minutes. One
    # closed candle beyond the level is the replay's confirmation — counting
    # ticks as candles would demand 45 minutes beyond ±entry at 3 × 15 min.
    p.update(trading_hours={"enabled": False}, no_entry_buffer_min=0,
             regime_enabled=False, max_entry_spread_divergence=0, confirmation_ticks=1)
    study = SignalStudy(None, sig, lambda: p, clock=lambda: float(bars[cur["i"]][0]) + tf_sec,
                        pair_provider=lambda: pair, source="replayed")
    for i in range(len(bars)):
        cur["i"] = i
        study.tick()
    return study._rows


def _r(v, d: int = 4):
    return None if v is None else round(float(v), d)


def summarize(rows: List[Dict]) -> Dict:
    """Totals over the rows given. Unmeasured is None, never zero."""
    taken = [r for r in rows if r.get("status") in ("open", "closed", "running")]
    closed = [r for r in taken if r.get("status") == "closed"]
    measured = [r for r in closed if r.get("net") is not None]
    won = [r for r in measured if r["result"] == "won"]
    losers = [r for r in measured if r["result"] == "lost"]
    flat = [r for r in measured if r["result"] == "flat"]
    filtered = [r for r in rows if r.get("status") == "filtered"]
    never = [r for r in rows if r.get("status") == "never_back"]
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
        b = by_reason.setdefault(r.get("exit_reason") or "exit", {"count": 0, "won": 0, "lost": 0,
                                                     "net": None, "avg_hold_sec": None})
        b["count"] += 1
        if r.get("result") == "won":
            b["won"] += 1
        elif r.get("result") == "lost":
            b["lost"] += 1
    for k, b in by_reason.items():
        b["net"] = _sum([r for r in measured if (r.get("exit_reason") or "exit") == k], "net")
        b["avg_hold_sec"] = _avg([r for r in closed if (r.get("exit_reason") or "exit") == k], "held_sec")

    reasons: Dict[str, int] = {}
    for r in filtered:
        key = reason_key(r.get("filter_reason"))
        reasons[key] = reasons.get(key, 0) + 1

    decided = len(won) + len(losers)
    return {
        "signals": len(taken) + len(filtered) + len(never) + len(armed_now),
        "taken": len(taken), "filtered": len(filtered), "filter_reasons": reasons,
        "armed": len([r for r in rows if r.get("armed_ts") is not None]),
        "armed_now": len(armed_now), "never_back": len(never),
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
