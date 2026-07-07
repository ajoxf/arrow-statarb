"""Auto-trader lifecycle: entry → hold → exit/stop, EV gate, time-stop, dry-run.

The algo is driven directly through ``_tick`` with a controllable signal dict so
the z-score lifecycle is deterministic (no threads, no network)."""

import time

from arrow_statarb.core.algo import ArrowAutoTrader


def _make(params_over=None, prices=None):
    state = {"sig": None, "prices": prices}
    calls = {"execute": [], "close": []}

    def execute_fn(direction, lots):
        calls["execute"].append((direction, lots))
        return {"success": True, "dry_run": True, "results": [{"order_id": "A"}, {"order_id": "B"}]}

    def close_fn(direction, lots):
        calls["close"].append((direction, lots))
        return {"success": True, "results": [{"order_id": "C"}, {"order_id": "D"}]}

    params = {"entry_zscore": 2.0, "exit_zscore": 0.0, "stop_zscore": 4.0, "lots": 1,
              "tick_interval": 0.5, "cooldown": 300, "lot_multiplier": 75.0,
              "enable_probability_filter": False, "min_win_probability": 0.60,
              "min_expected_value": 0.0, "brokerage_per_lot": 10.0,
              "slippage_per_lot": 5.0, "time_stop_half_lives": 3.0}
    if params_over:
        params.update(params_over)

    algo = ArrowAutoTrader(
        signal_provider=lambda: state["sig"],
        params_provider=lambda: params,
        execute_fn=execute_fn,
        close_fn=close_fn,
        prices_provider=(lambda: state["prices"]) if prices is not None else None,
    )
    return algo, state, calls


def _sig(z, ready=True, half_life=0.0, std=1.0, spread=100.0, regime=None, slope=0.0):
    return {"zscore": z, "std": std, "ready": ready, "leg_a": 110.0, "leg_b": 10.0,
            "spread": spread, "mean": 100.0, "samples": 300, "half_life": half_life,
            "sample_interval_sec": 0.5, "entry_zscore": 2.0, "exit_zscore": 0.0,
            "stop_zscore": 4.0, "min_signal_minutes": 1.0, "span_minutes": 5.0,
            "regime": regime, "regime_detail": {"slope": slope, "state": regime}}


def test_not_ready_collects():
    algo, state, calls = _make()
    state["sig"] = _sig(-3.0, ready=False)
    algo._tick()
    assert "collecting" in algo.get_state()["status"]
    assert not calls["execute"]


def test_entry_long_spread():
    algo, state, calls = _make()
    state["sig"] = _sig(-2.5)            # z ≤ -entry → LONG_SPREAD (buy A / sell B)
    algo._tick()
    assert calls["execute"] == [("LONG_SPREAD", 1)]
    assert algo.get_state()["in_position"] is True
    assert algo.get_state()["position"]["dry_run"] is True


def test_entry_short_spread():
    algo, state, calls = _make()
    state["sig"] = _sig(2.5)             # z ≥ +entry → SHORT_SPREAD (sell A / buy B)
    algo._tick()
    assert calls["execute"] == [("SHORT_SPREAD", 1)]


def test_hold_then_exit_target():
    algo, state, calls = _make()
    state["sig"] = _sig(-2.5); algo._tick()         # enter LONG
    state["sig"] = _sig(-1.0); algo._tick()         # still holding
    assert algo.get_state()["in_position"] is True
    assert "holding" in algo.get_state()["status"]
    state["sig"] = _sig(0.1); algo._tick()          # reverted through exit_z → exit
    assert calls["close"] == [("LONG_SPREAD", 1)]
    assert algo.get_state()["in_position"] is False


def test_min_hold_suppresses_target_exit():
    algo, state, calls = _make({"min_hold_sec": 60.0})
    state["sig"] = _sig(-2.5); algo._tick()         # enter LONG
    state["sig"] = _sig(0.1); algo._tick()          # reverted, but held < min_hold
    assert calls["close"] == []                     # exit suppressed
    assert "min-hold" in algo.get_state()["status"]
    # once the position has lived past the minimum hold, the target exit fires
    algo._pos["entry_time"] = time.time() - 100
    state["sig"] = _sig(0.1); algo._tick()
    assert calls["close"] == [("LONG_SPREAD", 1)]


