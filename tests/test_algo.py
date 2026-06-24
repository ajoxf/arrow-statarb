"""Auto-trader lifecycle: entry → hold → exit/stop, EV gate, time-stop, dry-run.

The algo is driven directly through ``_tick`` with a controllable signal dict so
the z-score lifecycle is deterministic (no threads, no network)."""

import time

from arrow_statarb.core.algo import ArrowAutoTrader


def _make(params_over=None):
    state = {"sig": None}
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
    )
    return algo, state, calls


def _sig(z, ready=True, half_life=0.0, std=1.0):
    return {"zscore": z, "std": std, "ready": ready, "leg_a": 110.0, "leg_b": 10.0,
            "spread": 100.0, "mean": 100.0, "samples": 300, "half_life": half_life,
            "sample_interval_sec": 0.5, "entry_zscore": 2.0, "exit_zscore": 0.0,
            "stop_zscore": 4.0, "min_signal_minutes": 1.0, "span_minutes": 5.0}


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
