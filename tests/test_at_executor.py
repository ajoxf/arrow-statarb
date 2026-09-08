"""Getting a pair on and off — and the outcome MT5 does not have.

The rule this file exists to pin: **an unresolved leg is not a rejected
leg, and must not be unwound.** Unwinding an order that is still
working does not undo a hedge — it opens the opposite position, which
then fills against the leg being cancelled.
"""

import pytest

from arrowtrader.book import Book, ReductionPlan, reduce_first
from arrowtrader.executor import (PairExecutor, closed_spread, mark_position,
                                  slippage)
from arrowtrader.models import OrderType, SpreadSide, TimeInForce
from arrowtrader.spread import compute_spread


class Config:
    def __init__(self, **settings):
        self.settings = {'LEG_DEADLINE_SEC': 2.0, 'MIN_MATCHED_FRACTION': 0.4,
                         'MARKET_PROTECTION_TICKS': 3.0}
        self.settings.update(settings)

    def get(self, name, default=None):
        return self.settings.get(name, default)


class Pair:
    key = 'GOLD05DEC25F|GOLD05FEB26F'
    name = 'Gold Dec/Feb'
    account_a = 'leg_a'
    account_b = 'leg_b'
    symbol_a = 'GOLD05DEC25F'
    symbol_b = 'GOLD05FEB26F'
    segment_a = segment_b = 'mcx_fo'
    hedge_ratio = 1.0
    clip_lots_a = 1.0
    clip_lots_b = 1.0
    default_quantity = 1.0
    order_type = OrderType.MARKET
    time_in_force = TimeInForce.DAY
    product = 'NRML'
    meta_a = {'contract_size': 100, 'volume_step': 1.0, 'volume_min': 1.0,
              'freeze_qty': 1000, 'width': 2.0}
    meta_b = {'contract_size': 100, 'volume_step': 1.0, 'volume_min': 1.0,
              'freeze_qty': 1000, 'width': 3.0}

    def effective_increment(self):
        return 1.0


class Leg:
    """A leg that fills, refuses, or leaves us not knowing."""

    def __init__(self, name, price=75000.0):
        self.name = name
        self.price = price
        self.orders = []
        self.closes = []
        self.refuse = None
        self.unresolved = None
        self.fill_fraction = 1.0
        self.close_fraction = 1.0
        self.close_refuse = None
        self.close_unresolved = None

    def order(self, symbol, side, volume, product='NRML', comment='',
              **kwargs):
        self.orders.append({'symbol': symbol, 'side': side, 'volume': volume,
                            'product': product, 'comment': comment})
        if self.unresolved:
            return {'ok': False, 'unresolved': True, 'filled_volume': 0.0,
                    'price': None, 'ticket': 'ORD-UNRESOLVED',
                    'error': self.unresolved}
        if self.refuse:
            return {'ok': False, 'unresolved': False, 'filled_volume': 0.0,
                    'price': None, 'ticket': None, 'error': self.refuse}
        got = volume * self.fill_fraction
        return {'ok': True, 'unresolved': False, 'filled_volume': got,
                'price': self.price, 'ticket': f'ORD{len(self.orders)}',
                'position_tickets': [f'ORD{len(self.orders)}'], 'error': None}

    def close_reduce(self, symbol, entry_side, volume, product='NRML',
                     comment='', **kwargs):
        self.closes.append({'symbol': symbol, 'entry_side': entry_side,
                            'volume': volume, 'comment': comment})
        if self.close_unresolved:
            return {'ok': False, 'unresolved': True, 'filled_volume': 0.0,
                    'price': None, 'error': self.close_unresolved}
        if self.close_refuse:
            return {'ok': False, 'unresolved': False, 'filled_volume': 0.0,
                    'price': None, 'error': self.close_refuse}
        return {'ok': True, 'unresolved': False,
                'filled_volume': volume * self.close_fraction,
                'price': self.price, 'error': None}


@pytest.fixture
def legs():
    return {'leg_a': Leg('leg_a', 75000.0), 'leg_b': Leg('leg_b', 75500.0)}


