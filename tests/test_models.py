"""Quant models: probability filter (OU win-prob + EV) and spread calculator."""

from arrow_statarb.models.probability_filter import ProbabilityFilter
from arrow_statarb.models.spread_calculator import SpreadCalculator


def test_win_probability_monotonic():
    pf = ProbabilityFilter(exit_zscore=0.0, stop_zscore=4.0)
    # Closer to the stop ⇒ lower win probability.
    p2 = pf._win_probability(2.0)
    p3 = pf._win_probability(3.0)
    p35 = pf._win_probability(3.5)
    assert 0.0 < p35 < p3 < p2 < 1.0


def test_win_probability_bounds():
    pf = ProbabilityFilter(exit_zscore=0.0, stop_zscore=4.0)
    assert pf._win_probability(0.0) == 1.0     # already at exit
    assert pf._win_probability(4.0) == 0.0     # at the stop


def test_entry_allowed_typical():
    pf = ProbabilityFilter(commission_per_lot=10, slippage_per_lot=5,
                           lot_multiplier=75, min_win_probability=0.60,
                           min_expected_value=0.0, exit_zscore=0.0, stop_zscore=4.0)
    allow, reason, m = pf.check_entry(z_score=2.5, std=10.0, half_life=20.0, contracts=1)
    assert allow is True
    assert reason == "all_checks_passed"
    assert m["win_probability"] >= 0.60


def test_entry_blocked_low_winprob():
    pf = ProbabilityFilter(min_win_probability=0.999, exit_zscore=0.0, stop_zscore=4.0,
                           lot_multiplier=75)
    allow, reason, _ = pf.check_entry(z_score=2.0, std=10.0, half_life=20.0, contracts=1)
    assert allow is False
    assert reason == "low_win_probability"


def test_entry_disabled_passes():
    pf = ProbabilityFilter(enabled=False)
    allow, reason, _ = pf.check_entry(z_score=0.1, std=10.0, half_life=20.0)
    assert allow is True
    assert reason == "filter_disabled"


def test_time_stop_in_hold():
    pf = ProbabilityFilter(max_half_lives=3.0, exit_zscore=0.0, stop_zscore=4.0,
                           lot_multiplier=75)
    should_exit, reason, _ = pf.check_hold(z_entry=2.5, z_current=1.5, std=10.0,
                                           half_life=2.0, days_held=10.0, contracts=1)
    assert should_exit is True
    assert reason == "time_stop"


def test_spread_calculator_zscore_lifecycle():
    sc = SpreadCalculator(lookback_period=50)
    for _ in range(60):
        sc.add(110.0, 10.0)          # steady spread = 100
    assert sc.is_ready
    # A large deviation gives a large z.
    sc.add(140.0, 10.0)              # spread jumps to 130
    z = sc.calculate_zscore()
    assert abs(z) > 3.0


def test_spread_calculator_half_life():
    sc = SpreadCalculator(lookback_period=100)
    x = 5.0
    for _ in range(150):
        x = 0.85 * x
        sc.add(100.0 + x, 0.0)
    hl = sc.compute_half_life()
    assert hl > 0
