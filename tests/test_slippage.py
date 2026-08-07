"""Slippage decomposition (crossing vs slippage) + trade-review record."""

from arrow_statarb.core import slippage as slp
from arrow_statarb.core import trade_review
from arrow_statarb.core.exits import BUY_BASIS, SELL_BASIS


def test_touch_and_crossing_sign_positive_is_cost():
    # Buying: touch is the ask; crossing = ask - mid > 0 (a cost).
    r = slp.leg_report("BUY", bid=99.0, ask=101.0, fill=None)
    assert r["mid"] == 100.0 and r["quote"] == 101.0
    assert r["crossing"] == 1.0                         # paid the half-spread
    # Selling: touch is the bid; crossing = mid - bid > 0.
    r = slp.leg_report("SELL", bid=99.0, ask=101.0, fill=None)
    assert r["quote"] == 99.0 and r["crossing"] == 1.0


def test_slippage_quote_to_fill_and_improvement():
    # Bought at 101.5 vs quoted 101 → +0.5 slippage (cost).
    r = slp.leg_report("BUY", 99.0, 101.0, fill=101.5)
    assert r["slippage"] == 0.5 and r["total"] == 1.5
    # Bought at 100.8 (inside the ask) → negative slippage = price improvement.
    r = slp.leg_report("BUY", 99.0, 101.0, fill=100.8)
    assert round(r["slippage"], 2) == -0.2


def test_selling_the_spread_flips_on_close():
    assert slp.selling_the_spread(SELL_BASIS, closing=False) is True
    assert slp.selling_the_spread(SELL_BASIS, closing=True) is False
    assert slp.selling_the_spread(BUY_BASIS, closing=False) is False
    # Arrow alias: SHORT_SPREAD == SELL_BASIS
    assert slp.selling_the_spread("SHORT_SPREAD", closing=False) is True


def test_pair_report_converts_spread_to_inr_via_k():
    ref = {"spot_bid": 99.0, "spot_ask": 101.0,
           "futures_bid": 199.0, "futures_ask": 201.0}
    # BUY_BASIS open: long futures (BUY), short spot (SELL). k = L_B*C_B = 50.
    rep = slp.build(BUY_BASIS, closing=False, beta=1.0, k=50.0,
                    spot_side="SELL", futures_side="BUY",
                    reference=ref, spot_fill=99.0, futures_fill=201.5)
    # decision spread = mid_fut - mid_spot = 200 - 100 = 100
    assert rep["decision_spread"] == 100.0
    assert rep["crossing_inr"] is not None
    # slippage_inr = slippage_spread * k
    assert abs(rep["slippage_inr"] - rep["slippage_spread"] * 50.0) < 1e-9


def test_build_returns_none_without_snapshot():
    assert slp.build(BUY_BASIS, False, 1.0, 50.0, "SELL", "BUY",
                     reference=None, spot_fill=1, futures_fill=1) is None


def test_trade_review_record_shape_and_outcome():
    lvl = {"be": 99.0, "ex": 99.5, "tp": 102.5, "sl": 90.0}
    entry_slip = {"crossing_spread": 0.2, "crossing_inr": 10.0,
                  "slippage_spread": 0.1, "slippage_inr": 5.0}
    rec = trade_review.build(
        entry_time=1.0, exit_time=2.0, asset="NIFTY", direction=BUY_BASIS,
        entry_z=-3.0, exit_z=0.1, entry_sigma=1.0, capture_target_inr=500.0,
        cost_est_inr=100.0, realized_pnl=350.0, exit_reason="TAKE_PROFIT",
        peak_pnl=400.0, peak_min=12.0, spread_levels=lvl, notional_inr=1e6,
        entry_slip=entry_slip)
    assert rec["outcome"] == "TARGET_HIT"
    assert rec["tp_spread"] == 102.5 and rec["be_spread"] == 99.0
    assert rec["entry_crossing_inr"] == 10.0
    assert rec["exit_slippage_inr"] is None            # no exit slip supplied

    # A stop with z NOT reverted → STOPPED_IN_TREND
    rec2 = trade_review.build(
        entry_time=1.0, exit_time=2.0, asset="NIFTY", direction=SELL_BASIS,
        entry_z=3.0, exit_z=4.6, entry_sigma=1.0, capture_target_inr=0.0,
        cost_est_inr=100.0, realized_pnl=-300.0, exit_reason="DOLLAR_STOP")
    assert rec2["outcome"] == "STOPPED_IN_TREND"
