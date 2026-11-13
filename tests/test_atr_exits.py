"""ATR-sized stops and targets (Mirror-MT5's): a multiple of the spread's
average true range — close to close, Wilder's smoothing, on the algo's own
candles — measured from break-even and FROZEN at entry. No ATR, no level."""

import pytest

from arrow_statarb.core.algo import ArrowAutoTrader
from arrow_statarb.core.candles import close_atr

MULT = 10.0


def _p(**over):
    p = {"entry_zscore": 2.0, "exit_zscore": 0.0, "stop_zscore": 99.0, "lots": 1,
         "tick_interval": 0.5, "cooldown": 0, "lot_multiplier": MULT,
         "enable_probability_filter": False, "brokerage_per_lot": 20.0,
         "slippage_per_lot": 5.0, "time_stop_half_lives": 0.0, "confirmation_ticks": 1,
         "entry_mode": "touch", "stop_mode": "atr", "target_mode": "atr",
         "atr_stop_mult": 2.0, "atr_target_mult": 1.5, "atr_now": 3.0}
    p.update(over)
    return p


def _algo(params):
    return ArrowAutoTrader(signal_provider=dict, params_provider=lambda: params,
                           execute_fn=None, close_fn=None)


SHORT = {"direction": "SHORT_SPREAD", "lots": 1, "entry_spread": 120.0,
         "entry_fill_spread": 120.0, "entry_leg_a": 9000.0, "entry_leg_b": 8880.0}


# ── the ATR ──────────────────────────────────────────────────────────────────

def test_atr_is_wilders_average_of_the_close_to_close_moves():
    closes = [100, 102, 101, 104, 104, 103]                 # moves 2 1 3 0 1
    assert close_atr(closes, 3) == pytest.approx(((2 + 1 + 3) / 3 * 2 + 0) / 3 * 2 / 3 + 1 / 3)
    assert close_atr(closes, 5) == pytest.approx((2 + 1 + 3 + 0 + 1) / 5)


def test_too_few_candles_is_no_atr_not_zero():
    assert close_atr([100, 101, 102], 14) is None
    assert close_atr([], 14) is None


# ── the levels ───────────────────────────────────────────────────────────────

def test_a_short_has_its_atr_stop_above_and_target_below_break_even():
    p = _p()
    a = _algo(p)
    lv = a.entry_levels(p, dict(SHORT, entry_atr=3.0), {"mean": 100.0, "std": 10.0})
    be = lv["break_even"]
    assert lv["stop_kind"] == "ATR" and lv["stop_spread"] == pytest.approx(be + 2.0 * 3.0, abs=0.02)
    assert lv["target_kind"] == "ATR" and lv["target_spread"] == pytest.approx(be - 1.5 * 3.0, abs=0.02)
    assert lv["entry_atr"] == 3.0


def test_a_long_mirrors_it():
    p = _p()
    long = dict(SHORT, direction="LONG_SPREAD", entry_spread=80.0, entry_fill_spread=80.0, entry_atr=3.0)
    lv = _algo(p).entry_levels(p, long, {"mean": 100.0, "std": 10.0})
    be = lv["break_even"]
    assert lv["stop_spread"] == pytest.approx(be - 6.0, abs=0.02)
    assert lv["target_spread"] == pytest.approx(be + 4.5, abs=0.02)


def test_the_atr_is_frozen_at_entry():
    p = _p(atr_now=9.0)                                     # the market is busier NOW
    lv = _algo(p).entry_levels(p, dict(SHORT, entry_atr=3.0), {"mean": 100.0, "std": 10.0})
    assert lv["stop_spread"] - lv["break_even"] == pytest.approx(6.0, abs=0.02)   # 2 × the ENTRY ATR


def test_no_atr_is_no_atr_level():
    p = _p(atr_now=None, profit_target_inr=500.0, dollar_stop_inr=300.0)
    lv = _algo(p).entry_levels(p, dict(SHORT, entry_atr=None), {"mean": 100.0, "std": 10.0})
    assert lv["stop_kind"] != "ATR" and lv["target_kind"] != "ATR"
    assert lv["stop_kind"] != "₹"                           # nor the ₹ stop: ATR mode has no stop on nothing


