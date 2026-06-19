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


def test_excursion_counts_touches_and_reversions():
    # Drive the z-tally directly with a controlled z path. Hysteresis re-arms a
    # band only after z returns inside |z|<1.
    eng = SignalEngine(lambda: (None, None), lambda: _params())
    for z in [0.0, 2.1, 1.0, 0.0, -2.2, 0.0, 3.1, 0.0]:
        eng._tally_z(z)
    e = eng.get_excursions()
    assert e["touch_2_up"] == 2          # +2.1 and (later) +3.1 both cross +2
    assert e["touch_2_down"] == 1        # -2.2
    assert e["touch_3_up"] == 1          # +3.1
    assert e["touch_3_down"] == 0
    assert e["reversions"] == 3          # each ≥2σ stretch returned through 0
    assert e["touch_2_total"] == 3
    assert e["max_z"] == 3.1 and e["min_z"] == -2.2


def test_excursion_hysteresis_no_double_count_near_band():
    # Wobbling around +2 without returning inside |z|<1 counts as ONE touch.
    eng = SignalEngine(lambda: (None, None), lambda: _params())
    for z in [0.0, 2.1, 1.9, 2.2, 1.8, 2.3]:
        eng._tally_z(z)
    assert eng.get_excursions()["touch_2_up"] == 1


def test_excursion_reset():
    eng = SignalEngine(lambda: (None, None), lambda: _params())
    for z in [0.0, 2.5, 0.0]:
        eng._tally_z(z)
    assert eng.get_excursions()["touch_2_up"] == 1
    eng.reset_excursions()
    e = eng.get_excursions()
    assert e["touch_2_up"] == 0 and e["reversions"] == 0