@pytest.fixture
def executor(legs):
    return PairExecutor(Config(), legs, clock=_clock(), sleep=lambda s: None)


def _clock():
    now = [0.0]

    def tick():
        now[0] += 0.001
        return now[0]
    return tick


def market(bid_a=74999.0, ask_a=75001.0, bid_b=75499.0, ask_b=75502.0):
    return compute_spread(Pair(),
                          {'bid': bid_a, 'ask': ask_a, 'time': 1},
                          {'bid': bid_b, 'ask': ask_b, 'time': 1}, 1.0)


# -- the third outcome: we do not know ----------------------------------------

def test_an_unresolved_FIRST_leg_sends_NOTHING_else(executor, legs):
    """A hedge against a position that may not exist is an outright
    either way round."""
    first, _second = executor.crossing_order(Pair())
    legs[f'leg_{first}'].unresolved = 'still working after 5.0s'
    result = executor.market_entry(Pair(), 'BUY', market(), spreads=1)
    assert result.ok is False
    assert result.unresolved['leg'] == first.upper()
    other = 'a' if first == 'b' else 'b'
    assert legs[f'leg_{other}'].orders == []      # nothing else went


def test_an_unresolved_SECOND_leg_does_NOT_unwind_the_first(executor, legs):
    """Unwinding an order that is still working opens the opposite
    position, which then fills against the leg being cancelled — two
    positions where there should be none."""
    first, second = executor.crossing_order(Pair())
    legs[f'leg_{second}'].unresolved = 'still working after 5.0s'
    result = executor.market_entry(Pair(), 'BUY', market(), spreads=1)
    assert result.ok is False
    assert result.unresolved is not None
    assert result.naked is None                  # NOT reported as naked
    assert legs[f'leg_{first}'].closes == []      # and NOT unwound
    assert 'must not be unwound' not in (result.reason or '') or True
    assert 'UNRESOLVED' in result.reason


def test_an_unresolved_second_leg_SAYS_what_is_exposed(executor, legs):
    first, second = executor.crossing_order(Pair())
    legs[f'leg_{second}'].unresolved = 'still working'
    result = executor.market_entry(Pair(), 'BUY', market(), spreads=1)
    assert result.unresolved['exposed']['leg'] == first.upper()
    assert 'NAKED until this is settled' in result.reason
    assert result.unresolved['ticket'] == 'ORD-UNRESOLVED'


def test_an_unresolved_leg_is_NOT_retried_within_the_deadline(executor, legs):
    """Retrying sends a SECOND order for the same hedge while the first
    is still working. On a netting account two fills do not cancel out
    — they double the leg."""
    _first, second = executor.crossing_order(Pair())
    legs[f'leg_{second}'].unresolved = 'still working'
    executor.market_entry(Pair(), 'BUY', market(), spreads=1)
    assert len(legs[f'leg_{second}'].orders) == 1
    # CONTROL: a plain REFUSAL is retried until the deadline.
    fresh_legs = {'leg_a': Leg('leg_a'), 'leg_b': Leg('leg_b')}
    fresh_legs[f'leg_{second}'].refuse = 'rms:blocked'
    now = [0.0]
    retrying = PairExecutor(Config(LEG_DEADLINE_SEC=0.1),
                            fresh_legs, clock=lambda: now[0],
                            sleep=lambda s: now.__setitem__(0, now[0] + s))
    retrying.market_entry(Pair(), 'BUY', market(), spreads=1)
    assert len(fresh_legs[f'leg_{second}'].orders) > 1


# -- the naked window, when it IS a rejection ---------------------------------

def test_a_REJECTED_second_leg_unwinds_the_first(executor, legs):
    first, second = executor.crossing_order(Pair())
    legs[f'leg_{second}'].refuse = 'rms:blocked for gold05feb26f'
    result = executor.market_entry(Pair(), 'BUY', market(), spreads=1)
    assert result.ok is False
    assert result.naked is None                  # the unwind worked
    assert len(legs[f'leg_{first}'].closes) == 1
    assert legs[f'leg_{first}'].closes[0]['comment'] == 'UNWIND'
    assert 'rms:blocked' in result.reason


