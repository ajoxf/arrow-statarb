"""Per-segment Indian cost model (core/costs.py) — the STT/CTT + txn + GST +
SEBI + stamp + brokerage stack that replaces the crypto maker/taker model."""

from arrow_statarb.core import costs


def test_resolve_defaults_and_overrides():
    etf = costs.resolve_segment_costs("etf")
    assert etf["stt_sell_pct"] == 0.001                     # ETF delivery STT
    fut = costs.resolve_segment_costs("nse_fo")
    assert fut["stt_sell_pct"] == 0.02                      # NSE futures STT
    mcx = costs.resolve_segment_costs("mcx_fo")
    assert mcx["stt_sell_pct"] == 0.01                      # MCX CTT
    # override one rate, keep the rest
    ov = costs.resolve_segment_costs("nse_fo", {"nse_fo": {"stt_sell_pct": 0.0125}})
    assert ov["stt_sell_pct"] == 0.0125
    assert ov["txn_pct"] == 0.0019
    # unknown key → all-zero, no phantom cost
    assert costs.resolve_segment_costs("bogus") == {}


def test_leg_round_trip_cost_components():
    # one leg, notional 100000, brokerage 40 (2 orders), futures rates.
    r = costs.resolve_segment_costs("nse_fo")
    c = costs.leg_round_trip_cost(r, 100000.0, 40.0)
    txn = 0.0019 / 100 * 100000 * 2      # 3.8
    sebi = 0.0001 / 100 * 100000 * 2     # 0.2
    stamp = 0.002 / 100 * 100000         # 2.0
    stt = 0.02 / 100 * 100000            # 20.0
    gst = 0.18 * (40.0 + txn + sebi)     # on charges only
    assert abs(c - (40.0 + txn + sebi + stamp + stt + gst)) < 1e-6


def test_etf_vs_future_asymmetric_stt():
    # A NIFTYBEES(ETF)/NIFTY(future) round trip: the ETF leg's STT (0.001%) is
    # ~20× smaller than the future leg's (0.02%) — the whole point of the pair.
    ra = costs.resolve_segment_costs("etf")
    rb = costs.resolve_segment_costs("nse_fo")
    n = 500000.0
    total = costs.round_trip_cost(ra, n, rb, n, brokerage_per_lot=20, lots=1,
                                  slippage_per_lot=5)
    # STT contribution: ETF 0.001% + future 0.02% on n each
    etf_stt = 0.001 / 100 * n
    fut_stt = 0.02 / 100 * n
    assert fut_stt == 20 * etf_stt
    # sanity: total is positive and dominated by the future-side STT
    assert total > fut_stt


def test_round_trip_matches_two_legs_plus_slippage():
    ra = costs.resolve_segment_costs("nse_fo")
    rb = costs.resolve_segment_costs("nse_fo")
    n = 200000.0
    brk_leg = 20 * 1 * 2                  # brokerage per leg (2 orders)
    slip = 5 * 1 * 4                      # 2 legs × 2 sides
    expected = (costs.leg_round_trip_cost(ra, n, brk_leg)
                + costs.leg_round_trip_cost(rb, n, brk_leg) + slip)
    got = costs.round_trip_cost(ra, n, rb, n, brokerage_per_lot=20, lots=1,
                                slippage_per_lot=5)
    assert abs(got - expected) < 1e-6
