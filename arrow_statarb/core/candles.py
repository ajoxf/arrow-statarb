"""Spread candles and TradingView-style bands (Bollinger Bands, basis = EMA).

Mirrors what a TradingView spread chart shows for ``k × A − B`` on a chosen
timeframe, so the engine's mean and σ sit where the trader sees them:

  * Candles are built from each leg's LAST-TRADED price (a TradingView spread
    chart uses the legs' trades, not the book), bucketed on the exchange
    session's own grid: MCX 15-min / 1 H / 4 H candles start at 09:00 IST
    (4 H = 09:00, 13:00, 17:00, 21:00), NSE ones at 09:15.
  * The CURRENT (still forming) candle is part of the series, exactly as on
    the chart — the bands move while the candle is open.
  * basis = ta.ema(close, N): alpha = 2 / (N + 1), seeded with the SMA of the
    first N closes (Pine's own definition).
  * σ     = ta.stdev(close, N): population standard deviation of the last N
    closes around their simple average (Pine's default, biased = true).

Candles survive restarts and the overnight gap (they are saved to disk), and
history is back-filled from the broker, so the bands are ready at start-up
instead of after a warm-up. Each leg's close is stored separately, so a change
of hedge ratio k re-prices the series instead of invalidating it.
"""

from __future__ import annotations

import json
import math
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from loguru import logger

IST = timezone(timedelta(hours=5, minutes=30))

#: timeframe key → seconds. The keys are what settings and the UI use.
TIMEFRAMES: Dict[str, int] = {"5m": 300, "15m": 900, "1h": 3600, "4h": 14400}
TF_LABELS: Dict[str, str] = {"5m": "5 min", "15m": "15 min", "1h": "1 hour", "4h": "4 hours"}

#: How many candles each timeframe keeps (and asks the broker for). EMA needs
#: a few × N candles to settle to the chart's value; 600 is ample for N ≤ 200.
MAX_BARS = 600

#: Calendar days of history requested per timeframe on a back-fill.
BACKFILL_DAYS: Dict[str, int] = {"5m": 6, "15m": 14, "1h": 45, "4h": 120}


def tf_seconds(tf: str) -> int:
    return TIMEFRAMES.get(str(tf).lower(), 900)


def normalise_tf(tf) -> str:
    t = str(tf or "15m").lower().replace(" ", "")
    aliases = {"5": "5m", "5min": "5m", "15": "15m", "15min": "15m", "60": "1h",
               "60m": "1h", "1hour": "1h", "240": "4h", "240m": "4h", "4hour": "4h"}
    t = aliases.get(t, t)
    return t if t in TIMEFRAMES else "15m"


# ── the two TradingView functions, exactly ───────────────────────────────────
def pine_ema(values: Sequence[float], length: int) -> Optional[float]:
    """ta.ema(src, length) on the LAST element: SMA-seeded, then
    ema = alpha·x + (1 − alpha)·ema[1] with alpha = 2 / (length + 1)."""
    n = int(length)
    if n < 1 or len(values) < n:
        return None
    alpha = 2.0 / (n + 1.0)
    ema = sum(values[:n]) / n
    for x in values[n:]:
        ema = alpha * x + (1.0 - alpha) * ema
    return ema


def pine_stdev(values: Sequence[float], length: int) -> Optional[float]:
    """ta.stdev(src, length) (biased = true) on the last ``length`` values:
    population σ around their simple average."""
    n = int(length)
    if n < 1 or len(values) < n:
        return None
    w = values[-n:]
    avg = sum(w) / n
    var = sum((x - avg) ** 2 for x in w) / n
    return math.sqrt(max(var, 0.0))


# ── candle grid ───────────────────────────────────────────────────────────────
def bucket_start(ts: float, tf_sec: int, session_open_min: int) -> float:
    """Start (epoch s) of the candle holding ``ts``: the grid is anchored at the
    session open of ts's IST day, so candles line up with the exchange's (and
    TradingView's) — not with UTC midnight."""
    d = datetime.fromtimestamp(ts, IST)
    anchor = datetime(d.year, d.month, d.day, tzinfo=IST).timestamp() + session_open_min * 60
    return anchor + math.floor((ts - anchor) / tf_sec) * tf_sec


def align_legs(a: List[Tuple[float, float]], b: List[Tuple[float, float]],
               tf_sec: int, session_open_min: int) -> List[Tuple[float, float, float]]:
    """Merge two legs' (ts, close) candles into [(bucket, close_a, close_b)] on
    one grid. A bucket where only one leg traded carries the other leg's last
    close forward (a spread chart does the same); buckets before both legs
    have traded are dropped."""
    def bucketed(rows):
        out: Dict[float, float] = {}
        for ts, c in sorted(rows):
            if c is None or not (c > 0):
                continue
            out[bucket_start(float(ts), tf_sec, session_open_min)] = float(c)
        return out
    ba, bb = bucketed(a), bucketed(b)
    last_a = last_b = None
    merged = []
    for t in sorted(set(ba) | set(bb)):
        last_a = ba.get(t, last_a)
        last_b = bb.get(t, last_b)
        if last_a is not None and last_b is not None:
            merged.append((t, last_a, last_b))
    return merged


