"""What is working and what is on — the engine's own books.

Three clicks at 58.40 are three orders of 1, not one order of 3, and
each must be individually cancellable. That is the rule this module
exists to keep: synthetics are tracked one per click, and any
aggregation (into a single real pending, or into a Work-column number)
is a VIEW over them, computed here, never a replacement for them.

PORTED FROM MT5-Trader's `book.py`. The code is very nearly unchanged;
what changed is WHY half of it exists, and that is worth writing down,
because a reason nobody can reconstruct is a reason somebody deletes.

On MT5 this ledger existed because the accounts are HEDGING: the broker
would never net, an opposite order always opened a second position, so
the netting had to be done here by closing tickets one at a time.

**On Arrow the exchange nets for us.** That does not make this ledger
redundant — it makes it the only record of something the exchange has
thrown away. The exchange holds one net quantity per (symbol, product)
and cannot say which part of it was opened at which price, by which
click, on which side of which spread. Every entry price, every P&L
attribution, every slippage measurement and the whole distinction
between our position and the trader's own manual dealing lives here and
nowhere else.

So `positions_to_reduce` still walks OLDEST FIRST — but it is now
attributing a reduction across our own records, not issuing a separate
close per ticket. `reduction_plan` is the shape that follows from that:
one close per LEG for the whole click, and the fill attributed back
across the positions it covered.
"""

import time
from collections import defaultdict

from . import sizing
from .models import OrderState, SpreadSide, SyntheticOrder


def _side_value(side):
    """'BUY' from a string, an enum, or anything that names a side.

    One reader, because "which side is this" is asked of values that
    arrive from three places — the command bridge (strings), the
    quoter (enums) and the recovered book (whatever was persisted).
    """
    if side is None:
        return None
    value = getattr(side, 'value', side)
    try:
        return str(value).strip().upper() or None
    except Exception:
        return None


