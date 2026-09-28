"""Drawdown + adverse-excursion (ported, INR)."""

from arrow_statarb.core import performance as perf


def _row(net, peak=None, trough=None, z=None, notional=None, peak_min=None):
    return {"net_pnl": net, "peak_pnl": peak, "trough_pnl": trough,
            "entry_zscore": z, "notional": notional, "peak_min": peak_min}


def test_drawdown_from_newest_first_rows():
    # equity path (oldest→newest): +100, -60, +20  →  peak 100, trough 40
    rows_newest_first = [_row(20), _row(-60), _row(100)]
    dd = perf.drawdown_block(rows_newest_first, newest_first=True)
    assert dd["peak_equity_inr"] == 100.0
    assert dd["max_inr"] == 60.0                 # 100 → 40 drawdown
    assert dd["current_inr"] == 40.0             # ended 60 below the peak
    assert dd["max_pct"] == 60.0
    assert dd["trades"] == 3


def test_drawdown_empty_is_zero():
    dd = perf.drawdown_block([])
    assert dd["max_inr"] == 0.0 and dd["peak_equity_inr"] == 0.0


def test_excursion_row_mae_mfe_and_type():
    # winning short trade (z>0): MAE from trough, MFE from peak
    r = _row(350.0, peak=400.0, trough=-120.0, z=3.0, notional=1_000_000,
             peak_min=12.0)
    row = perf.excursion_row(r)
    assert row["position_type"] == "SHORT"
    assert row["mae_inr"] == 120.0 and row["mfe_inr"] == 400.0
    assert row["pnl_inr"] == 350.0
    assert row["utilization_pct"] == 87.5        # 350/400
    assert row["mae_pct"] == 0.012               # 100*120/1e6
    assert row["hit_be"] is True


def test_excursion_rows_filters_to_trades_with_extremes():
    rows = [_row(10, peak=20, trough=-5, z=-2), _row(5)]   # 2nd has no extremes
    out = perf.excursion_rows(rows)
    assert len(out) == 1
    assert out[0]["position_type"] == "LONG"     # z<0
