"""Domain models: the vocabulary the whole system speaks.

A ladder trades a SPREAD. A spread order has a side (BUY or SELL the
spread) and becomes two leg orders, one per contract. Nothing here
knows about pyarrow_client — that lives in broker.py alone.

PORTED FROM MT5-Trader's `models.py`. One thing is genuinely different
and it changes what a CLOSE is:

MT5-Trader's accounts are HEDGING mode, so an opposite market order
opens a SECOND position and every close must name a position TICKET.
Indian exchanges NET: one net quantity per (TradingSymbol, product),
and a close is simply an opposite order for the same units. There are
no tickets to name.

So `LegFill.position_tickets` becomes `order_ids` — kept for the audit
trail, not for addressing a close — and our own `SpreadPosition` ledger
becomes the ONLY record of what this system opened, at what price, in
what size. The broker can tell us the net; only we can tell the two
apart.
"""

import itertools
import time
import uuid
from enum import Enum

from . import sizing


class OrderSide(Enum):
    """A side on ONE leg — what a single-contract order does."""

    BUY = "BUY"
    SELL = "SELL"

    @property
    def opposite(self):
        return OrderSide.SELL if self is OrderSide.BUY else OrderSide.BUY


class SpreadSide(Enum):
    """A side on the SPREAD, which is the thing the trader clicks.

    `spread = P_B - beta * P_A`, so buying the spread buys leg B and
    sells leg A. Keeping this distinct from OrderSide is deliberate: a
    'BUY' that means leg A on one line and the spread on the next is
    exactly how a hedge gets inverted.
    """

    BUY = "BUY"      # buy leg B, sell leg A   -> lifts long_spread
    SELL = "SELL"    # sell leg B, buy leg A   -> hits short_spread

    @property
    def opposite(self):
        return SpreadSide.SELL if self is SpreadSide.BUY else SpreadSide.BUY

    def leg_sides(self):
        """(leg A side, leg B side) for this spread side."""
        if self is SpreadSide.BUY:
            return OrderSide.SELL, OrderSide.BUY
        return OrderSide.BUY, OrderSide.SELL


class OrderType(Enum):
    """What a click on the ladder does. Both are first-class (spec §4)."""

    LIMIT = "LIMIT"      # rest a synthetic order; quote one leg, cross the other
    MARKET = "MARKET"    # cross both legs now, clicked price as the guard


class TimeInForce(Enum):
    """Working-order lifetime (spec §3.1).

    Neither survives this process stopping — nothing at the broker knows
    what a spread is, so an order that "survived" would be a promise
    nothing could keep. The UI says so beside the selector.
    """

    DAY = "DAY"      # cancelled at the session cutoff
    GTC = "GTC"      # until the trader cancels — or until this system stops


class OvernightMode(Enum):
    """What happens to a POSITION at the session cutoff (spec §3.2)."""

    ALLOW = "ALLOW"                      # keep it and pay the swap
    EXIT_IF_PROFIT = "EXIT_IF_PROFIT"    # flatten only if NET P&L > 0
    EXIT_ALWAYS = "EXIT_ALWAYS"          # flatten regardless


class OrderState(Enum):
    WORKING = "WORKING"        # resting, backed by a real pending (LIMIT)
    FILLING = "FILLING"        # quoting leg filled; crossing leg going on
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"


#: Stamped on every order this application sends, where the broker
#: gives us a field to stamp it in, so a sweep can scope itself to our
#: own orders.
#:
#: IT DOES NOT SCOPE POSITIONS, AND THAT IS THE POINT TO REMEMBER.
#: MT5-Trader scopes every position read, every pending read and every
#: sweep by its magic number, so it can never touch the trader's own
#: terminal clicks. On a netting account there is no such field on a
#: POSITION: our net and the trader's own net in the same contract are
#: one number, and nothing at the exchange can separate them.
#:
#: The mitigation is a DEDICATED ARROW ACCOUNT used by nothing else,
#: which restores the guarantee outright. Where that is not true, the
#: reconciler says so on the banner rather than quietly assuming an
#: unexplained net is ours.
MAGIC_NUMBER = 24680

_uid = itertools.count(1)