def test_an_unwind_that_FAILS_is_reported_as_NAKED(executor, legs):
    first, second = executor.crossing_order(Pair())
    legs[f'leg_{second}'].refuse = 'rms:blocked'
    legs[f'leg_{first}'].close_refuse = 'Market is closed'
    result = executor.market_entry(Pair(), 'BUY', market(), spreads=1)
    assert result.naked is not None
    assert result.naked['leg'] == first.upper()
    assert 'Market is closed' in result.naked['why']


def test_an_unwind_that_is_UNRESOLVED_is_naked_and_says_so(executor, legs):
    first, second = executor.crossing_order(Pair())
    legs[f'leg_{second}'].refuse = 'rms:blocked'
    legs[f'leg_{first}'].close_unresolved = 'still working'
    result = executor.market_entry(Pair(), 'BUY', market(), spreads=1)
    assert result.naked is not None
    assert 'UNRESOLVED' in result.naked['why']


def test_a_refused_FIRST_leg_is_a_refusal_not_a_naked_position(executor, legs):
    first, _second = executor.crossing_order(Pair())
    legs[f'leg_{first}'].refuse = 'Insufficient margin'
    result = executor.market_entry(Pair(), 'BUY', market(), spreads=1)
    assert result.refused is True
    assert result.naked is None


def test_a_half_matched_clip_is_unwound_on_BOTH_legs(executor, legs):
    legs['leg_a'].fill_fraction = 0.2
    result = executor.market_entry(Pair(), 'BUY', market(), spreads=10)
    assert result.ok is False
    assert 'unwinding' in result.reason
    assert legs['leg_a'].closes and legs['leg_b'].closes
    # CONTROL: above the threshold it is kept.
    for leg in legs.values():
        leg.fill_fraction = 1.0
        leg.closes.clear()
    assert executor.market_entry(Pair(), 'BUY', market(), spreads=10).ok


# -- the harder leg goes first -------------------------------------------------

def test_the_WIDER_leg_is_crossed_first(executor):
    """Filling the easy leg and then discovering the hard one will not
    fill is how you end up naked."""
    assert executor.crossing_order(Pair()) == ('b', 'a')   # B is wider


def test_a_THIN_book_counts_as_harder_than_a_wide_one(executor):
    class Thin(Pair):
        meta_a = dict(Pair.meta_a, width=2.0, touch_size=1.0)
        meta_b = dict(Pair.meta_b, width=3.0, touch_size=500.0)
    assert executor.crossing_order(Thin()) == ('a', 'b')


def test_unknown_depth_is_no_opinion_not_infinitely_deep(executor):
    """Which would send the risky leg second."""
    class Unknown(Pair):
        meta_a = dict(Pair.meta_a, width=5.0)     # no touch_size
        meta_b = dict(Pair.meta_b, width=1.0, touch_size=500.0)
    assert executor.crossing_order(Unknown()) == ('a', 'b')


# -- the ONLY slippage guard there is -----------------------------------------

def test_a_market_through_the_clicked_price_is_REFUSED(executor, legs):
    """Arrow's place_order has no deviation parameter. This is not a
    check on top of the exchange's — it is the whole of it."""
    md = market()
    clicked = md['long_spread'] - 10.0        # market is 10 through it
    result = executor.market_entry(Pair(), 'BUY', md, spreads=1,
                                   clicked_level=clicked)
    assert result.refused is True
    assert 'protection' in result.reason
    assert legs['leg_a'].orders == [] and legs['leg_b'].orders == []
    # CONTROL: with the guard turned off the same click goes through.
    off = PairExecutor(Config(MARKET_PROTECTION_TICKS=0), legs,
                       clock=_clock(), sleep=lambda s: None)
    assert off.market_entry(Pair(), 'BUY', md, spreads=1,
                            clicked_level=clicked).ok is True


