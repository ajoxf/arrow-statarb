"""SQLite analysis persistence (ported, INR) — additive layer."""

from arrow_statarb.core.database import Database
from arrow_statarb.core import trade_review
from arrow_statarb.core.exits import BUY_BASIS


def _db(tmp_path):
    return Database(tmp_path / "analysis.db")


def test_init_is_idempotent(tmp_path):
    db = _db(tmp_path)
    db.init(); db.init()                       # re-running must not error
    assert db.recent_reviews() == []


def test_trade_review_roundtrip_from_builder(tmp_path):
    db = _db(tmp_path)
    lvl = {"be": 99.0, "ex": 99.5, "tp": 102.5, "sl": 90.0}
    slip = {"crossing_spread": 0.2, "crossing_inr": 10.0,
            "slippage_spread": 0.1, "slippage_inr": 5.0}
    rec = trade_review.build(
        entry_time=1.0, exit_time=2.0, asset="NIFTY", direction=BUY_BASIS,
        entry_z=-3.0, exit_z=0.1, entry_sigma=1.0, capture_target_inr=500.0,
        cost_est_inr=100.0, realized_pnl=350.0, exit_reason="TAKE_PROFIT",
        peak_pnl=400.0, peak_min=12.0, spread_levels=lvl, notional_inr=1e6,
        entry_slip=slip)
    db.log_trade_review(rec, position_id="p1", opened="t0", closed="t1")
    rows = db.recent_reviews()
    assert len(rows) == 1
    r = rows[0]
    assert r["position_id"] == "p1" and r["asset"] == "NIFTY"
    assert r["outcome"] == "TARGET_HIT" and r["realized_pnl"] == 350.0
    assert r["tp_spread"] == 102.5 and r["entry_cross_inr"] == 10.0
    # insert-or-replace on the same id doesn't duplicate
    db.log_trade_review(rec, position_id="p1", closed="t2")
    assert len(db.recent_reviews()) == 1


def test_sd_touch_and_shadow(tmp_path):
    db = _db(tmp_path)
    db.log_sd_touch("2026-08-07T10:00", "NIFTY", 2, "up", 2.1, 105.0)
    db.log_sd_touch("2026-08-07T10:01", "CRUDEOIL", -3, "down", -3.2, -80.0)
    assert len(db.sd_touch_rows()) == 2
    assert len(db.sd_touch_rows(asset="NIFTY")) == 1
    db.log_shadow({"position_id": "s1", "asset": "NIFTY", "verdict": "REVERTED",
                   "what_if_net": 220.0, "completed": "t1"})
    assert db.recent_shadows()[0]["verdict"] == "REVERTED"


def test_market_data_warm_start_series_isolation(tmp_path):
    db = _db(tmp_path)
    db.log_market_data(100.0, "NIFTY", 10.0, "A|B|1.0")
    db.log_market_data(101.0, "NIFTY", 11.0, "A|B|1.0")
    db.log_market_data(102.0, "NIFTY", 99.0, "X|Y|2.0")   # different series
    got = db.recent_spreads("NIFTY", "A|B|1.0", since=0.0)
    assert got == [(100.0, 10.0), (101.0, 11.0)]          # other series excluded
    # since-cutoff filters
    assert db.recent_spreads("NIFTY", "A|B|1.0", since=101.0) == [(101.0, 11.0)]


def test_position_state_save_load_clear(tmp_path):
    db = _db(tmp_path)
    db.save_position_state("p1", "NIFTY", '{"lots": 1}', "t0")
    assert len(db.load_open_position_states()) == 1
    db.clear_position_state("p1")
    assert db.load_open_position_states() == []
