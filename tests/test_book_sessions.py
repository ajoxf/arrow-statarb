"""Top-of-book from the FULL-mode stream, and exchange session hours."""

from datetime import date, datetime
from types import SimpleNamespace

from arrow_statarb.core import sessions
from arrow_statarb.core.executor import SpreadExecutor


def _tick(token, ltp, bids=(), asks=()):
    lv = lambda rows: [{"price": p, "quantity": q, "orders": 1} for p, q in rows]
    return SimpleNamespace(token=token, ltp=ltp, bids=lv(bids), asks=lv(asks))


def test_full_tick_gives_best_bid_and_ask_in_rupees(arrow_broker):
    arrow_broker._sym_token = {"CRUDEOIL19OCT26F": 7}
    # paise; empty levels carry price 0; level order does not matter
    arrow_broker._ingest_tick(_tick(7, 911000, bids=[(910900, 5), (910800, 2), (0, 0)],
                                    asks=[(911200, 3), (911100, 4), (0, 0)]))
    bk = arrow_broker.get_streamed_book(["CRUDEOIL19OCT26F"])["CRUDEOIL19OCT26F"]
    assert (bk["bid"], bk["ask"], bk["ltp"]) == (9109.0, 9111.0, 9110.0)
    assert arrow_broker.get_streamed_ltp(["CRUDEOIL19OCT26F"]) == {"CRUDEOIL19OCT26F": 9110.0}


def test_ltp_only_tick_keeps_the_book_and_empty_side_is_None(arrow_broker):
    arrow_broker._sym_token = {"X": 1}
    arrow_broker._ingest_tick(_tick(1, 10000, bids=[(9990, 1)], asks=[(10010, 1)]))
    arrow_broker._ingest_tick(_tick(1, 10005))                 # no depth
    bk = arrow_broker.get_streamed_book(["X"])["X"]
    assert (bk["bid"], bk["ask"], bk["ltp"]) == (99.9, 100.1, 100.05)
    arrow_broker._ingest_tick(_tick(1, 10005, bids=[(9990, 1)], asks=[(0, 0)]))
    assert arrow_broker.get_streamed_book(["X"])["X"]["ask"] is None   # never the LTP


def test_mcx_close_follows_us_daylight_time():
    assert sessions.segment_session("mcx_fo", date(2026, 9, 28)) == (540, 23 * 60 + 30)
    assert sessions.segment_session("mcx_fo", date(2026, 12, 1)) == (540, 23 * 60 + 55)
    assert sessions.segment_session("nse_fo", date(2026, 9, 28)) == (555, 930)


def test_pair_session_is_the_intersection():
    assert sessions.pair_session(["mcx_fo", "nse_fo"], date(2026, 9, 28)) == (555, 930)


def test_is_open():
    ist = sessions.IST
    assert sessions.is_open(["mcx_fo", "mcx_fo"], datetime(2026, 9, 28, 20, 0, tzinfo=ist))
    assert not sessions.is_open(["nse_fo", "nse_fo"], datetime(2026, 9, 28, 20, 0, tzinfo=ist))
    assert not sessions.is_open(["mcx_fo"], datetime(2026, 9, 27, 12, 0, tzinfo=ist))  # Sunday


def test_executor_prices_off_the_touch_for_its_side():
    seen = []

    def price_fn(seg, sym, side=None):
        seen.append(side)
        return {"buy": 101.0, "sell": 99.0}.get(side, 100.0)

    ex = SpreadExecutor(broker_fn=lambda: None, price_fn=price_fn, params_fn=dict)
    assert ex._px(SimpleNamespace(segment="mcx_fo", symbol="X", side="buy")) == 101.0
    assert ex._px(SimpleNamespace(segment="mcx_fo", symbol="X", side="sell")) == 99.0

    ex2 = SpreadExecutor(broker_fn=lambda: None, price_fn=lambda seg, sym: 100.0,
                         params_fn=dict)
    assert ex2._px(SimpleNamespace(segment="mcx_fo", symbol="X", side="buy")) == 100.0


def test_margin_route_is_discovered_and_remembered(arrow_broker):
    """MCX contracts refuse the trading symbol under some exchange values;
    the first combination Arrow answers is used, and cached."""
    from pyarrow_client import Exchange
    calls = []

    def order_margin(ex, ident, qty, prod, ot, tt, price):
        name = getattr(ex, "value", ex)
        calls.append((name, ident))
        if name == "MCX" and ident == "4242":
            return {"data": {"requiredMargin": 65000.0}, "status": "success"}
        raise RuntimeError("invalid trading symbol")

    baskets = []
    arrow_broker._client.order_margin = order_margin
    arrow_broker._client.basket_margin = lambda orders: (baskets.append(orders) or
                                                         {"data": {"requiredMargin": 30000.0}})
    arrow_broker._sym_token = {"CRUDEOIL19OCT26F": 4242, "CRUDEOIL18DEC26F": 4242}
    legs = [{"segment": "mcx_fo", "symbol": "CRUDEOIL19OCT26F", "side": "buy", "quantity": 100, "price": 9110},
            {"segment": "mcx_fo", "symbol": "CRUDEOIL18DEC26F", "side": "sell", "quantity": 100, "price": 8523}]
    res = arrow_broker.get_pair_margin(legs)
    assert res["error"] is None
    assert res["basket"]["total"] == 30000.0
    assert [l["total"] for l in res["legs"]] == [65000.0, 65000.0]
    assert baskets[0][0]["exchange"] == "MCX" and baskets[0][0]["symbol"] == "4242"
    n = len(calls)
    arrow_broker.get_pair_margin(legs)
    assert len(calls) == n + 2          # cached route: one call per leg


def test_margin_route_reports_every_refusal(arrow_broker):
    def order_margin(*a, **k):
        raise RuntimeError("invalid trading symbol")
    arrow_broker._client.order_margin = order_margin
    arrow_broker._sym_token = {}
    res = arrow_broker.get_pair_margin([{"segment": "mcx_fo", "symbol": "X", "side": "buy",
                                         "quantity": 1, "price": 1}])
    assert res["basket"] is None
    assert "MCXFO/symbol" in res["error"] and "invalid trading symbol" in res["error"]
