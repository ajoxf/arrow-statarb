"""The ledger: one click one order, and what a reduce covers.

On a netting venue this book is the ONLY record of something the
exchange throws away — which of our positions a reduction came off, and
therefore which entry price the P&L is measured against.
"""

import pytest

from arrowtrader.book import Book, ReductionPlan, close_failure, reduce_first
from arrowtrader.models import (LegFill, OrderType, SpreadPosition,
                                SpreadSide, TimeInForce)


class Pair:
    key = 'GOLD05DEC25F|GOLD05FEB26F'
    order_type = OrderType.LIMIT
    time_in_force = TimeInForce.DAY


def fill(volume, side, price=75000.0, lot_size=100):
    return LegFill('arrow', 'GOLD05DEC25F', side, volume, price,
                   contract_size=lot_size, product='NRML')


def position(book, side='SELL', quantity=1.0, entry=500.0, at=0.0,
             lots_a=None, lots_b=None):
    """One recorded spread position, with its own leg fills."""
    leg_a, leg_b = SpreadSide(side).leg_sides()
    built = SpreadPosition(Pair.key, side, quantity,
                           fill(lots_a if lots_a is not None else quantity,
                                leg_a.value),
                           fill(lots_b if lots_b is not None else quantity,
                                leg_b.value),
                           entry, 'MARKET', 100.0 * quantity)
    built.opened_at = at
    return book.add_position(built)


class Executor:
    """A close that goes through, or does not, on demand."""

    def __init__(self, fraction=1.0, ok=True, imbalanced=False, legs=None,
                 reason=None):
        self.fraction = fraction
        self.ok = ok
        self.imbalanced = imbalanced
        self.legs = legs
        self.reason = reason
        self.calls = []

    def close_spread(self, pair, plan, md, reason=None):
        self.calls.append(plan)
        return {'ok': self.ok, 'fraction': self.fraction,
                'imbalanced': self.imbalanced, 'legs': self.legs,
                'reason': self.reason}


# -- one click is one order ---------------------------------------------------

def test_three_clicks_at_one_level_are_three_cancellable_orders():
    book = Book()
    for _ in range(3):
        book.add_order(Pair(), 'BUY', 58.40, 1)
    assert len(book.orders(Pair.key)) == 3
    # The Work column AGGREGATES them — as a view, never a replacement.
    assert book.working_at(Pair.key, 58.40) == (3.0, 0.0)
    # ...and pulling the cell pulls exactly one.
    book.cancel(book.orders(Pair.key)[0].order_id)
    assert len(book.orders(Pair.key)) == 2
    assert book.working_at(Pair.key, 58.40) == (2.0, 0.0)


def test_cancel_where_reports_what_it_actually_pulled():
    """So the button can report a count instead of claiming success
    over an empty set."""
    book = Book()
    book.add_order(Pair(), 'BUY', 58.40, 1)
    book.add_order(Pair(), 'SELL', 58.60, 1)
    assert len(book.cancel_where(Pair.key, side='BUY')) == 1
    assert book.working_counts(Pair.key) == (0, 1)
    assert book.cancel_where(Pair.key, side='BUY') == []


def test_an_automation_target_is_not_pulled_by_a_hand_placed_close():
    """Standing AutoRouting down pulls what AutoRouting armed. A trader
    who clicks to get out and finds their order swept by a switch they
    did not touch believes they are covered and is not."""
    book = Book()
    book.add_order(Pair(), 'BUY', 58.40, 1, position_id='POS1',
                   auto_armed=True)
    by_hand = book.add_order(Pair(), 'BUY', 58.30, 1, position_id='POS1')
    armed = book.auto_armed_for('POS1')
    assert len(armed) == 1
    assert armed[0].order_id != by_hand.order_id
    # And only the trader's own counts as cover already in place.
    assert book.armed_by_hand('POS1') == 1.0


# -- what a reduce covers -----------------------------------------------------

def test_positions_to_reduce_takes_OLDEST_FIRST():
    book = Book()
    old = position(book, 'SELL', 5.0, at=1.0)
    new = position(book, 'SELL', 5.0, at=2.0)
    picked = book.positions_to_reduce(Pair.key, 'BUY', 7.0)
    assert [p.position_id for p, _ in picked] == [old.position_id,
                                                  new.position_id]
    assert [take for _, take in picked] == [5.0, 2.0]


def test_the_LAST_position_is_taken_IN_PART():
    """Whole positions only ended the scan at the first one too big,
    and the remainder of the click opened the OTHER WAY — live
    2026-09-03, 93 of a 100-lot cover opened longs against a short."""
    book = Book()
    position(book, 'SELL', 5.0, at=1.0)
    position(book, 'SELL', 2.0, at=2.0)
    position(book, 'SELL', 1200.0, at=3.0)
    picked = book.positions_to_reduce(Pair.key, 'BUY', 100.0)
    assert [take for _, take in picked] == [5.0, 2.0, 93.0]
    assert sum(take for _, take in picked) == 100.0


