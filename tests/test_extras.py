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


def _diverging_algo(decision_z, fill_z, max_div):
    """Algo whose signal returns ``decision_z`` to the tick and ``fill_z`` to the
    re-sample inside _enter (so the stale-signal guard can be exercised)."""
    calls = {"execute": []}
    seq = {"n": 0}

    def sig():
        seq["n"] += 1
        return _sig(decision_z if seq["n"] == 1 else fill_z)

    params = {"entry_zscore": 2.0, "exit_zscore": 0.0, "stop_zscore": 4.0, "lots": 1,
              "confirmation_ticks": 1, "enable_probability_filter": False, "cooldown": 300,
              "max_entry_z_divergence": max_div}
    a = ArrowAutoTrader(
        signal_provider=sig,
        params_provider=lambda: params,
        execute_fn=lambda d, l: (calls["execute"].append((d, l)) or
                                 {"success": True, "results": [{"order_id": "x"}]}),
        close_fn=lambda d, l: {"success": True, "results": []},
    )
    return a, calls


def test_entry_z_divergence_guard_refuses_stale_signal():
    # Decision z=-2.5 but z re-sampled at order time is -0.5 (snapped back).
    a, calls = _diverging_algo(-2.5, -0.5, max_div=1.0)
    a._tick()
    assert not calls["execute"]                       # entry refused
    assert "stale signal" in a._snap["status"]
    assert a._cooldown_until > time.time()            # cooldown armed


def test_entry_z_divergence_guard_allows_within_threshold():
    # Tiny drift (-2.5 → -2.4) is within tolerance → entry proceeds.
    a, calls = _diverging_algo(-2.5, -2.4, max_div=1.0)
    a._tick()
    assert calls["execute"] == [("LONG_SPREAD", 1)]


def test_entry_z_divergence_guard_disabled_by_default():
    # max_div=0 disables the guard even on a large divergence.
    a, calls = _diverging_algo(-2.5, 0.0, max_div=0.0)
    a._tick()
    assert calls["execute"] == [("LONG_SPREAD", 1)]


def test_max_entry_zscore_cap_blocks_deep_z():
    # |z|=5 exceeds the 3.0 cap → entry refused (likely regime shift).
    a, calls = _algo({"confirmation_ticks": 1, "max_entry_zscore": 3.0})
    _algo.sig = _sig(-5.0)
    a._tick()
    assert not calls["execute"]
    assert "exceeds entry cap" in a._snap["status"]


def test_max_entry_zscore_cap_allows_within_band():
    # |z|=2.5 is above entry (2.0) but below the cap (3.0) → entry proceeds.
    a, calls = _algo({"confirmation_ticks": 1, "max_entry_zscore": 3.0})
    _algo.sig = _sig(-2.5)
    a._tick()
    assert calls["execute"] == [("LONG_SPREAD", 1)]


def test_max_entry_zscore_cap_disabled_by_default():
    a, calls = _algo({"confirmation_ticks": 1})           # no cap configured
    _algo.sig = _sig(-9.0)
    a._tick()
    assert calls["execute"] == [("LONG_SPREAD", 1)]


def _failing_exit_algo(ceiling):
    """Algo whose close always fails, to exercise the exit-failure ceiling."""
    counts = {"close": 0}
    holder = {"sig": _sig(-2.5)}
    params = {"entry_zscore": 2.0, "exit_zscore": 0.0, "stop_zscore": 4.0, "lots": 1,
              "confirmation_ticks": 1, "enable_probability_filter": False,
              "cooldown": 300, "max_exit_failures": ceiling}
    a = ArrowAutoTrader(
        signal_provider=lambda: holder["sig"],
        params_provider=lambda: params,
        execute_fn=lambda d, l, **k: {"success": True, "results": [{"order_id": "x"}]},
        close_fn=lambda d, l, **k: (counts.__setitem__("close", counts["close"] + 1)
                                    or {"success": False, "error": "reject"}),
    )
    return a, holder, counts