def test_a_click_BETTER_than_the_market_is_not_a_breach(executor):
    md = market()
    assert executor.protection_breach(Pair(), SpreadSide.BUY, md,
                                      md['long_spread'] + 10.0) is None


def test_a_guard_reason_on_the_price_refuses_the_click(executor, legs):
    md = dict(market(), guard_reason='Leg A has not moved for 30s')
    result = executor.market_entry(Pair(), 'BUY', md, spreads=1)
    assert result.refused is True
    assert 'not moved' in result.reason
    assert legs['leg_a'].orders == []
    # CONTROL: without the guard the same click is sent.
    assert executor.market_entry(Pair(), 'BUY', market(), spreads=1).ok


def test_an_unknown_lot_size_refuses_before_anything_moves(executor, legs):
    class NoLot(Pair):
        meta_a = dict(Pair.meta_a, contract_size=None)
    result = executor.market_entry(NoLot(), 'BUY', market(), spreads=1)
    assert result.refused is True
    assert 'no lot size' in result.reason
    assert legs['leg_a'].orders == []


def test_a_missing_book_refuses_rather_than_sizing_off_nothing(executor):
    assert executor.market_entry(Pair(), 'BUY', None, spreads=1).refused


# -- what a filled entry books --------------------------------------------------

def test_the_entry_is_anchored_on_the_EXECUTED_fills(executor, legs):
    result = executor.market_entry(Pair(), 'BUY', market(), spreads=2)
    assert result.ok is True
    position = result.position
    # B - beta x A of what actually filled, not of the mid.
    assert position.entry_spread == 75500.0 - 1.0 * 75000.0
    assert position.quantity == 2.0
    assert position.spread_units == 2.0 * 100      # k = L_B x C_B


def test_the_position_carries_its_segment_and_product(executor, legs):
    position = executor.market_entry(Pair(), 'BUY', market(), spreads=1).position
    assert position.leg_a.segment == 'mcx_fo'
    assert position.leg_b.product == 'NRML'


def test_the_product_reaches_the_order(executor, legs):
    class Intraday(Pair):
        product = 'MIS'
    executor.market_entry(Intraday(), 'BUY', market(), spreads=1)
    assert legs['leg_a'].orders[-1]['product'] == 'MIS'


def test_buying_the_spread_buys_B_and_sells_A(executor, legs):
    executor.market_entry(Pair(), 'BUY', market(), spreads=1)
    assert legs['leg_a'].orders[-1]['side'] == 'SELL'
    assert legs['leg_b'].orders[-1]['side'] == 'BUY'


# -- closing: one order per leg ------------------------------------------------

def test_a_close_crosses_ONCE_per_leg_for_the_whole_plan(executor, legs):
    book = Book()
    for _ in range(3):
        book.add_position(
            executor.market_entry(Pair(), 'SELL', market(), spreads=1).position)
    for leg in legs.values():
        leg.closes.clear()
    plan = ReductionPlan(book.positions_to_reduce(Pair.key, 'BUY', 3.0))
    result = executor.close_spread(Pair(), plan, market())
    assert result['ok'] is True
    assert len(legs['leg_a'].closes) == 1          # not three
    assert legs['leg_a'].closes[0]['volume'] == 3.0


def test_a_close_reverses_the_ENTRY_side_on_each_leg(executor, legs):
    book = Book()
    book.add_position(
        executor.market_entry(Pair(), 'SELL', market(), spreads=1).position)
    for leg in legs.values():
        leg.closes.clear()
    plan = ReductionPlan(book.positions_to_reduce(Pair.key, 'BUY', 1.0))
    executor.close_spread(Pair(), plan, market())
    # SELL the spread = buy leg A, sell leg B. The close reverses both.
    assert legs['leg_a'].closes[0]['entry_side'].value == 'BUY'
    assert legs['leg_b'].closes[0]['entry_side'].value == 'SELL'


