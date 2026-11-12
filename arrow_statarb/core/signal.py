"""Server-side signal engine — the SINGLE source of truth for the spread/z.

Both the auto-trader (``core/algo.py``) and the web dashboard read this one
engine, so the z-score they show/act on is always identical (the alignment
requirement). It samples live leg prices into a TIME-based rolling window
(``signal.window_minutes``, default 120) every ``signal.sample_interval_sec``,
and computes mean/std/z plus a mean-reversion half-life over that window.

Convention (Arrow fact #10):
    spread = k × leg_a − leg_b ; z = (spread − mean) / std

The window is built from the MID of each leg's book (last trade when the feed
carries no book). Two EXECUTABLE prices are published beside it:

    sell_spread = k × bid_a − ask_b    (what selling the spread gets: sell A, buy B)
    buy_spread  = k × ask_a − bid_b    (what buying the spread costs:  buy A, sell B)

each with its own z against the same mean/std, so an entry or exit is judged on
the price it would actually trade at, not the mid.
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections import deque
from pathlib import Path
from typing import Callable, Deque, Dict, List, Optional, Tuple

import numpy as np
from loguru import logger


class SignalEngine:
    """Samples the spread into a time window and serves the live signal."""

    def __init__(
        self,
        prices_provider: Callable[[], Tuple[Optional[float], Optional[float]]],
        params_provider: Callable[[], Dict],
        persist_path: Optional[Path] = None,
        series_key_provider: Optional[Callable[[], str]] = None,
        book_provider: Optional[Callable[[], Optional[Dict]]] = None,
        session_provider: Optional[Callable[[], bool]] = None,
        candles=None,
        excursions_path: Optional[Path] = None,
        pair_warmup_path: Optional[Path] = None,
    ):
        # After an INSTRUMENT CHANGE the candle bands are ready as soon as the
        # new pair's history loads, while its live quotes are minutes old. A
        # pair warm-up then also requires the "Warm-up before trading"
        # (min_signal_minutes) of live samples on the new pair. Kept in its own
        # file so a restart part-way through cannot skip it.
        self._pw_path = Path(pair_warmup_path) if pair_warmup_path else None
        self._pw: Optional[Dict] = None          # {"key": series key, "since": ts}
        self._pw_loaded = False
        # Where the excursion tally AND its arming state live between runs.
        # None = in memory only (tests, backtests).
        self._exc_path = Path(excursions_path) if excursions_path else None
        self._exc_saved_at = 0.0
        # Optional SpreadCandles: TradingView-style candle bands (EMA basis).
        # Fed with every sample; used for mean/σ when signal.band_source is
        # "candles" (and always published per timeframe, so a position keeps
        # the bands it entered with when the setting changes).
        self.candles = candles
        self._prices = prices_provider
        # Optional top-of-book source: {"leg_a": {bid, ask, ltp, ts}, "leg_b": {…}}.
        # When it answers, the window samples the MID and the executable
        # sell/buy spreads are published; otherwise prices_provider (LTP) is used.
        self._book_provider = book_provider
        # Optional "are both legs' exchanges open?" — no sampling while shut, so
        # a frozen price from a closed market never enters the window.
        self._session_provider = session_provider
        self._book: Optional[Dict] = None        # latest book, for get_signal
        self._sample_status = ""                 # why the last sample was skipped
        self._k_last: Optional[float] = None     # hedge ratio the window was built with
        self._stats_cache: Optional[Tuple[float, float, float]] = None
        self._params = params_provider
        # Optional disk persistence of the rolling window so a quick restart
        # resumes the warm-up instead of re-collecting it (with strict freshness
        # + same-series guards on restore — see _restore).
        self._persist_path = Path(persist_path) if persist_path else None
        self._series_key_provider = series_key_provider
        self._last_persist = 0.0

        # (timestamp, leg_a, leg_b, spread)
        self._samples: Deque[Tuple[float, float, float, float]] = deque()
        self._lock = threading.RLock()

        self._thread: Optional[threading.Thread] = None
        self._stop_evt = threading.Event()
        self.running = False
        self._last_error = ""

        # ── z-score excursion counters (session-cumulative; manual reset) ─────
        # Counts how often z stretches out to ±2σ / ±3σ and how often a stretch
        # (|z| ≥ 2) reverts back through the mean — the mean-reversion frequency.
        self._exc: Dict = self._fresh_exc()
        self._exc_events: Deque[Dict] = deque(maxlen=500)   # timestamped event log
        self._z_prev: Optional[float] = None
        # per-side arming so boundary chatter near a band isn't double-counted:
        # a band only re-arms once z falls back inside the re-arm zone (|z|<1).
        self._arm_2u = self._arm_3u = self._arm_2d = self._arm_3d = True
        self._arm_eu = self._arm_ed = True          # the entry band (entry_zscore)
        self._active_up = self._active_dn = False   # a ≥2σ excursion is open
        self._load_exc_state()
        # Regime detection: a "morning anchor" frozen once per IST day after
        # warm-up, against which trend/range is measured.
        self._anchor: Optional[float] = None
        self._anchor_day: Optional[int] = None

    # ── excursion persistence ────────────────────────────────────────────────
    # The counts and the arming that gates them are ONE state: counts restored
    # without their arming re-count a stretch that was already counted (a
    # spread parked beyond +2σ logs a fresh touch after every restart), and
    # arming without counts forgets what it was protecting. Both or neither.
    _ARMING = ("_arm_2u", "_arm_3u", "_arm_2d", "_arm_3d", "_active_up", "_active_dn",
               "_arm_eu", "_arm_ed")
    #: arming added later — a state file without them still restores (armed)
    _ARMING_OPTIONAL = ("_arm_eu", "_arm_ed")
    _EXC_SAVE_EVERY_SEC = 5.0          # _z_prev alone moves every sample

    def _arming_state(self) -> Tuple:
        return tuple(getattr(self, a) for a in self._ARMING)

    def _load_exc_state(self) -> None:
        if not self._exc_path or not self._exc_path.exists():
            return
        try:
            d = json.loads(self._exc_path.read_text())
            exc = self._fresh_exc()
            for k in exc:
                if k in (d.get("exc") or {}):
                    exc[k] = type(exc[k])(d["exc"][k])
            arming = d.get("arming") or {}
            for a in self._ARMING:
                if a in self._ARMING_OPTIONAL and a not in arming:
                    arming[a] = True
                if not isinstance(arming.get(a), bool):
                    raise ValueError(f"arming state {a} missing")
            zp = d.get("z_prev")
            events = [e for e in (d.get("events") or []) if isinstance(e, dict)]
        except Exception as exc_:                          # noqa: BLE001
            # Unreadable → start fresh, loudly; never half-restore (counts
            # without arming is the bug this file exists to prevent).
            logger.warning("SignalEngine: excursion state {} unreadable ({}) — "
                           "starting a fresh tally", self._exc_path, exc_)
            return
        self._exc = exc
        for a in self._ARMING:
            setattr(self, a, arming[a])
        self._z_prev = float(zp) if zp is not None else None
        if d.get("basis"):
            self._exc_basis = d["basis"]          # a later switch re-arms (see above)
        self._exc_events.extend(events[-self._exc_events.maxlen:])
        logger.info("SignalEngine: excursion tally restored — {} event(s) since {}",
                    len(self._exc_events),
                    time.strftime("%Y-%m-%d %H:%M", time.localtime(exc["since"])))

    def _save_exc_state(self, force: bool = False, now: Optional[float] = None) -> None:
        """Write counts + arming + events atomically. Immediately when anything
        that gates counting changed (``force``); otherwise at most every few
        seconds, for ``_z_prev``."""
        if not self._exc_path:
            return
        now = time.time() if now is None else now
        if not force and now - self._exc_saved_at < self._EXC_SAVE_EVERY_SEC:
            return
        body = {"version": 1, "basis": getattr(self, "_exc_basis", None),
                "exc": dict(self._exc),
                "arming": {a: getattr(self, a) for a in self._ARMING},
                "z_prev": self._z_prev,
                "events": list(self._exc_events)}
        try:
            self._exc_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._exc_path.with_suffix(self._exc_path.suffix + ".tmp")
            tmp.write_text(json.dumps(body))
            os.replace(tmp, self._exc_path)               # never a half-written file
            self._exc_saved_at = now
        except Exception as exc_:                          # noqa: BLE001
            logger.warning("SignalEngine: could not save excursion state — {}", exc_)

    @staticmethod
    def _fresh_exc() -> Dict:
        return {"touch_2_up": 0, "touch_2_down": 0, "touch_3_up": 0,
                "touch_3_down": 0, "touch_entry_up": 0, "touch_entry_down": 0,
                "reversions": 0, "max_z": 0.0, "min_z": 0.0,
                "since": time.time()}

    # ── params ───────────────────────────────────────────────────────────────
    def _p(self) -> Dict:
        p = {
            "window_minutes": 120.0,
            "sample_interval_sec": 0.5,
            "min_signal_minutes": 10.0,
            "entry_zscore": 2.0,
            "exit_zscore": 0.0,
            "stop_zscore": 4.0,
            # Stats-update interval (s): recompute the rolling mean/std only this
            # often (cache between), so the bands stay STABLE instead of shifting
            # every tick — the z still uses the live spread against the (possibly
            # slightly older) mean/std. 0 = recompute every tick (bands drift).
            "stats_update_interval_sec": 0.0,
            # Non-1:1 pairs mode: spread = hedge_ratio × leg_a − leg_b, so the two
            # legs are put on the SAME price scale (e.g. an ETF vs its index
            # future, ~90× apart). 1.0 = same-scale legs (calendar / cash-future),
            # i.e. the original raw-difference behaviour.
            "hedge_ratio": 1.0,
            # Skip a sample when either leg's last tick is older than this (s):
            # a stalled feed must not add flat, fake-calm samples. 0 = off.
            "max_quote_age_sec": 60.0,
        }
        try:
            p.update({k: float(v) for k, v in (self._params() or {}).items()
                      if k in p and v is not None})
        except Exception:
            pass
        return p

    def _band_params(self) -> Tuple[str, str, int]:
        """(band_source, timeframe, length) — 'ticks' unless candles are set up."""
        from arrow_statarb.core.candles import normalise_tf
        try:
            raw = self._params() or {}
        except Exception:
            raw = {}
        src = str(raw.get("band_source", "ticks") or "ticks").lower()
        if src != "candles" or self.candles is None:
            src = "ticks"
        try:
            n = max(2, int(float(raw.get("band_length", 20) or 20)))
        except (TypeError, ValueError):
            n = 20
        return src, normalise_tf(raw.get("band_timeframe", "15m")), n

    # ── control ──────────────────────────────────────────────────────────────
    def start(self) -> bool:
        with self._lock:
            if self.running:
                return False
            self._restore()               # resume the window from disk if fresh
            self._stop_evt.clear()
            self.running = True
            self._thread = threading.Thread(target=self._loop, daemon=True, name="SignalEngine")
            self._thread.start()
            logger.info("SignalEngine: started")
            return True

    def stop(self) -> None:
        self._stop_evt.set()
        self.running = False
        self._persist()                   # best-effort save so a restart resumes
        with self._lock:
            self._save_exc_state(force=True)
        logger.info("SignalEngine: stopped")

    def reset(self) -> None:
        with self._lock:
            self._samples.clear()
            self._stats_cache = None      # never serve stats from the old window
            self._reset_exc_state()       # series discontinues; counters preserved
            self._save_exc_state(force=True)
            self._clear_persisted()       # don't let a restore undo an intentional reset

    def _reset_exc_state(self) -> None:
        """Clear the transient crossing state (not the counters)."""
        self._z_prev = None
        self._arm_2u = self._arm_3u = self._arm_2d = self._arm_3d = True
        self._arm_eu = self._arm_ed = True
        self._active_up = self._active_dn = False

    def reset_excursions(self) -> None:
        """Zero the session-cumulative z-score excursion counters (Reset button)."""
        with self._lock:
            self._exc = self._fresh_exc()
            self._exc_events.clear()
            self._reset_exc_state()
            self._save_exc_state(force=True)
        logger.info("SignalEngine: excursion counters reset")

    def get_excursions(self, max_events: int = 100) -> Dict:
        """Snapshot of the z-score excursion counters + recent timestamped events
        (newest first) for the Analysis page."""
        with self._lock:
            e = dict(self._exc)
            events = list(self._exc_events)
        e["touch_2_total"] = e["touch_2_up"] + e["touch_2_down"]
        e["touch_3_total"] = e["touch_3_up"] + e["touch_3_down"]
        e["since_sec"] = round(max(0.0, time.time() - e.get("since", time.time())), 0)
        e["max_z"] = round(e["max_z"], 2)
        e["min_z"] = round(e["min_z"], 2)
        e["event_count"] = len(events)
        e["basis"] = getattr(self, "_exc_basis", None)   # the z it counts on
        e["entry_zscore"] = self._p()["entry_zscore"]   # the entry band it also counts
        e["events"] = list(reversed(events))[:max_events]   # newest first, capped
        return e

    def _tally_z(self, z: float, ts: Optional[float] = None) -> None:
        """Tally one z for both sides (no book: the sell and buy z are the
        same number). Kept for callers that have a single z."""
        self._tally_sides(z, z, z, ts)

    def _tally_sides(self, z_sell: Optional[float], z_buy: Optional[float],
                     z_mid: Optional[float], ts: Optional[float] = None,
                     spreads: Tuple[Optional[float], Optional[float]] = (None, None)) -> None:
        """Update excursion counters from the latest z. Called once per sample.

        Counted on the SAME z the Desk shows and the algo trades on: the upper
        side on the SELL spread's z (k·bid_A − ask_B — where a short would
        enter), the lower side on the BUY spread's z (k·ask_A − bid_B — where a
        long would enter). A 'touch' of ±2σ / ±3σ / ±entry is counted on the
        OUTWARD crossing of that band, then disarmed until that side's z is
        back inside |z| < 1 (hysteresis). A 'reversion' is counted when an open
        ≥2σ excursion comes back to the mean on the side that would CLOSE it:
        an upper stretch when the buy z is ≤ 0, a lower one when the sell z is
        ≥ 0. Each counted event is appended to the timestamped event log."""
        ts = time.time() if ts is None else ts
        e = self._exc
        before = (self._arming_state(), len(self._exc_events))
        self._tally_sides_inner(z_sell, z_buy, z_mid, ts, e, spreads)
        changed = (self._arming_state(), len(self._exc_events)) != before
        self._save_exc_state(force=changed, now=ts)

    def _tally_sides_inner(self, zs: Optional[float], zb: Optional[float],
                           zm: Optional[float], ts: float, e: Dict,
                           spreads: Tuple[Optional[float], Optional[float]]) -> None:
        entry = abs(float(self._p().get("entry_zscore", 2.0) or 0.0))
        # The entry band is its own event unless it IS the 2σ / 3σ band.
        entry_own = entry > 0 and abs(entry - 2.0) > 1e-9 and abs(entry - 3.0) > 1e-9
        sp_sell, sp_buy = spreads

        if zs is not None:                                   # upper side: sell z
            if zs > e["max_z"]:
                e["max_z"] = zs
            for lvl, flag, cnt in ((2.0, "_arm_2u", "touch_2_up"),
                                   (entry if entry_own else None, "_arm_eu", "touch_entry_up"),
                                   (3.0, "_arm_3u", "touch_3_up")):
                if lvl is not None and zs >= lvl and getattr(self, flag):
                    e[cnt] = e.get(cnt, 0) + 1
                    setattr(self, flag, False)
                    if lvl >= 2.0:
                        self._active_up = True
                    self._log_event(cnt, zs, ts, band=lvl, side="sell", spread=sp_sell,
                                    entry=(abs(lvl - entry) < 1e-9))
            if zs < 1.0:
                self._arm_2u = self._arm_3u = self._arm_eu = True

        if zb is not None:                                   # lower side: buy z
            if zb < e["min_z"]:
                e["min_z"] = zb
            for lvl, flag, cnt in ((2.0, "_arm_2d", "touch_2_down"),
                                   (entry if entry_own else None, "_arm_ed", "touch_entry_down"),
                                   (3.0, "_arm_3d", "touch_3_down")):
                if lvl is not None and zb <= -lvl and getattr(self, flag):
                    e[cnt] = e.get(cnt, 0) + 1
                    setattr(self, flag, False)
                    if lvl >= 2.0:
                        self._active_dn = True
                    self._log_event(cnt, zb, ts, band=-lvl, side="buy", spread=sp_buy,
                                    entry=(abs(lvl - entry) < 1e-9))
            if zb > -1.0:
                self._arm_2d = self._arm_3d = self._arm_ed = True

        # Back to the mean, on the side that would close the stretch.
        if self._active_up and zb is not None and zb <= 0.0:
            e["reversions"] += 1
            self._active_up = False
            self._log_event("reversion", zb, ts, band=0.0, side="buy", spread=sp_buy)
        if self._active_dn and zs is not None and zs >= 0.0:
            e["reversions"] += 1
            self._active_dn = False
            self._log_event("reversion", zs, ts, band=0.0, side="sell", spread=sp_sell)
        if zm is not None:
            self._z_prev = zm

    def _log_event(self, etype: str, z: float, ts: float, band: Optional[float] = None,
                   side: Optional[str] = None, spread: Optional[float] = None,
                   entry: bool = False) -> None:
        self._exc_events.append({"ts": ts, "basis": getattr(self, "_exc_basis", "ticks"),
                                 "time": time.strftime("%H:%M:%S", time.localtime(ts)),
                                 "type": etype, "z": round(z, 2),
                                 "band": band, "side": side, "entry": bool(entry),
                                 "spread": round(spread, 4) if spread is not None else None})

    # ── sampling loop ────────────────────────────────────────────────────────
    def _loop(self) -> None:
        while not self._stop_evt.is_set():
            interval = 0.5
            try:
                interval = max(0.05, self._p()["sample_interval_sec"])
                self.sample_once()
                self._maybe_persist()
            except Exception as exc:
                self._last_error = str(exc)
                logger.exception("SignalEngine: sample error")
            self._stop_evt.wait(interval)

    @staticmethod
    def _mid(leg: Optional[Dict]) -> Optional[float]:
        """Mid of a leg's book, else its last trade, else None."""
        if not leg:
            return None
        bid, ask = leg.get("bid"), leg.get("ask")
        if bid and ask and bid > 0 and ask > 0:
            return (float(bid) + float(ask)) / 2.0
        ltp = leg.get("ltp")
        return float(ltp) if ltp and ltp > 0 else None

    def _read_prices(self, now: float) -> Tuple[Optional[float], Optional[float]]:
        """(leg_a, leg_b) to sample — book mids when available — or (None, None)
        with ``_sample_status`` saying why nothing was sampled."""
        if self._session_provider is not None:
            try:
                if not self._session_provider():
                    self._sample_status = "market closed"
                    return None, None
            except Exception:
                pass
        book = None
        if self._book_provider is not None:
            try:
                book = self._book_provider()
            except Exception:
                book = None
        if book and book.get("leg_a") and book.get("leg_b"):
            a, b = book["leg_a"], book["leg_b"]
            max_age = float(self._p().get("max_quote_age_sec", 0) or 0)
            if max_age > 0:
                ages = [now - float(x.get("ts") or 0) for x in (a, b) if x.get("ts")]
                if ages and max(ages) > max_age:
                    self._book = book
                    self._sample_status = f"stale quotes (last tick {max(ages):.0f}s ago)"
                    return None, None
            self._book = book
            la, lb = self._mid(a), self._mid(b)
            if la is not None and lb is not None:
                self._sample_status = ""
                return la, lb
        self._book = book if book else None
        la, lb = self._prices()
        self._sample_status = "" if (la and lb) else "waiting for prices"
        return la, lb

    def _check_hedge_ratio(self, k: float) -> None:
        """A window mixes spreads from ONE hedge ratio only. A change of k is a
        different series: discard the window rather than blend the two."""
        if self._k_last is not None and abs(k - self._k_last) > 1e-12 and self._samples:
            logger.info("SignalEngine: hedge ratio changed {} → {} — window reset",
                        self._k_last, k)
            self._samples.clear()
            self._stats_cache = None
            self._reset_exc_state()
        self._k_last = k

    def sample_once(self, now: Optional[float] = None) -> None:
        """Take one spread sample from the live feed and trim the window.
        Exposed (not just internal) so tests can drive it deterministically."""
        now = time.time() if now is None else now
        la, lb = self._read_prices(now)
        if la is None or lb is None or la <= 0 or lb <= 0:
            return
        k = self._p().get("hedge_ratio", 1.0) or 1.0
        spread = k * float(la) - float(lb)         # non-1:1 pairs: scale leg_a to leg_b
        window_sec = self._p()["window_minutes"] * 60.0
        with self._lock:
            self._check_hedge_ratio(k)
            self._samples.append((now, float(la), float(lb), spread))
            self._trim(now, window_sec)
        # Candles first: the tally must read the bands the Desk reads, which
        # include this sample's price in the current candle.
        self._feed_candles(now, la, lb)
        with self._lock:
            self._update_excursions_locked(now)

    def _feed_candles(self, now: float, la: float, lb: float) -> None:
        """Candles are built from each leg's LAST TRADE, as a TradingView
        spread chart is; the book mid is only the fallback without an LTP."""
        if self.candles is None:
            return
        book = self._book or {}
        ta = (book.get("leg_a") or {}).get("ltp") or la
        tb = (book.get("leg_b") or {}).get("ltp") or lb
        try:
            self.candles.update(now, float(ta), float(tb),
                                k=float(self._p().get("hedge_ratio", 1.0) or 1.0))
        except Exception as exc:                      # noqa: BLE001 — never stop sampling
            logger.debug("SignalEngine: candle update failed: {}", exc)

    def push(self, leg_a: float, leg_b: float, ts: Optional[float] = None) -> None:
        """Inject a sample directly (used by tests)."""
        ts = time.time() if ts is None else ts
        k = self._p().get("hedge_ratio", 1.0) or 1.0
        with self._lock:
            self._check_hedge_ratio(k)
            self._samples.append((ts, float(leg_a), float(leg_b), k * float(leg_a) - float(leg_b)))
            self._trim(ts, self._p()["window_minutes"] * 60.0)
            self._update_excursions_locked(ts)

    def _update_excursions_locked(self, ts: Optional[float] = None) -> None:
        """Feed the excursion tally the z the Desk shows and the algo trades on.
        Must be called while holding ``self._lock``.

        Same bands (``_live_bands``: the candle EMA/σ when band_source is
        candles, else the tick window) and the same prices (the executable
        sell / buy spreads from the book; the mid when there is no book), so
        an event's z is the z the Desk showed at that moment. Not ready (too
        few candles / a warming window) → nothing counted, arming untouched."""
        if len(self._samples) < 2:
            return
        p = self._p()
        src, tf, n_len = self._band_params()
        basis = (f"candles:{tf}:{n_len}" if src == "candles" else "ticks") + ":exec"
        if basis != getattr(self, "_exc_basis", basis):
            self._reset_exc_state()           # a different z: a stretch starts afresh
        self._exc_basis = basis
        now = self._samples[-1][0] if ts is None else ts
        try:
            bands = self._live_bands(self._samples, p, now)
        except Exception:
            return
        mean, std = bands["mean"], bands["std"]
        if not bands["ready"] or std <= 1e-12:
            return
        z_mid = (self._samples[-1][3] - mean) / std
        ex = self._exec_spreads(self._book, p.get("hedge_ratio", 1.0) or 1.0)
        if ex is None:                        # LTP-only feed: both sides on the mid
            self._tally_sides(z_mid, z_mid, z_mid, ts, spreads=(self._samples[-1][3],) * 2)
            return
        zs = (ex["sell"] - mean) / std if ex["sell"] is not None else None
        zb = (ex["buy"] - mean) / std if ex["buy"] is not None else None
        self._tally_sides(zs, zb, z_mid, ts, spreads=(ex["sell"], ex["buy"]))

    def _trim(self, now: float, window_sec: float) -> None:
        while self._samples and (now - self._samples[0][0]) > window_sec:
            self._samples.popleft()

    # ── window persistence (resume warm-up across a quick restart) ────────────
    def _series_key(self) -> str:
        """Identity of the current leg pair (e.g. 'NIFTY30JUN26F|NIFTY28JUL26F').
        Persisted with the window so a restore is REFUSED when the legs changed —
        a different contract pair is a different spread series, not resumable."""
        try:
            return str(self._series_key_provider()) if self._series_key_provider else ""
        except Exception:
            return ""

    def _persist_cfg(self) -> Tuple[bool, float, float]:
        """(enabled, resume_max_gap_sec, persist_interval_sec) from live params."""
        p = self._params() or {}
        enabled = bool(p.get("persist_window", True))
        gap = float(p.get("resume_max_gap_min", 10.0) or 0.0) * 60.0
        every = float(p.get("persist_interval_sec", 30.0) or 30.0)
        return enabled, gap, every

    def _maybe_persist(self, now: Optional[float] = None) -> None:
        now = time.time() if now is None else now
        _, _, every = self._persist_cfg()
        if now - self._last_persist >= max(5.0, every):
            self._persist(now)

    def _persist(self, now: Optional[float] = None) -> None:
        """Write the current window to disk (atomically). No-op when persistence
        is off, no path is set, the series is unknown, or the window is empty."""
        if not self._persist_path:
            return
        enabled, _, _ = self._persist_cfg()
        if not enabled:
            return
        key = self._series_key()
        if not key:
            return                          # never persist a window we can't label
        now = time.time() if now is None else now
        with self._lock:
            samples = list(self._samples)
        if not samples:
            return
        payload = {"series_key": key,
                   "hedge_ratio": self._p().get("hedge_ratio", 1.0) or 1.0,
                   "window_minutes": self._p()["window_minutes"],
                   "saved_at": now,
                   "samples": [list(s) for s in samples]}
        try:
            self._persist_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._persist_path.with_suffix(".tmp")
            with open(tmp, "w") as f:
                json.dump(payload, f)
            tmp.replace(self._persist_path)
            self._last_persist = now
        except Exception as exc:           # never let persistence break sampling
            logger.warning("SignalEngine: could not persist window — {}", exc)

    def _clear_persisted(self) -> None:
        if not self._persist_path:
            return
        try:
            self._persist_path.unlink(missing_ok=True)
        except Exception:
            pass

    def _restore(self) -> None:
        """Repopulate the window from disk IFF it is safe to resume:
          • persistence enabled and the file exists/parses;
          • the saved leg series matches the CURRENT legs (else discard);
          • samples older than the window are dropped (handles overnight);
          • the newest surviving sample is within ``resume_max_gap_min`` of now
            (a larger gap = stale regime → re-collect fresh).
        Anything short of all four → start cold (the safe default)."""
        if not self._persist_path or not self._persist_path.exists():
            return
        enabled, max_gap, _ = self._persist_cfg()
        if not enabled:
            return
        try:
            with open(self._persist_path) as f:
                data = json.load(f) or {}
        except Exception as exc:
            logger.warning("SignalEngine: could not read persisted window — {}", exc)
            return
        cur_key = self._series_key()
        if not cur_key or data.get("series_key") != cur_key:
            logger.info("SignalEngine: window not restored — leg series changed "
                        "(saved={}, current={})", data.get("series_key"), cur_key)
            return
        k_now = self._p().get("hedge_ratio", 1.0) or 1.0
        if abs(float(data.get("hedge_ratio", k_now) or k_now) - k_now) > 1e-12:
            logger.info("SignalEngine: window not restored — hedge ratio changed "
                        "(saved={}, current={})", data.get("hedge_ratio"), k_now)
            return
        now = time.time()
        window_sec = self._p()["window_minutes"] * 60.0
        kept = [tuple(s) for s in (data.get("samples") or [])
                if isinstance(s, (list, tuple)) and len(s) == 4 and (now - s[0]) <= window_sec]
        if not kept:
            return
        kept.sort(key=lambda s: s[0])
        gap = now - kept[-1][0]
        if max_gap > 0 and gap > max_gap:
            logger.info("SignalEngine: window not restored — last sample {:.1f} min old "
                        "(> {:.0f} min cap); collecting fresh", gap / 60.0, max_gap / 60.0)
            return
        with self._lock:
            self._samples = deque(kept)
            self._stats_cache = None
            self._k_last = k_now
            self._reset_exc_state()
        span = (kept[-1][0] - kept[0][0]) / 60.0
        logger.info("SignalEngine: restored {} samples spanning {:.1f} min "
                    "(gap {:.1f} min; only sampled time counts toward warm-up)",
                    len(kept), span, gap / 60.0)

    # ── stats ────────────────────────────────────────────────────────────────
    @staticmethod
    def compute_stats(spreads: List[float]) -> Tuple[float, float]:
        """(mean, std) with sample std (ddof=1); std floored away from zero."""
        if len(spreads) < 2:
            return (float(spreads[0]) if spreads else 0.0, 0.0)
        arr = np.asarray(spreads, dtype=float)
        mean = float(np.mean(arr))
        std = float(np.std(arr, ddof=1))
        if std < 1e-10:
            std = 0.0
        return mean, std

    @staticmethod
    def half_life(spreads: List[float]) -> float:
        """Mean-reversion half-life in SAMPLES via AR(1). 0.0 if not reverting."""
        if len(spreads) < 30:
            return 0.0
        data = np.asarray(spreads, dtype=float)
        y = data - np.mean(data)
        y_lag, y_t = y[:-1], y[1:]
        den = float(np.dot(y_lag, y_lag))
        if den == 0:
            return 0.0
        phi = float(np.dot(y_t, y_lag)) / den
        if phi <= 0 or phi >= 1:
            return 0.0
        return max(0.0, float(np.log(2) / (-np.log(phi))))

    def regime(self, spreads: Optional[List[float]] = None,
               ts_now: Optional[float] = None) -> Dict:
        """Classify the spread's regime over the trailing window with three
        orthogonal measures, and freeze a per-IST-day 'morning anchor' after
        warm-up:
          • efficiency ratio (Kaufman) = |net move| ÷ path length → 1 = trending;
          • zero-crossings of (S − anchor): few = trending, many = reverting;
          • variance ratio VR(k): >1 trending, <1 reverting, ≈1 random walk.
        Returns {state, efficiency_ratio, zero_crossings, variance_ratio, slope,
        anchor}; state ∈ {TRENDING, RANGE, UNKNOWN}. Pure read of config; only
        side effect is the once-per-day anchor freeze."""
        p = self._params() or {}
        if spreads is None:
            with self._lock:
                spreads = [s[3] for s in self._samples]
        n_win = int(p.get("regime_window_samples", 120) or 120)
        blank = {"state": "UNKNOWN", "efficiency_ratio": None, "zero_crossings": None,
                 "variance_ratio": None, "slope": 0.0, "anchor": self._anchor}
        if len(spreads) < max(20, n_win // 4):
            return blank
        arr = np.asarray(spreads[-n_win:], dtype=float)
        now = time.time() if ts_now is None else ts_now
        day = int((now + 5.5 * 3600) // 86400)               # IST day index
        if self._anchor is None or self._anchor_day != day:
            self._anchor = float(np.mean(arr)); self._anchor_day = day
        anchor = self._anchor
        path = float(np.sum(np.abs(np.diff(arr))))
        er = abs(float(arr[-1] - arr[0])) / path if path > 1e-12 else 0.0
        signs = np.sign(arr - anchor)
        zc = int(np.sum(signs[1:] * signs[:-1] < 0))
        k = max(2, int(p.get("regime_vr_lag", 5) or 5))
        d1 = np.diff(arr)
        vk = arr[k:] - arr[:-k]
        v1 = float(np.var(d1, ddof=1)) if len(d1) > 1 else 0.0
        vr = (float(np.var(vk, ddof=1)) / (k * v1)) if (v1 > 1e-12 and len(vk) > 1) else 1.0
        er_max = float(p.get("regime_efficiency_ratio_max", 0.6) or 0.6)
        zc_min = int(p.get("regime_min_zero_crossings", 4) or 4)
        trending = (er >= er_max) and (zc <= zc_min)
        return {"state": "TRENDING" if trending else "RANGE",
                "efficiency_ratio": round(er, 3), "zero_crossings": zc,
                "variance_ratio": round(vr, 3), "slope": float(arr[-1] - arr[0]),
                "anchor": round(anchor, 4)}

    def export_bars(self):
        """The collected window as [(ts, leg_a, leg_b), …] — real data to backtest
        the strategy on (no fabricated numbers)."""
        with self._lock:
            return [(ts, la, lb) for (ts, la, lb, _sp) in self._samples]

    @staticmethod
    def _coverage(ts: List[float], interval: float) -> Tuple[float, float]:
        """(history_sec, measured_interval_sec) for a window's timestamps.

        history_sec counts only time the engine was actually SAMPLING: a gap
        longer than the tolerance (a restart, a closed market, a stalled feed)
        adds nothing. A window restored after a 2-hour gap is therefore not
        "2 hours warm". measured_interval is the median spacing between
        samples, which is what half-life in samples must be converted with —
        the loop's nominal interval understates it (each fetch takes time)."""
        if len(ts) < 2:
            return 0.0, float(interval)
        d = np.diff(np.asarray(ts, dtype=float))
        tol = max(5.0, 10.0 * float(interval))
        live = d[(d > 0) & (d <= tol)]
        history = float(np.sum(live))
        measured = float(np.median(live)) if live.size else float(interval)
        return history, measured

    def _window_stats(self, spreads: List[float], ready: bool, now: float,
                      interval: float) -> Tuple[float, float]:
        """Mean/std shared by the signal AND the chart. Cached for
        ``stats_update_interval_sec`` so the bands hold still — but only once
        the window is ready: during warm-up a cache would freeze a σ taken from
        a handful of samples for minutes."""
        cache = self._stats_cache
        if ready and interval > 0 and cache is not None and (now - cache[2]) < interval:
            return cache[0], cache[1]
        mean, std = self.compute_stats(spreads)
        self._stats_cache = (mean, std, now) if ready else None
        return mean, std

    # ── warm-up after an instrument change ───────────────────────────────────
    def begin_pair_warmup(self, now: Optional[float] = None) -> None:
        """Called when the legs change: the new pair must collect the warm-up
        of live samples before it is ready, even when candle history makes
        its bands ready at once. Off with signal.warmup_after_pair_change."""
        if not self._pair_warmup_enabled():
            return
        key = self._series_key()
        with self._lock:
            self._pw = {"key": key, "since": time.time() if now is None else now}
            self._pw_loaded = True
        self._pw_save()
        logger.info("SignalEngine: new pair {} — trading waits for {:.0f} min of live data",
                    key or "?", self._p()["min_signal_minutes"])

    def _pair_warmup_enabled(self) -> bool:
        try:
            raw = self._params() or {}
        except Exception:
            raw = {}
        v = raw.get("warmup_after_pair_change", True)
        return str(v).lower() not in ("0", "false", "no", "off") if v is not None else True

    def _pair_warmup(self) -> Optional[Dict]:
        """The active pair warm-up, if any (loaded once from disk; ignored when
        it belongs to another pair or the setting is off)."""
        if not self._pw_loaded:
            self._pw_loaded = True
            try:
                if self._pw_path and self._pw_path.exists():
                    d = json.loads(self._pw_path.read_text() or "{}")
                    if d.get("active"):
                        self._pw = {"key": d.get("key"), "since": float(d.get("since") or 0)}
            except Exception as exc:                       # noqa: BLE001
                logger.warning("SignalEngine: pair warm-up state unreadable — {}", exc)
        pw = self._pw
        if pw is None or not self._pair_warmup_enabled():
            return None
        key = self._series_key()
        if key and pw.get("key") and pw["key"] != key:
            return None                       # a marker for another pair: not ours
        return pw

    def _end_pair_warmup(self) -> None:
        with self._lock:
            if self._pw is None:
                return
            self._pw = None
        self._pw_save()
        logger.info("SignalEngine: new-pair warm-up complete — signal ready")

    def _pw_save(self) -> None:
        if not self._pw_path:
            return
        pw = self._pw
        body = {"active": pw is not None, **(pw or {})}
        try:
            self._pw_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._pw_path.with_suffix(self._pw_path.suffix + ".tmp")
            tmp.write_text(json.dumps(body))
            os.replace(tmp, self._pw_path)
        except Exception as exc:                           # noqa: BLE001
            logger.warning("SignalEngine: could not save pair warm-up state — {}", exc)

    def _live_bands(self, samples: List, p: Dict, now: float,
                    all_timeframes: bool = False) -> Dict:
        """The mean/σ the algo trades on, and whether they are usable yet —
        ONE computation shared by get_signal (Desk, algo) and the excursion
        tally, so the Excursion Log can never count on a different z.

        Tick mode: the rolling window (cached per stats_update_interval_sec),
        ready once min_signal_minutes of SAMPLED history exist. Candle mode:
        the configured timeframe's Bollinger basis (EMA) and σ, ready once N
        candles exist."""
        ts = [x[0] for x in samples]
        spreads = [x[3] for x in samples]
        history_sec, measured = self._coverage(ts, p["sample_interval_sec"])
        enough = history_sec >= p["min_signal_minutes"] * 60.0
        mean, std = self._window_stats(spreads, enough, now,
                                       float(p.get("stats_update_interval_sec", 0) or 0))
        out = {"tick_mean": mean, "tick_std": std, "history_sec": history_sec,
               "measured": measured, "bands": {}, "count": 0}
        src, tf, n_len = self._band_params()
        k_now = p.get("hedge_ratio", 1.0) or 1.0
        if self.candles is not None:
            from arrow_statarb.core.candles import TIMEFRAMES
            for _tf in (TIMEFRAMES if all_timeframes else (tf,) if src == "candles" else ()):
                try:
                    b = self.candles.bands(_tf, n_len, k_now)
                except Exception:
                    b = {"ready": False, "mean": None, "std": None, "count": 0}
                out["bands"][_tf] = {"mean": b.get("mean"), "std": b.get("std"),
                                     "ready": bool(b.get("ready")), "count": b.get("count", 0),
                                     "status": b.get("status")}
        if src == "candles":
            b = out["bands"].get(tf) or {}
            if b.get("ready"):
                mean, std = float(b["mean"]), float(b["std"])
            else:
                std = 0.0                                 # not tradeable until N candles exist
            enough = bool(b.get("ready"))
            out["count"] = b.get("count", 0)
            # After an instrument change the bands alone are not enough: the
            # new pair also needs the warm-up of LIVE samples (option 2).
            pw = self._pair_warmup()
            if pw is not None:
                need = p["min_signal_minutes"] * 60.0
                if history_sec >= need:
                    self._end_pair_warmup()
                else:
                    out["pair_warmup"] = {"since": pw.get("since"),
                                          "have_sec": round(history_sec, 1), "need_sec": need}
                    enough = False
        elif self._pw is not None and enough:
            self._end_pair_warmup()           # tick mode: its own warm-up already covered it
        out.update(src=src, tf=tf, n=n_len, mean=mean, std=std, ready=enough)
        return out

    @staticmethod
    def _exec_spreads(book: Optional[Dict], k: float) -> Optional[Dict]:
        """The spreads an order would actually meet: SELL the spread at
        k·bid_A − ask_B, BUY it at k·ask_A − bid_B (None where a side of the
        book is missing). None when there is no book at all."""
        if not (book and book.get("leg_a") and book.get("leg_b")):
            return None
        A, B = book["leg_a"], book["leg_b"]
        ba, aa, bb, ab = A.get("bid"), A.get("ask"), B.get("bid"), B.get("ask")
        return {"bid_a": ba, "ask_a": aa, "bid_b": bb, "ask_b": ab,
                "full": all(v is not None for v in (ba, aa, bb, ab)),
                "sell": (k * float(ba) - float(ab)) if ba is not None and ab is not None else None,
                "buy": (k * float(aa) - float(bb)) if aa is not None and bb is not None else None}

    def get_signal(self) -> Dict:
        """Return the live signal snapshot — the ONE z the algo + dashboard use."""
        p = self._p()
        with self._lock:
            samples = list(self._samples)
        book = self._book

        out: Dict = {
            "running": self.running,
            "samples": len(samples),
            "window_minutes": p["window_minutes"],
            "sample_interval_sec": p["sample_interval_sec"],
            "min_signal_minutes": p["min_signal_minutes"],
            "entry_zscore": p["entry_zscore"],
            "exit_zscore": p["exit_zscore"],
            "stop_zscore": p["stop_zscore"],
            "leg_a": None, "leg_b": None, "spread": None,
            "mean": None, "std": None, "zscore": None,
            "sell_spread": None, "buy_spread": None, "z_sell": None, "z_buy": None,
            "bid_a": None, "ask_a": None, "bid_b": None, "ask_b": None,
            "book": False,
            "half_life": 0.0, "half_life_sec": 0.0,
            "history_sec": 0.0, "min_history_sec": p["min_signal_minutes"] * 60.0,
            "window_sec": p["window_minutes"] * 60.0,
            "measured_interval_sec": p["sample_interval_sec"],
            "quote_rate_per_min": 0,
            "degenerate": False,
            "sample_status": self._sample_status,
            "ready": False, "last_error": self._last_error,
        }
        _src, _tf, _n = self._band_params()
        out.update(band_source=_src, band_timeframe=_tf, band_length=_n)
        if not samples:
            return out

        ts = [x[0] for x in samples]
        ts0 = ts[0]
        tsN, la, lb, spread = samples[-1]
        now = time.time()
        span_min = (tsN - ts0) / 60.0
        spreads = [x[3] for x in samples]
        lb_ = self._live_bands(samples, p, now, all_timeframes=True)
        mean, std, enough = lb_["mean"], lb_["std"], lb_["ready"]
        history_sec, measured = lb_["history_sec"], lb_["measured"]
        # The tick window's own mean/σ are always published (a position opened
        # in tick mode keeps them if the band source is switched to candles).
        out["tick_mean"], out["tick_std"] = round(lb_["tick_mean"], 4), round(lb_["tick_std"], 6)
        src, tf, n_len = lb_["src"], lb_["tf"], lb_["n"]
        out.update(band_source=src, band_timeframe=tf, band_length=n_len, bands=lb_["bands"])
        if src == "candles":
            out["candles_have"], out["candles_need"] = lb_["count"], n_len
        out["pair_warmup"] = lb_.get("pair_warmup")       # None unless a new pair is warming up
        hl = self.half_life(spreads)
        usable = std > 1e-12
        z = (spread - mean) / std if usable else 0.0
        cutoff = tsN - 60.0
        rate = sum(1 for t in ts if t >= cutoff)

        out.update(
            leg_a=round(la, 4), leg_b=round(lb, 4), spread=round(spread, 4),
            mean=round(mean, 4), std=round(std, 6), zscore=round(z, 4),
            half_life=round(hl, 4), half_life_sec=round(hl * measured, 2),
            span_minutes=round(span_min, 3),
            history_sec=round(history_sec, 1),
            measured_interval_sec=round(measured, 3),
            quote_rate_per_min=rate,
            degenerate=(len(samples) >= 2 and not usable),
            # Need enough SAMPLED history AND a usable std before it is tradeable.
            ready=(enough and usable),
        )

        # Executable spreads from the book (None where a side is missing).
        ex = self._exec_spreads(book, p.get("hedge_ratio", 1.0) or 1.0)
        if ex is not None:
            out.update(bid_a=ex["bid_a"], ask_a=ex["ask_a"], bid_b=ex["bid_b"], ask_b=ex["ask_b"],
                       book=ex["full"])
            if ex["sell"] is not None:
                out["sell_spread"] = round(ex["sell"], 4)
                out["z_sell"] = round((ex["sell"] - mean) / std, 4) if usable else None
            if ex["buy"] is not None:
                out["buy_spread"] = round(ex["buy"], 4)
                out["z_buy"] = round((ex["buy"] - mean) / std, 4) if usable else None

        reg = self.regime(spreads=spreads, ts_now=tsN)
        out["regime"] = reg["state"]
        out["regime_detail"] = reg
        return out

    def get_series(self, max_points: int = 200, last_sec: Optional[float] = None) -> Dict:
        """Recent spread + per-sample z series for the dashboard charts.

        z is computed against the window's CURRENT mean/std (one consistent
        snapshot), so the chart matches the live signal/algo z. Downsampled to
        at most ``max_points`` so the charts stay light regardless of window
        size (a 120-min window at 0.5s holds ~14.4k samples)."""
        with self._lock:
            samples = list(self._samples)
        if not samples:
            return {"points": [], "spread_min": None, "spread_max": None,
                    "entry_zscore": self._p()["entry_zscore"],
                    "stop_zscore": self._p()["stop_zscore"]}

        spreads = [s[3] for s in samples]
        # mean/std always come from the WHOLE window (what the algo trades on);
        # ``last_sec`` only trims which samples are PLOTTED.
        if last_sec and last_sec > 0:
            cut = samples[-1][0] - float(last_sec)
            recent = [x for x in samples if x[0] >= cut]
            samples = recent or samples[-1:]
        # The SAME mean/std the live signal uses (cached per the stats-update
        # interval), so the chart's z matches the traded z exactly.
        cache = self._stats_cache
        if cache is not None:
            mean, std = cache[0], cache[1]
        else:
            mean, std = self.compute_stats(spreads)
        src, tf, n_len = self._band_params()
        if src == "candles":
            b = self.candles.bands(tf, n_len, self._p().get("hedge_ratio", 1.0) or 1.0)
            mean, std = (b["mean"], b["std"]) if b.get("ready") else (mean, 0.0)
        sd = std if std > 1e-12 else 0.0

        step = max(1, len(samples) // max_points)
        points = []
        for i in range(0, len(samples), step):
            sp = samples[i][3]
            z = (sp - mean) / sd if sd else 0.0
            points.append({"spread": round(sp, 4), "z": round(z, 4)})
        # Always include the most recent sample as the final point.
        if (len(samples) - 1) % step != 0:
            sp = samples[-1][3]
            points.append({"spread": round(sp, 4),
                           "z": round((sp - mean) / sd, 4) if sd else 0.0})

        return {
            "points": points,
            "spread_min": round(min(spreads), 4),
            "spread_max": round(max(spreads), 4),
            "entry_zscore": self._p()["entry_zscore"],
            "stop_zscore": self._p()["stop_zscore"],
        }
