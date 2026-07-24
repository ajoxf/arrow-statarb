"""SignalEngine — the single source of truth: time window, stats, z, half-life."""

import math
import time

from arrow_statarb.core.signal import SignalEngine


def _persist_eng(path, series="A|B", **pover):
    return SignalEngine(prices_provider=lambda: (None, None),
                        params_provider=lambda: _params(**pover),
                        persist_path=path, series_key_provider=lambda: series)


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


def test_hedge_ratio_scales_spread():
    # Non-1:1 pair: spread = hedge_ratio × leg_a − leg_b. An ETF at ~280 vs a
    # future at ~24000 only becomes a real basis once leg_a is scaled up ~87×.
    eng = SignalEngine(prices_provider=lambda: (None, None),
                       params_provider=lambda: _params(hedge_ratio=87.0))
    eng.push(280.0, 24000.0, ts=1000.0)             # 87×280 − 24000 = 360
    sig = eng.get_signal()
    assert sig["spread"] == 87.0 * 280.0 - 24000.0  # == 360.0
    assert sig["leg_a"] == 280.0 and sig["leg_b"] == 24000.0   # raw prices preserved


def test_stats_update_interval_caches_bands():
    # With a stats interval, the mean/std are cached: a new deviating sample moves
    # the z (live spread vs cached bands) but NOT the mean/std until the interval
    # elapses. With interval 0 the bands recompute every tick.
    eng = SignalEngine(prices_provider=lambda: (None, None),
                       params_provider=lambda: _params(stats_update_interval_sec=3600.0))
    t = 1000.0
    for i in range(50):
        eng.push(110.0 + (i % 3), 10.0, ts=t + i)   # spread ≈ 100 with some variation
    s1 = eng.get_signal()
    mean1, std1 = s1["mean"], s1["std"]
    assert std1 > 0
    eng.push(150.0, 10.0, ts=t + 50)            # big deviation → spread 140
    s2 = eng.get_signal()
    assert s2["mean"] == mean1 and s2["std"] == std1   # bands cached (unchanged)
    assert abs(s2["zscore"]) > abs(s1["zscore"])       # z moved on the live spread


def test_hedge_ratio_default_is_raw_difference():
    eng = SignalEngine(prices_provider=lambda: (None, None), params_provider=_params)
    eng.push(24080.0, 24000.0, ts=1000.0)           # default k=1 → raw diff
    assert eng.get_signal()["spread"] == 80.0


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


def test_excursion_event_log_has_timestamps():
    eng = SignalEngine(lambda: (None, None), lambda: _params())
    eng._tally_z(0.0, ts=1000.0)
    eng._tally_z(2.5, ts=1001.0)     # touch +2σ
    eng._tally_z(0.0, ts=1002.0)     # reversion
    e = eng.get_excursions()
    assert e["event_count"] == 2
    types = [ev["type"] for ev in e["events"]]   # newest first
    assert types == ["reversion", "touch_2_up"]
    assert e["events"][-1]["ts"] == 1001.0
    assert "time" in e["events"][0] and e["events"][0]["z"] == 0.0
    eng.reset_excursions()
    assert eng.get_excursions()["event_count"] == 0


# ── window persistence (resume warm-up across a quick restart) ───────────────
def test_persist_and_restore_resumes_window(tmp_path):
    p = tmp_path / "win.json"
    now = time.time()
    eng = _persist_eng(p)
    for i in range(30):
        eng.push(110.0, 10.0, ts=now - 30 + i)        # recent ~30s of samples
    eng._persist()
    assert p.exists()
    eng2 = _persist_eng(p)
    eng2._restore()
    assert len(eng2._samples) == 30
    assert eng2.get_signal()["samples"] == 30


def test_restore_refused_on_series_mismatch(tmp_path):
    p = tmp_path / "win.json"
    now = time.time()
    eng = _persist_eng(p, series="A|B")
    for i in range(20):
        eng.push(110.0, 10.0, ts=now - 20 + i)
    eng._persist()
    eng2 = _persist_eng(p, series="C|D")               # different contracts
    eng2._restore()
    assert len(eng2._samples) == 0                     # not resumable


