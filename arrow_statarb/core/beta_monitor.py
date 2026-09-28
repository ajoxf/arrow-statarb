"""Hedge-ratio (beta) drift monitor — MONITORING / DISPLAY ONLY.

A pair trade assumes the two legs keep a stable linear relationship: leg_b ≈
β·leg_a. β is the hedge ratio. The signal engine z-scores the spread against
its own rolling mean and does NOT re-estimate β tick-by-tick — so if the real
relationship drifts (a contract rolls, one leg's carry re-prices, a corporate
action, a regime break), the configured hedge ratio silently goes stale and
the "mean" the z-score reverts to is no longer the right one. This module
watches for exactly that and raises a flag. Nothing in the signal, sizing or
exit path may read it — it is an operator's dashboard light, not a gate.

How it works, all derived from the live price window (no persistence, no
fabricated numbers — recomputed each poll from the collected samples):

  • ROLLING β  — the mean price ratio ``leg_b / leg_a`` over a trailing
    ``beta_window``, evaluated at a series of points across the window (O(1)
    per point via prefix sums). The ratio is the same value-neutral basis the
    app's sizing uses for its dollar-neutral β, and unlike an OLS slope on
    short, low-variance windows it is numerically robust — always defined so
    long as leg_a ≠ 0, never blown up by a near-flat regressor.
  • ANCHOR β   — the same mean ratio over the EARLIEST ``anchor_window`` of the
    data: this session's baseline ("this morning's β").
  • z          — how far today's β has moved from the anchor, measured in the
    beta series' OWN volatility: z = (β_now − β_anchor) / σ_β. A rock-stable
    β that suddenly moves a little is significant relative to its own history;
    a naturally noisy β needs a bigger move to matter. σ_β is floored at a
    small fraction of |anchor| so pure numerical jitter can't manufacture a z.

States (mirrors the badge the dashboard renders):
  WARMUP            not enough distinct data to anchor + measure yet
  STABLE            |z| < stable_z — β near its baseline
  DRIFTING          stable_z ≤ |z| < drift_z — moving away (watch)
  STRUCTURAL_DRIFT  |z| ≥ drift_z sustained ≥ structural_min — treat the
                    baseline as broken; re-baseline / roll the legs
  DRIFTED_RETURNED  |z| back under stable_z after having exceeded drift_z —
                    a transient excursion that reverted (no action needed)

Returns ``z=None`` with ``status="WARMUP"`` whenever it cannot answer
honestly — a missing reading is fine; a guessed one is not.
"""

from __future__ import annotations

import bisect
import math
from typing import Dict, List, Optional, Sequence, Tuple

WARMUP = "WARMUP"
STABLE = "STABLE"
DRIFTING = "DRIFTING"
STRUCTURAL_DRIFT = "STRUCTURAL_DRIFT"
DRIFTED_RETURNED = "DRIFTED_RETURNED"

# The smallest β move we trust as signal, as a fraction of |anchor β|. σ_β is
# floored here so that when the rolling β is numerically flat, sub-0.1% jitter
# cannot blow up into a large z. Real structural drift is typically whole
# percent moves, well above this floor.
_STD_FLOOR_FRAC = 1e-3
_EPS = 1e-12


def _warmup(reason: str = "collecting baseline", **extra) -> Dict:
    d = {"z": None, "status": WARMUP, "anchor": None, "current_beta": None,
         "max_abs_z": None, "minutes_beyond": 0, "beta_std": None,
         "n_samples": 0, "span_min": 0.0, "reason": reason}
    d.update(extra)
    return d


def _mean_ratio(i: int, j: int, sr) -> Optional[float]:
    """Mean of leg_b/leg_a over samples [i, j) using a prefix-sum array of the
    per-sample ratios. Returns None for an empty window."""
    n = j - i
    if n < 1:
        return None
    return (sr[j] - sr[i]) / n