def test_min_hold_never_suppresses_stop_loss():
    algo, state, calls = _make({"min_hold_sec": 60.0})
    state["sig"] = _sig(-2.5); algo._tick()         # enter LONG (held ~0s)
    state["sig"] = _sig(-4.5); algo._tick()         # |z| ≥ stop within the hold window
    assert calls["close"] == [("LONG_SPREAD", 1)]   # stop is never gated
    assert "STOP" in algo._snap["status"]


def test_stop_loss():
    algo, state, calls = _make()
    state["sig"] = _sig(-2.5); algo._tick()         # enter LONG
    state["sig"] = _sig(-4.5); algo._tick()         # |z| ≥ stop → stop out
    assert calls["close"] == [("LONG_SPREAD", 1)]
    assert "STOP" in algo._snap["status"]


# ── Phase 1: dollar-P&L exit overrides ──────────────────────────────────────
# net P&L (no fees) = (cur_spread − entry_fill) × lots × lot_mult  for LONG,
#                     (entry_fill − cur_spread) × lots × lot_mult  for SHORT.
# Entry fill spread falls back to the entry signal spread (100.0 here) when the
# test execute stub returns no fill price. lot_mult=1, brokerage=0 → clean math.
def _pnl_make(over=None):
    params = {"dollar_stop_inr": 0.0, "profit_target_inr": 0.0,
              "lot_multiplier": 1.0, "brokerage_per_lot": 0.0}
    if over:
        params.update(over)
    return _make(params)


def test_live_net_pnl_long_and_short_signs():
    algo, state, calls = _pnl_make()
    state["sig"] = _sig(-2.5, spread=100.0); algo._tick()           # enter LONG @100
    state["sig"] = _sig(-1.0, spread=130.0); algo._tick()           # spread rose 30
    assert algo.get_state()["net_pnl"] == 30.0                      # LONG gains
    # fresh SHORT trade
    algo, state, calls = _pnl_make()
    state["sig"] = _sig(2.5, spread=100.0); algo._tick()            # enter SHORT @100
    state["sig"] = _sig(1.0, spread=130.0); algo._tick()            # spread rose 30
    assert algo.get_state()["net_pnl"] == -30.0                     # SHORT loses


def test_dollar_stop_fires_long():
    algo, state, calls = _pnl_make({"dollar_stop_inr": 50.0})
    state["sig"] = _sig(-2.5, spread=100.0); algo._tick()           # enter LONG
    state["sig"] = _sig(-1.0, spread=40.0); algo._tick()            # net = −60 ≤ −50
    assert calls["close"] == [("LONG_SPREAD", 1)]
    assert "STOP PRICE" in algo._snap["status"]


def test_dollar_stop_fires_short():
    algo, state, calls = _pnl_make({"dollar_stop_inr": 50.0})
    state["sig"] = _sig(2.5, spread=100.0); algo._tick()            # enter SHORT
    state["sig"] = _sig(1.0, spread=160.0); algo._tick()            # net = −60 ≤ −50
    assert calls["close"] == [("SHORT_SPREAD", 1)]
    assert "STOP PRICE" in algo._snap["status"]


def test_profit_target_fires_long():
    algo, state, calls = _pnl_make({"profit_target_inr": 50.0})
    state["sig"] = _sig(-2.5, spread=100.0); algo._tick()           # enter LONG
    state["sig"] = _sig(-1.5, spread=160.0); algo._tick()           # net = +60 ≥ 50, z not reverted
    assert calls["close"] == [("LONG_SPREAD", 1)]
    assert "PROFIT TARGET" in algo._snap["status"]


def test_profit_target_fires_short():
    algo, state, calls = _pnl_make({"profit_target_inr": 50.0})
    state["sig"] = _sig(2.5, spread=100.0); algo._tick()            # enter SHORT
    state["sig"] = _sig(1.5, spread=40.0); algo._tick()             # net = +60 ≥ 50
    assert calls["close"] == [("SHORT_SPREAD", 1)]
    assert "PROFIT TARGET" in algo._snap["status"]


def test_position_detail_in_snapshot():
    # The Signal & Position card reads these live snapshot fields.
    algo, state, calls = _pnl_make({"lot_multiplier": 65.0, "time_stop_half_lives": 2.0})
    state["sig"] = _sig(-2.5, spread=100.0, half_life=10.0); algo._tick()    # enter LONG
    state["sig"] = _sig(-1.0, spread=130.0, half_life=10.0); algo._tick()    # holding
    st = algo.get_state()
    assert st["entry_spread"] == 100.0
    assert st["delta_spread"] == 30.0
    assert st["held_sec"] is not None
    assert st["max_hold_sec"] == 10.0                 # 2 × 10 × 0.5
    assert st["notional"] == 7150                     # 1 lot × 65 × leg_a(110)
    assert st["position"]["entry_z"] == -2.5


