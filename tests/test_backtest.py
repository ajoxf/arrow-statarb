"""Backtest harness — replays bars through the real signal + algo and measures."""

from arrow_statarb.core.backtest import Backtester, synthetic_ou


def _bt(**over):
    sp = {"window_minutes": 30.0, "sample_interval_sec": 0.5, "min_signal_minutes": 1.0,
          "entry_zscore": 2.0, "exit_zscore": 0.0, "stop_zscore": 4.0}
    strat = {"entry_zscore": 2.0, "exit_zscore": 0.0, "stop_zscore": 4.0,
             "confirmation_ticks": 1, "time_stop_half_lives": 5.0, "cooldown": 0.0,
             "enable_probability_filter": False, "lot_multiplier": 65.0,
             "brokerage_per_lot": 10.0, "slippage_per_lot": 5.0}
    strat.update(over)
    return Backtester(signal_params=sp, strategy_params=strat, lot_size=65,
                      brokerage_per_lot=10.0, slippage_per_lot=5.0, lots=1, capital=200000)


def test_backtest_runs_and_trades_on_mean_reverting_data():
    bars = synthetic_ou(4000, sigma=2.0, theta=0.03, seed=7)
    m = _bt().run(bars)
    assert m["bars"] == 4000
    assert m["trades"] >= 1                      # a mean-reverting series should trigger entries
    assert "win_rate_pct" in m and "max_drawdown" in m
    assert "annualized_roi_pct" in m and "roi_pct" in m
    # equity curve has one point per closed trade
    assert len(m["equity_curve"]) == m["trades"]


def test_backtest_pnl_consistent_with_trades():
    bars = synthetic_ou(3000, sigma=2.0, theta=0.03, seed=3)
    m = _bt().run(bars)
    # total_pnl equals the final equity-curve point
    if m["trades"]:
        assert abs(m["total_pnl"] - m["equity_curve"][-1]) < 1e-6


def test_backtest_needs_data():
    assert "error" in Backtester().run([(0, 1, 1)])
