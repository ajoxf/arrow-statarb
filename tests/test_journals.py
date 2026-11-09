"""Trade journal and signal journal: the levels a position opened against,
the 30-day replay ("Derive") and its rules — derived rows are labelled,
replace each other, and never touch or overlap a live row."""

import pytest

from arrow_statarb.core.algo import ArrowAutoTrader
from arrow_statarb.core.signal_study import SignalStudy, replay_candles
from arrow_statarb.core.trade_log import TradeLog

MULT = 10.0


def _p(**over):
    p = {"entry_zscore": 2.0, "exit_zscore": 0.0, "stop_zscore": 4.0, "lots": 1,
         "tick_interval": 0.5, "cooldown": 0, "lot_multiplier": MULT,
         "enable_probability_filter": False, "brokerage_per_lot": 20.0,
         "slippage_per_lot": 5.0, "time_stop_half_lives": 3.0, "confirmation_ticks": 1,
         "entry_mode": "touch"}
    p.update(over)
    return p


# ── the levels a position opened against ─────────────────────────────────────

def test_a_short_has_its_stop_above_and_target_at_the_mean_in_z():
    algo = ArrowAutoTrader(signal_provider=dict, params_provider=_p, execute_fn=None, close_fn=None)
    pos = {"direction": "SHORT_SPREAD", "lots": 1, "entry_spread": 120.0,
           "entry_fill_spread": 120.0, "entry_leg_a": 9000.0, "entry_leg_b": 8880.0}
    lv = algo.entry_levels(_p(), pos, {"mean": 100.0, "std": 10.0})
    assert lv["target_kind"] == "z" and lv["target_spread"] == 100.0       # mean + 0·σ
    assert lv["stop_kind"] == "z" and lv["stop_spread"] == 140.0           # ABOVE: mean + 4σ
    assert lv["break_even"] < 120.0                                        # a short gains down


def test_a_long_has_its_stop_below():
    algo = ArrowAutoTrader(signal_provider=dict, params_provider=_p, execute_fn=None, close_fn=None)
    pos = {"direction": "LONG_SPREAD", "lots": 1, "entry_spread": 80.0,
           "entry_fill_spread": 80.0, "entry_leg_a": 9000.0, "entry_leg_b": 8920.0}
    lv = algo.entry_levels(_p(), pos, {"mean": 100.0, "std": 10.0})
    assert lv["stop_spread"] == 60.0 and lv["break_even"] > 80.0


def test_rupee_exits_win_over_the_z_levels_when_set():
    algo = ArrowAutoTrader(signal_provider=dict, params_provider=_p, execute_fn=None, close_fn=None)
    p = _p(profit_target_inr=200.0, dollar_stop_inr=300.0)
    pos = {"direction": "SHORT_SPREAD", "lots": 1, "entry_spread": 120.0,
           "entry_fill_spread": 120.0, "entry_leg_a": 9000.0, "entry_leg_b": 8880.0}
    lv = algo.entry_levels(p, pos, {"mean": 100.0, "std": 10.0})
    assert lv["target_kind"] == "₹" and lv["stop_kind"] == "₹"
    assert lv["target_spread"] < lv["break_even"] < 120.0 < lv["stop_spread"]


def test_control_a_manual_position_records_no_rupee_exits():
    algo = ArrowAutoTrader(signal_provider=dict, params_provider=_p, execute_fn=None, close_fn=None)
    p = _p(profit_target_inr=200.0, dollar_stop_inr=300.0)
    pos = {"direction": "SHORT_SPREAD", "lots": 1, "entry_spread": 120.0,
           "entry_fill_spread": 120.0, "entry_leg_a": 9000.0, "entry_leg_b": 8880.0}
    lv = algo.entry_levels(p, pos, {"mean": 100.0, "std": 10.0}, auto_exits=False)
    assert lv["target_kind"] == "z" and lv["stop_kind"] == "z"


def test_the_algo_journals_its_levels_on_entry_and_the_close_carries_them(tmp_path):
    log = TradeLog(path=tmp_path / "t.json")
    state = {"z": 2.5}
    sig = lambda: {"zscore": state["z"], "std": 10.0, "mean": 100.0, "ready": True,
                   "spread": 100.0 + 10 * state["z"], "leg_a": 9100.0, "leg_b": 9000.0,
                   "half_life": 0.0, "sample_interval_sec": 0.5, "regime_detail": {}}

    def execute(direction, lots, **kw):
        log.record(action="OPEN", direction=direction, lots=lots, spread=125.0,
                   dry_run=True, status="DRY-RUN", source="algo", lot_size=10)
        return {"success": True, "dry_run": True, "results": []}

    def close(direction, lots, **kw):
        log.record(action="CLOSE", direction=direction, lots=lots, spread=99.0,
                   dry_run=True, status="DRY-RUN", source="algo", lot_size=10, exit_reason="target")
        return {"success": True, "results": []}

    algo = ArrowAutoTrader(signal_provider=sig, params_provider=_p, execute_fn=execute,
                           close_fn=close, on_entered=lambda lv: log.annotate_open(**lv))
    algo._tick()
    state["z"] = -0.1
    algo._tick()
    trip = log.round_trips()["trips"][0]
    assert trip["stop_spread"] == 140.0 and trip["stop_kind"] == "z"
    assert trip["target_spread"] == 100.0 and trip["break_even"] is not None


