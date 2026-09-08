"""The book — and the ways a price can lie when there isn't one.

Blocker 3.2: the stat-arb Arrow layer reads LTP only. These tests pin
what must happen when that is all there is: NOT a plausible ladder
around a number nobody can trade at.
"""

from arrowtrader import quotes
from arrowtrader.spread import compute_spread


class Pair:
    key = 'GOLD05DEC25F|GOLD05FEB26F'


FULL = {
    'TradingSymbol': 'GOLD05DEC25F',
    'Ltp': 7500000, 'BestBidPrice': 7499900, 'BestAskPrice': 7500100,
    'Bids': [{'price': 7499900, 'quantity': 3},
             {'price': 7499800, 'quantity': 5}],
    'Asks': [{'price': 7500100, 'quantity': 2},
             {'price': 7500200, 'quantity': 9}],
}


# -- paise, converted once ---------------------------------------------------

def test_paise_become_rupees_on_every_price_field():
    tick = quotes.normalise_quote(FULL, scale=quotes.PAISE)
    assert tick['bid'] == 74999.0
    assert tick['ask'] == 75001.0
    assert tick['last'] == 75000.0
    assert tick['depth'][0]['price'] == 74999.0


def test_a_rupee_feed_is_not_divided():
    tick = quotes.normalise_quote({'bid': 74999, 'ask': 75001, 'ltp': 75000},
                                  scale=quotes.RUPEES)
    assert (tick['bid'], tick['ask']) == (74999.0, 75001.0)


# -- a missing side stays missing --------------------------------------------

def test_a_missing_bid_is_none_and_NOT_the_last_trade():
    tick = quotes.normalise_quote({'Ltp': 7500000, 'BestAskPrice': 7500100},
                                  scale=quotes.PAISE)
    assert tick['bid'] is None
    assert tick['last'] == 75000.0
    assert tick['executable'] is False


def test_a_zero_price_is_none_because_no_exchange_quotes_zero():
    tick = quotes.normalise_quote({'BestBidPrice': 0, 'BestAskPrice': 7500100,
                                   'Ltp': 7500000}, scale=quotes.PAISE)
    assert tick['bid'] is None


def test_an_ltp_only_tick_can_price_nothing():
    tick = quotes.tick_from_ltp(7500000)
    assert tick['last'] == 75000.0
    assert tick['bid'] is None and tick['ask'] is None
    assert tick['depth'] is None
    assert tick['executable'] is False


# -- and the spread refuses it -----------------------------------------------

def test_the_spread_REFUSES_an_ltp_only_feed():
    """Read as zero, a missing bid puts the spread at `-beta x ask_A`
    and draws a perfectly plausible ladder around it."""
    ltp_only = quotes.tick_from_ltp(7500000)
    full = quotes.normalise_quote(FULL, scale=quotes.PAISE)
    assert compute_spread(Pair(), ltp_only, full) is None
    assert compute_spread(Pair(), full, ltp_only) is None
    # CONTROL: with both books present it prices normally.
    assert compute_spread(Pair(), full, full) is not None


def test_the_reason_names_the_leg_and_the_cause():
    full = quotes.normalise_quote(FULL, scale=quotes.PAISE)
    reason = quotes.missing_book_reason(quotes.tick_from_ltp(1), full,
                                        'GOLD05DEC25F', 'GOLD05FEB26F')
    assert 'GOLD05DEC25F' in reason and 'LTP mode' in reason
    # CONTROL: two full books have no complaint.
    assert quotes.missing_book_reason(full, full) is None


def test_one_missing_side_is_named_as_that_side():
    half = quotes.normalise_quote({'BestBidPrice': 7499900, 'Ltp': 7500000},
                                  scale=quotes.PAISE)
    full = quotes.normalise_quote(FULL, scale=quotes.PAISE)
    assert 'no offer' in quotes.missing_book_reason(half, full, 'A', 'B')


# -- an absent book is empty, never zero -------------------------------------

def test_no_depth_is_none_not_a_row_of_zeroes():
    """Zero sizes read as 'the market can do nothing'. None reads as
    'we do not know', which is the truth, and the ladder's size
    columns then stay empty."""
    tick = quotes.normalise_quote({'BestBidPrice': 7499900,
                                   'BestAskPrice': 7500100},
                                  scale=quotes.PAISE)
    assert tick['depth'] is None
    assert tick['bid_size'] is None and tick['ask_size'] is None
    # CONTROL: a book that IS published comes through with its sizes.
    full = quotes.normalise_quote(FULL, scale=quotes.PAISE)
    assert full['bid_size'] == 3.0 and full['ask_size'] == 2.0


def test_depth_is_sorted_best_first_on_each_side():
    depth = quotes.normalise_quote(FULL, scale=quotes.PAISE)['depth']
    bids = [level['price'] for level in depth if level['type'] == 'bid']
    asks = [level['price'] for level in depth if level['type'] == 'ask']
    assert bids == sorted(bids, reverse=True)     # highest bid first
    assert asks == sorted(asks)                   # lowest offer first


def test_a_level_that_has_just_cleared_keeps_its_zero_size():
    """Zero AT a published level is a real reading. Missing is not."""
    tick = quotes.normalise_quote(
        dict(FULL, Bids=[{'price': 7499900, 'quantity': 0}]),
        scale=quotes.PAISE)
    assert tick['bid_size'] == 0.0


# -- shape tolerance ----------------------------------------------------------

def test_titlecase_camelcase_and_snakecase_all_read():
    for payload in ({'BestBidPrice': 100, 'BestAskPrice': 101},
                    {'bidPrice': 100, 'askPrice': 101},
                    {'bid_price': 100, 'ask_price': 101},
                    {'bp': 100, 'sp': 101}):
        tick = quotes.normalise_quote(payload, scale=quotes.RUPEES)
        assert (tick['bid'], tick['ask']) == (100.0, 101.0), payload


def test_a_nested_depth_block_reads_too():
    tick = quotes.normalise_quote(
        {'bid': 100, 'ask': 101,
         'depth': {'buy': [{'price': 100, 'quantity': 4}],
                   'sell': [{'price': 101, 'quantity': 7}]}},
        scale=quotes.RUPEES)
    assert tick['bid_size'] == 4.0 and tick['ask_size'] == 7.0


def test_a_non_dict_payload_is_none_not_an_exception():
    assert quotes.normalise_quote(None) is None
    assert quotes.normalise_quote([1, 2, 3]) is None
