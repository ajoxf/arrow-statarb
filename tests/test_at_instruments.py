"""The instrument master: every field read, and unknown staying unknown.

The test this file exists for is `test_lot_size_unknown_is_none_not_one`.
"""

import datetime
import gzip
import json

import pytest

from arrowtrader import instruments as I
from arrowtrader.segments import SegmentTable


MCX_ROWS = [
    {'ExchSeg': 'MCXFO', 'Symbol': 'GOLD', 'TradingSymbol': 'GOLD05FEB26F',
     'OptionType': '', 'Expiry': '05-Feb-2026', 'LotSize': '100',
     'Token': '218123', 'TickSize': '1'},
    {'ExchSeg': 'MCXFO', 'Symbol': 'GOLD', 'TradingSymbol': 'GOLD05DEC25F',
     'OptionType': '', 'Expiry': '05-Dec-2025', 'LotSize': '100',
     'Token': '218124', 'TickSize': '1'},
    {'ExchSeg': 'MCXFO', 'Symbol': 'GOLDM', 'TradingSymbol': 'GOLDM05DEC25F',
     'OptionType': '', 'Expiry': '05-Dec-2025', 'LotSize': '10',
     'Token': '218125', 'TickSize': '1'},
    {'ExchSeg': 'MCXFO', 'Symbol': 'SILVER', 'TradingSymbol': 'SILVER05MAR26F',
     'OptionType': '', 'Expiry': '05-Mar-2026', 'LotSize': '30',
     'Token': '218126', 'TickSize': '1'},
    {'ExchSeg': 'NSEFO', 'Symbol': 'NIFTY', 'TradingSymbol': 'NIFTY30JUN26F',
     'OptionType': '', 'Expiry': '30-Jun-2026', 'LotSize': '75',
     'Token': '111'},
]


@pytest.fixture
def master():
    return I.Master(MCX_ROWS, segments=SegmentTable())


# -- the one that may not be deleted -----------------------------------------

def test_lot_size_unknown_is_none_not_one(master):
    """A missing lot size REFUSES. It never silently reads as 1.

    Units on the wire are lots x LotSize. A lot size that defaults to 1
    does not make an order fail — it makes it fill at a hundredth of
    the intended size on GOLD, and at a thirtieth on SILVER. The
    stat-arb broker returns 1 here with a warning; against this
    arithmetic that fallback IS the error.
    """
    assert master.lot_size('GOLD05DEC25F') == 100
    assert master.lot_size('NOT-A-CONTRACT') is None

    no_lot = I.Master([dict(MCX_ROWS[0], LotSize='')], segments=SegmentTable())
    assert no_lot.lot_size('GOLD05FEB26F') is None

    zero_lot = I.Master([dict(MCX_ROWS[0], LotSize='0')],
                        segments=SegmentTable())
    assert zero_lot.lot_size('GOLD05FEB26F') is None


def test_a_missing_lot_size_is_reported_with_the_fix(master):
    broken = I.Master([dict(MCX_ROWS[0], LotSize='')], segments=SegmentTable())
    report = broken.report('GOLD05FEB26F')
    assert report['found'] is True
    assert report['ok'] is False
    assert any('LotSize' in problem for problem in report['problems'])
    # CONTROL: with the lot size present there is no complaint about it.
    good = master.report('GOLD05FEB26F')
    assert good['ok'] is True
    assert good['problems'] == []


# -- the payload arrives in four shapes --------------------------------------

def test_master_parses_a_list():
    assert len(I.parse_master(MCX_ROWS)) == 5


def test_master_parses_json_bytes():
    assert len(I.parse_master(json.dumps(MCX_ROWS).encode())) == 5


def test_master_parses_gzipped_json():
    blob = gzip.compress(json.dumps(MCX_ROWS).encode())
    assert blob[:2] == b'\x1f\x8b'
    assert len(I.parse_master(blob)) == 5


def test_master_parses_csv():
    header = 'ExchSeg,Symbol,TradingSymbol,Expiry,LotSize,Token'
    body = 'MCXFO,GOLD,GOLD05DEC25F,05-Dec-2025,100,218124'
    rows = I.parse_master(f'{header}\n{body}\n')
    assert rows[0]['TradingSymbol'] == 'GOLD05DEC25F'


def test_master_parses_a_wrapped_dict():
    assert len(I.parse_master({'data': MCX_ROWS})) == 5


def test_unreadable_payload_is_empty_not_an_exception():
    assert I.parse_master(None) == []
    assert I.parse_master(b'') == []


# -- expiries -----------------------------------------------------------------

