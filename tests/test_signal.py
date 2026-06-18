"""SignalEngine — the single source of truth: time window, stats, z, half-life."""

import math

from arrow_statarb.core.signal import SignalEngine


def _params(**over):
    p = {"window_minutes": 2.0, "sample_interval_sec": 0.5, "min_signal_minutes": 1.0,
         "entry_zscore": 2.0, "exit_zscore": 0.0, "stop_zscore": 4.0}
    p.update(over)
    return p


def test_compute_stats():
    mean, std = SignalEngine.compute_stats([1.0, 2.0, 3.0, 4.0, 5.0])
    assert mean == 3.0
    assert std > 0


def test_empty_signal():
    eng = SignalEngine(prices_provider=lambda: (None, None), params_provider=_params)
    sig = eng.get_signal()
    assert sig["samples"] == 0
    assert sig["ready"] is False


def test_spread_and_z():
    eng = SignalEngine(prices_provider=lambda: (None, None), params_provider=_params)
    # Inject a window of samples with a clear deviation at the end.
    base = 100.0
    t = 1000.0
    for i in range(50):
        eng.push(base + 10.0, 10.0, ts=t + i)      # spread = 100, steady
    # Final sample deviates strongly upward → positive z.
    eng.push(base + 20.0, 10.0, ts=t + 50)         # spread = 110
    sig = eng.get_signal()
    assert sig["spread"] == 110.0
    assert sig["zscore"] > 2.0                      # rich spread


def test_window_trims_old_samples():
    eng = SignalEngine(prices_provider=lambda: (None, None),
                       params_provider=lambda: _params(window_minutes=1.0))  # 60s window
    eng.push(110.0, 10.0, ts=0.0)        # old, should be trimmed
    eng.push(110.0, 10.0, ts=100.0)      # newer
    eng.push(111.0, 10.0, ts=120.0)      # window = [60s before 120] → keeps 100,120
    sig = eng.get_signal()
    assert sig["samples"] == 2


def test_ready_requires_min_minutes():
    eng = SignalEngine(prices_provider=lambda: (None, None),
                       params_provider=lambda: _params(min_signal_minutes=5.0))
    for i in range(20):
        eng.push(110.0 + (i % 3), 10.0, ts=i)        # only ~20s span
    assert eng.get_signal()["ready"] is False


def test_half_life_mean_reverting():
    # AR(1) with phi in (0,1) is mean-reverting → positive finite half-life.
    spreads = []
    x = 5.0
    for _ in range(200):
        x = 0.8 * x          # decays toward 0 → phi≈0.8
        spreads.append(x + 100.0)
    hl = SignalEngine.half_life(spreads)
    assert hl > 0
    assert math.isfinite(hl)