def test_the_paper_algo_puts_its_levels_on_the_row():
    state = {"z": 2.5}
    sig = lambda: {"zscore": state["z"], "std": 10.0, "mean": 100.0, "ready": True,
                   "spread": 100.0 + 10 * state["z"], "leg_a": 9100.0, "leg_b": 9000.0,
                   "half_life": 0.0, "sample_interval_sec": 0.5, "regime_detail": {}}
    st = SignalStudy(None, sig, _p, pair_provider=lambda: "A − B")
    st.tick()
    (r,) = st.report("all")["rows"]
    assert r["pair"] == "A − B" and r["source"] == "live"
    assert r["stop_spread"] == 140.0 and r["target_spread"] == 100.0


# ── derive: replay over candles ──────────────────────────────────────────────

def _bars(spreads, t0=1_700_000_000.0, tf=900):
    return [(t0 + i * tf, 9000.0 + s, 9000.0) for i, s in enumerate(spreads)]


def test_replay_trades_on_the_candle_bands_and_labels_its_rows():
    calm = [0.5 * ((-1) ** i) for i in range(30)]
    bars = _bars(calm + [8.0, 8.5, 0.0, -0.3] + calm[:5])
    rows = replay_candles(bars, _p(), "15m", 900, 20, pair="A − B")
    taken = [r for r in rows if r["status"] in ("open", "closed")]
    assert taken and all(r["source"] == "replayed" for r in rows)
    r = taken[0]
    assert r["side"] == "SHORT" and r["entry_ts"] == bars[30][0] + 900   # at that candle's CLOSE
    assert r["status"] == "closed" and r["exit_reason"] == "target"


def test_replay_z_is_the_live_candle_band_formula():
    from arrow_statarb.core.candles import pine_ema, pine_stdev
    calm = [0.5 * ((-1) ** i) for i in range(30)]
    spreads = calm + [8.0]
    rows = replay_candles(_bars(spreads), _p(), "15m", 900, 20)
    r = [r for r in rows if r["status"] in ("open", "closed")][0]
    expect = (spreads[-1] - pine_ema(spreads, 20)) / pine_stdev(spreads, 20)
    assert r["entry_z"] == round(expect, 4)


def _live_study(t):
    sig = lambda: {"zscore": 0.0, "std": 1.0, "mean": 0.0, "ready": True, "spread": 0.0,
                   "leg_a": 1.0, "leg_b": 1.0, "half_life": 0.0, "sample_interval_sec": 0.5,
                   "regime_detail": {}}
    return SignalStudy(None, sig, _p, clock=lambda: t["now"])


def test_derived_rows_replace_each_other_and_never_touch_live_rows():
    t = {"now": 2_000_000_000.0}
    st = _live_study(t)
    live = st._new_row("SHORT", _p())                  # a live row at "now"
    old = [{"id": 1, "entry_ts": t["now"] - 5 * 86400, "status": "closed", "side": "SHORT"}]
    assert st.replace_derived(old) == 1
    new = [{"id": 1, "entry_ts": t["now"] - 3 * 86400, "status": "closed", "side": "LONG"},
           {"id": 2, "entry_ts": t["now"] - 2 * 86400, "status": "open", "side": "SHORT"}]
    assert st.replace_derived(new) == 2
    rows = st.report("all", now=t["now"])["rows"]
    assert [r["source"] for r in rows].count("replayed") == 2      # replaced, not added
    assert any(r["id"] == live["id"] and r["source"] == "live" for r in rows)
    assert "running" in [r["status"] for r in rows if r["source"] == "replayed"]


def test_derived_rows_never_overlap_the_live_record():
    t = {"now": 2_000_000_000.0}
    st = _live_study(t)                                  # the live record starts now
    rows = [{"entry_ts": t["now"] - 60, "status": "closed", "side": "SHORT"},
            {"entry_ts": t["now"] + 60, "status": "closed", "side": "SHORT"}]   # after: dropped
    assert st.replace_derived(rows) == 1


def test_report_filters_by_ist_day_and_pair():
    t = {"now": 2_000_000_000.0}
    st = _live_study(t)
    st.replace_derived([
        {"entry_ts": 1_999_900_000.0, "status": "closed", "side": "SHORT", "pair": "X − Y"},
        {"entry_ts": 1_999_000_000.0, "status": "closed", "side": "SHORT", "pair": "P − Q"}])
    import time as _t
    day = _t.strftime("%Y-%m-%d", _t.gmtime(1_999_900_000.0 + 5.5 * 3600))
    assert len(st.report("all", now=t["now"], day=day)["rows"]) == 1
    assert [r["pair"] for r in st.report("all", now=t["now"], pair="P − Q")["rows"]] == ["P − Q"]
    assert st.report("all", now=t["now"])["pairs"] == ["P − Q", "X − Y"]


def test_derive_refuses_tick_bands(tmp_path):
    import arrow_statarb.web.app as appmod
    from arrow_statarb.config.config import Config
    (tmp_path / "s.yaml").write_text("mode: dry_run\nbroker:\n  name: arrow\n")
    appmod.TRADES_FILE = tmp_path / "trades.json"
    app, _ = appmod.create_app(Config(tmp_path / "s.yaml"))
    app.extensions["arrow"]["signal"].stop()
    r = app.test_client().post("/api/analysis/derive", json={}).get_json()
    assert r["success"] is False and "Candles" in r["error"]


def test_replay_confirms_on_one_candle_close_not_on_n_candles():
    calm = [0.5 * ((-1) ** i) for i in range(30)]
    bars = _bars(calm + [8.0, 0.0, -0.3] + calm[:5])           # one candle beyond, then back
    rows = replay_candles(bars, _p(confirmation_ticks=3), "15m", 900, 20)
    assert [r for r in rows if r["status"] in ("open", "closed")]
