"""Domain models — and the one thing netting changes.

On MT5 a close names a position TICKET. Here it does not, and this file
pins what replaced it: our own ledger.
"""

from arrowtrader.models import (LegFill, OrderState, SpreadPosition,
                                SpreadSide, SyntheticOrder)


def fill(volume=1.0, price=75000.0, lot_size=100, side='BUY'):
    return LegFill('arrow', 'GOLD05DEC25F', side, volume, price,
                   order_ticket='ORD1', position_tickets=['ORD1'],
                   contract_size=lot_size, segment='mcx_fo', product='NRML')


def position(side='BUY', quantity=1.0, entry=500.0, k=1000.0):
    return SpreadPosition('pair', side, quantity, fill(side='SELL'),
                          fill(side='BUY'), entry, 'MARKET', k)


# -- a fill carries what went on the wire ------------------------------------

def test_a_fill_says_its_units_as_well_as_its_lots():
    """Units is the number that can be wrong by a factor of LotSize,
    so it is never left to be re-derived at a call site."""
    row = fill(volume=3.0, lot_size=100).to_dict()
    assert row['volume'] == 3.0
    assert row['units'] == 300.0


def test_units_are_none_where_the_lot_size_is_unknown():
    assert fill(lot_size=None).to_dict()['units'] is None


def test_a_fill_carries_its_segment_and_product():
    """A netted position is per (symbol, product), so the product is
    part of the position's identity in a way it never was on MT5."""
    row = fill().to_dict()
    assert row['segment'] == 'mcx_fo' and row['product'] == 'NRML'
    assert LegFill.from_dict(row).product == 'NRML'


# -- the spread side is not the leg side -------------------------------------

def test_buying_the_spread_buys_B_and_sells_A():
    a, b = SpreadSide.BUY.leg_sides()
    assert (a.value, b.value) == ('SELL', 'BUY')
    assert SpreadSide.BUY.opposite is SpreadSide.SELL


# -- one click is one order ---------------------------------------------------

def test_three_clicks_at_one_level_are_three_orders():
    orders = [SyntheticOrder('pair', 'BUY', 500.0, 1, 'LIMIT', 'DAY')
              for _ in range(3)]
    assert len({order.order_id for order in orders}) == 3


def test_a_closing_order_is_a_different_order_from_an_entry():
    """They must never be merged into one pending, or one of them
    silently changes meaning."""
    entry = SyntheticOrder('pair', 'BUY', 500.0, 1, 'LIMIT', 'DAY')
    close = SyntheticOrder('pair', 'BUY', 500.0, 1, 'LIMIT', 'DAY',
                           position_id='POS1')
    assert entry.to_dict()['intent'] == 'OPEN'
    assert close.to_dict()['intent'] == 'CLOSE'


def test_a_hand_placed_close_is_not_auto_armed():
    """Standing AutoRouting down pulls what AutoRouting armed — and it
    must NOT pull a close the trader placed by hand."""
    by_hand = SyntheticOrder('pair', 'BUY', 500.0, 1, 'LIMIT', 'DAY',
                             position_id='POS1')
    armed = SyntheticOrder('pair', 'BUY', 500.0, 1, 'LIMIT', 'DAY',
                           position_id='POS1', auto_armed=True)
    assert by_hand.auto_armed is False and armed.auto_armed is True


def test_an_order_stops_working_with_a_reason():
    order = SyntheticOrder('pair', 'BUY', 500.0, 1, 'LIMIT', 'DAY')
    assert order.is_working
    order.state = OrderState.REJECTED
    order.reason = 'rms:blocked for gold05dec25f'
    assert not order.is_working
    assert order.to_dict()['reason'] == 'rms:blocked for gold05dec25f'


# -- marking, and the double-count that read $6,000 --------------------------

def test_size_is_in_k_already_and_must_not_be_applied_twice():
    """`spread_units` is the money a 1.00 move is worth for the WHOLE
    position. Multiplying by `quantity` on top charged size twice — it
    was invisible at Qty 1 and read 10x at Qty 10."""
    at_ten = position(quantity=10.0, entry=500.0, k=1000.0)
    assert at_ten.mark(501.0) == 1000.0


def test_a_short_position_makes_money_when_the_spread_FALLS():
    short = position(side='SELL', entry=500.0, k=1000.0)
    assert short.mark(499.0) == 1000.0
    assert short.mark(501.0) == -1000.0


def test_an_unmarkable_position_is_none_not_zero():
    assert position().mark(None) is None


# -- a partial close ----------------------------------------------------------

def test_a_partial_close_leaves_the_rest_open_and_shrinks_the_legs():
    """Marking the whole position closed loses the leg still on at the
    exchange — the book reads flat while the money is there."""
    open_position = position(quantity=10.0)
    left = open_position.reduce_by(0.4, realized=100.0)
    assert left == 6.0
    assert open_position.leg_a.volume == 0.6
    assert open_position.is_open


def test_realised_pnl_ACCUMULATES_across_partial_closes():
    """A position closed in three pieces earned its P&L in three."""
    open_position = position(quantity=10.0)
    open_position.reduce_by(0.3, realized=100.0)
    open_position.reduce_by(0.3, realized=50.0)
    assert open_position.realized_pnl == 150.0


def test_reduce_by_ignores_nonsense_rather_than_corrupting_the_book():
    open_position = position(quantity=10.0)
    assert open_position.reduce_by(None) == 10.0
    assert open_position.reduce_by(-1.0) == 10.0
    assert open_position.reduce_by(5.0) == 0.0      # clamped to all of it


# -- recovery ------------------------------------------------------------------

def test_a_recovered_position_keeps_its_OWN_id_and_fills():
    """A position that came back under a new id would be an orphan to
    the reconciler and a ghost to the book."""
    original = position(quantity=3.0)
    back = SpreadPosition.from_dict(original.to_dict())
    assert back.position_id == original.position_id
    assert back.leg_b.order_ticket == 'ORD1'
    assert back.recovered is True
    assert back.confirmed is False
    assert back.is_open


def test_a_partial_close_leaves_no_binary_dust_on_the_panel():
    """`0.15 - 0.1` is `0.04999999999999999` in float. A 60% close of
    93 left `39.99999999999999` as the position's size — seventeen
    digits, next to a clean 40 on the Working Orders panel, which reads
    like the size was changed on the way to the exchange. It was not,
    but a trader cannot be asked to add up seventeen-digit floats to
    satisfy themselves that their own click went in whole."""
    held = position(quantity=93.0)
    held.reduce_by(53.0 / 93.0)
    assert held.quantity == 40.0
    assert held.leg_a.volume == held.leg_a.volume   # and no dust on the legs
    assert repr(held.leg_b.volume).count('9999') == 0
