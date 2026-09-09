"""Assume you will lose track. Then find out, and say so.

Ported from MT5-Trader's `reconcile.py`. The DISCIPLINE survives intact
and the mechanism is replaced outright, because the thing it compared
does not exist here.

MT5-Trader matches TICKETS. Every position it opened has a ticket, the
broker lists tickets, and a ticket in one list and not the other is an
orphan or a ghost. Arrow nets: the exchange holds ONE quantity per
(symbol, product) and cannot say which part of it came from which
order, let alone which system.

So this compares NETS. Our ledger says what the book expects each
contract to be; the exchange says what it is; the difference is the
finding. Two shapes, and they are not symmetrical:

- **EXCESS** — more at the exchange than our book expects. On MT5 this
  was unambiguous, because the magic number proved the position was
  ours. Here it proves nothing.
- **SHORTFALL** — less at the exchange than our book expects. Our
  ledger claims a position that is not there: squared off by the broker
  on MIS, closed by hand, or taken by an RMS action.

## THE THING THAT CANNOT BE FIXED IN CODE

On a SHARED account an excess is genuinely unattributable. Our net and
the trader's own dealing in the same contract are one number, and no
API field separates them. So on a shared account this module **never
auto-closes an excess** and never calls one an orphan: it says it
cannot tell, names the contract, and stops. A dedicated account
restores the guarantee MT5-Trader gets from its magic number — and
whether the account IS dedicated is the operator's declaration, so it
is reported beside every finding rather than assumed.

Four rules kept exactly:

1. **Three strikes, not one.** A single poll can catch the exchange
   mid-fill, and acting on that is how a healthy position is closed for
   being briefly invisible.
2. **A close that did not go through leaves the position OPEN and
   ACTIVE.** Marking it ERROR removes it from every active lookup — the
   ladder, the monitor, and this module's own expectations — so the
   money sits at the exchange while the UI reads flat.
3. **None is not empty.** A leg that could not be read is UNKNOWN, and
   an unknown account produces no findings at all. Treating it as flat
   would turn every position we hold into a shortfall and clear the
   book while the money is still out there.
4. **Nothing is auto-closed until recovery says the book is complete.**
   An orphan is only an orphan if we are sure it is not ours.
"""

import logging
import time

from .models import SpreadSide