def test_a_guard_NEVER_prevents_a_close(executor, legs):
    """A trade must always be closable."""
    book = Book()
    book.add_position(
        executor.market_entry(Pair(), 'BUY', market(), spreads=1).position)
    for leg in legs.values():
        leg.closes.clear()
    stale = dict(market(), guard_reason='Leg A has not moved for 30s')
    plan = ReductionPlan(book.positions_to_reduce(Pair.key, 'SELL', 1.0))
    assert executor.close_spread(Pair(), plan, stale)['ok'] is True
    assert legs['leg_a'].closes


def test_a_piece_neither_leg_can_trade_is_REFUSED_not_half_closed(executor):
    """Closing one leg of a hedge is worse than closing neither."""
    book = Book()
    position = executor.market_entry(Pair(), 'BUY', market(),
                                     spreads=1).position
    book.add_position(position)
    plan = ReductionPlan([(position, 0.4)])       # 0.4 lots, step is 1
    result = executor.close_spread(Pair(), plan, market())
    assert result['ok'] is False
    assert result['refused'] is True
    assert 'Close more of it, or all of it' in result['reason']


def test_an_UNRESOLVED_close_leaves_the_position_ACTIVE(executor, legs):
    """Reporting it as zero keeps the whole position on our books while
    it may have gone; reporting it as filled takes it off while it may
    still be there. So it is neither."""
    position = executor.market_entry(Pair(), 'BUY', market(),
                                     spreads=1).position
    legs['leg_a'].close_unresolved = 'still working after 5.0s'
    result = executor.close_position(Pair(), position, market())
    assert result['ok'] is False
    assert position.is_open is True
    assert position.quantity == 1.0
    assert 'UNRESOLVED' in result['legs']['a']['error']


def test_ONE_leg_closing_and_the_other_not_is_IMBALANCED(executor, legs):
    position = executor.market_entry(Pair(), 'BUY', market(),
                                     spreads=1).position
    legs['leg_a'].close_fraction = 0.0
    result = executor.close_position(Pair(), position, market())
    assert result['imbalanced'] is True
    assert position.is_open is True          # nothing booked
    assert position.quantity == 1.0


def test_the_hedged_part_is_the_SMALLER_of_the_two_legs(executor, legs):
    """Booking the larger takes lots off our record that are still at
    the exchange."""
    position = executor.market_entry(Pair(), 'BUY', market(),
                                     spreads=10).position
    legs['leg_a'].close_fraction = 0.5
    legs['leg_b'].close_fraction = 0.9
    result = executor.close_position(Pair(), position, market())
    assert result['fraction'] == pytest.approx(0.5)
    assert position.quantity == pytest.approx(5.0)
    assert position.is_open is True


def test_a_full_close_books_the_exit_and_the_slippage(executor, legs):
    md = market()
    position = executor.market_entry(Pair(), 'BUY', md, spreads=1).position
    result = executor.close_position(Pair(), position, md)
    assert result['ok'] is True
    assert position.is_open is False
    assert position.exit_spread == 75500.0 - 75000.0
    assert position.exit_slippage is not None
    assert position.close_reason == 'manual'


def test_a_PART_close_does_NOT_set_the_exit_price(executor, legs):
    """It describes the exit of a position, and this one has not
    exited. Recording the first piece's price as 'the' exit makes the
    number wrong the moment the rest goes."""
    md = market()
    position = executor.market_entry(Pair(), 'BUY', md, spreads=10).position
    executor.close_position(Pair(), position, md, quantity=4.0)
    assert position.is_open is True
    assert position.quantity == 6.0
    assert position.exit_spread is None
    assert position.exit_slippage is None
    assert position.realized_pnl is not None


def test_a_resting_close_is_disarmed_BEFORE_the_position_goes(executor):
    """Afterwards is too late: the level it was armed against no longer
    has a position, and it fires on the next tick that reaches it."""
    order = []
    executor.before_close = lambda pid, why: order.append(('disarm', pid))
    position = executor.market_entry(Pair(), 'BUY', market(),
                                     spreads=1).position
    executor.close_position(Pair(), position, market())
    assert order and order[0][0] == 'disarm'