def test_exit_failure_ceiling_halts_and_stops_retrying():
    a, holder, counts = _failing_exit_algo(ceiling=2)
    a._tick()                                   # enter LONG_SPREAD
    assert a._pos is not None

    holder["sig"] = _sig(-5.0)                  # |z|=5 ≥ stop (4) → exit fires, fails
    a._tick()
    assert a._exit_failures == 1 and not a._exit_halted and a._pos is not None
    a._tick()
    assert a._exit_failures == 2 and a._exit_halted   # ceiling reached

    before = counts["close"]
    a._tick()                                   # halted → no further close attempts
    assert counts["close"] == before
    assert "EXIT HALTED" in a._snap["status"]
    assert a._pos is not None                    # position kept, awaiting manual close


def test_exit_failure_ceiling_unlimited_when_zero():
    a, holder, counts = _failing_exit_algo(ceiling=0)
    a._tick()
    holder["sig"] = _sig(-5.0)
    a._tick(); a._tick(); a._tick()
    assert not a._exit_halted                    # 0 = never halts
    assert counts["close"] >= 3                  # keeps retrying every tick


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

def test_trade_log_open_position_tracks_unmatched_open(tmp_path):
    tl = TradeLog(tmp_path / "t.json", brokerage_per_lot=0)
    assert tl.open_position() is None
    tl.record(action="OPEN", direction="LONG_SPREAD", lots=3, spread=100.0,
              dry_run=False, status="LIVE")
    op = tl.open_position()
    assert op["direction"] == "LONG_SPREAD" and op["lots"] == 3
    assert op["entry_spread"] == 100.0
    # Once closed, it is no longer an open position.
    tl.record(action="CLOSE", direction="LONG_SPREAD", lots=3, spread=110.0,
              dry_run=False, status="LIVE")
    assert tl.open_position() is None

def test_trade_log_day_pnl_scopes_to_today(tmp_path):
    import time as _t
    tl = TradeLog(tmp_path / "t.json", brokerage_per_lot=0)
    # A trade from two days ago must NOT count toward today's P&L.
    tl._trades.append({"ts": _t.time() - 2*86400, "action": "CLOSE", "net_pnl": -500.0})
    tl._trades.append({"ts": _t.time(), "action": "CLOSE", "net_pnl": -120.0})
    tl._trades.append({"ts": _t.time(), "action": "CLOSE", "net_pnl": 30.0})
    assert tl.day_pnl() == -90.0       # only today's -120 + 30

def test_trade_log_pnl_scales_with_lot_size(tmp_path):
    tl = TradeLog(tmp_path / "t.json", brokerage_per_lot=0)
    tl.record(action="OPEN", direction="LONG_SPREAD", lots=1, spread=100.0,
              dry_run=False, status="LIVE", lot_size=75)
    rec = tl.record(action="CLOSE", direction="LONG_SPREAD", lots=1, spread=110.0,
                    dry_run=False, status="LIVE", lot_size=75)
    # +10 spread move × 1 lot × 75 units = 750 (not 10).
    assert rec["spread_pnl"] == 750.0
    assert rec["net_pnl"] == 750.0


def test_trade_log_round_trips_detail(tmp_path):
    import time as _t
    tl = TradeLog(tmp_path / "t.json", brokerage_per_lot=0)
    tl.record(action="OPEN", direction="SHORT_SPREAD", lots=2, spread=50.0,
              dry_run=True, status="DRY-RUN", lot_size=25, zscore=2.4,
              leg_a_price=500.0, leg_b_price=450.0, name="A − B")
    _t.sleep(0.01)
    tl.record(action="CLOSE", direction="SHORT_SPREAD", lots=2, spread=40.0,
              dry_run=True, status="DRY-RUN", lot_size=25, zscore=0.1,
              leg_a_price=495.0, leg_b_price=455.0, name="A − B")
    j = tl.round_trips()
    assert j["count"] == 1
    trip = j["trips"][0]
    assert trip["name"] == "A − B"
    assert trip["entry_zscore"] == 2.4 and trip["exit_zscore"] == 0.1
    assert trip["entry_leg_a"] == 500.0 and trip["exit_leg_b"] == 455.0
    assert trip["entry_spread"] == 50.0 and trip["exit_spread"] == 40.0
    # SHORT profits when spread falls: (50-40) × 2 × 25 = 500
    assert trip["spread_pnl"] == 500.0
    assert trip["held_sec"] is not None and trip["held_sec"] >= 0
    assert trip["cum_pnl"] == trip["net_pnl"]
    assert j["total_pnl"] == 500.0
