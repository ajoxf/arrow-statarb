"""Real-time spread calculation with Z-score and mean-reversion half-life.

Generic 2-leg spread:  spread = leg_a - ratio * leg_b.
Z-Score = (spread - mean) / std over a rolling sample window.

(Ported from the BMD repo's SpreadCalculator; the Brent/Sour/FX-specific
``calculate_spread`` was replaced with a generic ``add`` so it works for any
2-leg spread. The half-life estimator is unchanged.)
"""

from __future__ import annotations

import threading
from collections import deque
from typing import Dict, Optional

import numpy as np
from loguru import logger


class SpreadCalculator:
    """Maintains a rolling spread history and derived statistics."""

    def __init__(self, lookback_period: int = 250, hedge_ratio: float = 1.0):
        self.lookback_period = lookback_period
        self.hedge_ratio = hedge_ratio

        # Rolling spread history
        self._spread_history = deque(maxlen=100000)
        self._lock = threading.Lock()

        # Cached statistics
        self._mean = 0.0
        self._std = 1.0
        self._recalc_count = 0
        self._recalc_interval = 100  # recalculate stats every N ticks

    def add(self, leg_a_price: float, leg_b_price: float) -> float:
        """Append a new spread sample (leg_a - hedge_ratio * leg_b) and return it."""
        spread = leg_a_price - self.hedge_ratio * leg_b_price

        with self._lock:
            self._spread_history.append(spread)
            count = len(self._spread_history)
            self._recalc_count += 1

            # Recalculate statistics every _recalc_interval ticks AND at the
            # exact moment the lookback window is full for the first time, so
            # is_ready never becomes True while mean/std are still at defaults.
            if self._recalc_count >= self._recalc_interval or count == self.lookback_period:
                self._update_statistics()
                self._recalc_count = 0

        return spread

    @property
    def is_ready(self) -> bool:
        """True once we have at least lookback_period samples for a valid mean/std."""
        with self._lock:
            return len(self._spread_history) >= self.lookback_period

    @property
    def sample_count(self) -> int:
        """Number of spread samples collected so far."""
        with self._lock:
            return len(self._spread_history)

    def calculate_zscore(self, spread: Optional[float] = None) -> float:
        """Z-score of ``spread`` (latest sample if None)."""
        with self._lock:
            if spread is None:
                if not self._spread_history:
                    return 0.0
                spread = self._spread_history[-1]

            if self._std == 0 or np.isnan(self._std):
                return 0.0

            return (spread - self._mean) / self._std

    def _update_statistics(self):
        """Recalculate rolling mean and std (caller must hold lock)."""
        if len(self._spread_history) < 20:
            return

        data = np.array(list(self._spread_history)[-self.lookback_period:])
        self._mean = float(np.mean(data))
        self._std = float(np.std(data, ddof=1))

        if self._std < 1e-10:
            self._std = 1.0  # prevent division by zero

    def update_hedge_ratio(self, new_ratio: float):
        """Update the hedge ratio (e.g., from a Kalman filter)."""
        self.hedge_ratio = new_ratio
        logger.debug("Hedge ratio updated to {:.4f}", new_ratio)

    def get_statistics(self) -> Dict:
        """Get current spread statistics."""
        with self._lock:
            data = list(self._spread_history)
            count = len(data)
            if not data:
                return {"mean": 0, "std": 0, "count": 0,
                        "lookback_period": self.lookback_period, "is_ready": False}

            arr = np.array(data[-self.lookback_period:])
            return {
                "mean": self._mean,
                "std": self._std,
                "count": count,
                "lookback_period": self.lookback_period,
                "is_ready": count >= self.lookback_period,
                "min": float(np.min(arr)),
                "max": float(np.max(arr)),
                "current": data[-1] if data else 0,
                "hedge_ratio": self.hedge_ratio,
            }

    def get_spread_series(self, n: int = 1000) -> np.ndarray:
        """Get recent spread values as an array."""
        with self._lock:
            return np.array(list(self._spread_history)[-n:])

    def compute_half_life(self) -> float:
        """
        Estimate mean-reversion half-life via AR(1) on the spread residuals.
        Returns half-life in number of ticks (same unit as lookback_period).
        Returns 0.0 if insufficient data or series not mean-reverting.
        """
        with self._lock:
            if len(self._spread_history) < max(30, self.lookback_period):
                return 0.0
            data = np.array(list(self._spread_history)[-self.lookback_period:])

        mean = np.mean(data)
        y = data - mean
        y_lag = y[:-1]
        y_t   = y[1:]
        den = float(np.dot(y_lag, y_lag))
        if den == 0:
            return 0.0
        phi = float(np.dot(y_t, y_lag)) / den
        if phi <= 0 or phi >= 1:
            return 0.0
        half_life = np.log(2) / (-np.log(phi))
        return max(0.0, float(half_life))
