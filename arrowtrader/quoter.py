"""LIMIT mode: quote one leg, cross the other — entries AND closes.

Ported from MT5-Trader's `quoter.py`, and the port is where that
module's central asymmetry disappears.

## WHAT WAS TWO PATHS IS NOW ONE

MT5-Trader has to keep two kinds of resting order and they are not
symmetrical. An ENTRY is backed by a real pending on one leg. A CLOSE
is backed by NOTHING at the broker: it is a level watched in memory and
a close by ticket when the market reaches it.

That asymmetry was forced, not chosen. MT5 honours `position` on
TRADE_ACTION_DEAL and ignores it on TRADE_ACTION_PENDING, so a
"closing" limit rests as an ordinary limit — and on a hedging account
an ordinary opposite limit OPENS a second position. Live 2026-09-02:
ticket 2092 was rested to close ticket 2090 and filled as a BUY 0.01
beside the SELL 0.01 it was meant to close. The engine believed that
leg was flat, closed the other leg on the strength of it, and the
reconciler swept both futures as orphans a minute later.

**On a netting exchange an opposite resting limit REDUCES the net.**
That is what it looks like it does. So a close rests at the exchange
like any other order, and there is one path here instead of two.

Three things follow, and the screen has to say all of them:

1. A resting close is REAL. It sits in the exchange's book, it can be
   seen in the broker's own terminal, and it earns the level rather
   than crossing at the touch when it is reached.
2. It SURVIVES this process — as a leg order. The SPREAD does not: the
   other leg is only crossed while this is running, so a close that
   fills while we are down leaves an outright. That is the sweep's job
   and it is why the sweep runs at startup as well as at shutdown.
3. It consumes the position it closes even if we never see the fill.
   The reconciler is what notices, and it reads a shortfall rather than
   a ghost.

## THE PEG, WHICH IS UNCHANGED

The trader clicked a SPREAD level, but the order lives on a LEG, and
the price that produces the clicked spread depends on where the OTHER
leg is right now:

    quoting B, SELL the spread at S:   P_B = S + beta * ask_A
    quoting B, BUY  the spread at S:   P_B = S + beta * bid_A
    quoting A, SELL the spread at S:   P_A = (bid_B - S) / beta
    quoting A, BUY  the spread at S:   P_A = (ask_B - S) / beta

So the order is re-priced to hold the implied spread at the clicked
level — chasing the OTHER leg, for its whole life. A peg anchored on
its own leg's book chases the market away from itself, never fills,
times out and crosses: in the stat-arb system that was 13.96 seconds
from click to pair-on against 24ms for the market path, and +0.4700 of
slippage.

Three rules, each with a test:

1. **Re-peg on a dead band, not on every tick.** Every modify loses
   queue position; re-pricing three times a second guarantees you are
   never at the front of a queue, which defeats the entire point.
2. **Re-peg by MODIFY, never cancel-and-replace** — one order id for
   the order's life. Where the SDK exposes no modify at all this says
   so rather than silently falling back, because it changes what the
   peg costs.
3. **Every modify can race a fill.** Check for the fill BEFORE
   modifying, or you re-price an order that has already become a naked
   position.
"""

import logging

from . import sizing
from .book import ReductionPlan, close_failure
from .executor import slippage
from .models import (LegFill, OrderState, OrderType, SpreadPosition,
                     SpreadSide, new_id)


def quoting_leg(pair):
    """Which leg rests the order.

    Default: the leg with the WIDER bid-ask — that is the spread being
    earned. The tension is real and the ladder shows both widths, so
    the choice is made from measurement: the wider leg is usually the
    less liquid one, where you queue longest and fill least.
    """
    if pair.quoting_leg in ('a', 'b'):
        return pair.quoting_leg
    width_a = (pair.meta_a or {}).get('width') or 0.0
    width_b = (pair.meta_b or {}).get('width') or 0.0
    return 'b' if width_b >= width_a else 'a'