@pytest.mark.parametrize('raw,expected', [
    ('2025-06-26', datetime.date(2025, 6, 26)),
    ('30-Jun-2026', datetime.date(2026, 6, 30)),
    ('GOLD05DEC25F', datetime.date(2025, 12, 5)),
    ('26-06-2025', datetime.date(2025, 6, 26)),
    ('1750896000', datetime.date(2025, 6, 26)),
])
def test_expiry_parses_every_spelling(raw, expected):
    assert I.parse_expiry(raw) == expected


def test_an_unreadable_expiry_is_none():
    assert I.parse_expiry('') is None
    assert I.parse_expiry('RELIANCE-EQ') is None


def test_an_unreadable_expiry_sorts_LAST_not_first(master):
    """A contract whose expiry could not be read must never be offered
    as the front month — that is the one the trader picks by reflex."""
    rows = MCX_ROWS + [{'ExchSeg': 'MCXFO', 'Symbol': 'GOLD',
                        'TradingSymbol': 'GOLDXX', 'Expiry': '',
                        'LotSize': '100', 'Token': '9'}]
    built = I.Master(rows, segments=SegmentTable())
    order = [c.trading_symbol for c in built.contracts('mcx_fo', 'GOLD')]
    assert order == ['GOLD05DEC25F', 'GOLD05FEB26F', 'GOLDXX']


# -- the picker ---------------------------------------------------------------

def test_contracts_are_chronological(master):
    order = [c.trading_symbol for c in master.contracts('mcx_fo', 'GOLD')]
    assert order == ['GOLD05DEC25F', 'GOLD05FEB26F']


def test_a_mini_contract_is_its_own_underlying(master):
    """GOLDM is not GOLD. Folding a mini into its parent is how a
    ratio pair gets sized as if it were a calendar."""
    assert master.underlyings('mcx_fo') == ['GOLD', 'GOLDM', 'SILVER']
    assert master.lot_size('GOLDM05DEC25F') == 10
    assert master.lot_size('GOLD05DEC25F') == 100


def test_derive_underlying_keeps_the_mini_suffix():
    assert I.derive_underlying('CRUDEOILM19DEC25F') == 'CRUDEOILM'
    assert I.derive_underlying('CRUDEOIL19DEC25F') == 'CRUDEOIL'
    assert I.derive_underlying('RELIANCE-EQ') == 'RELIANCE'


def test_segments_are_kept_apart(master):
    assert master.underlyings('nse_fo') == ['NIFTY']
    assert master.contracts('mcx_fo', 'NIFTY') == []


def test_search_finds_by_underlying_and_symbol(master):
    found = [c.trading_symbol for c in master.search('GOLD')]
    assert set(found) == {'GOLD05DEC25F', 'GOLD05FEB26F', 'GOLDM05DEC25F'}
    assert [c.trading_symbol for c in master.search('SILVER')] \
        == ['SILVER05MAR26F']


def test_next_contracts_is_the_roll(master):
    """An MCX calendar has to be re-pointed every month or two."""
    nxt = [c.trading_symbol for c in master.next_contracts('GOLD05DEC25F')]
    assert nxt == ['GOLD05FEB26F']
    assert master.next_contracts('GOLD05FEB26F') == []


# -- fields the master does not always carry ---------------------------------

def test_freeze_quantity_and_tick_size_come_from_config_when_absent():
    built = I.Master(MCX_ROWS, segments=SegmentTable(),
                     freeze_quantities={'GOLD': 1000},
                     tick_sizes={'GOLDM': 1.0})
    assert built.contract('GOLD05DEC25F').freeze_qty == 1000
    assert built.tick_size('GOLDM05DEC25F') == 1.0
    # And nothing is invented where neither source has it.
    assert built.contract('SILVER05MAR26F').freeze_qty is None


def test_the_master_wins_over_config():
    built = I.Master([dict(MCX_ROWS[0], FreezeQty='2000')],
                     segments=SegmentTable(), freeze_quantities={'GOLD': 1000})
    assert built.contract('GOLD05FEB26F').freeze_qty == 2000


def test_rows_in_an_unknown_segment_are_counted_not_dropped_silently():
    built = I.Master([{'ExchSeg': 'NCDEX', 'Symbol': 'JEERA',
                       'TradingSymbol': 'JEERA20DEC25F', 'LotSize': '3'}],
                     segments=SegmentTable())
    assert built.unknown_exch_segs == {'NCDEX': 1}
    # It is still findable by symbol — we just cannot group it.
    assert built.lot_size('JEERA20DEC25F') == 3


def test_a_missing_contract_is_reported_not_defaulted(master):
    report = master.report('GOLD05JAN99F')
    assert report['found'] is False
    assert 'instrument master' in report['error']