def test_a_reduce_never_over_closes_and_flips_the_net():
    book = Book()
    position(book, 'SELL', 3.0, at=1.0)
    picked = book.positions_to_reduce(Pair.key, 'BUY', 100.0)
    assert sum(take for _, take in picked) == 3.0


def test_a_click_the_SAME_way_closes_nothing():
    """`side` arrives as a plain string from the command bridge and as
    an enum from the quoter. `is not` against a string is always true,
    so every open position looked opposite — and a second buy while
    long would have closed the trader's own position."""
    book = Book()
    position(book, 'BUY', 5.0, at=1.0)
    assert book.positions_to_reduce(Pair.key, 'BUY', 5.0) == []
    assert book.positions_to_reduce(Pair.key, SpreadSide.BUY, 5.0) == []
    # CONTROL: the opposite side does cover it, in both spellings.
    assert len(book.positions_to_reduce(Pair.key, 'SELL', 5.0)) == 1
    assert len(book.positions_to_reduce(Pair.key, SpreadSide.SELL, 5.0)) == 1


def test_a_resting_close_only_covers_what_is_not_already_spoken_for():
    """Or a 10-lot short ends up with 20 lots of resting closes against
    it, and the second one to fill opens a long."""
    book = Book()
    held = position(book, 'SELL', 10.0, at=1.0)
    book.add_order(Pair(), 'BUY', 58.40, 4, position_id=held.position_id)
    picked = book.positions_to_reduce(Pair.key, 'BUY', 10.0,
                                      reserve_armed=True)
    assert [take for _, take in picked] == [6.0]
    # CONTROL: a MARKET close is now, and takes the whole thing —
    # it disarms whatever was resting as it goes.
    assert book.positions_to_reduce(Pair.key, 'BUY', 10.0)[0][1] == 10.0


# -- the plan: one close per LEG ----------------------------------------------

def test_the_plan_totals_the_LEG_lots_for_the_whole_click():
    """N ledger positions is N chances to be refused halfway and N
    brokerage charges. One order per leg either goes or does not."""
    book = Book()
    position(book, 'SELL', 5.0, at=1.0, lots_a=5.0, lots_b=50.0)
    position(book, 'SELL', 2.0, at=2.0, lots_a=2.0, lots_b=20.0)
    plan = ReductionPlan(book.positions_to_reduce(Pair.key, 'BUY', 7.0))
    assert plan.spreads == 7.0
    assert plan.leg_lots() == (7.0, 70.0)


def test_the_plan_reads_each_positions_OWN_fills_not_the_current_clip():
    """The clip may have been changed since the position was opened,
    and what has to come off is what actually went on."""
    book = Book()
    position(book, 'SELL', 4.0, at=1.0, lots_a=4.0, lots_b=8.0)   # old 1:2
    position(book, 'SELL', 4.0, at=2.0, lots_a=4.0, lots_b=40.0)  # new 1:10
    plan = ReductionPlan(book.positions_to_reduce(Pair.key, 'BUY', 8.0))
    assert plan.leg_lots() == (8.0, 48.0)


def test_a_part_take_scales_the_leg_lots_with_it():
    book = Book()
    position(book, 'SELL', 10.0, at=1.0, lots_a=10.0, lots_b=100.0)
    plan = ReductionPlan(book.positions_to_reduce(Pair.key, 'BUY', 3.0))
    assert plan.leg_lots() == (3.0, 30.0)


def test_the_plan_names_the_side_each_leg_was_ENTERED_on():
    book = Book()
    position(book, 'SELL', 5.0, at=1.0)
    plan = ReductionPlan(book.positions_to_reduce(Pair.key, 'BUY', 5.0))
    # SELL the spread = buy leg A, sell leg B. A close reverses it.
    assert [side.value for side in plan.entry_sides()] == ['BUY', 'SELL']


# -- attributing the fill ------------------------------------------------------

def test_a_partial_fill_is_attributed_FIFO_NOT_pro_rata():
    """A 60% fill of a 100-spread reduce over 5, 2 and 93 closes the 5,
    the 2 and 53 of the 93 — it does not take 60% off each. Pro-rata
    would leave three part-closed positions where the trader closed two
    whole ones, and every entry price downstream would be an average of
    things that did not happen."""
    book = Book()
    first = position(book, 'SELL', 5.0, at=1.0)
    second = position(book, 'SELL', 2.0, at=2.0)
    third = position(book, 'SELL', 93.0, at=3.0)
    plan = ReductionPlan(book.positions_to_reduce(Pair.key, 'BUY', 100.0))
    closed, covered = plan.apply(0.60)
    assert covered == 60.0
    assert closed == [first.position_id, second.position_id]
    assert third.quantity == 40.0        # 93 - 53
    assert third.is_open


def test_a_closed_position_gets_a_REAL_timestamp():
    """`is_open` reads `closed_at is None`, so a truthy placeholder
    satisfies the book and then reads as a nonsense time in the
    journal, the slippage window and the monitor."""
    book = Book()
    held = position(book, 'SELL', 5.0, at=1.0)
    plan = ReductionPlan(book.positions_to_reduce(Pair.key, 'BUY', 5.0))
    plan.apply(1.0, clock=lambda: 1_700_000_000.0)
    assert held.closed_at == 1_700_000_000.0
    assert held.is_open is False
    assert 'opposite click' in held.close_reason


