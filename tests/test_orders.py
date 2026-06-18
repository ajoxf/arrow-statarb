"""Arrow facts #6-9: order kwargs (mpp), MCX reject, paise→rupee, positions."""

import types

import pytest

from tests.conftest import SAMPLE_MASTER


def test_market_order_kwargs_have_mpp_and_zero_price(arrow_broker):
    res = arrow_broker.submit_order(
        symbol="NIFTY30JUN26F", side="buy", quantity=75,
        order_type="market", exchange_segment="nse_fo", product="NRML")
    assert res["status"] == "submitted"
    assert res["order_id"] == "ORDER123"

    kw = arrow_broker._client.placed_orders[-1]
    # Plain MKT is disabled on Arrow → must send mpp=True with price=0.
    assert kw["mpp"] is True
    assert kw["price"] == 0.0
    assert kw["quantity"] == 75                 # units, not lots
    assert kw["exchange"].value == "NFO"        # nse_fo → NFO enum
    assert kw["transaction_type"].value == "BUY"
    assert kw["product"].value == "M"           # NRML → 'M'
    assert kw["order_type"].value == "MKT"


def test_limit_order_no_mpp(arrow_broker):
    arrow_broker.submit_order(
        symbol="NIFTY30JUN26F", side="sell", quantity=75,
        order_type="limit", price=25000.5, exchange_segment="nse_fo")
    kw = arrow_broker._client.placed_orders[-1]
    assert kw["mpp"] is False
    assert kw["price"] == 25000.5
    assert kw["transaction_type"].value == "SELL"


def test_mcx_is_rejected(arrow_broker):
    res = arrow_broker.submit_order(
        symbol="CRUDEOIL25JULFUT", side="buy", quantity=100,
        order_type="market", exchange_segment="mcx_fo")
    assert res["status"] == "error"
    assert "MCX" in res["message"]


def test_unknown_segment_rejected(arrow_broker):
    res = arrow_broker.submit_order(
        symbol="X", side="buy", quantity=1, order_type="market", exchange_segment="zzz")
    assert res["status"] == "error"


def test_not_connected_returns_error():
    from arrow_statarb.brokers.arrow_broker import ArrowBroker
    b = ArrowBroker(config={"app_id": "x"})
    res = b.submit_order(symbol="X", side="buy", quantity=1)
    assert res["status"] == "error"
    assert "connected" in res["message"].lower()


def test_stream_paise_to_rupees(arrow_broker):
    arrow_broker._instruments = SAMPLE_MASTER
    arrow_broker._build_instrument_index()      # populates _sym_token
    assert arrow_broker.start_price_stream(["NIFTY30JUN26F"]) is True

    from pyarrow_client import ArrowStreams
    cb = ArrowStreams.last_instance.data_stream.on_ticks
    assert cb is not None
    # Arrow sends prices in paise (integer) → broker divides by 100.
    cb(types.SimpleNamespace(token=111, ltp=2500050))
    got = arrow_broker.get_streamed_ltp(["NIFTY30JUN26F"])
    assert got["NIFTY30JUN26F"] == pytest.approx(25000.50)


def test_get_ltp_parses_titlecase(arrow_broker):
    arrow_broker._client.get_quotes = lambda mode, pairs: [
        {"TradingSymbol": "NIFTY30JUN26F", "Ltp": "25001.25"},
    ]
    out = arrow_broker.get_ltp([{"exchange_segment": "nse_fo", "instrument_token": "NIFTY30JUN26F"}])
    assert out["NIFTY30JUN26F"] == pytest.approx(25001.25)


def test_positions_parse_titlecase(arrow_broker):
    arrow_broker._client.get_positions = lambda: [
        {"TradingSymbol": "NIFTY30JUN26F", "NetQty": "75", "AvgPrice": "25000",
         "Ltp": "25010", "Pnl": "750", "ExchSeg": "NSEFO"},
    ]
    pos = arrow_broker.get_positions()
    assert pos[0]["symbol"] == "NIFTY30JUN26F"
    assert pos[0]["net_quantity"] == 75
    assert pos[0]["average_price"] == pytest.approx(25000.0)
    assert pos[0]["pnl"] == pytest.approx(750.0)