class Reconciler:

    #: Strikes before acting. One poll can catch the exchange mid-fill.
    STRIKES = 3

    def __init__(self, config, legs, book, executor, clock=time.time):
        self.config = config
        self.legs = legs
        self.book = book
        self.executor = executor
        self.clock = clock
        #: (symbol, product) -> consecutive polls the difference held.
        self.strikes = {}
        self.close_failures = {}
        #: What we closed that we never opened, with what it cost. The
        #: operator can read this; it is not a log line.
        self.untracked_closes = []
        #: Differences we have given up on, so the broker is not
        #: hammered.
        self.escalated = set()
        self.last_run = None
        self.unknown_accounts = []
        #: Set by recovery at startup. FALSE means our book may be
        #: incomplete, and nothing is auto-closed while it is false.
        self.book_complete = False
        #: Differences a person has to decide about. Never struck out,
        #: never auto-closed: adopt, or close.
        self.unclaimed = {}

    @property
    def dedicated(self):
        """Is this Arrow account used by nothing else?

        The single most consequential fact in this module. It is a
        DECLARATION, not a measurement — nothing at the exchange can
        confirm it — so it is reported beside every finding rather than
        quietly relied on.
        """
        return bool(getattr(getattr(self.config, 'account', None),
                            'dedicated', False))

    # -- what we think, and what is actually there ------------------------

    def expected(self):
        """What our LEDGER says each contract should be, in LOTS.

        Signed: long positive. Summed across every open position,
        because two ladders can hold the same contract — a GOLD
        Dec/Feb and a GOLD Feb/Apr share the February leg, and the
        exchange nets them into one number that belongs to neither
        ladder alone.
        """
        wanted = {}
        for position in self.book.positions():
            pair = self.config.pairs.get(position.pair_key)
            if pair is None:
                continue
            side_a, side_b = position.side.leg_sides()
            for symbol, side, fill in (
                    (pair.symbol_a, side_a, position.leg_a),
                    (pair.symbol_b, side_b, position.leg_b)):
                if not symbol or fill is None:
                    continue
                lots = float(fill.volume or 0.0)
                if lots <= 0:
                    continue
                key = (symbol, fill.product or pair.product)
                signed = lots if side.value == 'BUY' else -lots
                wanted[key] = wanted.get(key, 0.0) + signed
        return wanted

    def actual(self):
        """What the exchange holds, in LOTS. None where it is UNKNOWN.

        Returns (nets, unknown account names). An account that could
        not be read contributes NOTHING — not zero.
        """
        nets = {}
        unknown = []
        for name, leg in self.legs.items():
            positions = leg.positions()
            if positions is None:
                # UNKNOWN, not flat.
                unknown.append(name)
                logging.warning(
                    "reconcile: account '%s' could not be read — skipped, "
                    "NOT treated as flat", name)
                continue
            for row in positions:
                lots = row.get('volume')
                if lots is None:
                    # The net is there but the lot size is not, so we
                    # cannot compare it against a book kept in lots.
                    # Unmeasured is not zero.
                    continue
                signed = lots if row.get('side') == 'BUY' else -lots
                key = (row.get('symbol'), row.get('product') or 'NRML')
                nets[key] = nets.get(key, 0.0) + signed
        return nets, unknown

    # -- the pass ----------------------------------------------------------

    def run(self):
        """One pass. Returns what it found and what it did."""
        self.last_run = self.clock()
        nets, unknown = self.actual()
        self.unknown_accounts = unknown
        if unknown:
            # An account we could not read makes EVERY comparison
            # meaningless, not just its own: our book does not know
            # which account a contract sits on any better than the
            # exchange does.
            return self._report([], 'an account could not be read')

        wanted = self.expected()
        findings = []
        seen = set()
        for key in set(wanted) | set(nets):
            seen.add(key)
            difference = round((nets.get(key, 0.0)
                                - wanted.get(key, 0.0)), 9)
            if abs(difference) < 1e-9:
                self.strikes.pop(key, None)
                self.unclaimed.pop(key, None)
                continue
            strikes = self.strikes.get(key, 0) + 1
            self.strikes[key] = strikes
            findings.append(self._finding(key, wanted.get(key, 0.0),
                                          nets.get(key, 0.0), difference,
                                          strikes))
        # A contract that has agreed for a whole pass stops being
        # counted against: strikes are CONSECUTIVE, or a flicker every
        # hour accumulates into an action nobody watched happen.
        for key in list(self.strikes):
            if key not in seen:
                self.strikes.pop(key, None)
        return self._report(findings)

    def _finding(self, key, wanted, actual, difference, strikes):
        symbol, product = key
        # THREE KINDS, NOT TWO. Magnitude alone cannot express the
        # worst of them: expecting +2 lots and finding -2 is the same
        # SIZE and the opposite exposure, so it reads as neither an
        # excess nor a shortfall while being worse than both — the
        # position is four lots away from where the book thinks it is,
        # and the hedge is inverted rather than merely wrong.
        if wanted and actual and (wanted > 0) != (actual > 0):
            kind = 'REVERSED'
        elif abs(actual) > abs(wanted) + 1e-9 or (
                wanted == 0.0 and actual != 0.0):
            kind = 'EXCESS'
        else:
            kind = 'SHORTFALL'
        finding = {
            'symbol': symbol, 'product': product, 'kind': kind,
            'expected_lots': wanted, 'actual_lots': actual,
            'difference_lots': difference, 'strikes': strikes,
            'ready': strikes >= self.STRIKES,
            'dedicated': self.dedicated,
            'note': None, 'actionable': False,
        }
        if kind == 'REVERSED':
            finding['note'] = (
                f'our book expects {wanted:g} lots of {symbol} on {product} '
                f'and the exchange holds {actual:g} — the OTHER WAY ROUND. '
                f'The exposure is {abs(actual - wanted):g} lots from where '
                f'the book thinks it is and the hedge is inverted, not '
                f'merely mis-sized. Nothing here can guess how that '
                f'happened; look at the order book before trading this '
                f'pair again.')
            # Ours to explain whoever else uses the account: no amount
            # of somebody else's dealing turns our long into a short.
            finding['actionable'] = finding['ready'] and self.book_complete
        elif kind == 'SHORTFALL':
            finding['note'] = (
                f'our book expects {wanted:g} lots of {symbol} on {product} '
                f'and the exchange holds {actual:g}. Something closed it '
                f'that we did not: an MIS square-off, a manual close, or an '
                f'RMS action. The other leg of that spread may now be '
                f'NAKED.')
            # A shortfall is always ours to explain — it is OUR book
            # that is wrong — so it is actionable whoever else uses the
            # account.
            finding['actionable'] = finding['ready'] and self.book_complete
        elif not self.dedicated:
            # THE CASE THAT CANNOT BE RESOLVED IN CODE.
            finding['note'] = (
                f'the exchange holds {actual:g} lots of {symbol} on {product} '
                f'and our book expects {wanted:g}. THIS ACCOUNT IS NOT '
                f'DECLARED DEDICATED, so the difference may be your own '
                f'dealing and nothing here can tell. Nothing will be closed '
                f'automatically. Declare the account dedicated on the '
                f'Exchanges page if this system is the only thing using it.')
        else:
            finding['note'] = (
                f'the exchange holds {actual:g} lots of {symbol} on {product} '
                f'and our book expects {wanted:g}. On a dedicated account '
                f'nothing else should have put it there.')
            finding['actionable'] = finding['ready'] and self.book_complete

        if finding['ready'] and key not in self.escalated:
            # EVERY ready finding goes in front of a person, not only
            # the ones we could not act on ourselves.
            #
            # MT5-Trader auto-closes an orphan after three strikes,
            # and it can: the magic number proves the position is its
            # own. Nothing proves that here. `actionable` therefore
            # means "a person MAY act on this", not "we will" — `run`
            # closes nothing, ever, and a test pins that.
            self.unclaimed[key] = dict(finding, at=self.clock())
        if finding['ready'] and not self.book_complete:
            finding['note'] += (
                ' Recovery has not finished, so nothing may be closed yet: '
                'an orphan is only an orphan if we are sure it is not ours.')
        return finding

    def _report(self, findings, skipped=None):
        return {
            'at': self.last_run,
            'findings': findings,
            'unknown_accounts': list(self.unknown_accounts),
            'skipped': skipped,
            'book_complete': self.book_complete,
            'dedicated': self.dedicated,
        }

    # -- what a person can do about it -------------------------------------

    def adopt(self, symbol, product='NRML'):
        """Stop reporting this difference — the operator says it is theirs.

        It does NOT create a position in our book. Our ledger records
        what this system opened, at what price; adopting a net we did
        not open would put a fabricated entry price into every P&L
        figure downstream. What it does is stop asking.
        """
        key = (symbol, product)
        self.unclaimed.pop(key, None)
        self.strikes.pop(key, None)
        self.escalated.add(key)
        logging.warning('reconcile: %s on %s ADOPTED by the operator — it '
                        'will not be reported again this session, and it is '
                        'NOT in the book', symbol, product)
        return {'ok': True, 'symbol': symbol, 'product': product}

    def close_excess(self, symbol, product='NRML', lots=None):
        """Flatten a difference the operator has decided is not theirs.

        Only ever from a person's click. Nothing in `run` calls this.
        """
        key = (symbol, product)
        finding = self.unclaimed.get(key)
        if finding is None:
            return {'ok': False, 'error': f'nothing unclaimed on {symbol}'}
        amount = abs(float(lots if lots is not None
                           else finding['difference_lots']))
        leg = next(iter(self.legs.values()), None)
        if leg is None:
            return {'ok': False, 'error': 'no session'}
        # The side it is ON, so `close_reduce` crosses the other way.
        entry_side = 'BUY' if finding['difference_lots'] > 0 else 'SELL'
        result = leg.close_reduce(symbol, entry_side, amount,
                                  product=product, comment='UNCLAIMED')
        if result.get('unresolved'):
            return {'ok': False, 'unresolved': True,
                    'error': result.get('error')}
        if not result.get('ok'):
            self.close_failures[key] = self.close_failures.get(key, 0) + 1
            if self.close_failures[key] >= int(
                    self.config.get('CLOSE_ATTEMPTS', 3)):
                self.escalated.add(key)
            return {'ok': False, 'error': result.get('error')}
        entry = {'symbol': symbol, 'product': product,
                 'lots': result.get('filled_volume'),
                 'price': result.get('price'), 'at': self.clock()}
        self.untracked_closes.append(entry)
        self.unclaimed.pop(key, None)
        self.strikes.pop(key, None)
        logging.critical('reconcile: closed UNCLAIMED %s on %s — %s lots at '
                         '%s', symbol, product, entry['lots'], entry['price'])
        return {'ok': True, **entry}

    def lot_size(self, symbol):
        """(lot size, assumed?) — from the pairs' own master metadata.

        MT5-Trader's equivalent returns 1.0 and SAYS SO when it cannot
        find one, because its P&L multiplies by contract size and a
        silent 1.0 booked closes at 1% of what they cost. Here it
        returns **None**, because units are lots x LotSize and a silent
        1 does not understate a P&L — it sends an order a hundred times
        the wrong size.
        """
        for pair in self.config.pairs.values():
            for symbol_name, meta in ((pair.symbol_a, pair.meta_a),
                                      (pair.symbol_b, pair.meta_b)):
                if symbol_name == symbol:
                    size = (meta or {}).get('contract_size')
                    if size:
                        return float(size), False
        return None, True

    def naked_legs(self):
        """Spreads whose two legs no longer match at the exchange.

        The consequence a shortfall has that a bare number does not
        convey: an MIS square-off takes ONE contract, and what is left
        of a spread with one leg gone is an outright in a commodity.
        """
        nets, unknown = self.actual()
        if unknown:
            return []
        naked = []
        for position in self.book.positions():
            pair = self.config.pairs.get(position.pair_key)
            if pair is None:
                continue
            side_a, side_b = position.side.leg_sides()
            held = []
            for symbol, side, fill in (
                    (pair.symbol_a, side_a, position.leg_a),
                    (pair.symbol_b, side_b, position.leg_b)):
                key = (symbol, (fill.product if fill else None)
                       or pair.product)
                held.append(abs(nets.get(key, 0.0)) > 1e-9)
            if any(held) and not all(held):
                naked.append({
                    'position_id': position.position_id,
                    'pair_key': position.pair_key,
                    'on': pair.symbol_a if held[0] else pair.symbol_b,
                    'gone': pair.symbol_b if held[0] else pair.symbol_a,
                    'note': (f'{position.pair_key}: one leg is gone at the '
                             f'exchange and the other is still on. That is '
                             f'not a spread any more — it is an outright.'),
                })
        return naked

    def snapshot(self):
        """What the monitor's Reconciler tab shows."""
        return {
            'last_run': self.last_run,
            'strikes': {f'{symbol}:{product}': count
                        for (symbol, product), count in self.strikes.items()},
            'untracked_closes': list(self.untracked_closes),
            'escalated': [f'{symbol}:{product}'
                          for symbol, product in self.escalated],
            'unknown_accounts': list(self.unknown_accounts),
            'book_complete': self.book_complete,
            #: THE FACT EVERY FINDING DEPENDS ON, on the screen beside
            #: them rather than buried in a config file.
            'dedicated': self.dedicated,
            'unclaimed': [dict(row) for row in self.unclaimed.values()],
            'naked_legs': self.naked_legs() if self.legs else [],
        }
