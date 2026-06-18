"""Arrow facts #2-5: instrument parsing, picker index, lot resolution."""

import gzip
import json

from arrow_statarb.brokers.arrow_broker import ArrowBroker
from tests.conftest import SAMPLE_MASTER


def test_parse_instruments_list_passthrough():
    assert ArrowBroker._parse_instruments(SAMPLE_MASTER) == SAMPLE_MASTER


def test_parse_instruments_dict_wrapped():
    out = ArrowBroker._parse_instruments({"data": SAMPLE_MASTER})
    assert out == SAMPLE_MASTER


def test_parse_instruments_json_bytes():
    raw = json.dumps(SAMPLE_MASTER).encode()
    assert ArrowBroker._parse_instruments(raw) == SAMPLE_MASTER


def test_parse_instruments_gzip_json():
    raw = gzip.compress(json.dumps(SAMPLE_MASTER).encode())
    assert raw[:2] == b"\x1f\x8b"
    assert ArrowBroker._parse_instruments(raw) == SAMPLE_MASTER


def test_parse_instruments_csv():
    csv_text = ("ExchSeg,Symbol,TradingSymbol,OptionType,Expiry,LotSize,Token\n"
                "NSEFO,NIFTY,NIFTY30JUN26F,,30-Jun-2026,75,111\n")
    out = ArrowBroker._parse_instruments(csv_text)
    assert out[0]["TradingSymbol"] == "NIFTY30JUN26F"
    assert out[0]["LotSize"] == "75"


def test_extract_symbol_lot_uses_trading_symbol():
    # Lot must be keyed by the tradeable symbol, not the underlying.
    sym, ls = ArrowBroker._extract_symbol_lot(SAMPLE_MASTER[0])
    assert sym == "NIFTY30JUN26F"
    assert ls == 75


def test_build_index_groups_and_sorts(arrow_broker):
    arrow_broker._instruments = SAMPLE_MASTER
    arrow_broker._build_instrument_index()

    # Futures underlyings on NSEFO
    assert "NIFTY" in arrow_broker.list_underlyings("NSEFO", "future")
    # Options grouped separately
    assert "NIFTY" in arrow_broker.list_underlyings("NSEFO", "option")
    # Cash on NSECM
    assert "RELIANCE" in arrow_broker.list_underlyings("NSECM", "cash")

    futs = arrow_broker.list_contracts("NSEFO", "future", "NIFTY")
    syms = [c["trading_symbol"] for c in futs]
    # Chronological by expiry: Jun → Jul → Aug
    assert syms == ["NIFTY30JUN26F", "NIFTY28JUL26F", "NIFTY25AUG26F"]


def test_token_index(arrow_broker):
    arrow_broker._instruments = SAMPLE_MASTER
    arrow_broker._build_instrument_index()
    assert arrow_broker._sym_token["NIFTY30JUN26F"] == 111


def test_lot_resolution_exact_and_strip(arrow_broker):
    arrow_broker._instruments = SAMPLE_MASTER
    # Populate lot index the way _fetch_instruments does
    for inst in SAMPLE_MASTER:
        s, ls = ArrowBroker._extract_symbol_lot(inst)
        if s and ls:
            arrow_broker._lot_sizes.setdefault(s, ls)

    # Exact match
    assert arrow_broker.resolve_lot_size("nse_fo", "NIFTY30JUN26F") == 75
    assert arrow_broker.resolve_lot_size("nse_fo", "NIFTY25AUG26F") == 65


def test_lot_resolution_strip_to_override():
    # Config override keyed by base symbol must match an expiry-coded symbol.
    b = ArrowBroker(config={"app_id": "x", "lot_sizes": {"CRUDEOIL": 100}})
    assert b.resolve_lot_size("nse_fo", "CRUDEOIL25JULFUT") == 100


def test_lot_resolution_unknown_defaults_to_one():
    b = ArrowBroker(config={"app_id": "x"})
    assert b.resolve_lot_size("nse_fo", "UNKNOWNSYM99") == 1


def test_parse_date_tuple_formats():
    assert ArrowBroker._parse_date_tuple("2025-06-26") == (2025, 6, 26)
    assert ArrowBroker._parse_date_tuple("30JUN26") == (2026, 6, 30)
    assert ArrowBroker._parse_date_tuple("30-Jun-2026") == (2026, 6, 30)
    assert ArrowBroker._parse_date_tuple("") is None


def test_derive_underlying():
    assert ArrowBroker._derive_underlying("NIFTY30JUN26F") == "NIFTY"
    assert ArrowBroker._derive_underlying("RELIANCE-EQ") == "RELIANCE"
    assert ArrowBroker._derive_underlying("HDFCBANK30JUN26C875") == "HDFCBANK"