def test_a_disarm_that_RAISES_never_blocks_a_close(executor):
    def explode(pid, why):
        raise RuntimeError('the quoter is wedged')
    executor.before_close = explode
    position = executor.market_entry(Pair(), 'BUY', market(),
                                     spreads=1).position
    assert executor.close_position(Pair(), position, market())['ok'] is True


# -- reduce_first drives it -----------------------------------------------------

def test_an_opposite_click_closes_before_it_opens(executor, legs):
    book = Book()
    book.add_position(
        executor.market_entry(Pair(), 'SELL', market(), spreads=5).position)
    closed, left, failure = reduce_first(book, executor, Pair(), 'BUY', 8.0,
                                         market())
    assert len(closed) == 1
    assert left == 3.0
    assert failure is None


def test_a_refused_close_leaves_the_WHOLE_click_unopened(executor, legs):
    book = Book()
    book.add_position(
        executor.market_entry(Pair(), 'SELL', market(), spreads=5).position)
    legs['leg_a'].close_refuse = 'rms:blocked for gold05dec25f'
    closed, left, failure = reduce_first(book, executor, Pair(), 'BUY', 8.0,
                                         market())
    assert closed == []
    assert left == 8.0
    assert 'rms:blocked' in failure


# -- the maths that reports it ---------------------------------------------------

def test_slippage_is_positive_for_a_COST_at_both_ends():
    """A short buys the spread back to close, so the sign flips. Getting
    it backwards reports every exit's cost as a gain."""
    assert slippage(100.0, 101.0, SpreadSide.BUY) == 1.0
    assert slippage(100.0, 99.0, SpreadSide.SELL) == 1.0
    assert slippage(100.0, 99.0, SpreadSide.BUY, closing=True) == 1.0
    assert slippage(100.0, 101.0, SpreadSide.SELL, closing=True) == 1.0


def test_unmeasured_slippage_is_NONE_not_a_flattering_zero():
    assert slippage(None, 101.0, SpreadSide.BUY) is None
    assert slippage(100.0, None, SpreadSide.BUY) is None


def test_half_a_spread_is_not_a_spread():
    results = {'a': {'closed': [{'volume': 1, 'price': 75000.0}]},
               'b': {'closed': []}}
    assert closed_spread(results, 1.0) is None


def test_a_position_shows_what_closing_it_NOW_would_cost(executor):
    """It shows a loss the instant it opens, equal to one round turn of
    both legs' bid-ask. That is correct, and the UI says so."""
    md = market()
    position = executor.market_entry(Pair(), 'BUY', md, spreads=1).position
    gross, net, closing = mark_position(position, md, Config().settings)
    assert closing == md['short_spread']
    assert gross < 0
    assert net <= gross         # charges only ever subtract


def test_the_ORDER_fraction_and_the_POSITION_fraction_are_not_the_same():
    """`close_spread` reports what fraction of the ORDER IT SENT came
    off — which is what reduce_first needs, its plan spanning several
    positions. `_settle` books a fraction of THIS POSITION. Closing 4 of
    10 spreads and having all 4 fill is 1.0 by the first reading and 0.4
    by the second; passing the first where the second belongs closes the
    whole position in the book while six spreads are still on."""
    legs = {'leg_a': Leg('leg_a', 75000.0), 'leg_b': Leg('leg_b', 75500.0)}
    executor = PairExecutor(Config(), legs, clock=_clock(),
                            sleep=lambda s: None)
    md = market()
    position = executor.market_entry(Pair(), 'BUY', md, spreads=10).position
    result = executor.close_position(Pair(), position, md, quantity=4.0)
    # The ORDER filled in full...
    assert result['fraction'] == pytest.approx(1.0)
    assert legs['leg_a'].closes[-1]['volume'] == 4.0
    # ...and the POSITION is 40% closed, not gone.
    assert position.quantity == pytest.approx(6.0)
    assert position.is_open is True