def test_dollar_stop_priority_over_time_stop():
    # Both the dollar stop and the time-stop would fire; risk-first must win.
    algo, state, calls = _pnl_make({"dollar_stop_inr": 50.0, "time_stop_half_lives": 1.0})
    state["sig"] = _sig(-2.5, spread=100.0, half_life=1.0); algo._tick()
    algo._pos["entry_time"] = time.time() - 10_000             # time-stop also due
    state["sig"] = _sig(-1.0, spread=40.0, half_life=1.0); algo._tick()
    assert calls["close"] == [("LONG_SPREAD", 1)]
    assert "STOP PRICE" in algo._snap["status"]              # not TIME-STOP


def test_dollar_stop_not_gated_by_min_hold():
    # Risk control must fire immediately even inside the min-hold window.
    algo, state, calls = _pnl_make({"dollar_stop_inr": 50.0, "min_hold_sec": 600.0})
    state["sig"] = _sig(-2.5, spread=100.0); algo._tick()           # enter (held ~0s)
    state["sig"] = _sig(-1.0, spread=40.0); algo._tick()            # net −60 within hold
    assert calls["close"] == [("LONG_SPREAD", 1)]
    assert "STOP PRICE" in algo._snap["status"]


def test_dollar_exits_disabled_when_zero():
    # 0/0 → no dollar exits; a big paper loss with z un-reverted just holds.
    algo, state, calls = _pnl_make()
    state["sig"] = _sig(-2.5, spread=100.0); algo._tick()
    state["sig"] = _sig(-1.0, spread=40.0); algo._tick()            # net −60 but disabled
    assert calls["close"] == []
    assert "holding" in algo.get_state()["status"]


def test_dollar_stop_uses_entry_fill_not_decision():
    # The execute stub reports a fill spread of 100 while the decision spread is
    # 999. P&L MUST be measured from the FILL: at cur=130 the fill-based net is
    # +30 (no stop), whereas a decision-based net would be −869 (would stop).
    algo, state, calls = _make({"dollar_stop_inr": 50.0, "lot_multiplier": 1.0,
                                "brokerage_per_lot": 0.0})

    def execute_with_fill(direction, lots, **kw):
        calls["execute"].append((direction, lots))
        return {"success": True, "dry_run": True, "fill_spread": 100.0,
                "results": [{"order_id": "A"}, {"order_id": "B"}]}
    algo._execute = execute_with_fill
    state["sig"] = _sig(-2.5, spread=999.0); algo._tick()           # decision 999, fill 100
    assert algo._pos["entry_fill_spread"] == 100.0
    state["sig"] = _sig(-1.0, spread=130.0); algo._tick()           # fill net +30 → no stop
    assert calls["close"] == []
    assert algo.get_state()["net_pnl"] == 30.0                      # measured from fill, not 999


def test_cooldown_blocks_reentry():
    algo, state, calls = _make()
    state["sig"] = _sig(-2.5); algo._tick()         # enter
    state["sig"] = _sig(0.1); algo._tick()          # exit → cooldown starts
    state["sig"] = _sig(-2.5); algo._tick()         # would re-enter but cooling down
    assert len(calls["execute"]) == 1
    assert algo.get_state()["status"] == "cooldown"


def test_ev_gate_blocks_entry():
    # Impossibly high min EV → filter blocks the entry.
    algo, state, calls = _make({"enable_probability_filter": True,
                                "min_expected_value": 1e12})
    state["sig"] = _sig(-2.5)
    algo._tick()
    assert not calls["execute"]
    assert "blocked" in algo.get_state()["status"]


def test_time_stop():
    algo, state, calls = _make({"time_stop_half_lives": 3.0})
    state["sig"] = _sig(-2.5, half_life=10.0); algo._tick()   # enter (max hold = 3*10*0.5 = 15s)
    # Pretend the position has been held well past the time stop.
    algo._pos["entry_time"] = time.time() - 100
    state["sig"] = _sig(-1.5, half_life=10.0)                 # not reverted, not stopped
    algo._tick()
    assert calls["close"] == [("LONG_SPREAD", 1)]
    assert "TIME-STOP" in algo._snap["status"]


