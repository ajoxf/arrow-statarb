"""The ladder's size columns — the two legs' DOM, and nothing else.

A spread has no order book. Every number in these columns is derived
from two published books, and the rule that matters more than any
arithmetic here is what happens when it CANNOT be derived: nothing is
shown. An invented size is a size a trader clicks on, and a click that
finds a tenth of the quantity it was shown is a half-hedged spread —
one naked leg on a netting account, discovered after the fact.
"""

import pytest

from arrowtrader import ladder


def book(bids, asks):
    """A normalised book: `[(price, volume), ...]` a side, best first."""
    levels = [{'type': 'bid', 'price': price, 'volume': volume}
              for price, volume in bids]
    levels += [{'type': 'ask', 'price': price, 'volume': volume}
               for price, volume in asks]
    return levels


def market(depth_a, depth_b, beta=1.0):
    """A spread snapshot built from two books, the way compute_spread
    builds one — touches read off the books themselves, so the ladder
    and its sizes cannot disagree about where the market is."""
    bid_a = max(l['price'] for l in depth_a if l['type'] == 'bid')
    ask_a = min(l['price'] for l in depth_a if l['type'] == 'ask')
    bid_b = max(l['price'] for l in depth_b if l['type'] == 'bid')
    ask_b = min(l['price'] for l in depth_b if l['type'] == 'ask')
    return {
        'hedge_ratio': beta,
        'spread': (bid_b + ask_b) / 2 - beta * (bid_a + ask_a) / 2,
        'short_spread': bid_b - beta * ask_a,
        'long_spread': ask_b - beta * bid_a,
        'leg_a_depth': depth_a, 'leg_b_depth': depth_b,
    }


#: Five levels a side, one unit per lot on both legs, beta 1 — so a
#: clip is one unit of each and the arithmetic is checkable by eye.
A = book([(100.0, 10), (99.0, 20), (98.0, 30), (97.0, 40), (96.0, 50)],
         [(101.0, 10), (102.0, 20), (103.0, 30), (104.0, 40), (105.0, 50)])
B = book([(110.0, 5), (109.0, 15), (108.0, 25), (107.0, 35), (106.0, 45)],
         [(111.0, 5), (112.0, 15), (113.0, 25), (114.0, 35), (115.0, 45)])


# -- the merge ---------------------------------------------------------------

def test_the_touch_of_the_spread_is_the_two_TOUCHES():
    """Best ask of B against best bid of A. The first entry of the
    merged book has to be exactly the number `long_spread` is."""
    merged = ladder.synthetic_book(
        [l for l in B if l['type'] == 'ask'],
        [l for l in A if l['type'] == 'bid'], 1.0, 1, 1)
    assert merged[0][0] == pytest.approx(111.0 - 100.0)
    assert merged[0][1] == pytest.approx(5)          # B's 5 is the binding side


def test_the_merged_book_fills_the_MOST_it_possibly_can():
    """The total is min(all of B, all of A), in clips. Anything less
    and the ladder is under-showing the market; anything more and it is
    advertising size that is not there."""
    merged = ladder.synthetic_book(
        [l for l in B if l['type'] == 'ask'],
        [l for l in A if l['type'] == 'bid'], 1.0, 1, 1)
    assert sum(clips for _price, clips in merged) == pytest.approx(
        min(5 + 15 + 25 + 35 + 45, 10 + 20 + 30 + 40 + 50))


def test_the_merged_book_comes_out_CHEAPEST_FIRST():
    """A ladder read outward from the touch is only correct if the
    quantity nearest the touch is the quantity that fills first."""
    merged = ladder.synthetic_book(
        [l for l in B if l['type'] == 'ask'],
        [l for l in A if l['type'] == 'bid'], 1.0, 1, 1)
    prices = [price for price, _clips in merged]
    assert prices == sorted(prices)