def test_control_rupee_mode_ignores_the_atr():
    p = _p(stop_mode="inr", target_mode="inr", profit_target_inr=200.0, dollar_stop_inr=300.0)
    lv = _algo(p).entry_levels(p, dict(SHORT, entry_atr=3.0), {"mean": 100.0, "std": 10.0})
    assert lv["stop_kind"] == "₹" and lv["target_kind"] == "₹"
    assert lv["stop_spread"] - lv["break_even"] == pytest.approx(300.0 / MULT, abs=0.02)


def test_the_target_lands_exactly_after_tax():
    p = _p(capital_gains_pct=20.0)
    lv = _algo(p).entry_levels(p, dict(SHORT, entry_atr=3.0), {"mean": 100.0, "std": 10.0})
    assert lv["break_even"] - lv["target_spread"] == pytest.approx(4.5, abs=0.02)


# ── the live engine: frozen at entry, and the stop fires there ───────────────

def _live(params, start_z=2.5):
    state = {"z": start_z, "spread": 120.0}
    sig = lambda: {"zscore": state["z"], "std": 10.0, "mean": 100.0, "ready": True,
                   "spread": state["spread"], "leg_a": 9000.0 + state["spread"], "leg_b": 9000.0,
                   "half_life": 0.0, "sample_interval_sec": 0.5, "regime_detail": {}}
    calls = {"open": [], "close": []}
    algo = ArrowAutoTrader(
        signal_provider=sig, params_provider=lambda: params,
        execute_fn=lambda d, n, **kw: calls["open"].append(d) or {"success": True, "dry_run": True, "results": [],
                                                                   "fill_spread": state["spread"]},
        close_fn=lambda d, n, reason="", **kw: calls["close"].append(reason) or {"success": True, "results": []})
    return algo, state, calls


def test_the_live_position_freezes_the_atr_and_stops_at_it():
    p = _p(target_mode="inr")
    algo, state, calls = _live(p)
    algo._tick()
    assert calls["open"] == ["SHORT_SPREAD"] and algo._pos["entry_atr"] == 3.0
    p["atr_now"] = 50.0                                     # later ATR must not move the stop
    be = algo._spread_levels(p, 0.0, 0.0, pos=algo._pos)["break_even"]
    state.update(z=2.6, spread=be + 2.0 * 3.0 + 0.5)        # beyond the frozen 2 × ATR stop
    algo._tick()
    assert calls["close"] == ["dollar_stop"]


def test_control_inside_the_atr_stop_it_holds():
    p = _p(target_mode="inr")
    algo, state, calls = _live(p)
    algo._tick()
    be = algo._spread_levels(p, 0.0, 0.0, pos=algo._pos)["break_even"]
    state.update(z=2.6, spread=be + 2.0 * 3.0 - 1.0)        # short of the stop
    algo._tick()
    assert calls["close"] == []


# ── settings and the journal ─────────────────────────────────────────────────

def test_the_settings_round_trip_and_refuse_nonsense(tmp_path):
    from tests.test_web import _app
    app, _ = _app(tmp_path)
    c = app.test_client()
    c.post("/api/settings", json={"exits": {"stop_mode": "atr", "target_mode": "ATR", "atr_period": 20,
                                            "atr_stop_mult": 2.5, "atr_target_mult": 1.2}})
    x = c.get("/api/settings").get_json()["exits"]
    assert (x["stop_mode"], x["target_mode"], x["atr_period"], x["atr_stop_mult"], x["atr_target_mult"]) == \
        ("atr", "atr", 20, 2.5, 1.2)
    c.post("/api/settings", json={"exits": {"stop_mode": "bogus", "atr_period": 1, "atr_stop_mult": -1}})
    x = c.get("/api/settings").get_json()["exits"]
    assert x["stop_mode"] == "atr" and x["atr_period"] == 2 and x["atr_stop_mult"] == 2.5


def test_the_entry_atr_is_journaled_and_survives_a_restart(tmp_path):
    from arrow_statarb.core.trade_log import TradeLog
    log = TradeLog(path=tmp_path / "t.json")
    log.record(action="OPEN", direction="SHORT_SPREAD", lots=1, spread=120.0, dry_run=True,
               status="DRY-RUN", source="algo", lot_size=10)
    log.annotate_open(entry_atr=3.0, break_even=119.0)
    reopened = TradeLog(path=tmp_path / "t.json")
    op = reopened.open_position()
    assert op["entry_atr"] == 3.0
    algo = _algo(_p(atr_now=40.0))
    algo.restore_position(dict(op, source="algo"))
    assert algo._pos["entry_atr"] == 3.0                    # the restart keeps the ENTRY's ATR, not today's
