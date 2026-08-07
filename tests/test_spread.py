"""Per-asset SpreadStats (ported) — warmth, frozen z, dedup, trend."""

from arrow_statarb.core.spread import SpreadStats

CFG = {"LOOKBACK_SEC": 10000, "STATS_INTERVAL_SEC": 0, "MIN_SAMPLES": 5,
       "MIN_HISTORY_SEC": 0, "MIN_SIGMA": 0.0, "MAX_ABS_Z": 25.0,
       "LOOKBACK_HALF_LIVES": 6.0, "TREND_WINDOW_SEC": 10000}


def _clock():
    t = [1000.0]
    return t, (lambda: t[0])


def test_warm_and_z_sign():
    t, clk = _clock()
    s = SpreadStats(CFG, clock=clk)
    for i, v in enumerate([100, 101, 99, 100, 101, 99]):
        t[0] += 1
        s.update(v, quote_id=i)
    assert s.warm is True
    assert s.mu is not None and s.sigma > 0
    # last value 99 is below the mean → negative z
    assert s.z < 0


def test_quote_id_dedup_ignores_repeats():
    t, clk = _clock()
    s = SpreadStats(CFG, clock=clk)
    for _ in range(4):
        t[0] += 1
        s.update(100.0, quote_id="same")     # same quote repeated
    assert len(s.samples) == 1                # counted once


def test_degenerate_when_sigma_zero():
    t, clk = _clock()
    s = SpreadStats(CFG, clock=clk)
    for i in range(6):
        t[0] += 1
        s.update(100.0, quote_id=i)           # constant → sigma 0
    assert s.degenerate is True
    assert s.z is None                        # not warm (degenerate)


def test_trend_slope_sign():
    t, clk = _clock()
    s = SpreadStats(CFG, clock=clk)
    for i in range(12):
        t[0] += 1
        s.update(100.0 + i, quote_id=i)       # steadily rising
    assert s.trend_slope() > 0


def test_seed_credits_history_from_oldest():
    t, clk = _clock()
    s = SpreadStats(CFG, clock=clk)
    seeded = s.seed([(900.0, 100.0), (901.0, 101.0), (902.0, 99.0)])
    assert seeded == 3
    assert s.history_sec >= 98.0              # from t=900 to now 1000