def test_restore_position_readopts_open_trade():
    algo, state, calls = _make()
    ok = algo.restore_position({"direction": "SHORT_SPREAD", "lots": 2,
                                "entry_spread": 101.5, "ts": time.time(), "dry_run": False})
    assert ok is True
    st = algo.get_state()
    assert st["in_position"] is True
    assert st["position"]["direction"] == "SHORT_SPREAD"
    assert st["position"]["lots"] == 2
    # entry_z sign must reflect a SHORT entry (z >= +entry → positive)
    assert algo._pos["entry_z"] > 0


def test_restore_position_noop_when_already_in_position():
    algo, state, calls = _make()
    state["sig"] = _sig(-2.5); algo._tick()          # opens a LONG_SPREAD
    assert algo.restore_position({"direction": "SHORT_SPREAD", "lots": 9}) is False
    assert algo._pos["direction"] == "LONG_SPREAD"


def test_restore_position_ignores_empty():
    algo, state, calls = _make()
    assert algo.restore_position(None) is False
    assert algo.restore_position({}) is False
    assert algo.get_state()["in_position"] is False


def test_restored_position_can_exit_on_revert():
    algo, state, calls = _make()
    algo.restore_position({"direction": "LONG_SPREAD", "lots": 1, "ts": time.time()})
    state["sig"] = _sig(0.2)                          # reverted through exit_z (0.0)
    algo._tick()
    assert calls["close"] == [("LONG_SPREAD", 1)]


def test_daily_loss_limit_blocks_entry():
    algo, state, calls = _make({"max_daily_loss": 1000.0, "day_pnl": -1000.0})
    state["sig"] = _sig(-2.5)          # would normally enter LONG_SPREAD
    algo._tick()
    assert not calls["execute"]
    assert "daily loss limit" in algo.get_state()["status"]


def test_daily_loss_limit_allows_when_within():
    algo, state, calls = _make({"max_daily_loss": 1000.0, "day_pnl": -200.0})
    state["sig"] = _sig(-2.5)
    algo._tick()
    assert calls["execute"] == [("LONG_SPREAD", 1)]


def test_daily_loss_zero_means_disabled():
    algo, state, calls = _make({"max_daily_loss": 0.0, "day_pnl": -999999.0})
    state["sig"] = _sig(-2.5)
    algo._tick()
    assert calls["execute"] == [("LONG_SPREAD", 1)]   # 0 = no limit


def test_entry_records_decision_z_and_source():
    # The journal should capture the z/spread the algo ACTED on, via execute_fn
    # kwargs — not a value re-sampled later.
    seen = {}

    def execute_fn(direction, lots, source=None, z=None, spread=None):
        seen.update(direction=direction, source=source, z=z, spread=spread)
        return {"success": True, "dry_run": True, "results": []}

    def close_fn(direction, lots, source=None, reason=None, z=None, spread=None):
        return {"success": True, "results": []}

    params = {"entry_zscore": 2.0, "exit_zscore": 0.0, "stop_zscore": 4.0, "lots": 1,
              "tick_interval": 0.5, "cooldown": 300, "lot_multiplier": 75.0,
              "enable_probability_filter": False, "time_stop_half_lives": 3.0}
    state = {"sig": None}
    algo = ArrowAutoTrader(signal_provider=lambda: state["sig"],
                           params_provider=lambda: params,
                           execute_fn=execute_fn, close_fn=close_fn)
    state["sig"] = _sig(-2.6)         # z below -entry → LONG_SPREAD at z=-2.6
    algo._tick()
    assert seen["source"] == "algo"
    assert seen["z"] == -2.6          # exact decision z, not re-sampled
    assert seen["direction"] == "LONG_SPREAD"


def test_no_entry_within_close_buffer():
    from datetime import datetime, timedelta, timezone
    ist = timezone(timedelta(hours=5, minutes=30))
    close = datetime.now(ist) + timedelta(minutes=10)   # close 10 min away
    algo, state, calls = _make({"no_entry_buffer_min": 30,   # buffer 30 → within
        "trading_hours": {"close_hour": close.hour, "close_min": close.minute}})
    state["sig"] = _sig(-2.5)
    algo._tick()
    assert not calls["execute"]
    assert "no new entries" in algo.get_state()["status"]


