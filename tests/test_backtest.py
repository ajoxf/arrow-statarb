"""Backtest harness — replays bars through the real signal + algo and measures."""

from arrow_statarb.core.backtest import Backtester, synthetic_ou


def _bt(**over):
    sp = {"window_minutes": 30.0, "sample_interval_sec": 0.5, "min_signal_minutes": 1.0,
          "entry_zscore": 2.0, "exit_zscore": 0.0, "stop_zscore": 4.0}
    strat = {"entry_zscore": 2.0, "exit_zscore": 0.0, "stop_zscore": 4.0,
             "confirmation_ticks": 1, "time_stop_half_lives": 5.0, "cooldown": 0.0,
             "enable_probability_filter": False, "lot_multiplier": 65.0,
             "brokerage_per_lot": 20.0, "slippage_per_lot": 5.0}
    strat.update(over)
    return Backtester(signal_params=sp, strategy_params=strat, lot_size=65,
                      brokerage_per_lot=20.0, slippage_per_lot=5.0, lots=1, capital=80000)


def test_backtest_runs_and_trades_on_mean_reverting_data():
    bars = synthetic_ou(4000, sigma=2.0, theta=0.03, seed=7)
    m = _bt().run(bars)
    assert m["bars"] == 4000
    assert m["trades"] >= 1
    assert "win_rate_pct" in m and "max_drawdown" in m
    assert "annualized_roi_pct" in m and "roi_pct" in m
    assert len(m["equity_curve"]) == m["trades"]


def test_backtest_pnl_consistent_with_trades():
    bars = synthetic_ou(3000, sigma=2.0, theta=0.03, seed=3)
    m = _bt().run(bars)
    if m["trades"]:
        assert abs(m["total_pnl"] - m["equity_curve"][-1]) < 1e-6


def test_time_stop_changes_target_exits():
    # A longer time-stop should let more trades reach the z≈0 target.
    bars = synthetic_ou(5000, sigma=2.0, theta=0.03, seed=11)
    short = _bt(time_stop_half_lives=1.0).run(bars)["exit_reasons"].get("target", 0)
    long = _bt(time_stop_half_lives=30.0).run(bars)["exit_reasons"].get("target", 0)
    assert long >= short


def test_backtest_applies_indian_costs_and_metrics():
    bars = synthetic_ou(4000, sigma=2.0, theta=0.03, seed=7)
    sp = {"window_minutes": 30.0, "sample_interval_sec": 0.5, "min_signal_minutes": 1.0,
          "entry_zscore": 2.0, "exit_zscore": 0.0, "stop_zscore": 4.0}
    strat = {"entry_zscore": 2.0, "exit_zscore": 0.0, "stop_zscore": 4.0,
             "confirmation_ticks": 1, "time_stop_half_lives": 5.0, "cooldown": 0.0,
             "enable_probability_filter": False, "lot_multiplier": 65.0,
             "brokerage_per_lot": 20.0, "slippage_per_lot": 5.0}
    free = Backtester(signal_params=sp, strategy_params=strat, lot_size=65, lots=1).run(bars)
    costed = Backtester(signal_params=sp, strategy_params=strat, lot_size=65, lots=1,
                        stt_pct=0.02, other_cost_pct=0.005).run(bars)
    # Settlement-only cost params don't change the algo's ticks → same trades,
    # strictly lower net once STT/other are applied.
    assert costed["trades"] == free["trades"]
    if costed["trades"]:
        assert costed["total_pnl"] < free["total_pnl"]
    # new metrics present
    assert "below_cost_pct" in costed
    assert "reward_risk" in costed["expectancy"] and "expectancy_r" in costed["expectancy"]


def test_backtest_needs_data():
    assert "error" in Backtester().run([(0, 1, 1)])