def peg_price(pair, md, side, level, leg):
    """The quoting leg's price that makes the spread equal `level`.

    Returns (price, order side on that leg). Anchored on the OTHER
    leg's touch — the whole point of the module.
    """
    beta = float(pair.hedge_ratio or 1.0)
    side = SpreadSide(getattr(side, 'value', side))
    leg_a_side, leg_b_side = side.leg_sides()
    if leg == 'b':
        # The other leg is A, and we cross it when this fills — so the
        # anchor is the touch we would PAY on A.
        anchor = (md['leg_a_ask'] if side is SpreadSide.SELL
                  else md['leg_a_bid'])
        return level + beta * anchor, leg_b_side
    anchor = md['leg_b_bid'] if side is SpreadSide.SELL else md['leg_b_ask']
    return (anchor - level) / beta, leg_a_side


def implied_spread(pair, md, side, leg, price):
    """What spread an order resting at `price` currently implies."""
    beta = float(pair.hedge_ratio or 1.0)
    side = SpreadSide(getattr(side, 'value', side))
    if leg == 'b':
        anchor = (md['leg_a_ask'] if side is SpreadSide.SELL
                  else md['leg_a_bid'])
        return price - beta * anchor
    anchor = md['leg_b_bid'] if side is SpreadSide.SELL else md['leg_b_ask']
    return anchor - beta * price


class QuoteGroup:
    """One resting order at one level, backed by a REAL exchange order.

    Entries and closes are the same object here. `position_id` is what
    distinguishes them, and it changes what a FILL means — an entry
    opens a position, a close consumes one — but not how the order is
    placed, pegged, or pulled.
    """

    def __init__(self, pair_key, side, level, leg, position_id=None):
        self.group_id = new_id('QG')
        self.pair_key = pair_key
        self.side = SpreadSide(getattr(side, 'value', side))
        self.level = float(level)
        self.leg = leg
        #: The position this CLOSES, when it closes one.
        self.position_id = position_id
        self.orders = []            # the synthetics behind it
        self.ticket = None          # the exchange's own order id
        self.price = None           # what it is resting at
        self.volume = 0.0           # lots on the quoting leg
        self.filled = 0.0
        self.held_off = None        # why it is not resting, if it is not
        self.repegs = 0

    @property
    def quantity(self):
        return sizing.tidy(sum(order.remaining for order in self.orders
                               if order.is_working))

    @property
    def closing(self):
        return self.position_id is not None

    def to_dict(self):
        return {'group_id': self.group_id, 'pair_key': self.pair_key,
                'side': self.side.value, 'level': self.level,
                'leg': self.leg, 'position_id': self.position_id,
                'intent': 'CLOSE' if self.closing else 'OPEN',
                'ticket': self.ticket, 'price': self.price,
                'volume': self.volume, 'quantity': self.quantity,
                'filled': self.filled, 'held_off': self.held_off,
                'repegs': self.repegs,
                # ON THE SCREEN, because it is the thing that changed:
                # a resting close is now real, and it outlives us.
                'rests_at_exchange': True}