def test_entries_allowed_far_from_close():
    from datetime import datetime, timedelta, timezone
    ist = timezone(timedelta(hours=5, minutes=30))
    close = datetime.now(ist) + timedelta(minutes=90)
    algo, state, calls = _make({"no_entry_buffer_min": 10,
        "trading_hours": {"close_hour": close.hour, "close_min": close.minute}})
    state["sig"] = _sig(-2.5)
    algo._tick()
    assert calls["execute"] == [("LONG_SPREAD", 1)]


# ── Phase 2: max-hold upgrade + trailing stop ───────────────────────────────
def test_trailing_stop_fires_on_pullback():
    algo, state, calls = _pnl_make({"trailing_stop_pct": 20.0})   # floor 0 → arm from profit
    state["sig"] = _sig(-2.5, spread=100.0); algo._tick()         # enter LONG
    state["sig"] = _sig(-1.0, spread=150.0); algo._tick()         # net +50 → peak 50, no fire
    assert calls["close"] == []
    state["sig"] = _sig(-1.0, spread=135.0); algo._tick()         # net +35 < 50×0.8 → fire
    assert calls["close"] == [("LONG_SPREAD", 1)]
    assert "TRAILING STOP" in algo._snap["status"]


def test_trailing_floor_gate_blocks_small_peak():
    algo, state, calls = _pnl_make({"trailing_stop_pct": 20.0, "trailing_stop_floor_pct": 70.0,
                                    "profit_target_inr": 100.0})
    state["sig"] = _sig(-2.5, spread=100.0); algo._tick()         # enter
    state["sig"] = _sig(-1.0, spread=150.0); algo._tick()         # net +50; floor=70 → not armed
    state["sig"] = _sig(-1.0, spread=110.0); algo._tick()         # net +10 big pullback but un-armed
    assert calls["close"] == []                                  # floor not met → no trailing exit


def test_profit_target_beats_trailing():
    algo, state, calls = _pnl_make({"profit_target_inr": 40.0, "trailing_stop_pct": 20.0})
    state["sig"] = _sig(-2.5, spread=100.0); algo._tick()         # enter
    state["sig"] = _sig(-1.0, spread=150.0); algo._tick()         # net +50 ≥ 40 → profit target wins
    assert calls["close"] == [("LONG_SPREAD", 1)]
    assert "PROFIT TARGET" in algo._snap["status"]


def test_max_hold_silent_when_losing_with_stop():
    algo, state, calls = _pnl_make({"max_hold_silent_when_losing": True,
                                    "dollar_stop_inr": 500.0, "time_stop_half_lives": 1.0})
    state["sig"] = _sig(-2.5, spread=100.0, half_life=1.0); algo._tick()   # enter
    algo._pos["entry_time"] = time.time() - 10_000              # max-hold due
    state["sig"] = _sig(-1.0, spread=80.0, half_life=1.0); algo._tick()    # net −20 (losing)
    assert calls["close"] == []                                # silent → not closed
    assert algo.get_state()["max_hold_expired"] is True
    assert "EXPIRED" in algo._snap["status"]


def test_max_hold_fires_when_losing_without_stop():
    # Silent-when-losing only applies with a ₹ backstop; without one it still fires.
    algo, state, calls = _pnl_make({"max_hold_silent_when_losing": True,
                                    "dollar_stop_inr": 0.0, "time_stop_half_lives": 1.0})
    state["sig"] = _sig(-2.5, spread=100.0, half_life=1.0); algo._tick()
    algo._pos["entry_time"] = time.time() - 10_000
    state["sig"] = _sig(-1.0, spread=80.0, half_life=1.0); algo._tick()    # losing, no backstop
    assert calls["close"] == [("LONG_SPREAD", 1)]
    assert "TIME-STOP" in algo._snap["status"]


def test_max_hold_z_progress_gate_suppresses_winner():
    algo, state, calls = _pnl_make({"max_hold_z_progress_min": 0.5, "time_stop_half_lives": 1.0})
    state["sig"] = _sig(-2.5, spread=100.0, half_life=1.0); algo._tick()   # entry |z|=2.5
    algo._pos["entry_time"] = time.time() - 10_000
    # winning + z reverted 0.6 of the way (entry 2.5 → exit 0): (2.5−1.0)/2.5 = 0.6 ≥ 0.5
    state["sig"] = _sig(-1.0, spread=150.0, half_life=1.0); algo._tick()   # net +50
    assert calls["close"] == []                                # suppressed → let it run
    assert algo.get_state()["max_hold_expired"] is True