class Book:
    def __init__(self):
        self._orders = {}                 # order_id -> SyntheticOrder
        self._positions = {}              # position_id -> SpreadPosition
        #: Our own fills, per pair and level — the ladder's LTQ column.
        #: No exchange publishes a tape for a spread nobody listed, so
        #: this is all there is, and the UI says so rather than implying
        #: a market print. (MCX DOES list some calendar spreads as their
        #: own contracts; where a pair is one of those there is a real
        #: tape, and it is not this.)
        self.prints = defaultdict(list)

    # -- working orders ---------------------------------------------------

    def add_order(self, pair, side, level, quantity, order_type=None,
                  time_in_force=None, position_id=None, auto_armed=False):
        order = SyntheticOrder(
            pair.key, side, level, quantity,
            order_type or pair.order_type, time_in_force or pair.time_in_force,
            position_id=position_id, auto_armed=auto_armed)
        self._orders[order.order_id] = order
        return order

    def auto_armed_for(self, position_id):
        """Only the closing orders AUTOMATION armed against a position.

        What the AutoRouting switch is allowed to pull. A close the
        trader placed by hand is not automation and does not stand
        down with it.
        """
        return [o for o in self.orders_for_position(position_id)
                if getattr(o, 'auto_armed', False)]

    def orders_for_position(self, position_id, working_only=True):
        """The closing orders armed against one position.

        A closing order left armed after its position is gone would
        fire at the next tick that reaches its level and close
        something that is not there.
        """
        return [o for o in self._orders.values()
                if o.position_id == position_id
                and (o.is_working or not working_only)]

    def orders(self, pair_key=None, working_only=True):
        return [o for o in self._orders.values()
                if (pair_key is None or o.pair_key == pair_key)
                and (o.is_working or not working_only)]

    def is_our_order_id(self, broker_order_id):
        """Did WE send this broker order id?

        The only ownership evidence this venue offers that is a FACT
        rather than an inference. A netted position carries no marker,
        and there is no magic number — but an order id we were handed
        when we sent an order is ours beyond argument.

        Both places one can be held are searched: the ticket a resting
        synthetic holds at the broker, and the order ticket on every
        leg fill of every position, open or closed.
        """
        wanted = str(broker_order_id or '')
        if not wanted:
            return False
        for order in self._orders.values():
            if str(order.pending_ticket or '') == wanted:
                return True
        for position in self._positions.values():
            for fill in (position.leg_a, position.leg_b):
                if fill is not None and str(
                        getattr(fill, 'order_ticket', '') or '') == wanted:
                    return True
        return False

    def order(self, order_id):
        return self._orders.get(order_id)

    def cancel(self, order_id, reason='cancelled by trader'):
        """Pull ONE synthetic. Returns it, or None if it is not working."""
        order = self._orders.get(order_id)
        if order is None or not order.is_working:
            return None
        order.state = OrderState.CANCELLED
        order.reason = reason
        return order

    def cancel_where(self, pair_key=None, side=None,
                     reason='cancelled by trader'):
        """`CXL B` / `CXL S` / `CXL All`, and the global kill.

        Returns the orders actually pulled, so the button can report a
        count instead of claiming success over an empty set.
        """
        if side is not None:
            side = SpreadSide(getattr(side, 'value', side))
        pulled = []
        for order in self.orders(pair_key):
            if side is not None and order.side is not side:
                continue
            if self.cancel(order.order_id, reason):
                pulled.append(order)
        return pulled

    def working_at(self, pair_key, level, tolerance=1e-9):
        """The Work column's number for one row: (buy qty, sell qty).

        Aggregated for display only — `orders()` still holds each click
        separately, and cancelling the cell pulls exactly one of them.
        """
        buys = sells = 0.0
        for order in self.orders(pair_key):
            if abs(order.level - level) > tolerance:
                continue
            if order.side is SpreadSide.BUY:
                buys += order.remaining
            else:
                sells += order.remaining
        return buys, sells

    def working_counts(self, pair_key=None):
        """(buys, sells) — the superscripts on the CXL buttons, so the
        trader can see a button will do something before pressing it."""
        buys = sum(1 for o in self.orders(pair_key)
                   if o.side is SpreadSide.BUY)
        sells = sum(1 for o in self.orders(pair_key)
                    if o.side is SpreadSide.SELL)
        return buys, sells

    # -- positions ---------------------------------------------------------

    def add_position(self, position):
        self._positions[position.position_id] = position
        if position.entry_spread is not None:
            self.prints[position.pair_key].append(
                {'level': position.entry_spread, 'quantity': position.quantity,
                 'side': position.side.value, 'at': position.opened_at})
        return position

    def positions(self, pair_key=None, open_only=True):
        return [p for p in self._positions.values()
                if (pair_key is None or p.pair_key == pair_key)
                and (p.is_open or not open_only)]

    def position(self, position_id):
        return self._positions.get(position_id)

    def armed_by_hand(self, position_id):
        """Spreads of one position already covered by the TRADER's own
        resting closes.

        Not automation's: an AutoRouting target is subordinate to a
        click — the click either matches its level or replaces it — so
        counting it here would make the trader's own click look like it
        had nothing left to close.
        """
        return sizing.tidy(sum(
            float(order.remaining or 0.0)
            for order in self.orders_for_position(position_id)
            if not getattr(order, 'auto_armed', False)))

    def positions_to_reduce(self, pair_key, side, quantity, exclude=None,
                            reserve_armed=False):
        """What an opposite click covers: `[(position, spreads)]`.

        OLDEST FIRST, which is what FIFO means and what every desk
        expects when they say "close my position". A price ladder is
        expected to REDUCE before it opens: click the offer while short
        and you are covering, not stacking a second short.

        On MT5 that had to be enforced here because the accounts are
        HEDGING and an opposite order always opened a second position.
        The EXCHANGE nets here, so the click cannot stack whatever we
        do — but the ledger still has to decide which of OUR positions
        the reduction consumed, because that is what decides the entry
        price the P&L is measured against, and the exchange has no
        opinion about it at all.

        THE LAST ONE IS TAKEN IN PART. Whole tickets only was the rule
        here, with a ticket bigger than the click ending the scan:

            live 2026-09-03, short 1207 spreads, BUY 100 clicked
            -> "resting at 7.69 to CLOSE 2 position(s), then open 93"

        Two old tickets of 5 and 2 fitted; the 1,200 behind them did
        not, so 93 of the trader's 100 went the OTHER WAY — opening
        longs against their own short instead of covering it. A reduce
        that opens is the exact failure "reduce before you open" exists
        to prevent; it had simply moved from the side to the size.

        That was a hedging account, and here the EXCHANGE would have
        netted the 93 away. The ledger would not have: it would have
        recorded a 93-lot long beside a 1,200-lot short that the
        exchange had already merged, and every P&L, margin and
        reconciliation figure downstream would have been drawn from a
        book that does not match the account. Same bug, quieter.

        A part-close is supported end to end: `close_position` takes a
        quantity, `_closed_fraction` measures what actually came off,
        and `reduce_by` leaves the position open for the rest. So the
        oldest ticket is closed as far as the click reaches and no
        further, and nothing opens while there is still an opposite
        position to cover.
        """
        try:
            left = float(quantity)
        except (TypeError, ValueError):
            return []
        if left <= 0:
            return []
        # COMPARE ON THE VALUE, never on identity.
        #
        # `side` arrives as a plain string ('BUY') from the command
        # bridge and as a SpreadSide enum from the quoter. `is not`
        # against a string is ALWAYS true, so every open position —
        # including ones on the SAME side — looked opposite, and a
        # second buy while long would have closed the trader's own
        # position instead of adding to it. Caught by the end-to-end
        # suite, which clicks BUY twice.
        want = _side_value(side)
        if want is None:
            return []
        opposite = [p for p in self.positions(pair_key)
                    if _side_value(p.side) not in (None, want)
                    and (exclude is None or p.position_id != exclude)]
        opposite.sort(key=lambda p: (p.opened_at or 0, str(p.position_id)))
        picked = []
        for position in opposite:
            size = float(position.quantity or 0.0)
            if reserve_armed:
                # WHAT IS STILL UNCOVERED, for a click that RESTS.
                #
                # Two closing clicks at two prices are two working
                # orders — that is how a position is scaled out of, and
                # it is what the sell side does with two opening
                # clicks. But each of them may only cover what is not
                # already spoken for, or a 10-lot short ends up with 20
                # lots of resting closes against it and the second one
                # to fill opens a long.
                #
                # Only for the RESTING path. A market close is now, and
                # it disarms whatever was resting as it goes.
                size = sizing.tidy(size - self.armed_by_hand(
                    position.position_id))
            if size <= 0:
                continue
            # As much of this ticket as the click still reaches. Never
            # more: over-closing would flip the net the other way,
            # which is the mistake at the opposite end from the one
            # above and just as expensive.
            take = sizing.tidy(min(size, left))
            if take <= 1e-9:
                break
            picked.append((position, take))
            # Tidied, because this subtraction is what the NEXT take is
            # measured against: `0.15 - 0.1` is 0.04999999999999999 in
            # float, and that is then the size of the second closing
            # order, on the panel, in full.
            left = sizing.tidy(left - take)
            if left <= 1e-9:
                break
        return picked

    def net_position(self, pair_key):
        """(net spreads, average entry spread) for one ladder.

        Signed: buys positive. The average is weighted by quantity and
        anchored on executed fills; it is None when nothing is on, never
        0.0 — an average of no trades is not zero (spec §11).
        """
        net = 0.0
        weighted = 0.0
        volume = 0.0
        for position in self.positions(pair_key):
            signed = position.quantity if position.side is SpreadSide.BUY \
                else -position.quantity
            net += signed
            if position.entry_spread is not None:
                weighted += position.entry_spread * position.quantity
                volume += position.quantity
        return net, (weighted / volume if volume else None)

    def last_print(self, pair_key):
        prints = self.prints.get(pair_key) or []
        return prints[-1] if prints else None