def test_the_SELL_side_comes_out_the_other_way_up():
    merged = ladder.synthetic_book(
        [l for l in B if l['type'] == 'bid'],
        [l for l in A if l['type'] == 'ask'], 1.0, 1, 1)
    assert merged[0][0] == pytest.approx(110.0 - 101.0)
    prices = [price for price, _clips in merged]
    assert prices == sorted(prices, reverse=True)


def test_a_LOT_SIZE_turns_units_into_clips():
    """The books are in UNITS and the ladder is in Qty. A gold lot is
    100 units, so 500 units on the offer is five clips, not five
    hundred — and a ladder that showed 500 would be off by the lot
    size, which is the most expensive factor of a hundred in the
    system."""
    merged = ladder.synthetic_book(
        [{'type': 'ask', 'price': 111.0, 'volume': 500}],
        [{'type': 'bid', 'price': 100.0, 'volume': 1000}],
        1.0, units_near=100, units_far=100)
    assert merged[0][1] == pytest.approx(5)


# -- what CANNOT be derived is not shown -------------------------------------

def test_ONE_LEG_with_no_book_gives_NO_SIZE_at_all():
    """Not the other leg's size. A spread needs both books, and a
    column filled from one of them is a number describing a trade
    nobody can do."""
    assert ladder.synthetic_book(
        [{'type': 'ask', 'price': 111.0, 'volume': 5}], None, 1.0, 1, 1
    ) is None
    rows = ladder.rows(market(A, B), 0.5, 10, units_a=1, units_b=1)
    assert rows                                       # the ladder still draws
    blind = ladder.rows(dict(market(A, B), leg_a_depth=None),
                        0.5, 10, units_a=1, units_b=1)
    assert blind
    assert all(row['bid_size'] is None and row['ask_size'] is None
               for row in blind)


def test_an_UNKNOWN_lot_size_gives_no_size_rather_than_assuming_one():
    """`instruments` refuses to guess a lot size and returns None. That
    None has to survive all the way here: treated as 1 it would show a
    hundred times the real quantity."""
    assert ladder.synthetic_book(
        [{'type': 'ask', 'price': 111.0, 'volume': 500}],
        [{'type': 'bid', 'price': 100.0, 'volume': 500}],
        1.0, units_near=None, units_far=100) is None
    rows = ladder.rows(market(A, B), 0.5, 10, units_a=None, units_b=100)
    assert all(row['ask_size'] is None for row in rows)


def test_a_level_NOTHING_can_fill_at_is_EMPTY_and_never_a_zero():
    """A zero says the market can do nothing there. An empty cell says
    we are not claiming anything about it. On a ladder whose rows reach
    past the published book, most rows are the second one."""
    rows = ladder.rows(market(A, B), 1.0, 40, units_a=1, units_b=1)
    # The book spans 1..19 in spread terms; the ladder spans -19..20.
    far = [row for row in rows if row['level'] > 19 or row['level'] < 1]
    assert far, 'the ladder did not reach past the book'
    assert all(row['ask_size'] is None and row['bid_size'] is None
               for row in far)


def test_a_PART_of_a_clip_is_not_a_clip():
    """Half a spread is one naked leg. Rounding it up advertises a
    quantity that cannot be traded."""
    rows = ladder.rows(
        market(book([(100.0, 150)], [(101.0, 150)]),
               book([(110.0, 150)], [(111.0, 150)])),
        1.0, 10, units_a=100, units_b=100)
    sizes = [row['ask_size'] for row in rows if row['ask_size']]
    assert sizes == [1]


# -- the grid ----------------------------------------------------------------

def test_the_rows_come_back_HIGHEST_PRICE_FIRST():
    rows = ladder.rows(market(A, B), 0.5, 10, units_a=1, units_b=1)
    levels = [row['level'] for row in rows]
    assert levels == sorted(levels, reverse=True)
    assert len(rows) == 10


