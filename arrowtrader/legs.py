"""Leg interface: the engine's view of one contract's execution.

MT5-Trader has `LocalLeg` and `RemoteLeg` behind one interface, because
the MetaTrader5 package holds one connection per process and two
accounts therefore need two processes and an IPC hop. **Arrow needs
neither.** One authenticated session trades both legs, so there is one
implementation and no socket between the coordinator and the broker.

The interface is kept anyway, method for method, and that is
deliberate. It is what lets `coordinator.py`, `executor.py` and
`quoter.py` port across with their logic intact — they speak to a leg,
and they must never know or care what is behind it. It is also what
keeps a second venue (NSE, or another broker entirely) a new class here
rather than a rewrite above.

TWO CONVENTIONS ARE LOAD-BEARING AND EASY TO LOSE IN A REFACTOR:

- `positions()` and `pending_orders()` return **None for "unknown"** —
  the call failed, the token expired, the socket dropped. None is NOT
  "flat" / "no orders". Code that treats None as empty will sweep a
  live account clean in its own report while the money sits at the
  exchange.
- **Unmeasured is not zero.** Return None and render an em dash.

AND ONE THING THIS LAYER OWNS OUTRIGHT: **lots in, units out.** Every
method here takes LOTS, because that is what the ladder, the book and
the P&L speak. The conversion to units happens on the way past, once,
through `sizing.units`, and a lot size that cannot be resolved REFUSES
rather than defaulting.
"""

import logging

from . import sizing
from .models import OrderSide


