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

        # ── z-score excursion counters (session-cumulative; manual reset) ─────
        # Counts how often z stretches out to ±2σ / ±3σ and how often a stretch
        # (|z| ≥ 2) reverts back through the mean — the mean-reversion frequency.
        self._exc: Dict = self._fresh_exc()
        self._exc_events: Deque[Dict] = deque(maxlen=500)   # timestamped event log
        self._z_prev: Optional[float] = None
        # per-side arming so boundary chatter near a band isn't double-counted:
        # a band only re-arms once z falls back inside the re-arm zone (|z|<1).
        self._arm_2u = self._arm_3u = self._arm_2d = self._arm_3d = True
        self._active_up = self._active_dn = False   # a ≥2σ excursion is open

    @staticmethod
    def _fresh_exc() -> Dict:
        return {"touch_2_up": 0, "touch_2_down": 0, "touch_3_up": 0,
                "touch_3_down": 0, "reversions": 0, "max_z": 0.0, "min_z": 0.0,
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
            self._reset_exc_state()       # series discontinues; counters preserved

    def _reset_exc_state(self) -> None:
        """Clear the transient crossing state (not the counters)."""
        self._z_prev = None
        self._arm_2u = self._arm_3u = self._arm_2d = self._arm_3d = True
        self._active_up = self._active_dn = False

    def reset_excursions(self) -> None:
        """Zero the session-cumulative z-score excursion counters (Reset button)."""
        with self._lock:
            self._exc = self._fresh_exc()
            self._exc_events.clear()
            self._reset_exc_state()
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
        e["events"] = list(reversed(events))[:max_events]   # newest first, capped
        return e

    def _tally_z(self, z: float, ts: Optional[float] = None) -> None:
        """Update excursion counters from the latest z. Called once per sample.

        A 'touch' of ±2σ/±3σ is counted on the OUTWARD crossing of that band,
        then disarmed until z returns inside |z|<1 (hysteresis). A 'reversion'
        is counted when an open ≥2σ excursion crosses back through the mean (0).
        Each counted event is appended to the timestamped event log."""
        ts = time.time() if ts is None else ts
        e = self._exc
        if z > e["max_z"]:
            e["max_z"] = z
        if z < e["min_z"]:
            e["min_z"] = z

        # Upper side
        if z >= 2.0 and self._arm_2u:
            e["touch_2_up"] += 1; self._arm_2u = False; self._active_up = True
            self._log_event("touch_2_up", z, ts)
        if z >= 3.0 and self._arm_3u:
            e["touch_3_up"] += 1; self._arm_3u = False
            self._log_event("touch_3_up", z, ts)
        if z < 1.0:
            self._arm_2u = self._arm_3u = True

        # Lower side
        if z <= -2.0 and self._arm_2d:
            e["touch_2_down"] += 1; self._arm_2d = False; self._active_dn = True
            self._log_event("touch_2_down", z, ts)
        if z <= -3.0 and self._arm_3d:
            e["touch_3_down"] += 1; self._arm_3d = False
            self._log_event("touch_3_down", z, ts)
        if z > -1.0:
            self._arm_2d = self._arm_3d = True

        # Reversion to the mean: an open ≥2σ excursion crossed back through 0.
        if self._z_prev is not None:
            crossed_zero = (self._z_prev > 0 >= z) or (self._z_prev < 0 <= z)
            if crossed_zero and (self._active_up or self._active_dn):
                e["reversions"] += 1
                self._active_up = self._active_dn = False
                self._log_event("reversion", z, ts)
        self._z_prev = z

    def _log_event(self, etype: str, z: float, ts: float) -> None:
        self._exc_events.append({"ts": ts,
                                 "time": time.strftime("%H:%M:%S", time.localtime(ts)),
                                 "type": etype, "z": round(z, 2)})

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
            self._update_excursions_locked(now)

    def push(self, leg_a: float, leg_b: float, ts: Optional[float] = None) -> None:
        """Inject a sample directly (used by tests)."""
        ts = time.time() if ts is None else ts
        with self._lock:
            self._samples.append((ts, float(leg_a), float(leg_b), float(leg_a) - float(leg_b)))
            self._trim(ts, self._p()["window_minutes"] * 60.0)
            self._update_excursions_locked(ts)

    def _update_excursions_locked(self, ts: Optional[float] = None) -> None:
        """Compute the current z over the window and feed the excursion tally.
        Must be called while holding ``self._lock``."""
        if len(self._samples) < 2:
            return
        spreads = [s[3] for s in self._samples]
        mean, std = self.compute_stats(spreads)
        if std <= 1e-12:
            return
        self._tally_z((spreads[-1] - mean) / std, ts)

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

    def get_series(self, max_points: int = 200) -> Dict:
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
        mean, std = self.compute_stats(spreads)
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