def test_restore_refused_when_stale(tmp_path):
    p = tmp_path / "win.json"
    old = time.time() - 3600                           # 1 hour ago
    eng = _persist_eng(p, window_minutes=120.0)        # 2h window — samples still "in window"
    for i in range(20):
        eng.push(110.0, 10.0, ts=old + i)
    eng._persist()
    eng2 = _persist_eng(p, window_minutes=120.0)
    eng2._restore()
    assert len(eng2._samples) == 0                     # gap > resume_max_gap → fresh


def test_restore_drops_samples_older_than_window(tmp_path):
    p = tmp_path / "win.json"
    now = time.time()
    eng = _persist_eng(p, window_minutes=1.0)          # 60s window
    for i in range(10):                                # 10 old (>60s) — bypass push trim
        eng._samples.append((now - 300 + i, 110.0, 10.0, 100.0))
    for i in range(10):                                # 10 recent (<60s)
        eng._samples.append((now - 10 + i, 110.0, 10.0, 100.0))
    eng._persist()
    eng2 = _persist_eng(p, window_minutes=1.0)
    eng2._restore()
    assert len(eng2._samples) == 10                    # only in-window samples kept


def test_reset_clears_persisted_file(tmp_path):
    p = tmp_path / "win.json"
    now = time.time()
    eng = _persist_eng(p)
    for i in range(10):
        eng.push(110.0, 10.0, ts=now - 10 + i)
    eng._persist()
    assert p.exists()
    eng.reset()
    assert not p.exists()                              # intentional reset wipes it


def test_persist_disabled_writes_nothing(tmp_path):
    p = tmp_path / "win.json"
    now = time.time()
    eng = _persist_eng(p, persist_window=False)
    for i in range(10):
        eng.push(110.0, 10.0, ts=now - 10 + i)
    eng._persist()
    assert not p.exists()


def test_persist_skipped_without_series_key(tmp_path):
    p = tmp_path / "win.json"
    now = time.time()
    eng = _persist_eng(p, series="")                   # legs unknown
    for i in range(10):
        eng.push(110.0, 10.0, ts=now - 10 + i)
    eng._persist()
    assert not p.exists()


# ── regime detection ────────────────────────────────────────────────────────
def _reg_params(**over):
    p = {"window_minutes": 120.0, "sample_interval_sec": 1.0, "min_signal_minutes": 0.0,
         "entry_zscore": 2.0, "exit_zscore": 0.0, "stop_zscore": 4.0,
         "regime_window_samples": 60, "regime_efficiency_ratio_max": 0.6,
         "regime_min_zero_crossings": 4, "regime_vr_lag": 5}
    p.update(over)
    return p


def test_regime_flags_trending_series():
    eng = SignalEngine(lambda: (None, None), lambda: _reg_params())
    now = time.time()
    for i in range(80):                              # steady upward drift
        eng.push(100.0 + i, 0.0, ts=now - 80 + i)
    reg = eng.regime(ts_now=now)
    assert reg["state"] == "TRENDING"
    assert reg["efficiency_ratio"] > 0.9            # monotone → ER≈1
    assert reg["slope"] > 0


def test_regime_flags_range_series():
    eng = SignalEngine(lambda: (None, None), lambda: _reg_params())
    now = time.time()
    for i in range(80):                              # oscillating around 100
        eng.push(100.0 + (5 if i % 2 else -5), 0.0, ts=now - 80 + i)
    reg = eng.regime(ts_now=now)
    assert reg["state"] == "RANGE"
    assert reg["zero_crossings"] > 4                # many anchor crossings


def test_regime_unknown_when_thin():
    eng = SignalEngine(lambda: (None, None), lambda: _reg_params())
    eng.push(100.0, 0.0, ts=time.time())
    assert eng.regime()["state"] == "UNKNOWN"