def beta_drift_block(
    bars: Sequence[Tuple[float, float, float]],
    *,
    beta_window_sec: float = 1200.0,
    anchor_window_sec: float = 1200.0,
    min_points: int = 40,
    stable_z: float = 1.0,
    drift_z: float = 2.0,
    structural_min_sec: float = 600.0,
    max_estimates: int = 150,
) -> Dict:
    """Beta-drift reading for the dashboard.

    ``bars`` is the collected window as ``[(ts, leg_a, leg_b), …]`` (what
    ``SignalEngine.export_bars()`` returns), oldest first. All thresholds have
    live-markets-sane defaults; the caller may override from config.

    Returns a dict with: ``z`` (current, signed), ``status``, ``anchor`` (β),
    ``current_beta``, ``max_abs_z`` (peak |z| in the window), ``minutes_beyond``
    (how long the tail has held |z| ≥ drift_z), plus ``beta_std``, ``n_samples``
    and ``span_min`` for diagnostics.
    """
    if not bars or len(bars) < min_points:
        return _warmup(n_samples=len(bars or []))

    ts = [float(b[0]) for b in bars]
    n = len(ts)
    span_min = (ts[-1] - ts[0]) / 60.0

    # Per-sample ratio leg_b/leg_a; drop any tick with a non-positive leg_a
    # (a bad/zero quote would poison the mean). If too many are unusable the
    # legs aren't quoting cleanly yet → warm up.
    ratios: List[float] = []
    for _t, la, lb in bars:
        la = float(la)
        ratios.append(float(lb) / la if abs(la) > _EPS else math.nan)
    if sum(1 for r in ratios if not math.isnan(r)) < min_points:
        return _warmup("legs not quoting cleanly yet", n_samples=n,
                       span_min=round(span_min, 1))

    # Need at least an anchor window and a distinct current window of data.
    if (ts[-1] - ts[0]) < (anchor_window_sec + beta_window_sec) * 0.5:
        return _warmup("need more history to separate baseline from now",
                       n_samples=n, span_min=round(span_min, 1))

    # Prefix sum of the ratios (NaNs treated as carrying the previous value's
    # contribution of 0 — we simply skip them by counting only clean samples).
    # To keep O(1) windows with gaps, fill NaN with the running mean so a stray
    # bad tick doesn't shift the window mean; clean feeds have no NaNs at all.
    clean = [r for r in ratios if not math.isnan(r)]
    fill = clean[0] if clean else 0.0
    filled = [(r if not math.isnan(r) else fill) for r in ratios]
    sr = [0.0] * (n + 1)
    for k in range(n):
        sr[k + 1] = sr[k] + filled[k]

    # Anchor β: mean ratio over the earliest anchor_window of samples.
    a_end = bisect.bisect_right(ts, ts[0] + anchor_window_sec)
    a_end = max(a_end, 1)
    anchor = _mean_ratio(0, a_end, sr)

    # Rolling β series: at a spread of end-points across the window, the mean
    # ratio over the trailing beta_window ending at that point. Strided ~O(n).
    first_e = bisect.bisect_left(ts, ts[0] + beta_window_sec)
    first_e = max(first_e, 1)
    if first_e >= n:
        return _warmup("history shorter than one beta window",
                       n_samples=n, span_min=round(span_min, 1))
    stride = max(1, (n - first_e) // max(1, max_estimates))

    series: List[Tuple[float, float]] = []      # (ts, beta)
    e = first_e
    while e < n:
        s = bisect.bisect_left(ts, ts[e] - beta_window_sec)
        b = _mean_ratio(s, e + 1, sr)
        if b is not None:
            series.append((ts[e], b))
        e += stride
    # Always include the most recent point so "now" is the true latest β.
    if series and series[-1][0] != ts[-1]:
        s = bisect.bisect_left(ts, ts[-1] - beta_window_sec)
        b = _mean_ratio(s, n, sr)
        if b is not None:
            series.append((ts[-1], b))

    if anchor is None or len(series) < 2:
        return _warmup("not enough clean history to measure beta",
                       n_samples=n, span_min=round(span_min, 1))

    betas = [b for _, b in series]
    mean_b = sum(betas) / len(betas)
    var_b = sum((b - mean_b) ** 2 for b in betas) / len(betas)
    raw_std = math.sqrt(max(var_b, 0.0))
    std = max(raw_std, abs(anchor) * _STD_FLOOR_FRAC, _EPS)

    zs = [(t, (b - anchor) / std) for t, b in series]
    cur_ts, cur_z = zs[-1]
    current_beta = series[-1][1]
    max_abs_z = max(abs(z) for _, z in zs)

    # How long the tail has continuously held |z| ≥ drift_z (0 if not currently
    # beyond). Walk back through the estimate series from the end.
    minutes_beyond = 0.0
    if abs(cur_z) >= drift_z:
        run_start_ts = cur_ts
        for t, z in reversed(zs):
            if abs(z) >= drift_z:
                run_start_ts = t
            else:
                break
        minutes_beyond = (cur_ts - run_start_ts) / 60.0

    # Status.
    if abs(cur_z) >= drift_z:
        status = STRUCTURAL_DRIFT if (minutes_beyond * 60.0) >= structural_min_sec else DRIFTING
    elif abs(cur_z) >= stable_z:
        status = DRIFTING
    else:
        status = DRIFTED_RETURNED if max_abs_z >= drift_z else STABLE

    return {
        "z": round(cur_z, 3),
        "status": status,
        "anchor": round(anchor, 4),
        "current_beta": round(current_beta, 4),
        "max_abs_z": round(max_abs_z, 3),
        "minutes_beyond": int(round(minutes_beyond)),
        "beta_std": round(std, 6),
        "n_samples": n,
        "span_min": round(span_min, 1),
    }