HistoryFn = Callable[[str, float, float], Optional[Dict[str, List[Tuple[float, float]]]]]


class SpreadCandles:
    """Candles of both legs on every timeframe, their bands, persistence and
    back-fill. Thread-safe; ``update`` is called on every price sample."""

    def __init__(self, persist_path: Optional[Path] = None,
                 session_open_provider: Optional[Callable[[], int]] = None,
                 history_provider: Optional[HistoryFn] = None,
                 key_provider: Optional[Callable[[], str]] = None,
                 clock: Callable[[], float] = time.time):
        self._path = Path(persist_path) if persist_path else None
        self._open_min = session_open_provider or (lambda: 9 * 60)
        self._history = history_provider
        self._key_fn = key_provider or (lambda: "")
        self._clock = clock
        self._lock = threading.RLock()
        # tf → {bucket_start: [close_a, close_b]}
        self._bars: Dict[str, Dict[float, List[float]]] = {tf: {} for tf in TIMEFRAMES}
        self._key: Optional[str] = None
        self._dirty = False
        self._last_save = 0.0
        # per-timeframe back-fill status for the UI
        self.status: Dict[str, Dict] = {tf: {"state": "pending", "detail": "", "at": 0.0}
                                        for tf in TIMEFRAMES}
        self._fill_thread: Optional[threading.Thread] = None
        self._ema_cache: Dict[Tuple, Tuple[float, float]] = {}
        self._load()

    # ── identity: a different pair is a different series ─────────────────────
    def _check_key(self) -> None:
        try:
            key = str(self._key_fn() or "")
        except Exception:
            return
        if not key:
            return
        if self._key is None:
            self._key = key
        elif key != self._key:
            logger.info("Candles: pair changed ({} → {}) — candles restart and history is re-fetched",
                        self._key, key)
            self._key = key
            self._bars = {tf: {} for tf in TIMEFRAMES}
            self._ema_cache.clear()
            self.status = {tf: {"state": "pending", "detail": "", "at": 0.0} for tf in TIMEFRAMES}
            self._dirty = True

    # ── live ticks ────────────────────────────────────────────────────────────
    def update(self, ts: float, ltp_a: Optional[float], ltp_b: Optional[float]) -> None:
        """Fold one pair of last-traded prices into the current candle of every
        timeframe (the latest price is that candle's close)."""
        if not ltp_a or not ltp_b or ltp_a <= 0 or ltp_b <= 0:
            return
        om = self._open_min()
        with self._lock:
            self._check_key()
            for tf, sec in TIMEFRAMES.items():
                bars = self._bars[tf]
                bars[bucket_start(ts, sec, om)] = [float(ltp_a), float(ltp_b)]
                if len(bars) > MAX_BARS:
                    for t in sorted(bars)[:len(bars) - MAX_BARS]:
                        del bars[t]
            self._dirty = True
        self._maybe_save()

    # ── bands ─────────────────────────────────────────────────────────────────
    def closes(self, tf: str, k: float = 1.0) -> List[Tuple[float, float]]:
        """[(bucket_start, spread_close)] oldest first, current candle last."""
        tf = normalise_tf(tf)
        with self._lock:
            rows = sorted(self._bars[tf].items())
        return [(t, k * a - b) for t, (a, b) in rows]

    def bands(self, tf: str, length: int, k: float = 1.0) -> Dict:
        """EMA basis and σ of the spread on ``tf`` with the current candle
        included — the numbers a TradingView BB(length, basis EMA) shows now."""
        tf = normalise_tf(tf)
        n = max(2, int(length))
        series = self.closes(tf, k)
        closes = [c for _, c in series]
        out = {"timeframe": tf, "length": n, "count": len(closes), "ready": False,
               "mean": None, "std": None, "last_candle": series[-1][0] if series else None,
               "status": dict(self.status.get(tf, {}))}
        if len(closes) < n:
            return out
        # EMA of everything BEFORE the current candle is fixed until that candle
        # closes — cache it, then roll the current close in (cheap per tick).
        prev = closes[:-1]
        ck = (tf, n, round(k, 10), len(prev), series[-2][0] if len(series) > 1 else None,
              prev[-1] if prev else None)
        if len(prev) >= n:
            hit = self._ema_cache.get(ck)
            if hit is None:
                ema_prev = pine_ema(prev, n)
                if len(self._ema_cache) > 64:
                    self._ema_cache.clear()
                self._ema_cache[ck] = (ema_prev, 0.0)
            else:
                ema_prev = hit[0]
            alpha = 2.0 / (n + 1.0)
            mean = alpha * closes[-1] + (1.0 - alpha) * ema_prev
        else:
            mean = pine_ema(closes, n)           # exactly N candles: the SMA seed
        std = pine_stdev(closes, n)
        out.update(mean=mean, std=std, ready=bool(std and std > 1e-12))
        return out

    def series(self, tf: str, length: int, k: float = 1.0, last: int = 200) -> Dict:
        """Chart data: closes with the EMA / σ at each candle (current included)."""
        tf = normalise_tf(tf)
        n = max(2, int(length))
        rows = self.closes(tf, k)
        closes = [c for _, c in rows]
        alpha = 2.0 / (n + 1.0)
        pts, ema = [], None
        for i, (t, c) in enumerate(rows):
            if i + 1 < n:
                pts.append({"t": t, "close": c, "mean": None, "std": None})
                continue
            if ema is None:
                ema = sum(closes[:n]) / n
            else:
                ema = alpha * c + (1.0 - alpha) * ema
            pts.append({"t": t, "close": c, "mean": ema, "std": pine_stdev(closes[:i + 1], n)})
        return {"timeframe": tf, "length": n, "points": pts[-last:]}

    # ── back-fill ─────────────────────────────────────────────────────────────
    def backfill_async(self, force: bool = False) -> bool:
        """Fetch history for every timeframe in the background (no-op while one
        runs). Timeframes already filled recently are skipped unless forced."""
        if self._history is None:
            return False
        if self._fill_thread is not None and self._fill_thread.is_alive():
            return False
        self._fill_thread = threading.Thread(target=self._backfill, args=(force,),
                                             daemon=True, name="CandleBackfill")
        self._fill_thread.start()
        return True

    def _backfill(self, force: bool) -> None:
        now = self._clock()
        with self._lock:
            self._check_key()
        om = self._open_min()
        for tf, sec in TIMEFRAMES.items():
            st = self.status.get(tf, {})
            if not force and st.get("state") == "ok" and now - float(st.get("at", 0)) < 3600:
                continue
            self.status[tf] = {"state": "loading", "detail": "", "at": now}
            try:
                got = self._history(tf, now - BACKFILL_DAYS[tf] * 86400.0, now)
            except Exception as exc:                  # noqa: BLE001 — reported, not raised
                got, err = None, str(exc)
            else:
                err = "" if got else "the broker returned no history"
            if not got or not got.get("a") or not got.get("b"):
                self.status[tf] = {"state": "failed", "detail": err or "one leg returned no history",
                                   "at": now}
                logger.warning("Candles: {} history unavailable — {}", tf, self.status[tf]["detail"])
                continue
            merged = align_legs(got["a"], got["b"], sec, om)
            with self._lock:
                bars = self._bars[tf]
                current = bucket_start(now, sec, om)
                for t, ca, cb in merged:
                    if t == current and t in bars:
                        continue                      # the live candle keeps live prices
                    bars[t] = [ca, cb]
                for t in sorted(bars)[:max(0, len(bars) - MAX_BARS)]:
                    del bars[t]
                self._ema_cache.clear()
                self._dirty = True
            self.status[tf] = {"state": "ok", "detail": f"{len(merged)} candles from the broker",
                               "at": now}
            logger.info("Candles: {} back-filled — {} candles", tf, len(merged))
        self._maybe_save(force=True)

    # ── persistence ───────────────────────────────────────────────────────────
    def _maybe_save(self, force: bool = False) -> None:
        if not self._path or not self._dirty:
            return
        now = self._clock()
        if not force and now - self._last_save < 30.0:
            return
        with self._lock:
            data = {"key": self._key,
                    "saved_at": now,
                    "bars": {tf: [[t, a, b] for t, (a, b) in sorted(bars.items())]
                             for tf, bars in self._bars.items()}}
            self._dirty = False
            self._last_save = now
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data))
            tmp.replace(self._path)
        except Exception as exc:                      # noqa: BLE001
            logger.warning("Candles: could not save {}: {}", self._path, exc)

    def save(self) -> None:
        self._dirty = True
        self._maybe_save(force=True)

    def _load(self) -> None:
        if not self._path or not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text())
        except Exception as exc:                      # noqa: BLE001
            logger.warning("Candles: could not read {}: {}", self._path, exc)
            return
        try:
            key_now = str(self._key_fn() or "")
        except Exception:
            key_now = ""
        if key_now and data.get("key") and data["key"] != key_now:
            logger.info("Candles: saved candles are for another pair — not loaded")
            return
        self._key = data.get("key") or key_now or None
        for tf, rows in (data.get("bars") or {}).items():
            if tf in self._bars:
                self._bars[tf] = {float(t): [float(a), float(b)] for t, a, b in rows}
        n = {tf: len(b) for tf, b in self._bars.items()}
        logger.info("Candles: restored from disk — {}", n)
        for tf, cnt in n.items():
            if cnt:
                self.status[tf] = {"state": "restored", "detail": f"{cnt} candles from disk",
                                   "at": float(data.get("saved_at") or 0)}

    def counts(self) -> Dict[str, int]:
        with self._lock:
            return {tf: len(b) for tf, b in self._bars.items()}
