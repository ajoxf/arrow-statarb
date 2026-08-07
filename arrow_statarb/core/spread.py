"""Rolling statistics of one asset's spread (leg_b - hedge_ratio * leg_a).

Ported from the W3 basis system. mu/sigma are FROZEN between refreshes
(STATS_INTERVAL_SEC) so the anchor doesn't chase the spread intra-hold; z uses
the live spread against the frozen stats. One instance per asset — the engine
holds a dict of these for concurrent multi-asset trading.

The current single-pair SignalEngine (core/signal.py) is unaffected; this is a
standalone, instantiable unit so many spreads can be tracked at once.
"""

from __future__ import annotations

import math
import time as time_mod
from collections import deque


class SpreadStats:
    def __init__(self, signals_cfg, clock=time_mod.time):
        self.cfg = signals_cfg
        self.clock = clock
        self.samples = deque()          # (t, value)
        self.mu = None
        self.sigma = None
        self.half_life_sec = None
        self.last_refresh = 0.0
        self.last_value = None
        self.last_quote_id = None
        self.collecting_since = None

    def update(self, value, quote_id=None):
        """Feed one spread observation; refresh frozen stats when due. A repeated
        quote_id adds no sample (statistics belong to quote EVENTS, not poll
        iterations — polling faster than the feed ticks otherwise collapses
        sigma). Ageing runs every call so a dead feed drains out and goes cold."""
        now = self.clock()
        fresh = quote_id is None or quote_id != self.last_quote_id
        if fresh:
            self.last_quote_id = quote_id
            if self.collecting_since is None:
                self.collecting_since = now
            self.samples.append((now, value))
            self.last_value = value

        horizon = now - self.cfg["LOOKBACK_SEC"]
        while self.samples and self.samples[0][0] < horizon:
            self.samples.popleft()
        if not self.samples:
            self.collecting_since = None

        if (now - self.last_refresh >= self.cfg["STATS_INTERVAL_SEC"]
                or self.mu is None):
            self._refresh(now)

    def seed(self, samples):
        """Prime the window from persisted quotes, oldest first. Collection is
        credited from the OLDEST seeded sample so history_sec reflects when data
        really began. Consecutive-identical spreads are collapsed (older DBs
        wrote once per poll, which would re-create the collapsed-sigma bug)."""
        if not samples:
            return 0
        now = self.clock()
        horizon = now - self.cfg["LOOKBACK_SEC"]
        fresh = [(t, v) for t, v in samples if t >= horizon]
        if not fresh:
            return 0
        fresh.sort(key=lambda row: row[0])
        fresh = self._collapse_repeats(fresh)
        self.samples.extend(fresh)
        self.last_value = fresh[-1][1]
        self.collecting_since = (fresh[0][0] if self.collecting_since is None
                                 else min(self.collecting_since, fresh[0][0]))
        self._refresh(now)
        return len(fresh)

    @staticmethod
    def _collapse_repeats(rows):
        out = []
        previous = object()
        for stamp, value in rows:
            if value != previous:
                out.append((stamp, value))
                previous = value
        return out

    def _refresh(self, now):
        if len(self.samples) < 2:
            return
        values = [v for _, v in self.samples]
        n = len(values)
        mean = sum(values) / n
        variance = sum((v - mean) ** 2 for v in values) / n
        self.mu = mean
        self.sigma = math.sqrt(variance)
        self.half_life_sec = self._estimate_half_life()
        self.last_refresh = now

    def _estimate_half_life(self):
        """AR(1) fit S_{t+1} = c + phi*S_t; HL = -ln2/ln(phi) steps."""
        values = [v for _, v in self.samples]
        if len(values) < 30:
            return None
        x, y = values[:-1], values[1:]
        n = len(x)
        mx, my = sum(x) / n, sum(y) / n
        cov = sum((a - mx) * (b - my) for a, b in zip(x, y))
        var = sum((a - mx) ** 2 for a in x)
        if var <= 0:
            return None
        phi = cov / var
        if phi <= 0 or phi >= 1:
            return None
        avg_dt = (self.samples[-1][0] - self.samples[0][0]) / max(n - 1, 1)
        return (-math.log(2) / math.log(phi)) * avg_dt

    @property
    def degenerate(self):
        if self.sigma is None or self.sigma <= 0:
            return True
        floor = self.cfg.get("MIN_SIGMA", 0.0) or 0.0
        if floor and self.sigma < floor:
            return True
        raw = ((self.last_value - self.mu) / self.sigma
               if self.last_value is not None and self.mu is not None else 0.0)
        return abs(raw) > self.cfg.get("MAX_ABS_Z", 25.0)

    @property
    def suggested_lookback_sec(self):
        if not self.half_life_sec:
            return None
        multiple = self.cfg.get("LOOKBACK_HALF_LIVES", 6.0) or 6.0
        return self.half_life_sec * multiple

    @property
    def quote_rate_per_min(self):
        if len(self.samples) < 2:
            return None
        span = self.samples[-1][0] - self.samples[0][0]
        if span <= 0:
            return None
        return (len(self.samples) - 1) / span * 60.0

    @property
    def history_sec(self):
        if self.collecting_since is None:
            return 0.0
        return max(0.0, self.clock() - self.collecting_since)

    @property
    def min_history_sec(self):
        required = self.cfg.get("MIN_HISTORY_SEC", 0.0) or 0.0
        return min(float(required), float(self.cfg["LOOKBACK_SEC"]))

    @property
    def warm(self):
        return (len(self.samples) >= self.cfg["MIN_SAMPLES"]
                and self.history_sec >= self.min_history_sec
                and self.sigma is not None and not self.degenerate)

    @property
    def z(self):
        if not self.warm or self.last_value is None:
            return None
        return (self.last_value - self.mu) / self.sigma

    def trend_slope(self):
        """Spread change per second over the trend window (sign drives the
        direction filter)."""
        window = self.cfg.get("TREND_WINDOW_SEC", 900)
        now = self.clock()
        recent = [(t, v) for t, v in self.samples if t >= now - window]
        if len(recent) < 10:
            return 0.0
        t0 = recent[0][0]
        xs = [t - t0 for t, _ in recent]
        ys = [v for _, v in recent]
        n = len(xs)
        mx, my = sum(xs) / n, sum(ys) / n
        var = sum((a - mx) ** 2 for a in xs)
        if var <= 0:
            return 0.0
        return sum((a - mx) * (b - my) for a, b in zip(xs, ys)) / var
