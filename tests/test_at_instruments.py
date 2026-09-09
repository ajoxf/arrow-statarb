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


# -- futures, on a master that is mostly options ------------------------------

def _mcx_crude():
    """An MCX underlying as the master actually lists it: a couple of
    futures and a wall of strikes around them.

    Two details are deliberate and both are load-bearing.

    The options carry NO OptionType, which is how the master lists them
    and how they came to be classified as futures.

    And THE FUTURES COME LAST. Nothing promises the master groups an
    underlying's futures before its chain, and a search that stops
    collecting partway through only finds them if it happens to reach
    them — a fixture that puts them first proves the search works on a
    master shaped conveniently, which is not the one that shipped.
    """
    rows = []
    for strike in range(3000, 9000, 50):
        for letter in 'CP':
            rows.append({
                'ExchSeg': 'MCXFO', 'Symbol': 'CRUDEOIL',
                'TradingSymbol': f'CRUDEOIL17SEP26{letter}{strike}',
                'OptionType': '', 'Expiry': '17-Sep-2026',
                'StrikePrice': str(strike), 'LotSize': '100',
                'Token': str(10000 + strike), 'TickSize': '1'})
    rows += [
        {'ExchSeg': 'MCXFO', 'Symbol': 'CRUDEOIL',
         'TradingSymbol': 'CRUDEOIL17SEP26F', 'OptionType': '',
         'Expiry': '17-Sep-2026', 'LotSize': '100', 'Token': '1',
         'TickSize': '1'},
        {'ExchSeg': 'MCXFO', 'Symbol': 'CRUDEOIL',
         'TradingSymbol': 'CRUDEOIL19OCT26F', 'OptionType': '',
         'Expiry': '19-Oct-2026', 'LotSize': '100', 'Token': '2',
         'TickSize': '1'},
    ]
    return rows


def _master():
    from arrowtrader.instruments import Master
    from arrowtrader.segments import SegmentTable
    return Master(_mcx_crude(), segments=SegmentTable())


def test_an_option_with_NO_option_type_field_is_still_an_OPTION():
    """`OptionType` is documented as CE/PE with empty meaning a future,
    and MCX has been seen to leave it empty on options. Classified from
    that field alone, `CRUDEOIL17SEP26C8950` — a call — was a FUTURE,
    and a picker filtered to futures offered it as the September
    contract. A spread built on it is not the spread anybody meant."""
    master = _master()
    assert master.contract('CRUDEOIL17SEP26C8950').kind == 'option'
    assert master.contract('CRUDEOIL17SEP26P3400').kind == 'option'
    # The control: a future is still a future.
    assert master.contract('CRUDEOIL17SEP26F').kind == 'future'
    assert master.contract('CRUDEOIL19OCT26F').kind == 'future'


def test_a_search_for_the_UNDERLYING_finds_the_FUTURES_first():
    """The bug on the screen: two calls in the dropdown and no futures
    at all.

    Two faults met. The search stopped collecting at `limit * 4` and
    sorted what it had — on 240 options and 2 futures in insertion
    order, the futures never reached the sort. And with the options
    misclassified, no filter could have excluded them either.
    """
    found = _master().search('CRUDEOIL', segment='mcx_fo', limit=10)
    assert found[0].trading_symbol == 'CRUDEOIL17SEP26F'
    assert found[1].trading_symbol == 'CRUDEOIL19OCT26F'


def test_asking_for_FUTURES_returns_only_futures():
    found = _master().search('CRUDEOIL', segment='mcx_fo', kind='future',
                             limit=50)
    assert [c.trading_symbol for c in found] == ['CRUDEOIL17SEP26F',
                                                 'CRUDEOIL19OCT26F']


def test_asking_for_OPTIONS_still_gets_them():
    """The control. Filtering to futures by default is not the same as
    pretending the options are not there."""
    found = _master().search('CRUDEOIL', segment='mcx_fo', kind='option',
                             limit=5)
    assert found
    assert all(c.kind == 'option' for c in found)
    # Oldest expiry first, then by strike: a chain reads up.
    strikes = [c.strike for c in found]
    assert strikes == sorted(strikes)


def test_a_FUTURE_is_never_dropped_by_the_search_limit():
    """The limit trims the tail. It must not trim the head."""
    for limit in (1, 2, 5, 40):
        found = _master().search('CRUDEOIL', segment='mcx_fo', limit=limit)
        assert found[0].trading_symbol == 'CRUDEOIL17SEP26F'


MONTHS = ('JAN', 'FEB', 'MAR', 'APR', 'MAY', 'JUN',
          'JUL', 'AUG', 'SEP', 'OCT', 'NOV', 'DEC')


def test_EVERY_MONTH_of_the_year_classifies_as_a_FUTURE():
    """The regression this test exists for, and it was mine.

    Reading "an option letter followed by digits at the end of the
    symbol" finds the P of SEP and the C of DEC and OCT.
    `CRUDEOIL17SEP26` ends `P26`, so every September future whose
    symbol stops at the expiry was read as a call — three months out of
    twelve, silently, on the exchange this terminal exists for. What
    the operator saw was `no future in the master matches "crudeoil"`.

    The expiry is parsed explicitly now, so a month's own letters can
    never be mistaken for an option type.
    """
    from arrowtrader.instruments import classify
    for month in MONTHS:
        for symbol in (f'CRUDEOIL17{month}26', f'CRUDEOIL17{month}26F',
                       f'GOLDM05{month}25', f'GOLDM05{month}25F'):
            assert classify('MCXFO', '', symbol) == 'future', symbol


def test_EVERY_MONTH_still_tells_an_OPTION_apart():
    """The control. Parsing the expiry must not blind it to a strike
    sitting right after that expiry."""
    from arrowtrader.instruments import classify
    for month in MONTHS:
        for symbol in (f'CRUDEOIL17{month}26C3400',
                       f'CRUDEOIL17{month}26P3400',
                       f'GOLD05{month}25CE120000'):
            assert classify('MCXFO', '', symbol) == 'option', symbol


def test_a_STRIKE_says_option_even_where_the_symbol_cannot_be_parsed():
    """The strongest signal after the type field: a future has no
    strike. It decides on a symbol shape this build has never seen."""
    from arrowtrader.instruments import classify
    assert classify('MCXFO', '', 'SOMETHINGODD', strike=8950) == 'option'
    assert classify('MCXFO', '', 'SOMETHINGODD', strike=0) == 'future'
    assert classify('MCXFO', '', 'SOMETHINGODD', strike=None) == 'future'


def test_a_symbol_this_build_cannot_parse_says_NOTHING_either_way():
    """UNMEASURED IS NOT ZERO, applied to a shape. An unrecognised
    symbol is not evidence of a future — it is no evidence at all, and
    the fields decide."""
    from arrowtrader.instruments import option_from_symbol
    assert option_from_symbol('CRUDEOIL17SEP26') is False
    assert option_from_symbol('CRUDEOIL17SEP26C3400') is True
    assert option_from_symbol('WHAT-IS-THIS') is None
    assert option_from_symbol('') is None
