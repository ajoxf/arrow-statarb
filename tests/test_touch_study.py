"""Every σ touch played out as the trade it could have been, on the algo's own
exit code — the Timeline's Instrument / Side / Edge / levels / legs / status /
why / net columns."""

import pytest

from arrow_statarb.core.touch_study import TouchStudy, event_key

PAIR = "SILVERM27NOV26F|SILVERM26FEB27F"
P = {"lots": 1, "lot_multiplier": 5.0, "entry_zscore": 2.5, "exit_zscore": 0.0,
     "stop_zscore": 5.5, "z_stop_exit_enabled": False, "brokerage_per_lot": 20.0,
     "slippage_per_lot": 5.0, "stt_pct": 0.02, "other_cost_pct": 0.005,
     "min_edge_multiple": 2.0, "capital_at_risk_inr": 50000.0, "stop_capital_pct": 1.5,
     "tp_capital_pct": 0.5, "cost_floor_mult": 1.5, "min_hold_sec": 0, "cooldown": 0}


def _sig(spread, mean=3130.0, std=50.0):
    """A book around ``spread``: sell spread = spread − 2.5, buy = spread + 2.5."""
    a = 93000.0
    b = a - spread
    return {"zscore": (spread - mean) / std, "std": std, "mean": mean, "ready": True,
            "spread": spread, "leg_a": a, "leg_b": b,
            "bid_a": a - 1, "ask_a": a + 1, "bid_b": b - 1.5, "ask_b": b + 1.5,
            "sell_spread": spread - 2.5, "buy_spread": spread + 2.5,
            "z_sell": (spread - 2.5 - mean) / std, "z_buy": (spread + 2.5 - mean) / std,
            "band_source": "candles", "band_timeframe": "15m", "sample_interval_sec": 0.5}


def _touch(ts, spread, typ="touch_2_up", side="sell", band=2.0, mean=3130.0, std=50.0):
    a = 93000.0
    b = a - spread
    return {"ts": ts, "type": typ, "z": round((spread - mean) / std, 2),
            "z_exact": (spread - mean) / std, "band": band, "side": side, "entry": False,
            "spread": spread, "mean": mean, "std": std, "series": PAIR,
            "mid_a": a, "mid_b": b, "bid_a": a - 1, "ask_a": a + 1,
            "bid_b": b - 1.5, "ask_b": b + 1.5}


class _World:
    def __init__(self, tmp_path):
        self.t = 1_000_000.0
        self.sig = _sig(3130.0)
        self.study = TouchStudy(tmp_path / "touch.json", lambda: self.sig, lambda: dict(P),
                                clock=lambda: self.t)

    def step(self, spread, dt=60.0, **kw):
        self.t += dt
        self.sig = _sig(spread, **kw)
        self.study.tick()


def test_a_touch_opens_a_paper_trade_with_every_column(tmp_path):
    w = _World(tmp_path)
    ev = _touch(w.t, 3230.0)
    w.study.on_event(ev)
    w.sig = _sig(3232.5)
    w.study.tick()
    r = w.study.rows_for([event_key(ev)])[event_key(ev)]
    assert r["side"] == "H→L" and r["direction"] == "SHORT_SPREAD" and r["instrument"] == PAIR
    assert r["status"] == "open" and r["entry_spread"] == 3230.0 and r["entry_z"] == 2.0
    assert r["entry_std"] == 50.0
    # SHORT the spread = SELL A at its bid, BUY B at its ask
    assert r["leg_a_in"] == {"side": "sell", "price": 92999.0}
    assert r["leg_b_in"] == {"side": "buy", "price": round(93000.0 - 3230.0 + 1.5, 2)}
    e = r["edge"]
    assert e["min_mult"] == 2.0 and e["needed"] == pytest.approx(2 * e["cost"], abs=0.02)
    assert e["capture"] == pytest.approx(0.5 * 2.0 * 50.0 * 1 * 5.0, abs=0.01)   # ₹250
    assert e["ok"] is (e["capture"] >= e["needed"])
    # a SHORT: break-even below entry, take profit below it, stop above
    assert r["take_profit"] < r["break_even"] < r["entry_spread"] < r["stop_loss"]


def test_a_stretch_that_keeps_going_is_stopped_out_with_the_algos_words(tmp_path):
    w = _World(tmp_path)
    ev = _touch(w.t, 3230.0)
    w.study.on_event(ev)
    w.step(3240.0)
    for s in range(3260, 3500, 20):
        w.step(float(s))
    r = w.study.rows_for([event_key(ev)])[event_key(ev)]
    assert r["status"] == "stopped out" and r["exit_reason"] == "dollar_stop"
    assert r["why"].startswith("STOP PRICE (net ₹-") and "≤ −₹750" in r["why"]
    assert r["net"] <= -750.0
    assert r["leg_a_out"]["side"] == "buy" and r["leg_b_out"]["side"] == "sell"
    assert r["exit_std"] == 50.0 and r["exit_spread"] is not None


def test_a_dip_is_a_long_and_comes_back_for_a_profit(tmp_path):
    w = _World(tmp_path)
    ev = _touch(w.t, 3010.0, typ="touch_2_down", side="buy", band=-2.0)
    w.study.on_event(ev)
    w.step(3008.0)
    for s in (3050, 3100, 3140, 3200, 3260):
        w.step(float(s), std=55.0)
    r = w.study.rows_for([event_key(ev)])[event_key(ev)]
    assert r["side"] == "L→H" and r["leg_a_in"]["side"] == "buy"
    assert r["status"] in ("take profit", "back to mean") and r["net"] > 0
    assert r["exit_std"] == 55.0 and r["entry_std"] == 50.0        # σ in → out


def test_reversions_open_nothing_and_rows_survive_a_restart(tmp_path):
    w = _World(tmp_path)
    w.study.on_event({**_touch(w.t, 3130.0), "type": "reversion"})
    ev = _touch(w.t, 3230.0)
    w.study.on_event(ev)
    w.step(3232.0)
    assert len(w.study._rows) == 1
    again = TouchStudy(tmp_path / "touch.json", lambda: w.sig, lambda: dict(P), clock=lambda: w.t)
    r = again.rows_for([event_key(ev)])[event_key(ev)]
    assert r["status"] == "interrupted" and "restarted" in r["why"]


def test_the_timeline_api_carries_each_touchs_trade(tmp_path):
    from tests.test_web import _app
    app, _ = _app(tmp_path)
    eng = app.extensions["arrow"]["signal"]
    ev = _touch(1_000.0, 3230.0)
    eng._exc_events.append(ev)
    out = app.test_client().get("/api/signal/excursions?n=50").get_json()
    assert out["events"][0]["trade"] is None          # not played out yet