def test_max_hold_fires_when_not_reverted_enough():
    algo, state, calls = _pnl_make({"max_hold_z_progress_min": 0.5, "time_stop_half_lives": 1.0})
    state["sig"] = _sig(-2.5, spread=100.0, half_life=1.0); algo._tick()
    algo._pos["entry_time"] = time.time() - 10_000
    # z reverted only 0.2 of the way: (2.5−2.0)/2.5 = 0.2 < 0.5 → not suppressed
    state["sig"] = _sig(-2.0, spread=150.0, half_life=1.0); algo._tick()   # winning but z barely moved
    assert calls["close"] == [("LONG_SPREAD", 1)]
    assert "TIME-STOP" in algo._snap["status"]


# ── fresh-price entry guard (stale far-leg / phantom-z) ─────────────────────
def test_spread_divergence_guard_refuses_stale_entry():
    # Decision spread (signal) says −45, but the live leg prices read at order
    # time give −70 — a 25-pt gap (stale far leg). The guard must refuse.
    algo, state, calls = _make({"max_entry_spread_divergence": 8.0},
                               prices=(24210.0, 24280.0))   # live spread = −70
    state["sig"] = _sig(2.5, spread=-45.0)                  # decision spread −45
    algo._tick()
    assert calls["execute"] == []
    assert "stale signal" in algo.get_state()["status"]


def test_spread_divergence_guard_allows_within_threshold():
    algo, state, calls = _make({"max_entry_spread_divergence": 8.0},
                               prices=(24210.0, 24215.0))   # live spread = −5
    state["sig"] = _sig(2.5, spread=-8.0)                   # |(−5) − (−8)| = 3 ≤ 8
    algo._tick()
    assert calls["execute"] == [("SHORT_SPREAD", 1)]


def test_spread_divergence_guard_refuses_when_no_price():
    algo, state, calls = _make({"max_entry_spread_divergence": 8.0},
                               prices=(None, None))         # can't read → fail-safe refuse
    state["sig"] = _sig(2.5, spread=-8.0)
    algo._tick()
    assert calls["execute"] == []
    assert "stale signal" in algo.get_state()["status"]


def test_spread_divergence_guard_off_when_zero():
    algo, state, calls = _make({"max_entry_spread_divergence": 0.0},
                               prices=(24210.0, 24280.0))   # huge gap but guard disabled
    state["sig"] = _sig(2.5, spread=-45.0)
    algo._tick()
    assert calls["execute"] == [("SHORT_SPREAD", 1)]


def test_spread_divergence_guard_skipped_without_provider():
    algo, state, calls = _make({"max_entry_spread_divergence": 8.0})   # no prices_provider
    state["sig"] = _sig(2.5, spread=-45.0)
    algo._tick()
    assert calls["execute"] == [("SHORT_SPREAD", 1)]


# ── Tier A: gated reversion, z-reset, loss-streak ───────────────────────────
def test_reversion_gate_blocks_losing_take():
    algo, state, calls = _pnl_make({"reversion_require_profit": True})
    state["sig"] = _sig(-2.5, spread=100.0); algo._tick()     # enter LONG @100
    state["sig"] = _sig(0.1, spread=90.0); algo._tick()       # reverted but net −10
    assert calls["close"] == []                              # never book a losing take
    assert "holding" in algo.get_state()["status"]


def test_reversion_gate_allows_profit():
    algo, state, calls = _pnl_make({"reversion_require_profit": True})
    state["sig"] = _sig(-2.5, spread=100.0); algo._tick()
    state["sig"] = _sig(0.1, spread=115.0); algo._tick()      # reverted, net +15
    assert calls["close"] == [("LONG_SPREAD", 1)]
    assert "target" in algo._snap["status"]


def test_reversion_allowed_unit():
    algo, _, _ = _pnl_make()
    assert algo._reversion_allowed(-5.0, {}) is True                       # gate off
    on = {"reversion_require_profit": True}
    assert algo._reversion_allowed(None, on) is True                       # fail-open
    assert algo._reversion_allowed(5.0, on) is True
    assert algo._reversion_allowed(-1.0, on) is False
    assert algo._reversion_allowed(3.0, {**on, "reversion_gate_inr": 5.0}) is False