class ReductionPlan:
    """What one opposite click closes: the LEG lots, and who they came from.

    THIS IS THE SHAPE NETTING FORCES, and it is the one real structural
    change in this module.

    On MT5 a close had to name a position ticket, so N ledger positions
    meant N close orders — there was no choice about it. Here the
    exchange nets, so the whole reduction is ONE order per leg. That is
    not merely tidier: N orders is N chances to be refused halfway
    through, N brokerage charges where the desk is billed per order,
    and N opportunities to leave the two legs at different sizes. One
    order per leg either goes or does not.

    What that costs is that the fill has to be ATTRIBUTED afterwards —
    the exchange tells us 60 lots came off, and only this ledger can
    say which of our positions they came off. `apply` does that, FIFO,
    which is the same order `positions_to_reduce` picked them in.
    """

    def __init__(self, takes, pair=None):
        #: [(position, spreads)] — oldest first, the last one in part.
        self.takes = list(takes)
        self.pair = pair

    @property
    def spreads(self):
        return sizing.tidy(sum(take for _position, take in self.takes))

    @property
    def is_empty(self):
        return not self.takes

    def leg_lots(self):
        """(leg A lots, leg B lots) to cross, for the WHOLE click.

        Taken from each position's own recorded fills rather than
        recomputed from the pair's current clip, because the clip may
        have been changed since the position was opened — and what has
        to come off is what actually went on.
        """
        lots_a = lots_b = 0.0
        for position, take in self.takes:
            size = float(position.quantity or 0.0)
            if size <= 0:
                continue
            share = take / size
            if position.leg_a is not None:
                lots_a += float(position.leg_a.volume or 0.0) * share
            if position.leg_b is not None:
                lots_b += float(position.leg_b.volume or 0.0) * share
        return sizing.tidy(lots_a), sizing.tidy(lots_b)

    def entry_sides(self):
        """The side each leg was ENTERED on, which is what a close
        reverses. Every position in a plan is on the same spread side —
        `positions_to_reduce` only picks the ones opposite the click —
        so there is one answer per leg and not one per position."""
        if not self.takes:
            return None, None
        return self.takes[0][0].side.leg_sides()

    def apply(self, fraction, on_closed=None, clock=time.time,
              reason='reduced by an opposite click'):
        """Book a fill of `fraction` of this plan, FIFO. Returns
        (closed position ids, spreads actually covered).

        FIFO, NOT PRO-RATA. A 60% fill of a 100-spread reduce covering
        positions of 5, 2 and 93 closes the 5, the 2 and 53 of the 93 —
        it does not take 60% off each of them. Pro-rata would leave
        three part-closed positions where the trader closed two whole
        ones, and every entry price downstream would be an average of
        things that did not happen.
        """
        try:
            left = max(0.0, min(1.0, float(fraction))) * self.spreads
        except (TypeError, ValueError):
            return [], 0.0
        left = sizing.tidy(left)
        closed = []
        covered = 0.0
        for position, take in self.takes:
            if left <= 1e-9:
                break
            part = sizing.tidy(min(take, left))
            size = float(position.quantity or 0.0)
            position.reduce_by(part / size if size else 0.0)
            covered = sizing.tidy(covered + part)
            left = sizing.tidy(left - part)
            if position.quantity <= 1e-9:
                # A REAL timestamp. `is_open` reads `closed_at is
                # None`, so any truthy placeholder would satisfy the
                # book and then read as a nonsense time everywhere the
                # journal, the slippage window and the monitor show it.
                position.closed_at = clock()
                position.close_reason = reason
                closed.append(position.position_id)
                if on_closed is not None:
                    on_closed(position)
        return closed, covered