class Quoter:
    """Holds every LIMIT-mode group and works them each poll."""

    def __init__(self, config, legs, executor, book, clock=None):
        self.config = config
        self.legs = legs
        self.executor = executor
        self.book = book
        self.clock = clock or executor.clock
        self.groups = {}            # key -> QuoteGroup
        #: Click-to-hedged times for the crossing leg, so the operator
        #: can see what the peg actually costs against the market path.
        self.hedge_times = []

    # -- keys ---------------------------------------------------------------

    def key_for(self, order):
        """Orders MERGE into one exchange order only when they mean the
        same thing.

        An entry and a close at the same level on the same side are NOT
        the same order: one opens and one consumes, and merging them
        would make a fill mean two different things at once. The
        position id is therefore part of the key.
        """
        return (order.pair_key, order.side.value, round(order.level, 9),
                order.position_id)

    def group_for(self, pair, order):
        key = self.key_for(order)
        group = self.groups.get(key)
        if group is None:
            group = QuoteGroup(pair.key, order.side, order.level,
                               quoting_leg(pair),
                               position_id=order.position_id)
            self.groups[key] = group
        if order not in group.orders:
            group.orders.append(order)
        return group

    # -- the pass -------------------------------------------------------------

    def work(self, pair, md):
        """One poll for one pair: rest, re-peg, and act on fills."""
        for order in self.book.orders(pair.key):
            if order.order_type is not OrderType.LIMIT:
                continue
            self.group_for(pair, order)

        for key, group in list(self.groups.items()):
            if group.pair_key != pair.key:
                continue
            if group.quantity <= 0:
                # Nothing behind it any more. Pull whatever is resting.
                self._pull(pair, group, 'no working orders left')
                self.groups.pop(key, None)
                continue
            # THE FILL IS CHECKED BEFORE ANYTHING IS MODIFIED. Every
            # re-peg can race a fill, and re-pricing an order that has
            # already become a naked leg is the worst outcome here.
            if group.ticket is not None and self._check_fill(pair, md, group):
                continue
            self._rest_or_repeg(pair, md, group)

    def _rest_or_repeg(self, pair, md, group):
        if not md:
            return self._hold_off(group, 'no price')
        guard = md.get('guard_reason')
        if guard and not group.closing:
            # A guard may withhold an ORDER. It never withholds a
            # CLOSE, so a resting close keeps working through a stale
            # feed — the level is the trader's, not the market's.
            return self._hold_off(group, guard)

        wanted = self._wanted_volume(pair, group)
        if wanted is None or wanted <= 0:
            return self._hold_off(
                group, f'{group.quantity:g} spreads is under leg '
                       f'{group.leg.upper()}\'s one-lot minimum')

        price, order_side = peg_price(pair, md, group.side, group.level,
                                      group.leg)
        leg = self._leg(pair, group.leg)
        symbol = self._symbol(pair, group.leg)
        if leg is None:
            return self._hold_off(group, f'no session for {symbol}')

        if group.ticket is None:
            result = leg.place_limit(symbol, order_side.value, wanted, price,
                                     product=self._product(pair),
                                     comment=f'{group.group_id}:'
                                             f'{group.leg.upper()}')
            if not result.get('ok'):
                return self._hold_off(group, result.get('error'))
            group.ticket = result.get('ticket')
            group.price = result.get('price', price)
            group.volume = wanted
            group.held_off = None
            return None

        if abs(wanted - group.volume) > 1e-9:
            # A RESIZE cannot be a modify: the quantity is not what a
            # peg changes, and an amend that silently kept the old size
            # would rest a different order from the one the book holds.
            return self._resize(pair, leg, symbol, group, order_side, price,
                                wanted)

        if abs(price - (group.price or price)) < self._dead_band(pair):
            # Inside the dead band. Every modify loses queue position,
            # and re-pricing three times a second guarantees you are
            # never at the front of one.
            return None
        answer = leg.modify_order(group.ticket, price, symbol=symbol)
        if not answer.get('ok'):
            if answer.get('amend_unsupported'):
                # SAID, not silently worked around. Falling back to
                # cancel-and-replace changes what the peg costs, and
                # the screen has to be able to show that.
                return self._hold_off(
                    group, 'this build of the Arrow SDK cannot re-price a '
                           'resting order, so this level cannot chase the '
                           'other leg — it is resting where it was placed')
            return self._hold_off(group, answer.get('error'))
        group.price = answer.get('price', price)
        group.repegs += 1
        group.held_off = None
        return None

    def _resize(self, pair, leg, symbol, group, order_side, price, volume):
        cancelled = leg.cancel_order(group.ticket)
        if cancelled.get('filled_volume'):
            # It filled on the way out. That is a fill, not a resize.
            return self._check_fill(pair, None, group)
        group.ticket = None
        group.volume = 0.0
        result = leg.place_limit(symbol, order_side.value, volume, price,
                                 product=self._product(pair),
                                 comment=f'{group.group_id}:'
                                         f'{group.leg.upper()}')
        if not result.get('ok'):
            return self._hold_off(group, result.get('error'))
        group.ticket = result.get('ticket')
        group.price = result.get('price', price)
        group.volume = volume
        return None

    def _wanted_volume(self, pair, group):
        """Whole lots on the quoting leg for what this group still wants."""
        unit = (pair.clip_lots_a if group.leg == 'a' else pair.clip_lots_b)
        meta = (pair.meta_a if group.leg == 'a' else pair.meta_b) or {}
        lots = float(unit or 1.0) * float(group.quantity or 0.0)
        return sizing.round_step(lots, meta.get('volume_step') or 1.0,
                                 meta.get('volume_min') or 1.0, down=True)

    def _hold_off(self, group, reason):
        if group.held_off != reason:
            group.held_off = reason
            logging.info('quoter: %s held off — %s', group.group_id, reason)
        return None

    def _dead_band(self, pair):
        ticks = float(self.config.get('REPEG_DEAD_BAND_TICKS', 1.0) or 0.0)
        increment = pair.effective_increment() or 0.0
        return ticks * increment

    # -- fills ----------------------------------------------------------------

    def _check_fill(self, pair, md, group):
        """Did the resting order fill? Cross the other leg if it did.

        Returns True when the group was acted on and must not be
        re-pegged this pass.
        """
        leg = self._leg(pair, group.leg)
        if leg is None:
            return False
        state = leg.order_state(group.ticket)
        if not state.get('ok'):
            return False
        filled_units = float(state.get('filled_volume') or 0.0)
        if filled_units <= 0:
            if state.get('status') in ('REJECTED', 'CANCELLED'):
                self._died(pair, group, state)
                return True
            return False
        lot_size = leg.lot_size(self._symbol(pair, group.leg))
        if not lot_size:
            # We cannot say how many LOTS filled, so we cannot size the
            # crossing leg. Refusing to guess is the only safe move:
            # the alternative hedges the wrong size against a real fill.
            self._hold_off(group, 'the quoting leg filled but its lot size '
                                  'is unknown, so the hedge cannot be sized '
                                  '— cross it by hand')
            return True
        lots = filled_units / float(lot_size)
        new_lots = sizing.tidy(lots - group.filled)
        if new_lots <= 1e-9:
            return False
        group.filled = sizing.tidy(lots)
        return self._on_fill(pair, md, group, new_lots, state)

    def _on_fill(self, pair, md, group, lots, state):
        """The quoting leg filled. Cross the other leg IMMEDIATELY."""
        started = self.clock()
        other = 'a' if group.leg == 'b' else 'b'
        leg_a_side, leg_b_side = group.side.leg_sides()
        other_side = leg_a_side if other == 'a' else leg_b_side
        ratio = ((pair.clip_lots_a / pair.clip_lots_b) if other == 'a'
                 else (pair.clip_lots_b / pair.clip_lots_a))
        other_meta = (pair.meta_a if other == 'a' else pair.meta_b) or {}
        other_lots = sizing.round_step(lots * ratio,
                                       other_meta.get('volume_step') or 1.0,
                                       other_meta.get('volume_min') or 1.0,
                                       down=True)
        runner = self._leg(pair, other)
        symbol = self._symbol(pair, other)
        if runner is None or other_lots <= 0:
            logging.critical(
                '%s: the quoting leg filled %s lots and the hedge could not '
                'be sized — leg %s is NAKED', pair.key, lots, group.leg.upper())
            return True
        cross = runner.order(symbol, other_side.value, other_lots,
                             product=self._product(pair),
                             comment=f'{group.group_id}:{other.upper()}')
        self.hedge_times.append((self.clock() - started) * 1000.0)

        if cross.get('unresolved'):
            # The quoting leg IS on and we do not know about the hedge.
            # Nothing is unwound — an order still working cannot be
            # cancelled by trading against it.
            logging.critical(
                '%s: quoting leg %s filled %s lots; the crossing leg is '
                'UNRESOLVED (%s). NOTHING has been unwound. Leg %s is naked '
                'until this is settled.',
                pair.key, group.leg.upper(), lots, cross.get('error'),
                group.leg.upper())
            return True
        if not cross.get('ok'):
            logging.critical(
                '%s: quoting leg %s filled %s lots and the crossing leg was '
                'REJECTED (%s) — unwinding the quoting leg',
                pair.key, group.leg.upper(), lots, cross.get('error'))
            quoting_side = leg_a_side if group.leg == 'a' else leg_b_side
            self._leg(pair, group.leg).close_reduce(
                self._symbol(pair, group.leg), quoting_side, lots,
                product=self._product(pair), comment='UNWIND')
            return True

        if group.closing:
            self._book_close(pair, md, group, lots, state, cross)
        else:
            self._book_entry(pair, md, group, lots, state, cross)
        return True

    def _book_entry(self, pair, md, group, lots, state, cross):
        """A resting ENTRY filled: a position is on."""
        fills = {group.leg: {'filled_volume': lots,
                             'price': state.get('price'),
                             'ticket': group.ticket},
                 ('a' if group.leg == 'b' else 'b'): cross}
        leg_a_side, leg_b_side = group.side.leg_sides()
        contract_a = (pair.meta_a or {}).get('contract_size')
        contract_b = (pair.meta_b or {}).get('contract_size')
        leg_a = LegFill(pair.account_a, pair.symbol_a, leg_a_side,
                        fills['a'].get('filled_volume') or 0.0,
                        fills['a'].get('price'),
                        order_ticket=fills['a'].get('ticket'),
                        contract_size=contract_a, clock=self.clock,
                        segment=pair.segment_a, product=self._product(pair))
        leg_b = LegFill(pair.account_b, pair.symbol_b, leg_b_side,
                        fills['b'].get('filled_volume') or 0.0,
                        fills['b'].get('price'),
                        order_ticket=fills['b'].get('ticket'),
                        contract_size=contract_b, clock=self.clock,
                        segment=pair.segment_b, product=self._product(pair))
        entry_spread = None
        if leg_a.price is not None and leg_b.price is not None:
            entry_spread = leg_b.price - float(pair.hedge_ratio) * leg_a.price
        position = SpreadPosition(
            pair.key, group.side, lots, leg_a, leg_b, entry_spread,
            OrderType.LIMIT,
            sizing.spread_units(leg_b.volume, contract_b), clock=self.clock)
        # The level the trader CLICKED is what a resting order's
        # slippage is measured against — not the touch, which is where
        # a market order would have gone and is the thing this path
        # exists to beat.
        position.entry_slippage = slippage(group.level, entry_spread,
                                           group.side)
        self.book.add_position(position)
        self._settle(group, lots)
        return position

    def _book_close(self, pair, md, group, lots, state, cross):
        """A resting CLOSE filled: a position is consumed.

        On MT5 this could not exist. Here it is an ordinary fill.
        """
        position = self.book.position(group.position_id)
        if position is None:
            logging.error(
                '%s: a resting close filled for %s lots but position %s is '
                'gone — the exchange has reduced a net that our book no '
                'longer explains. The reconciler will report it.',
                pair.key, lots, group.position_id)
            self._settle(group, lots)
            return None
        plan = ReductionPlan([(position, min(lots, position.quantity))],
                             pair=pair)
        filled_spread = None
        if state.get('price') is not None and cross.get('price') is not None:
            prices = {group.leg: state['price'],
                      ('a' if group.leg == 'b' else 'b'): cross['price']}
            filled_spread = (prices['b']
                             - float(pair.hedge_ratio) * prices['a'])
        result = {'ok': True, 'fraction': 1.0, 'imbalanced': False,
                  'legs': {}, 'closed_spread': filled_spread}
        # `disarm=False`: THIS caller owns the resting order. Disarming
        # here would mark a close that WORKED as cancelled.
        self.executor._settle(pair, position, plan, result, md,
                              'closed at the resting level',
                              min(1.0, lots / max(position.quantity, 1e-9)))
        self._settle(group, lots)
        return position

    def _settle(self, group, lots):
        """Take the filled quantity off the synthetics behind the group."""
        left = lots
        for order in group.orders:
            if left <= 1e-9 or not order.is_working:
                continue
            take = min(order.remaining, left)
            order.filled_quantity = sizing.tidy(order.filled_quantity + take)
            left = sizing.tidy(left - take)
            if order.remaining <= 1e-9:
                order.state = OrderState.FILLED

    def _died(self, pair, group, state):
        """The exchange rejected or cancelled our resting order."""
        reason = state.get('error') or 'the exchange cancelled it'
        for order in group.orders:
            if order.is_working:
                order.state = OrderState.REJECTED
                order.reason = reason
        group.ticket = None
        self.groups.pop(self.key_for_group(group), None)
        logging.warning('%s: resting order %s died — %s', pair.key,
                        group.group_id, reason)

    # -- pulling ---------------------------------------------------------------

    def _pull(self, pair, group, reason):
        if group.ticket is None:
            return {'ok': True}
        leg = self._leg(pair, group.leg)
        if leg is None:
            return {'ok': False, 'error': 'no session'}
        result = leg.cancel_order(group.ticket)
        if result.get('leaked_fill'):
            # A cancel that did not prevent a fill is its own event.
            # Reporting 'cancelled' here is how the book comes to
            # believe a leg is flat while the money is at the exchange.
            logging.critical(
                '%s: the cancel of %s LOST THE RACE — %s filled first. The '
                'crossing leg has NOT been sent.',
                pair.key, group.group_id, result.get('filled_volume'))
        group.ticket = None
        return result

    def cancel(self, order):
        """Pull one synthetic, and the exchange order behind it where
        that was the last one."""
        key = self.key_for(order)
        group = self.groups.get(key)
        if group is None:
            return
        if order in group.orders:
            group.orders.remove(order)
        if group.quantity <= 0:
            pair = self.config.pairs.get(group.pair_key)
            if pair is not None:
                self._pull(pair, group, 'cancelled by the trader')
            self.groups.pop(key, None)

    def key_for_group(self, group):
        return (group.pair_key, group.side.value, round(group.level, 9),
                group.position_id)

    def sweep(self, pair, reason='sweep'):
        """Pull every resting order this system has on one pair.

        RUN AT STARTUP AND AT SHUTDOWN, and it matters more here than on
        MT5. There, no working order survived the process, so a sweep at
        startup found nothing. Here a leg order DOES survive us — and a
        spread order whose second leg is only crossed while we are
        running is, after a restart, an outright waiting to happen.
        """
        pulled = []
        for key, group in list(self.groups.items()):
            if group.pair_key != pair.key:
                continue
            self._pull(pair, group, reason)
            self.groups.pop(key, None)
            pulled.append(group.group_id)
        return pulled

    # -- AutoRouting -----------------------------------------------------------

    def arm(self, pair, position, level, quantity=None, auto=True):
        """Rest a closing order against one position at `level`.

        Priced from the fill the position actually got, never from the
        market — a take-profit that moves with the market is not a
        take-profit.
        """
        wanted = float(quantity if quantity is not None else position.quantity)
        already = self.book.armed_by_hand(position.position_id)
        room = sizing.tidy(position.quantity - already)
        if wanted > room:
            wanted = room
        if wanted <= 0:
            return None
        order = self.book.add_order(
            pair, position.side.opposite, level, wanted,
            order_type=OrderType.LIMIT,
            position_id=position.position_id, auto_armed=auto)
        self.group_for(pair, order)
        return order

    def disarm(self, position_id, reason='its position is gone'):
        """Pull EVERY resting close against a position — automation's
        and the trader's alike.

        Called before the position is closed by any other route. A
        closing order left armed after its position is gone fires at
        the next tick that reaches its level and closes something that
        is not there — which on a netting account means OPENING the
        other way.
        """
        return self._disarm(position_id, reason, auto_only=False)

    def disarm_auto(self, position_id, reason='AutoRouting stood down'):
        """Pull only what AUTOMATION armed.

        A trader who clicks to get out and finds their order swept by a
        switch they did not touch believes they are covered and is not.
        """
        return self._disarm(position_id, reason, auto_only=True)

    def _disarm(self, position_id, reason, auto_only):
        pulled = []
        for order in self.book.orders_for_position(position_id):
            if auto_only and not getattr(order, 'auto_armed', False):
                continue
            self.book.cancel(order.order_id, reason)
            self.cancel(order)
            pulled.append(order.order_id)
        return pulled

    # -- plumbing ---------------------------------------------------------------

    def _leg(self, pair, leg):
        account = pair.account_a if leg == 'a' else pair.account_b
        return self.legs.get(account)

    def _symbol(self, pair, leg):
        return pair.symbol_a if leg == 'a' else pair.symbol_b

    def _product(self, pair):
        return getattr(pair, 'product', None) or 'NRML'

    def snapshot(self, pair_key=None):
        return [group.to_dict() for group in self.groups.values()
                if pair_key is None or group.pair_key == pair_key]