def new_id(prefix):
    """Short, unique, and readable in a log line beside an MT5 ticket."""
    return f"{prefix}{next(_uid):04d}-{uuid.uuid4().hex[:4]}"


class SyntheticOrder:
    """One click: one working order on one ladder at one level.

    Three clicks at 58.40 are THREE of these, individually cancellable
    (spec §3). They may share a single real pending at the broker — the
    executor aggregates; this object does not know or care.
    """

    def __init__(self, pair_key, side, level, quantity, order_type,
                 time_in_force, clock=time.time, position_id=None,
                 auto_armed=False):
        self.order_id = new_id('SO')
        #: Did AUTOMATION arm this, or the trader?
        #:
        #: Both are closing orders and both carry a position_id, so
        #: nothing else can tell them apart. It matters because
        #: standing AutoRouting down pulls what AutoRouting armed —
        #: and it must NOT pull a close the trader placed by hand. A
        #: trader who clicks to get out and finds their order swept by
        #: a switch they did not touch is a trader who believes they
        #: are covered and is not.
        self.auto_armed = bool(auto_armed)
        #: The position this order CLOSES, when it is a closing order.
        #: None on an ordinary click. It is what makes an AutoRouting
        #: take-profit a different order from an entry at the same
        #: level on the same side — they must never be merged into one
        #: pending, or one of them silently changes meaning.
        self.position_id = position_id
        self.pair_key = pair_key
        self.side = SpreadSide(side)
        self.level = float(level)           # the SPREAD level clicked
        self.quantity = float(quantity)     # in SPREADS, never leg lots
        self.order_type = OrderType(order_type)
        self.time_in_force = TimeInForce(time_in_force)
        self.state = OrderState.WORKING
        self.created_at = clock()
        self.filled_quantity = 0.0
        #: The real pending backing this order, when one exists. Several
        #: synthetics can point at the same ticket.
        self.pending_ticket = None
        #: Set when the order stops working, in the broker's own words
        #: where the broker is the one who refused (spec §11).
        self.reason = None

    @property
    def remaining(self):
        return max(0.0, self.quantity - self.filled_quantity)

    @property
    def is_working(self):
        return self.state in (OrderState.WORKING, OrderState.FILLING)

    def to_dict(self):
        return {
            'order_id': self.order_id,
            'pair_key': self.pair_key,
            'side': self.side.value,
            'level': self.level,
            'quantity': self.quantity,
            'filled_quantity': self.filled_quantity,
            'order_type': self.order_type.value,
            'time_in_force': self.time_in_force.value,
            'state': self.state.value,
            'created_at': self.created_at,
            'pending_ticket': self.pending_ticket,
            'position_id': self.position_id,
            'intent': 'CLOSE' if self.position_id else 'OPEN',
            'auto_armed': self.auto_armed,
            'reason': self.reason,
        }


class LegFill:
    """What one leg actually did, at the exchange, as the broker reported it.

    `volume` is in LOTS, as everywhere else in this system. What went
    on the wire was `volume * contract_size` UNITS — the conversion
    happens once, in the broker module, and nowhere else.
    """

    def __init__(self, account, symbol, side, volume, price,
                 order_ticket=None, position_tickets=None,
                 contract_size=None, at=None, clock=time.time,
                 segment=None, product=None):
        self.account = account
        self.symbol = symbol
        self.side = OrderSide(side)
        self.volume = float(volume)
        self.price = price
        #: The broker's own order id for this fill.
        self.order_ticket = order_ticket
        #: Order ids that produced this fill. On MT5 these were POSITION
        #: tickets and a close had to name one; on a netting account
        #: they are an AUDIT TRAIL and nothing addresses a close by
        #: them. The name is kept so the ported code reads the same.
        self.position_tickets = list(position_tickets or [])
        #: `LotSize` — units per lot. The `C` in `k = L_B x C_B`.
        self.contract_size = contract_size
        #: Which segment and product this leg sits in. A netted
        #: position is per (symbol, product), so the product is part of
        #: the position's identity here in a way it never was on MT5.
        self.segment = segment
        self.product = product
        self.at = clock() if at is None else at

    def to_dict(self):
        return {
            'account': self.account, 'symbol': self.symbol,
            'side': self.side.value, 'volume': self.volume,
            'price': self.price, 'order_ticket': self.order_ticket,
            'position_tickets': list(self.position_tickets),
            'contract_size': self.contract_size, 'at': self.at,
            'segment': self.segment, 'product': self.product,
            #: What actually went on the wire, spelled out beside the
            #: lots. Units is the number that can be wrong by a factor
            #: of LotSize, so it is never left to be re-derived.
            'units': (None if self.contract_size is None
                      else self.volume * self.contract_size),
        }

    @classmethod
    def from_dict(cls, raw):
        if not raw:
            return None
        return cls(raw['account'], raw['symbol'], raw['side'], raw['volume'],
                   raw['price'], order_ticket=raw.get('order_ticket'),
                   position_tickets=raw.get('position_tickets'),
                   contract_size=raw.get('contract_size'), at=raw.get('at'),
                   segment=raw.get('segment'), product=raw.get('product'))