def reduce_first(book, executor, pair, side, quantity, md,
                 exclude=None, on_closed=None):
    """Close what an opposite click covers; return what is left to open.

    ONE implementation, called from both places a position can be
    created: the MARKET click closes before it opens, and a resting
    order closes when it fills. Two implementations of "what does this
    click cover" is two answers to reconcile the day they disagree, on
    a live book.

    Returns (closed position ids, quantity still to open, failure).

    `failure` is None when the close went through, and the BROKER'S OWN
    WORDS when it did not. It has to be told apart from "there was
    nothing to close": both used to return an empty list, so a caller
    could not tell a refusal from a quiet no-op, and the MARKET path
    opened a new position on top of one that had just failed to close —
    the exact mess this feature exists to prevent, and worse if the
    close half-executed and left a leg naked.

    A close is never withheld. `close_spread` consults no guard, and
    neither does this.
    """
    try:
        left = float(quantity)
    except (TypeError, ValueError):
        return [], quantity, None
    plan = ReductionPlan(
        book.positions_to_reduce(pair.key, side, left, exclude=exclude),
        pair=pair)
    if plan.is_empty:
        return [], max(left, 0.0), None

    result = executor.close_spread(pair, plan, md,
                                   reason='reduced by an opposite click')
    # WHAT ACTUALLY CAME OFF, not what was asked for.
    #
    # A close can come back having covered less than it was asked for,
    # and `imbalanced` is the case where it covered NOTHING but still
    # reported ok — one leg came down and the other's lot step was too
    # big to follow it. Both used to count as the whole plan, so the
    # click believed it had covered spreads that are still on, and
    # opened the remainder the other way. A reduce that opens is the
    # exact failure this function exists to prevent.
    fraction = float(result.get('fraction', 1.0) or 0.0)
    if not result.get('ok') or result.get('imbalanced') \
            or fraction <= 1e-9:
        # Do not step over a refusal. Opening the remainder on top of a
        # position that would NOT close is how a reduce quietly becomes
        # a bigger position.
        return [], max(left, 0.0), close_failure(result, plan)
    closed, covered = plan.apply(fraction, on_closed=on_closed)
    return closed, max(sizing.tidy(left - covered), 0.0), None


