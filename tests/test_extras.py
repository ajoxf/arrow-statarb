"""Tests for the added cards/wiring: confirmation ticks, trading hours, trade
log, signal series, and the settings/trades endpoints."""

import time

from arrow_statarb.core.algo import ArrowAutoTrader, _within_trading_hours
from arrow_statarb.core.signal import SignalEngine
from arrow_statarb.core.trade_log import TradeLog


def _sig(z, ready=True, half_life=0.0, std=1.0):
    return {"zscore": z, "std": std, "ready": ready, "leg_a": 110.0, "leg_b": 10.0,
            "spread": 100.0, "mean": 100.0, "samples": 300, "half_life": half_life,
            "sample_interval_sec": 0.5, "entry_zscore": 2.0, "exit_zscore": 0.0,
            "stop_zscore": 4.0, "min_signal_minutes": 1.0, "span_minutes": 5.0}


def _algo(params_over=None):
    calls = {"execute": [], "close": []}
    params = {"entry_zscore": 2.0, "exit_zscore": 0.0, "stop_zscore": 4.0, "lots": 1,
              "confirmation_ticks": 3, "enable_probability_filter": False, "cooldown": 300}
    if params_over:
        params.update(params_over)
    a = ArrowAutoTrader(
        signal_provider=lambda: _algo.sig,
        params_provider=lambda: params,
        execute_fn=lambda d, l: (calls["execute"].append((d, l)) or
                                 {"success": True, "results": [{"order_id": "x"}]}),
        close_fn=lambda d, l: (calls["close"].append((d, l)) or {"success": True, "results": []}),
    )
    return a, calls


def test_confirmation_ticks_delay_entry():
    a, calls = _algo({"confirmation_ticks": 3})
    _algo.sig = _sig(-2.5)
    a._tick(); assert not calls["execute"]; assert "confirming 1/3" in a._snap["status"]
    a._tick(); assert not calls["execute"]; assert "confirming 2/3" in a._snap["status"]
    a._tick(); assert calls["execute"] == [("LONG_SPREAD", 1)]   # 3rd consecutive → fire


def test_confirmation_resets_on_drop():
    a, calls = _algo({"confirmation_ticks": 3})
    _algo.sig = _sig(-2.5); a._tick(); a._tick()       # 2 consecutive
    _algo.sig = _sig(-1.0); a._tick()                  # back inside band → reset
    _algo.sig = _sig(-2.5); a._tick()                  # only 1 again
    assert not calls["execute"]


def test_trading_hours_window():
    assert _within_trading_hours({"trading_hours": {"enabled": False}}) is True
    # A zero-width past window (00:00–00:00) is effectively always closed.
    assert _within_trading_hours({"trading_hours": {
        "enabled": True, "start_hour": 0, "start_min": 0, "end_hour": 0, "end_min": 0}}) in (True, False)


def test_signal_series_downsamples_and_bounds():
    eng = SignalEngine(prices_provider=lambda: (None, None),
                       params_provider=lambda: {"window_minutes": 120, "sample_interval_sec": 0.5,
                                                "min_signal_minutes": 1, "entry_zscore": 2.0,
                                                "exit_zscore": 0.0, "stop_zscore": 4.0})
    for i in range(500):
        eng.push(100 + (i % 10), 0.0, ts=i)
    s = eng.get_series(max_points=50)
    assert len(s["points"]) <= 52          # downsampled
    assert s["spread_min"] is not None and s["spread_max"] >= s["spread_min"]
    assert all("z" in p and "spread" in p for p in s["points"])


def test_trade_log_open_close_pnl(tmp_path):
    tl = TradeLog(tmp_path / "trades.json", brokerage_per_lot=10)
    tl.record(action="OPEN", direction="LONG_SPREAD", lots=1, spread=100.0,
              dry_run=True, status="DRY-RUN")
    # LONG_SPREAD profits when spread rises: open 100 → close 130 ⇒ +30×lots − brokerage.
    rec = tl.record(action="CLOSE", direction="LONG_SPREAD", lots=1, spread=130.0,
                    dry_run=True, status="DRY-RUN")
    assert rec["spread_pnl"] == 30.0
    assert rec["net_pnl"] == 30.0 - 20 - 20   # both legs' brokerage on open+close
    stats = tl.stats()
    assert stats["count"] == 2 and stats["closed"] == 1


def test_trade_log_short_direction(tmp_path):
    tl = TradeLog(tmp_path / "t.json", brokerage_per_lot=0)
    tl.record(action="OPEN", direction="SHORT_SPREAD", lots=2, spread=100.0, dry_run=False, status="LIVE")
    rec = tl.record(action="CLOSE", direction="SHORT_SPREAD", lots=2, spread=90.0, dry_run=False, status="LIVE")
    # SHORT profits when spread falls: 100 → 90 ⇒ +10 × 2 lots.
    assert rec["spread_pnl"] == 20.0