class SpreadPosition:
    """A pair that is ON: leg A and leg B, both filled, both ours.

    ON A NETTING ACCOUNT THIS LEDGER IS THE ONLY RECORD THAT WE OPENED
    IT. The exchange knows a net quantity per contract and nothing
    else; it cannot say which part of that net was this system's and
    which was the trader's own dealing. Losing this object loses that
    distinction permanently, which is why it is written to the database
    on every change and recovered at startup.

    Entered at a real fill and marked at the touches it would actually
    CLOSE at (spec §5), which is why it shows a loss the instant it
    opens — that is what closing immediately would cost.
    """

    def __init__(self, pair_key, side, quantity, leg_a_fill, leg_b_fill,
                 entry_spread, order_type, spread_units, clock=time.time):
        self.position_id = new_id('POS')
        self.pair_key = pair_key
        self.side = SpreadSide(side)
        self.quantity = float(quantity)       # in spreads
        self.leg_a = leg_a_fill
        self.leg_b = leg_b_fill
        #: Anchored on the EXECUTED fills, never on the mid the decision
        #: was taken at (spec §11).
        self.entry_spread = entry_spread
        self.order_type = OrderType(order_type)
        #: `k` — dollars per 1.00 of spread. The ONE multiplier (spec §2).
        self.spread_units = spread_units
        self.opened_at = clock()
        self.closed_at = None
        self.close_reason = None
        self.realized_pnl = None
        self.exit_spread = None
        #: Measured against the executable touch at decision time, and
        #: unmeasured stays None — never 0.0 (spec §5, §11).
        self.entry_slippage = None
        self.exit_slippage = None
        self.click_to_on_ms = None
        #: True when this came back from the database at startup rather
        #: than being watched happen.
        self.recovered = False
        #: Set once the reconciler has seen both legs at the broker.
        self.confirmed = False

    @property
    def is_open(self):
        return self.closed_at is None

    def reduce_by(self, closed_fraction, realized=None):
        """Book PART of this position closing, and stay open for the rest.

        A closing order fills partially like any other, and both
        half-measures are wrong. Marking the whole position closed
        loses the leg still on at the broker — the book reads flat
        while the money is there, which is the one failure this system
        must never have. Leaving it at full size claims an exposure
        that is no longer on, and every close sized from it then asks
        the broker for more than it has.

        The leg volumes come down with the quantity because the
        reconciler and the P&L both read them. The order ids do not
        change: they are the audit trail of how this position was
        built, and a partial close does not un-happen the fills that
        opened it.

        Returns the quantity still open.
        """
        try:
            fraction = float(closed_fraction)
        except (TypeError, ValueError):
            return self.quantity
        fraction = max(0.0, min(1.0, fraction))
        if fraction <= 0.0:
            return self.quantity
        left = 1.0 - fraction
        # TIDIED, because this is the subtraction that walks a click
        # down a stack of positions, and its result is what the NEXT
        # take is measured against and what the Working Orders panel
        # prints. A 60% fill against 93 leaves 39.99999999999999 in
        # float — seventeen digits, next to a clean 40, which reads
        # like the size was changed on the way to the exchange.
        self.quantity = sizing.tidy(self.quantity * left)
        for fill in (self.leg_a, self.leg_b):
            if fill is not None:
                fill.volume = sizing.tidy(fill.volume * left)
        if realized is not None:
            # ACCUMULATED, not replaced: a position closed in three
            # pieces earned its P&L in three pieces.
            self.realized_pnl = (realized if self.realized_pnl is None
                                 else self.realized_pnl + realized)
        return self.quantity

    def mark(self, closing_spread):
        """Open P&L in dollars at the spread it would close at.

        Commission is NOT subtracted here — the caller adds it once
        (spec §5: the crossing is already in the two prices; charging
        the round trip again is the bid-ask twice).
        """
        if closing_spread is None or self.entry_spread is None:
            return None
        move = closing_spread - self.entry_spread
        if self.side is SpreadSide.SELL:
            move = -move
        # SIZE IS ALREADY IN `spread_units`, AND MUST NOT BE APPLIED
        # TWICE.
        #
        # Every place a position is built passes
        # `spread_units(leg_b.volume, contract_b)`, and `leg_b.volume`
        # is the volume of the WHOLE position — 1.0 lots for 100
        # spreads, not the 0.01 one spread costs. So `spread_units` is
        # already the dollars a 1.00 move in the spread is worth for
        # this position entire. Multiplying by `quantity` on top
        # charged the size a second time.
        #
        # It was invisible at Qty 1, which is the size everything was
        # tested at: there `spread_units` is 0.01 x 100 = 1.0 and
        # `quantity` is 1, so the double count is x1 and the answer
        # comes out right. At Qty 10 it read 10x, and at Qty 100 it
        # read $6,000.00 against MT5's own -$56.70.
        #
        # The per-ONE-spread k of the same name — `spread_units(
        # clip_lots_b, ...)` on the ladder and in the exit maths — is a
        # different number and IS multiplied by quantity by its own
        # callers. Only a POSITION's is whole already.
        return move * self.spread_units

    def to_dict(self):
        return {
            'position_id': self.position_id,
            'pair_key': self.pair_key,
            'side': self.side.value,
            'quantity': self.quantity,
            'entry_spread': self.entry_spread,
            'exit_spread': self.exit_spread,
            'order_type': self.order_type.value,
            'spread_units': self.spread_units,
            'opened_at': self.opened_at,
            'closed_at': self.closed_at,
            'close_reason': self.close_reason,
            'realized_pnl': self.realized_pnl,
            'entry_slippage': self.entry_slippage,
            'exit_slippage': self.exit_slippage,
            'click_to_on_ms': self.click_to_on_ms,
            'recovered': self.recovered,
            'confirmed': self.confirmed,
            'leg_a': self.leg_a.to_dict() if self.leg_a else None,
            'leg_b': self.leg_b.to_dict() if self.leg_b else None,
        }

    @classmethod
    def from_dict(cls, raw):
        """Rebuild a position from the database, at restart.

        It comes back OPEN and under management — its id, its fills and
        its tickets exactly as they were. A recovered position that came
        back under a new id would be an orphan to the reconciler and a
        ghost to the book.
        """
        position = cls(raw['pair_key'], raw['side'], raw['quantity'],
                       LegFill.from_dict(raw.get('leg_a')),
                       LegFill.from_dict(raw.get('leg_b')),
                       raw.get('entry_spread'),
                       raw.get('order_type') or OrderType.MARKET.value,
                       raw.get('spread_units') or 0.0)
        position.position_id = raw['position_id']
        position.opened_at = raw.get('opened_at') or position.opened_at
        position.closed_at = raw.get('closed_at')
        position.close_reason = raw.get('close_reason')
        position.realized_pnl = raw.get('realized_pnl')
        position.exit_spread = raw.get('exit_spread')
        position.entry_slippage = raw.get('entry_slippage')
        position.exit_slippage = raw.get('exit_slippage')
        position.click_to_on_ms = raw.get('click_to_on_ms')
        #: Recovered from disk rather than seen happen. The monitor says
        #: so until the reconciler has confirmed both legs at the broker.
        position.recovered = True
        return position
