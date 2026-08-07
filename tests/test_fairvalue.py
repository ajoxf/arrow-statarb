"""Cost-of-carry fair value (display-only) — ported from the W3 basis system."""

import math
from datetime import datetime

from arrow_statarb.core import fairvalue as fv


NOW = datetime(2026, 8, 7)


def test_spot_future_carry():
    # NSE cash-vs-future: fair future = spot * exp(r * years); fair spread =
    # fair_future - beta*spot.
    cfg = {"pair_type": "SPOT_FUTURE", "risk_free_rate": 0.0425,
           "futures_expiry": "2026-11-25"}
    val, detail = fv.fair_spread(cfg, spot_price=100.0, futures_price=101.0,
                                 hedge_ratio=1.0, now=NOW)
    years = fv.years_until("2026-11-25", NOW)
    expected = 100.0 * math.exp(0.0425 * years) - 100.0
    assert abs(val - expected) < 1e-9
    assert "carry" in detail


def test_future_future_calendar_carry():
    # Calendar: fair far = near * exp(r * gap_between_expiries).
    cfg = {"pair_type": "FUTURE_FUTURE", "risk_free_rate": 0.05,
           "spot_expiry": "2026-08-19", "futures_expiry": "2026-09-19"}
    val, _ = fv.fair_spread(cfg, spot_price=6000.0, futures_price=6030.0,
                            hedge_ratio=1.0, now=NOW)
    near = fv.years_until("2026-08-19", NOW)
    far = fv.years_until("2026-09-19", NOW)
    expected = 6000.0 * math.exp(0.05 * (far - near)) - 6000.0
    assert abs(val - expected) < 1e-6


def test_related_has_no_fair_value():
    cfg = {"pair_type": "RELATED", "risk_free_rate": 0.05,
           "futures_expiry": "2026-11-25"}
    val, detail = fv.fair_spread(cfg, 100.0, 101.0, now=NOW)
    assert val is None and "no arbitrage" in detail


def test_missing_rate_or_expiry_returns_none_honestly():
    assert fv.fair_spread({"pair_type": "SPOT_FUTURE"}, 100, 101, now=NOW)[0] is None
    assert fv.fair_spread({"pair_type": "SPOT_FUTURE", "risk_free_rate": 0.04},
                          100, 101, now=NOW)[0] is None            # no expiry
    # passed expiry → None
    assert fv.years_until("2020-01-01", NOW) is None


def test_fair_value_block_shape():
    cfg = {"pair_type": "SPOT_FUTURE", "risk_free_rate": 0.0425,
           "futures_expiry": "2026-11-25"}
    blk = fv.fair_value_block(cfg, 100.0, 101.0, spread=1.0, now=NOW)
    assert blk["pair_type"] == "SPOT_FUTURE"
    assert blk["fair_value"] is not None
    assert blk["fair_gap"] == 1.0 - blk["fair_value"]