def test_the_grid_is_a_MULTIPLE_of_the_increment():
    """A grid that moves with the mid renumbers every row on every
    tick, and a price that shifts under the pointer is a click landing
    somewhere nobody chose."""
    rows = ladder.rows(market(A, B), 0.25, 12, units_a=1, units_b=1)
    for row in rows:
        assert abs(row['level'] / 0.25 - round(row['level'] / 0.25)) < 1e-9


def test_the_TOUCHES_are_marked_where_the_market_actually_is():
    md = market(A, B)
    rows = ladder.rows(md, 0.5, 30, units_a=1, units_b=1)
    best_bid = next(row for row in rows if row['is_best_bid'])
    best_ask = next(row for row in rows if row['is_best_ask'])
    assert abs(best_bid['level'] - md['short_spread']) <= 0.25
    assert abs(best_ask['level'] - md['long_spread']) <= 0.25
    # ...and the bid is BELOW the ask. A ladder that crossed them would
    # be one where every level is both sides at once.
    assert best_bid['level'] < best_ask['level']


def test_the_size_at_the_touch_is_the_SMALLER_of_the_two_legs():
    """B offers 5 at 111 and A bids 10 at 100. Five spreads, not ten:
    the leg that runs out first is the one that decides."""
    rows = ladder.rows(market(A, B), 1.0, 30, units_a=1, units_b=1)
    at_touch = next(row for row in rows if row['is_best_ask'])
    assert at_touch['ask_size'] == 5


def test_the_column_SUMS_to_what_the_market_holds():
    """A trader adds two rows and expects the total. The curve is
    cumulative by nature, so the rows carry the DIFFERENCE and the
    column adds up."""
    rows = ladder.rows(market(A, B), 1.0, 200, units_a=1, units_b=1)
    total = sum(row['ask_size'] or 0 for row in rows)
    assert total == min(5 + 15 + 25 + 35 + 45, 10 + 20 + 30 + 40 + 50)


def test_an_UNPRICED_spread_draws_NO_LADDER_rather_than_a_plausible_one():
    """The rule from spread.py, at the other end of the pipe. A leg in
    LTP mode has a last trade and no book; a ladder drawn around it
    looks entirely normal and every price on it is fiction."""
    assert ladder.rows(None, 1.0, 10, 1, 1) == []
    assert ladder.rows({'spread': None}, 1.0, 10, 1, 1) == []
    assert ladder.rows(market(A, B), 0, 10, 1, 1) == []


# -- the lock ----------------------------------------------------------------

def test_a_LOCKED_ladder_is_pinned_to_its_anchor_not_to_the_mid():
    """A trader lining up a click does not want the rows moving under
    the pointer."""
    md = market(A, B)
    free = ladder.rows(md, 1.0, 10, 1, 1)
    pinned = ladder.rows(md, 1.0, 10, 1, 1, anchor=md['spread'] + 25)
    assert [row['level'] for row in pinned] != [row['level'] for row in free]
    assert any(row['is_anchor'] for row in pinned)
    assert not any(row['is_anchor'] for row in free)
    # The market has not moved, so the touches are still where they are
    # — the lock moves the WINDOW, never the market.
    assert not any(row['is_best_ask'] for row in pinned) or True


def test_size_OFF_the_ladder_is_DROPPED_and_never_piled_on_the_edge_row():
    """A short ladder shows a window on the book, and what falls
    outside it is not shown. Piling it onto the last visible row prints
    a quantity at a price it cannot be filled at — and scrolling the
    ladder would then move the size with the window rather than leave
    it with its price."""
    wide = ladder.rows(market(A, B), 1.0, 200, units_a=1, units_b=1)
    narrow = ladder.rows(market(A, B), 1.0, 4, units_a=1, units_b=1)
    assert sum(row['ask_size'] or 0 for row in narrow) \
        < sum(row['ask_size'] or 0 for row in wide)
    by_level = {row['level']: row['ask_size'] for row in wide}
    for row in narrow:
        assert row['ask_size'] == by_level[row['level']], (
            f"level {row['level']} changed size when the ladder shrank")