def close_failure(result, target):
    """Why a close did not go through, in the BROKER's own words.

    "check the log" is not an answer on a live account. Each leg
    reports its own error, and a close that went through on one leg and
    not the other has left a NAKED LEG — which is the sentence the
    trader has to see first, before any of the rest of it.

    `target` is a ReductionPlan or a single position; either way the
    message names what could not be closed.
    """
    result = result or {}
    what = _name(target)
    # A refusal that never reached the broker carries its own sentence
    # — the piece was too small for one leg's lot step, say — and there
    # are no leg errors to quote.
    if result.get('reason') and not (result.get('legs') or {}):
        return f"could not close {what}: {result['reason']}"
    legs = result.get('legs') or {}
    said = []
    done = []
    for leg, answer in sorted(legs.items()):
        if (answer or {}).get('ok'):
            done.append(leg.upper())
        else:
            reason = ((answer or {}).get('error')
                      or (answer or {}).get('reason') or 'refused')
            said.append(f'leg {leg.upper()}: {reason}')
    head = f'could not close {what}'
    if done and said:
        head = (f'NAKED LEG — {what} closed on leg {done[0]} but NOT on '
                f'the other')
    return f"{head} ({'; '.join(said) if said else 'no reason reported'})"


def _name(target):
    """How to refer to what failed to close, in a sentence."""
    if target is None:
        return 'the position'
    identifier = getattr(target, 'position_id', None)
    if identifier is not None:
        return f'position {identifier}'
    takes = getattr(target, 'takes', None)
    if takes:
        if len(takes) == 1:
            return f'position {takes[0][0].position_id}'
        return (f'{len(takes)} positions '
                f'({getattr(target, "spreads", 0):g} spreads)')
    return 'the position'