def test_a_zero_fill_closes_nothing():
    book = Book()
    held = position(book, 'SELL', 5.0, at=1.0)
    plan = ReductionPlan(book.positions_to_reduce(Pair.key, 'BUY', 5.0))
    assert plan.apply(0.0) == ([], 0.0)
    assert held.quantity == 5.0


# -- reduce_first: the one implementation -------------------------------------

def test_a_full_close_leaves_nothing_to_open():
    book = Book()
    held = position(book, 'SELL', 5.0, at=1.0)
    executor = Executor()
    closed, left, failure = reduce_first(book, executor, Pair(), 'BUY', 5.0,
                                         {})
    assert closed == [held.position_id]
    assert left == 0.0 and failure is None
    # ONE call, not one per position.
    assert len(executor.calls) == 1


def test_what_the_click_could_not_cover_is_left_to_OPEN():
    book = Book()
    position(book, 'SELL', 3.0, at=1.0)
    closed, left, failure = reduce_first(book, Executor(), Pair(), 'BUY',
                                         10.0, {})
    assert len(closed) == 1 and left == 7.0 and failure is None


def test_nothing_to_close_is_not_a_failure():
    book = Book()
    closed, left, failure = reduce_first(book, Executor(), Pair(), 'BUY',
                                         10.0, {})
    assert (closed, left, failure) == ([], 10.0, None)


def test_a_REFUSED_close_opens_NOTHING_and_says_why():
    """A caller that cannot tell a refusal from a quiet no-op opens a
    new position on top of one that just failed to close."""
    book = Book()
    held = position(book, 'SELL', 5.0, at=1.0)
    executor = Executor(ok=False, fraction=0.0,
                        legs={'a': {'ok': False,
                                    'error': 'rms:blocked for gold05dec25f'}})
    closed, left, failure = reduce_first(book, executor, Pair(), 'BUY', 10.0,
                                         {})
    assert closed == []
    assert left == 10.0                  # the WHOLE click is withheld
    assert 'rms:blocked' in failure
    assert held.quantity == 5.0          # and the book is untouched
    # CONTROL: the same click against a working close goes through.
    ok_closed, ok_left, ok_failure = reduce_first(book, Executor(), Pair(),
                                                  'BUY', 10.0, {})
    assert ok_closed and ok_left == 5.0 and ok_failure is None


def test_a_close_that_reported_ok_but_covered_NOTHING_is_a_failure():
    """`imbalanced`: one leg came down and the other's lot step was too
    big to follow it. Counted as covered, the click opens the
    remainder the other way."""
    book = Book()
    position(book, 'SELL', 5.0, at=1.0)
    executor = Executor(ok=True, fraction=1.0, imbalanced=True)
    closed, left, failure = reduce_first(book, executor, Pair(), 'BUY', 5.0,
                                         {})
    assert closed == [] and left == 5.0 and failure is not None


# -- the sentence the trader sees first ---------------------------------------

def test_a_naked_leg_is_the_FIRST_thing_the_message_says():
    book = Book()
    held = position(book, 'SELL', 5.0, at=1.0)
    plan = ReductionPlan(book.positions_to_reduce(Pair.key, 'BUY', 5.0))
    message = close_failure(
        {'ok': False, 'legs': {'a': {'ok': True},
                               'b': {'ok': False, 'error': 'RMS reject'}}},
        plan)
    assert message.startswith('NAKED LEG')
    assert 'leg A' in message and 'RMS reject' in message
    assert held.position_id in message


def test_a_refusal_that_never_reached_the_broker_carries_its_own_words():
    book = Book()
    position(book, 'SELL', 5.0, at=1.0)
    plan = ReductionPlan(book.positions_to_reduce(Pair.key, 'BUY', 5.0))
    message = close_failure({'reason': '0.4 lots is under leg B\'s minimum'},
                            plan)
    assert 'under leg B' in message


def test_a_multi_position_failure_names_how_many():
    book = Book()
    position(book, 'SELL', 5.0, at=1.0)
    position(book, 'SELL', 2.0, at=2.0)
    plan = ReductionPlan(book.positions_to_reduce(Pair.key, 'BUY', 7.0))
    message = close_failure({'ok': False, 'legs': {}}, plan)
    assert '2 positions' in message and '7 spreads' in message


# -- the net ------------------------------------------------------------------

def test_the_average_of_no_trades_is_NONE_not_zero():
    book = Book()
    assert book.net_position(Pair.key) == (0.0, None)


def test_the_net_is_signed_and_the_average_weighted():
    book = Book()
    position(book, 'BUY', 3.0, entry=500.0, at=1.0)
    position(book, 'BUY', 1.0, entry=520.0, at=2.0)
    net, average = book.net_position(Pair.key)
    assert net == 4.0
    assert average == pytest.approx(505.0)