def test_z_reset_blocks_same_direction_after_stop():
    algo, state, calls = _make({"z_reset_after_stop": True, "cooldown": 0.0, "stop_cooldown": 0.0})
    state["sig"] = _sig(-2.5); algo._tick()                   # enter LONG
    state["sig"] = _sig(-4.5); algo._tick()                   # z-stop
    assert calls["close"] == [("LONG_SPREAD", 1)]
    assert algo._stop_block_dir == "LONG_SPREAD"
    calls["execute"].clear()
    state["sig"] = _sig(-2.5); algo._tick()                   # blocked (same dir, z outside band)
    assert calls["execute"] == []
    assert "z-reset" in algo.get_state()["status"]
    state["sig"] = _sig(0.1); algo._tick()                    # z re-enters band → clears
    assert algo._stop_block_dir is None
    state["sig"] = _sig(-2.5); algo._tick()                   # now allowed
    assert calls["execute"] == [("LONG_SPREAD", 1)]


def test_stop_cooldown_longer_than_normal():
    algo, state, calls = _make({"cooldown": 10.0, "stop_cooldown": 300.0})
    state["sig"] = _sig(-2.5); algo._tick()
    state["sig"] = _sig(-4.5); algo._tick()                   # z-stop
    assert algo._cooldown_until - algo._clock() > 250         # stop cooldown, not 10s


def test_loss_streak_reduces_size():
    algo, state, calls = _make({"loss_streak": 3, "loss_streak_reduce_at": 3,
                                "loss_streak_reduce_pct": 20.0, "lots": 10})
    state["sig"] = _sig(-2.5); algo._tick()
    assert calls["execute"] == [("LONG_SPREAD", 8)]           # 10 × (1−0.2)


def test_loss_streak_pauses_entries():
    algo, state, calls = _make({"loss_streak": 6, "loss_streak_pause_at": 6})
    state["sig"] = _sig(-2.5); algo._tick()
    assert calls["execute"] == []
    assert "paused" in algo.get_state()["status"]


# ── Tier A: regime / trend-day guard ────────────────────────────────────────
def test_regime_halt_blocks_entries_and_latches():
    algo, state, calls = _make({"regime_enabled": True, "regime_halt_on_trending": True})
    state["sig"] = _sig(-2.5, regime="TRENDING"); algo._tick()
    assert calls["execute"] == []
    assert "regime TRENDING" in algo.get_state()["status"]
    assert algo._regime_halt_day is not None
    # latched for the day: even a RANGE reading now still blocks
    state["sig"] = _sig(-2.5, regime="RANGE"); algo._tick()
    assert calls["execute"] == []


def test_regime_disabled_allows_entry():
    algo, state, calls = _make({"regime_enabled": False})
    state["sig"] = _sig(-2.5, regime="TRENDING"); algo._tick()
    assert calls["execute"] == [("LONG_SPREAD", 1)]     # guard off → normal entry


def test_trend_direction_filter_blocks_wrong_side():
    algo, state, calls = _make({"regime_enabled": True, "regime_halt_on_trending": False,
                                "regime_trend_direction_filter": True})
    # rising spread (slope>0) → SHORT-only; a LONG signal (z<0) is blocked
    state["sig"] = _sig(-2.5, regime="RANGE", slope=5.0); algo._tick()
    assert calls["execute"] == []
    assert "trend filter" in algo.get_state()["status"]
    # a SHORT signal (z>0) into a rising spread is allowed
    state["sig"] = _sig(2.5, regime="RANGE", slope=5.0); algo._tick()
    assert calls["execute"] == [("SHORT_SPREAD", 1)]


def test_live_net_pnl_includes_stt():
    algo, state, calls = _pnl_make({"stt_pct": 0.02, "lot_multiplier": 65.0})
    state["sig"] = _sig(-2.5, spread=0.0); algo._tick()          # enter LONG
    algo._pos["entry_leg_a"] = 24000.0                          # notional basis
    state["sig"] = _sig(-1.0, spread=10.0); algo._tick()        # +10 spread
    # gross = 10 × 1 × 65 = 650; STT = 2 × 0.0002 × 24000 × 65 = 624; net ≈ 26
    expected = 650.0 - 2 * 0.0002 * 24000.0 * 65.0
    assert abs(algo.get_state()["net_pnl"] - expected) < 0.01
