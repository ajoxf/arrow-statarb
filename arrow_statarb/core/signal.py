"""Server-side signal engine — the SINGLE source of truth for the spread/z.

Both the auto-trader (``core/algo.py``) and the web dashboard read this one
engine, so the z-score they show/act on is always identical (the alignment
requirement). It samples live leg prices into a TIME-based rolling window
(``signal.window_minutes``, default 120) every ``signal.sample_interval_sec``,
and computes mean/std/z plus a mean-reversion half-life over that window.

Convention (Arrow fact #10):
    spread = leg_a - leg_b ; z = (spread - mean) / std
"""

from __future__ import annotations

import threading
import time
from collections import deque
from typing import Callable, Deque, Dict, List, Optional, Tuple

import numpy as np
from loguru import logger


class SignalEngine:
    """Samples the spread into a time window and serves the live signal."""

    def __init__(
        self,
        prices_provider: Callable[[], Tuple[Optional[float], Optional[float]]],
        params_provider: Callable[[], Dict],
    ):
        self._prices = prices_provider
        self._params = params_provider

        # (timestamp, leg_a, leg_b, spread)
        self._samples: Deque[Tuple[float, float, float, float]] = deque()
        self._lock = threading.RLock()

        self._thread: Optional[threading.Thread] = None
        self._stop_evt = threading.Event()
        self.running = False
        self._last_error = ""

    # ── params ───────────────────────────────────────────────────────────────
    def _p(self) -> Dict:
        p = {
            "window_minutes": 120.0,
            "sample_interval_sec": 0.5,
            "min_signal_minutes": 10.0,
            "entry_zscore": 2.0,
            "exit_zscore": 0.0,
            "stop_zscore": 4.0,
        }
        try:
            p.update({k: float(v) for k, v in (self._params() or {}).items()
                      if k in p and v is not None})
        except Exception:
            pass
        return p

    # ── control ──────────────────────────────────────────────────────────────
    def start(self) -> bool:
        with self._lock:
            if self.running:
                return False
            self._stop_evt.clear()
            self.running = True
            self._thread = threading.Thread(target=self._loop, daemon=True, name="SignalEngine")
            self._thread.start()
            logger.info("SignalEngine: started")
            return True

    def stop(self) -> None:
        self._stop_evt.set()
        self.running = False
        logger.info("SignalEngine: stopped")

    def reset(self) -> None:
        with self._lock:
            self._samples.clear()

    # ── sampling loop ────────────────────────────────────────────────────────
    def _loop(self) -> None:
        while not self._stop_evt.is_set():
            interval = 0.5
            try:
                interval = max(0.05, self._p()["sample_interval_sec"])
                self.sample_once()
            except Exception as exc:
                self._last_error = str(exc)
                logger.exception("SignalEngine: sample error")
            self._stop_evt.wait(interval)

    def sample_once(self, now: Optional[float] = None) -> None:
        """Take one spread sample from the live feed and trim the window.
        Exposed (not just internal) so tests can drive it deterministically."""
        now = time.time() if now is None else now
        la, lb = self._prices()
        if la is None or lb is None or la <= 0 or lb <= 0:
            return
        spread = float(la) - float(lb)
        window_sec = self._p()["window_minutes"] * 60.0
        with self._lock:
            self._samples.append((now, float(la), float(lb), spread))
            self._trim(now, window_sec)

    def push(self, leg_a: float, leg_b: float, ts: Optional[float] = None) -> None:
        """Inject a sample directly (used by tests)."""
        ts = time.time() if ts is None else ts
        with self._lock:
            self._samples.append((ts, float(leg_a), float(leg_b), float(leg_a) - float(leg_b)))
            self._trim(ts, self._p()["window_minutes"] * 60.0)

    def _trim(self, now: float, window_sec: float) -> None:
        while self._samples and (now - self._samples[0][0]) > window_sec:
            self._samples.popleft()

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

    def get_signal(self) -> Dict:
        """Return the live signal snapshot — the ONE z the algo + dashboard use."""
        p = self._p()
        with self._lock:
            samples = list(self._samples)

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
            "half_life": 0.0, "half_life_sec": 0.0,
            "ready": False, "last_error": self._last_error,
        }
        if not samples:
            return out

        ts0, _, _, _ = samples[0]
        tsN, la, lb, spread = samples[-1]
        span_min = (tsN - ts0) / 60.0
        spreads = [s[3] for s in samples]
        mean, std = self.compute_stats(spreads)
        hl = self.half_life(spreads)
        z = (spread - mean) / std if std > 1e-12 else 0.0

        out.update(
            leg_a=round(la, 4), leg_b=round(lb, 4), spread=round(spread, 4),
            mean=round(mean, 4), std=round(std, 6), zscore=round(z, 4),
            half_life=round(hl, 4), half_life_sec=round(hl * p["sample_interval_sec"], 2),
            span_minutes=round(span_min, 3),
            # Need enough history AND a usable std before the signal is tradeable.
            ready=(span_min >= p["min_signal_minutes"] and std > 1e-12),
        )
        return out