class ArrowLeg:
    """One leg of a pair, over a shared Arrow session.

    Both legs of every pair hold the SAME session. That is the whole
    of what "two accounts" became.
    """

    def __init__(self, name, session):
        self.name = name
        self.session = session

    # -- session ----------------------------------------------------------

    def connect(self):
        # Shared: the second leg to ask finds it already up.
        if self.session.connected:
            return True
        return self.session.initialize()

    def close(self):
        self.session.shutdown()

    def ping(self):
        return self.session.is_alive()

    def account_info(self):
        info = self.session.account_info()
        if not info:
            return None
        return dict(info, account=self.name)

    def terminal_report(self):
        return dict(self.session.terminal_report(), account=self.name)

    def server_offset(self):
        """Seconds the exchange's clock runs ahead of ours, or None.

        None is unknown, and unknown means the session cutoff does not
        fire. It is NOT zero.
        """
        return self.session.server_time_offset_sec()

    # -- instruments ------------------------------------------------------

    def ensure_symbol(self, symbol):
        return self.session.ensure_symbol(symbol)

    def symbol_report(self, symbol):
        return dict(self.session.symbol_report(symbol), account=self.name)

    def find_symbols(self, pattern, limit=40, segment=None):
        return self.session.find_symbols(pattern, limit=limit,
                                         segment=segment)

    def lot_size(self, symbol):
        """UNITS PER LOT, or None. None refuses; it is never 1."""
        return (self.session.master.lot_size(symbol)
                if self.session.master else None)

    # -- prices -----------------------------------------------------------

    def tick(self, symbol):
        return self.session.symbol_tick(symbol)

    def depth(self, symbol):
        return self.session.depth(symbol)

    def session_stats(self, symbol):
        return self.session.session_stats(symbol)

    def resubscribe(self, symbol):
        return self.session.resubscribe(symbol)

    def margin_for(self, legs):
        """SPAN + exposure for a basket, or None where it cannot be
        computed. Never derived from notional."""
        return self.session.margin_for(legs)

    # -- orders: LOTS in, UNITS on the wire -------------------------------

    def _units(self, symbol, lots):
        """(units, refusal). The one conversion, and its refusal.

        A lot size that could not be resolved comes back as None from
        the master, and this returns a refusal rather than a number.
        The alternative — defaulting to 1 — does not make an order
        fail, it makes it fill at a hundredth of the intended size on
        GOLD.
        """
        lot_size = self.lot_size(symbol)
        if lot_size is None:
            return None, (f'{symbol} has no lot size in the instrument '
                          f'master, so nothing can be sized on it — an '
                          f'order carries units, and units are lots x '
                          f'LotSize')
        units = sizing.units(lots, lot_size)
        if units is None:
            return None, (f'{lots:g} lots of {symbol} is not a whole number '
                          f'of units at {lot_size} per lot')
        return units, None

    def order(self, symbol, side, volume, slippage_points=None, comment='',
              product='NRML', deadline_sec=None):
        """Cross now. `volume` is LOTS.

        `slippage_points` is accepted and IGNORED, and the signature
        keeps it so the ported executor calls this unchanged. Arrow
        takes no deviation parameter — the exchange enforces nothing on
        our behalf — so the clicked-price guard in the executor is the
        only slippage protection there is, and it reads
        `requested_price` and `price` from what comes back here.
        """
        units, refusal = self._units(symbol, volume)
        if refusal:
            return {'ok': False, 'filled_volume': 0.0, 'price': None,
                    'ticket': None, 'position_tickets': [],
                    'unresolved': False, 'error': refusal}
        result = self.session.send_market_order(
            symbol, OrderSide(side), units, comment=comment,
            product=product, deadline_sec=deadline_sec)
        return self._fill(symbol, result)

    def place_limit(self, symbol, side, volume, price, comment='',
                    product='NRML'):
        """Rest a real limit at the exchange. `volume` is LOTS.

        Unlike MT5, this order CAN close: on a netting account an
        opposite resting limit reduces the net rather than opening a
        second position facing the other way.
        """
        units, refusal = self._units(symbol, volume)
        if refusal:
            return {'ok': False, 'ticket': None, 'error': refusal}
        return self.session.place_pending_limit(
            symbol, OrderSide(side), units, price, comment=comment,
            product=product)

    def close_reduce(self, symbol, entry_side, volume, slippage_points=None,
                     comment='', product='NRML', deadline_sec=None):
        """Get out of `volume` LOTS, by crossing the other way.

        This is what `close_ticket` became. `entry_side` is the side
        the position was ENTERED on — every caller in the ported code
        already holds it, and taking the closing side instead would
        invert silently the first time somebody passed the wrong one.

        A guard may withhold an order. **A guard never prevents a
        close**, and nothing in this path can.
        """
        units, refusal = self._units(symbol, volume)
        if refusal:
            return {'ok': False, 'filled_volume': 0.0, 'price': None,
                    'unresolved': False, 'error': refusal}
        result = self.session.close_by_reduction(
            symbol, OrderSide(entry_side), units, comment=comment,
            product=product, deadline_sec=deadline_sec)
        return self._fill(symbol, result)

    #: The old name, so ported call sites read the same. It does NOT
    #: take a ticket — there are none — and the signature says so.
    close_ticket = close_reduce

    def _fill(self, symbol, result):
        """An OrderResult in the dict shape the ported code reads.

        `filled_volume` comes back in LOTS, because that is what the
        book and the P&L speak. The units are carried beside it so a
        report can show what actually went to the exchange.
        """
        lot_size = self.lot_size(symbol)
        units = result.volume or 0.0
        return {
            'ok': result.success,
            'filled_volume': (units / float(lot_size)) if lot_size else 0.0,
            'filled_units': units,
            'price': result.executed_price,
            'requested_price': result.requested_price,
            'ticket': result.ticket,
            'position_tickets': [result.ticket] if result.ticket else [],
            #: TRUE MEANS WE DO NOT KNOW. Not a rejection, and never a
            #: reason to unwind — the order may fill a moment later.
            'unresolved': result.unresolved,
            'error': result.error,
        }

    def modify_order(self, ticket, price, symbol=None):
        return self.session.modify_pending(ticket, price, symbol=symbol)

    def cancel_order(self, ticket):
        return self.session.cancel_pending(ticket)

    def order_state(self, ticket):
        return self.session.order_fill_state(ticket)

    def verify_order(self, ticket):
        return dict(self.session.verify_ticket(ticket), account=self.name)

    # -- what is out there ------------------------------------------------

    def positions(self, symbol=None):
        """The NET, per (symbol, product). **None = unknown, not flat.**"""
        return self.session.net_positions(symbol)

    def pending_orders(self, symbol=None):
        """Resting orders. **None = unknown, not "no orders".**"""
        return self.session.working_orders(symbol)

    def order_log(self, hours=24):
        rows = self.session.order_log(hours)
        if rows is None:
            return None      # unknown, NOT "no activity"
        return [dict(row, account=self.name) for row in rows]


def make_legs(names, session):
    """One session, one leg object per configured leg name.

    They share the session by construction, so there is no way to end
    up with two logins by accident — which on MT5 was a red banner and
    here is simply not expressible.
    """
    if not names:
        logging.warning('Arrow: no legs configured')
    return {name: ArrowLeg(name, session) for name in names}
