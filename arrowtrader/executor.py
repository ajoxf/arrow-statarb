"""Getting a pair ON, and getting it OFF again.

This module owns the dangerous moments in the product. MT5-Trader has
two of them; here there are three, and the new one is the reason this
is not a copy.

**1. The naked window** — one leg on, the other not yet landed.
**2. The close** — which on MT5 had to name a position ticket and here
     crosses the other way instead, because the exchange nets.
**3. THE ORDER WHOSE OUTCOME WE DO NOT KNOW.** New, and it is
     structural. `mt5.order_send` returns `result.price`: when the call
     returns, the trade has happened or it has not. Arrow returns an
     order id, and the fill is polled out of the order book — so an
     order can be neither filled nor rejected but simply STILL WORKING
     when we stop waiting.

Three rules, each of which cost money in the system this is ported
from, and a fourth that this venue adds:

1. **The crossing order goes IMMEDIATELY on fill — not after a wait.**
   `LEG_DEADLINE_SEC` is a failure-ESCALATION window, not a patience
   window.
2. **Only a REJECTED crossing leg unwinds.** Slow is not rejected.
3. **The unwind CLOSES, it does not offset.** On MT5 that meant closing
   by ticket, because a plain opposite order opened a second position.
   Here the exchange nets, so an opposite order IS the close — the rule
   survives, its implementation does not.
4. **AN UNRESOLVED LEG IS NOT A REJECTED LEG, AND MUST NOT BE
   UNWOUND.** This is rule 2 with teeth. On MT5 "slow" meant a call
   that had not returned; here it means an order that may be sitting in
   the exchange's book about to fill. Unwinding on it does not undo a
   hedge — it opens a position facing the other way, which then fills
   against the leg we were trying to cancel. The honest answer is to
   say we do not know, loudly, and let the reconciler settle it against
   the exchange's own net.
"""

import logging
import time

from . import costs, sizing
from .book import ReductionPlan
from .models import (LegFill, OrderSide, OrderType, SpreadPosition,
                     SpreadSide, new_id)
from .spread import closing_prices, executable_spread


class ExecutionResult:
    """What a click did, in the words the ladder will print.

    `reason` is never "check the log" — it carries the broker's own
    words where the broker is the one who refused.
    """

    def __init__(self, ok, position=None, reason=None, refused=False,
                 naked=None, elapsed_ms=None, legs=None, unresolved=None):
        self.ok = ok
        self.position = position
        self.reason = reason
        #: Refused BEFORE anything moved — no money at risk. Distinct
        #: from a failure that left a leg on.
        self.refused = refused
        #: Set when a leg is on and its hedge is not. The loudest thing
        #: on the screen.
        self.naked = naked
        #: Set when we do not KNOW whether a leg is on. Louder still,
        #: because it cannot be acted on automatically at all.
        self.unresolved = unresolved
        self.elapsed_ms = elapsed_ms
        self.legs = legs or {}

    def to_dict(self):
        return {'ok': self.ok, 'reason': self.reason, 'refused': self.refused,
                'naked': self.naked, 'unresolved': self.unresolved,
                'elapsed_ms': self.elapsed_ms,
                'position': self.position.to_dict() if self.position else None,
                'legs': {key: (value.to_dict() if hasattr(value, 'to_dict')
                               else value)
                         for key, value in self.legs.items()}}


class PairExecutor:
    """Executes one click on one pair, across its two contracts."""

    def __init__(self, config, legs, clock=time.time, sleep=time.sleep):
        self.config = config
        #: {leg name: ArrowLeg}. Both hold the same Arrow session.
        self.legs = legs
        self.clock = clock
        self.sleep = sleep
        #: Every fill's click->pair-on time, so the operator can see
        #: whether the deadline is the right one.
        self.timings = []
        #: Called with (position_id, reason) BEFORE a position is
        #: closed, whoever closes it. The coordinator points it at the
        #: quoter's disarm, so a resting take-profit is pulled FIRST
        #: and then the position goes — never the other way round,
        #: which leaves a resting order with a guaranteed fill and
        #: nothing to close.
        self.before_close = None

    # -- entry ------------------------------------------------------------

    def market_entry(self, pair, side, md, spreads=None, clicked_level=None):
        """Cross both legs now. The clicked price is the slippage guard.

        ON ARROW THE CLICKED PRICE IS THE **ONLY** SLIPPAGE GUARD.
        `mt5.order_send` takes a `deviation` and the server enforces it;
        `place_order` takes nothing at all. So `protection_breach`
        below is not a belt-and-braces check on top of the broker's —
        it is the whole of it.

        Returns an ExecutionResult. Nothing here loops or re-enters:
        one click is one order.
        """
        side = SpreadSide(getattr(side, 'value', side))
        spreads = float(spreads if spreads is not None
                        else pair.default_quantity)
        started = self.clock()

        refusal = self.precheck(pair, side, md, spreads, clicked_level)
        if refusal is not None:
            return ExecutionResult(False, reason=refusal, refused=True)

        plan = self.size(pair, md, spreads)
        leg_a_side, leg_b_side = side.leg_sides()
        # The decision touch — what the market offered at the moment the
        # order was decided. Slippage is scored against THIS, not the mid.
        decision_spread = executable_spread(md, side)

        first, second = self.crossing_order(pair)
        sides = {'a': leg_a_side, 'b': leg_b_side}
        volumes = {'a': plan['leg_a_lots'], 'b': plan['leg_b_lots']}
        contracts = {'a': plan['leg_a_contract'], 'b': plan['leg_b_contract']}
        tag = new_id('LADDER')

        # The harder-to-fill leg FIRST. Filling the easy leg and then
        # discovering the hard one will not fill is how you end up naked.
        first_fill = self._send_leg(pair, first, sides[first], volumes[first],
                                    contracts[first], tag)
        if first_fill.get('unresolved'):
            # WE DO NOT KNOW whether this leg is on. Nothing else is
            # sent — a hedge against a position that may not exist is
            # an outright either way round — and nothing is unwound,
            # because unwinding an order that is still working opens
            # the opposite position and then fills against it.
            return self._unresolved(pair, first, first_fill, started,
                                    {first: first_fill})
        if not first_fill.get('ok'):
            # Nothing is on. This is a refusal, not a naked position.
            return ExecutionResult(
                False, refused=True,
                reason=(f"leg {first.upper()} refused: "
                        f"{first_fill.get('error')}"))

        # IMMEDIATELY — no wait, no poll interval, no patience.
        second_fill = self._cross_with_deadline(
            pair, second, sides[second], volumes[second], contracts[second],
            tag)

        elapsed_ms = (self.clock() - started) * 1000.0
        fills = {first: first_fill, second: second_fill}

        if second_fill.get('unresolved'):
            # Leg one is ON. Leg two may be on, may be about to be, may
            # never be. This is the worst state the system can reach and
            # it is NOT unwound automatically: cancelling into a working
            # order races it, and an opposite market order against a leg
            # that then fills leaves TWO positions where there should be
            # none.
            return self._unresolved(pair, second, second_fill, started, fills,
                                    on_leg=first, on_fill=first_fill)

        if not second_fill.get('ok'):
            # A genuine rejection. Unwind the leg that IS on.
            reason = (f"leg {second.upper()} rejected: "
                      f"{second_fill.get('error')} — unwinding leg "
                      f"{first.upper()}")
            logging.critical('%s: %s', pair.key, reason)
            unwind = self._unwind_leg(pair, first, sides[first], first_fill)
            naked = None if unwind.get('ok') else {
                'leg': first.upper(),
                'symbol': self._symbol(pair, first),
                'volume': first_fill.get('filled_volume'),
                'why': unwind.get('error'),
            }
            return ExecutionResult(False, reason=reason, naked=naked,
                                   elapsed_ms=elapsed_ms, legs=fills)

        matched = self._matched_fraction(plan, fills)
        if matched < float(self.config.get('MIN_MATCHED_FRACTION', 0.4)):
            reason = (f"only {matched:.0%} of the clip matched on both legs "
                      f"— unwinding rather than holding a part-hedged pair")
            logging.warning('%s: %s', pair.key, reason)
            self._unwind_leg(pair, 'a', sides['a'], fills['a'])
            self._unwind_leg(pair, 'b', sides['b'], fills['b'])
            return ExecutionResult(False, reason=reason,
                                   elapsed_ms=elapsed_ms, legs=fills)

        position = self._book_position(pair, side, spreads, plan, fills,
                                       sides, decision_spread, elapsed_ms)
        self.timings.append(elapsed_ms)
        return ExecutionResult(True, position=position,
                               elapsed_ms=elapsed_ms, legs=fills)

    def _unresolved(self, pair, leg, fill, started, fills, on_leg=None,
                    on_fill=None):
        """The third outcome, said in words a person can act on.

        There is exactly one correct action and it is not automatic:
        look at the order, at the exchange, and decide. So this returns
        the order id, the symbol, and — where a leg is already on — what
        is exposed if the unresolved one never fills.
        """
        exposure = None
        if on_leg is not None:
            exposure = {
                'leg': on_leg.upper(),
                'symbol': self._symbol(pair, on_leg),
                'volume': (on_fill or {}).get('filled_volume'),
            }
        reason = (
            f"leg {leg.upper()} ({self._symbol(pair, leg)}) is UNRESOLVED: "
            f"{fill.get('error')}. Nothing has been unwound — an order that "
            f"is still working cannot be cancelled by trading against it, "
            f"and doing so would leave two positions instead of none."
            + (f" Leg {exposure['leg']} IS ON for "
               f"{exposure['volume']:g} lots and is NAKED until this is "
               f"settled." if exposure and exposure.get('volume') else ''))
        logging.critical('%s: %s', pair.key, reason)
        return ExecutionResult(
            False, reason=reason, legs=fills,
            elapsed_ms=(self.clock() - started) * 1000.0,
            unresolved={'leg': leg.upper(),
                        'symbol': self._symbol(pair, leg),
                        'ticket': fill.get('ticket'),
                        'exposed': exposure})

    # -- the checks that happen BEFORE anything moves ---------------------

    def precheck(self, pair, side, md, spreads, clicked_level=None):
        """A one-line reason to refuse this click, or None.

        Both legs are verified up front: a pair whose child order is
        under either leg's minimum, or whose lot size is unknown, must
        be refused before any money moves.
        """
        if md is None:
            return (f"{pair.key} has no price yet — both legs need a live "
                    f"book before an order can be sized")
        for leg in ('a', 'b'):
            account = pair.account_a if leg == 'a' else pair.account_b
            if account not in self.legs:
                return (f"leg {leg.upper()} ('{account}') is not connected "
                        f"— check the Arrow session on the Exchanges page")

        plan = self.size(pair, md, spreads)
        if plan.get('reason'):
            return plan['reason']

        guard = md.get('guard_reason')
        if guard:
            return f'{guard} — the click was not sent'

        if clicked_level is not None:
            breach = self.protection_breach(pair, side, md, clicked_level)
            if breach:
                return breach
        return None

    def protection_breach(self, pair, side, md, clicked_level):
        """Refuse a fill worse than the clicked spread by more than
        MARKET_PROTECTION_TICKS.

        THE ONLY SLIPPAGE PROTECTION THERE IS. On MT5 this sat on top of
        the server's own `deviation`; Arrow's `place_order` has no such
        parameter, so removing or weakening this leaves a market click
        filling at whatever the touch happens to be — which on a
        desynced print is exactly the fault the jump guard exists for.
        """
        increment = pair.effective_increment()
        if not increment:
            return None                      # nothing to measure in
        ticks = float(self.config.get('MARKET_PROTECTION_TICKS', 3.0) or 0.0)
        if ticks <= 0:
            return None
        touch = executable_spread(md, side)
        if touch is None:
            return None
        # Worse means: paying MORE than clicked to buy, receiving LESS
        # than clicked to sell.
        slip = (touch - clicked_level) if side is SpreadSide.BUY \
            else (clicked_level - touch)
        allowed = ticks * increment
        if slip > allowed:
            return (f'the market is {slip:.4g} through your '
                    f'{clicked_level:.4g} — more than the {ticks:g} x '
                    f'{increment:g} protection. Nothing was sent; click '
                    f'again at the price on screen.')
        return None

    def size(self, pair, md, spreads):
        """Leg lots and `k` for this click — one place, one answer."""
        return sizing.clip_plan(pair, pair.meta_a or {}, pair.meta_b or {},
                                (md or {}).get('leg_a_mid'),
                                (md or {}).get('leg_b_mid'), spreads)

    def crossing_order(self, pair):
        """(first, second) leg keys — the harder-to-fill leg FIRST.

        On MT5 that was decided by minimum volume and then book width.
        Every contract here has a minimum of one lot, so that first term
        is gone — and what replaces it is DEPTH, not width, because
        depth is the question this ordering actually asks.

        "Harder to fill" is a question about whether the leg will fill
        AT ALL, not about what it costs. A book with one lot on the
        touch may not fill three; a wide book with fifty lots on it
        fills in full and merely costs more. Width is a cost, and a cost
        does not leave you naked. So depth decides, thinnest first, and
        width only breaks the tie.

        UNKNOWN DEPTH IS NO OPINION, and that has to mean falling back
        to width rather than picking a number. Sorting an unknown as
        zero would send every unmeasured leg first; sorting it as
        infinite would send the risky one second. So when either leg's
        depth is unknown, both are compared on width alone.
        """
        meta_a, meta_b = pair.meta_a or {}, pair.meta_b or {}
        depth_a, depth_b = meta_a.get('touch_size'), meta_b.get('touch_size')

        if depth_a is not None and depth_b is not None:
            score_a = (-float(depth_a), meta_a.get('width') or 0.0)
            score_b = (-float(depth_b), meta_b.get('width') or 0.0)
        else:
            score_a = (0.0, meta_a.get('width') or 0.0)
            score_b = (0.0, meta_b.get('width') or 0.0)
        return ('b', 'a') if score_b >= score_a else ('a', 'b')

    # -- leg mechanics ----------------------------------------------------

    def _symbol(self, pair, leg):
        return pair.symbol_a if leg == 'a' else pair.symbol_b

    def _segment(self, pair, leg):
        return getattr(pair, 'segment_a' if leg == 'a' else 'segment_b', None)

    def _leg(self, pair, leg):
        account = pair.account_a if leg == 'a' else pair.account_b
        return self.legs.get(account)

    def _product(self, pair):
        """NRML by default. MIS is a deliberate per-pair choice, because
        the broker squares an MIS position off near the close without
        asking — and a spread half-squared-off is an outright."""
        return getattr(pair, 'product', None) or 'NRML'

    def _send_leg(self, pair, leg, side, volume, contract, tag):
        runner = self._leg(pair, leg)
        symbol = self._symbol(pair, leg)
        if runner is None:
            return {'ok': False, 'error': f'no session for {symbol}'}
        result = runner.order(
            symbol, side.value, volume,
            product=self._product(pair),
            # Stamped so our own orders are tellable from the trader's
            # own dealing in the order log — where the build carries a
            # tag through at all.
            comment=f'{tag}:{leg.upper()}')
        result = dict(result or {})
        result['symbol'] = symbol
        result['side'] = side.value
        result['contract_size'] = contract
        return result

    def _cross_with_deadline(self, pair, leg, side, volume, contract, tag):
        """Send the crossing leg, retrying only until the deadline.

        The deadline is not patience — there is nothing to be patient
        for, the hedge must go on. It bounds how long a genuine fault
        can leave us naked before it is escalated.

        AN UNRESOLVED RESULT ENDS THE LOOP AT ONCE. Retrying it would
        send a SECOND order for the same hedge while the first is still
        working, and on a netting account two fills do not cancel out —
        they double the leg.
        """
        deadline = self.clock() + float(
            self.config.get('LEG_DEADLINE_SEC', 2.0))
        attempt = 0
        while True:
            attempt += 1
            result = self._send_leg(pair, leg, side, volume, contract, tag)
            if result.get('ok') or result.get('unresolved'):
                result['attempts'] = attempt
                return result
            if self.clock() >= deadline:
                result['attempts'] = attempt
                result['error'] = (f"{result.get('error')} (after {attempt} "
                                   f"attempts in "
                                   f"{self.config.get('LEG_DEADLINE_SEC')}s)")
                return result
            self.sleep(0.02)

    def _matched_fraction(self, plan, fills):
        """How much of the intended clip is hedged on BOTH legs.

        Hedge to what actually FILLED, not to what was sent.
        """
        wanted_a = plan['leg_a_lots'] or 0.0
        wanted_b = plan['leg_b_lots'] or 0.0
        got_a = (fills.get('a') or {}).get('filled_volume') or 0.0
        got_b = (fills.get('b') or {}).get('filled_volume') or 0.0
        if not wanted_a or not wanted_b:
            return 0.0
        return min(got_a / wanted_a, got_b / wanted_b)

    def _unwind_leg(self, pair, leg, entry_side, fill):
        """Close what is on, by crossing the other way.

        On MT5 this had to name a position ticket, because a plain
        opposite order opened a second position beside the first. Here
        the exchange nets and the opposite order IS the close — the
        rule ("the unwind CLOSES, it does not offset") survives, its
        implementation does not.

        What does NOT survive is doing this on an unresolved order.
        Callers check that first; if one ever stops, this would be the
        line that turns a maybe-fill into a definite double.
        """
        runner = self._leg(pair, leg)
        symbol = self._symbol(pair, leg)
        if runner is None:
            return {'ok': False, 'error': f'no session for {symbol}'}
        volume = float(fill.get('filled_volume') or 0.0)
        if volume <= 0:
            # Nothing filled, so there is nothing to unwind. Not an
            # error — it is the good case.
            return {'ok': True, 'closed': [], 'left': 0.0}
        result = runner.close_reduce(symbol, entry_side, volume,
                                     product=self._product(pair),
                                     comment='UNWIND')
        if result.get('unresolved'):
            return {'ok': False, 'unresolved': True,
                    'error': (f'{symbol}: the unwind is UNRESOLVED — '
                              f'{result.get("error")}')}
        if not result.get('ok'):
            return {'ok': False, 'error': result.get('error')}
        got = float(result.get('filled_volume') or 0.0)
        return {'ok': got >= volume - 1e-9,
                'closed': [{'volume': got, 'price': result.get('price')}],
                'left': max(0.0, volume - got),
                'error': (None if got >= volume - 1e-9 else
                          f'{symbol}: the unwind closed {got:g} of '
                          f'{volume:g} lots — {volume - got:g} still on')}

    # -- exit ---------------------------------------------------------------

    def close_spread(self, pair, plan, md=None, reason='manual', disarm=True):
        """Close a whole ReductionPlan — ONE order per leg.

        This is the contract `book.reduce_first` calls, and the shape
        netting forces. On MT5 a close named a ticket, so N ledger
        positions meant N pairs of orders; here the exchange nets, so
        the whole reduction crosses once per leg.

        A GUARD NEVER PREVENTS A CLOSE. Nothing in this path consults
        the staleness state, the jump state, or the price protection.
        A trade must always be closable.

        Returns `{ok, fraction, imbalanced, legs, reason}`. `fraction`
        is of the whole plan and is measured from what the two closes
        actually FILLED — never from what was asked for.
        """
        lots_a, lots_b = plan.leg_lots()
        entry_a, entry_b = plan.entry_sides()
        if entry_a is None:
            return {'ok': True, 'fraction': 0.0, 'legs': {}, 'reason': None}

        wanted, refusal = self._closable_lots(pair, lots_a, lots_b)
        if wanted is None:
            # Not a guard, and not a close being withheld: this is the
            # exchange's own lot step saying the PIECE asked for cannot
            # be traded on both legs — and closing one leg of a hedge is
            # worse than closing neither.
            logging.warning('%s: close refused: %s', pair.key, refusal)
            return {'ok': False, 'fraction': 0.0, 'refused': True,
                    'legs': {}, 'reason': refusal}

        if disarm and self.before_close is not None:
            # Disarm any resting close BEFORE closing. Afterwards is too
            # late: the level it was armed against no longer has a
            # position, and it would fire on the next tick that reaches it.
            for position, _take in plan.takes:
                try:
                    self.before_close(position.position_id,
                                      f'position closed ({reason})')
                except Exception as error:        # never block a close
                    logging.error('disarm before close failed: %s', error)

        results = {}
        for leg, entry_side, lots in (('a', entry_a, wanted['a']),
                                      ('b', entry_b, wanted['b'])):
            results[leg] = self._close_leg(pair, leg, entry_side, lots)

        fraction, imbalanced = self._closed_fraction(wanted, results)
        ok = all(result.get('ok') for result in results.values())
        if not ok:
            logging.error('%s: close failed, the position stays ACTIVE: %s',
                          pair.key, results)
        return {'ok': ok, 'fraction': fraction, 'imbalanced': imbalanced,
                'legs': results, 'reason': None,
                'closed_spread': closed_spread(results, pair.hedge_ratio)}

    def _close_leg(self, pair, leg, entry_side, lots):
        """Cross one leg the other way. Never withheld, never guarded."""
        runner = self._leg(pair, leg)
        symbol = self._symbol(pair, leg)
        if runner is None:
            return {'ok': False, 'error': f'no session for {symbol}',
                    'wanted': lots, 'closed': []}
        if not lots:
            return {'ok': True, 'wanted': 0.0, 'closed': [], 'left': 0.0}
        result = runner.close_reduce(symbol, entry_side, lots,
                                     product=self._product(pair),
                                     comment='CLOSE')
        got = float(result.get('filled_volume') or 0.0)
        if result.get('unresolved'):
            # WE DO NOT KNOW how much came off. Reporting it as zero
            # would keep the whole position on our books while it may
            # have gone; reporting it as filled would take it off while
            # it may still be there. So it is neither: ok is False, the
            # position stays ACTIVE, and the reason says why.
            return {'ok': False, 'unresolved': True, 'wanted': lots,
                    'closed': [], 'left': None,
                    'error': (f'{symbol}: the close is UNRESOLVED — '
                              f'{result.get("error")}')}
        return {
            'ok': bool(result.get('ok')),
            'wanted': lots,
            # WHAT FILLED, not what was asked. A market close can come
            # back short, and booking the request as the fill takes lots
            # off our record that are still at the exchange.
            'closed': ([{'volume': got, 'price': result.get('price')}]
                       if got else []),
            'left': max(0.0, lots - got),
            'error': result.get('error'),
        }

    def _closable_lots(self, pair, lots_a, lots_b):
        """Both legs' lots for a close, quantised so BOTH can trade it.

        Lots are whole here, so a plan of 2.5 spreads on a 1:1 pair asks
        for 2.5 lots a leg and neither leg can do it. Each leg rounds
        DOWN to its own step, the smaller resulting share wins, and both
        legs are re-derived from that one share — so what comes off is
        still hedged.

        Down and not up, because closing MORE than the click asked for
        moves the net the other way, which is the mistake at the far end.

        (None, why) when no piece works: the smallest tradable step on
        one leg is bigger than the whole piece asked for. The caller
        refuses the CLICK then, which is safe — nothing opens on top of
        a close that did not happen.
        """
        wanted = {'a': float(lots_a or 0.0), 'b': float(lots_b or 0.0)}
        metas = {'a': pair.meta_a or {}, 'b': pair.meta_b or {}}
        share = 1.0
        for leg, lots in wanted.items():
            if lots <= 0:
                continue
            meta = metas[leg]
            step = meta.get('volume_step') or 1.0
            minimum = meta.get('volume_min') or 1.0
            tradable = sizing.round_step(lots, step, minimum, down=True)
            if tradable <= 0:
                return None, self._too_small(pair, leg, meta, lots)
            share = min(share, tradable / lots)

        out = {}
        for leg, lots in wanted.items():
            if lots <= 0:
                out[leg] = 0.0
                continue
            meta = metas[leg]
            tradable = sizing.round_step(lots * share,
                                         meta.get('volume_step') or 1.0,
                                         meta.get('volume_min') or 1.0,
                                         down=True)
            if tradable <= 0:
                # The other leg pulled the share down far enough that
                # this one no longer reaches its own minimum.
                return None, self._too_small(pair, leg, meta, lots)
            out[leg] = tradable
        return out, None

    def _too_small(self, pair, leg, meta, lots):
        """Why this piece cannot be closed, with the size that can be.

        The trader's next move is to click a bigger size, and guessing
        it from "invalid quantity" is not a thing to do with a position
        on.
        """
        floor = max(float(meta.get('volume_step') or 1.0),
                    float(meta.get('volume_min') or 1.0))
        return (f'this close is {lots:g} lots on leg {leg.upper()} '
                f'({self._symbol(pair, leg)}), under the {floor:g} lots the '
                f'exchange can trade. Close more of it, or all of it.')

    def _closed_fraction(self, wanted, results):
        """How much of the plan actually came off, and whether the two
        legs came off TOGETHER.

        MT5 measured this from what was LEFT of our tickets at the
        broker, because deal history lagged and a ticket it no longer
        listed was already gone. There are no tickets here and no lag to
        work around: the close order's own filled quantity is the
        measurement, read back from the order book by `await_fill`.

        The hedged part is the SMALLER of the two legs. A close that
        took more off one leg than the other has not closed a spread —
        it has left an imbalance — and booking the larger of them would
        take lots off our record that are still at the exchange.

        `imbalanced` is the case that has to be shouted about: one leg
        came down and the other did not move at all.
        """
        shares = []
        for leg in ('a', 'b'):
            asked = float(wanted.get(leg) or 0.0)
            if asked <= 0:
                continue
            result = results.get(leg) or {}
            if result.get('left') is None:
                # Unmeasured. NOT zero, and not one: no opinion, which
                # makes the whole close unmeasured below.
                return 0.0, True
            got = asked - float(result['left'])
            shares.append(max(0.0, min(1.0, got / asked)))
        if not shares:
            return 0.0, False
        hedged = min(shares)
        # One leg down, the other untouched. The legs are IMBALANCED at
        # the exchange and a person has to look.
        imbalanced = hedged <= 1e-9 and max(shares) > 1e-9
        return hedged, imbalanced

    def close_position(self, pair, position, md=None, reason='manual',
                       disarm=True, quantity=None):
        """Close ONE position — a plan of one, so there is one path.

        Every close in the system goes through `close_spread`: the
        manual close, CLOSE ALL, the kill, the overnight rule, an
        AutoRouting take-profit and a reduce-first click. Two
        implementations of "how does a position come off" is two answers
        to reconcile the day they disagree, on a live book.
        """
        take = float(quantity if quantity is not None else position.quantity)
        take = max(0.0, min(take, float(position.quantity or 0.0)))
        plan = ReductionPlan([(position, take)], pair=pair)
        result = self.close_spread(pair, plan, md, reason=reason,
                                   disarm=disarm)
        if not result.get('ok') or result.get('imbalanced'):
            if result.get('imbalanced'):
                logging.critical(
                    '%s: position %s closed on one leg only — the other could '
                    'not match it. The legs are IMBALANCED at the exchange; '
                    'check it by hand.', pair.key, position.position_id)
            return dict(result, position=position.to_dict())
        # THE TWO FRACTIONS ARE NOT THE SAME NUMBER.
        #
        # `close_spread` reports what fraction of THE ORDER IT SENT came
        # off — which is what `reduce_first` needs, because its plan
        # spans several positions. `_settle` books a fraction of THIS
        # POSITION. Closing 4 of 10 spreads and having all 4 fill is a
        # fraction of 1.0 by the first reading and 0.4 by the second,
        # and passing the first where the second belongs closes the
        # whole position in the book while six spreads are still on.
        held = float(position.quantity or 0.0)
        share = float(result.get('fraction') or 0.0) * (take / held
                                                        if held else 0.0)
        self._settle(pair, position, plan, result, md, reason, share)
        return dict(result, position=position.to_dict())

    def _settle(self, pair, position, plan, result, md, reason, fraction):
        """Book a close that has already gone through.

        One place, so a manual close, a flatten, the overnight rule and
        an AutoRouting take-profit all mark the position the same way —
        and all leave it ACTIVE when the close did not go through.
        """
        filled_spread = result.get('closed_spread')
        # The price the exit was DECIDED at: the touch this position
        # would have closed at when the close was sent.
        decision_spread = (executable_spread(md, position.side, closing=True)
                           if md else None)
        exit_spread = (decision_spread if filled_spread is None
                       else filled_spread)
        gross = position.mark(exit_spread)
        charges = costs.mark_fees(position, self.config.settings)
        earned = None if gross is None else gross * fraction - charges * fraction

        whole = fraction >= 1.0 - 1e-9
        if whole:
            position.closed_at = self.clock()
            position.close_reason = reason
            position.exit_spread = exit_spread
            # Anchored on the EXECUTED fills for the same reason the
            # entry is. Where the exchange reported no close price there
            # is nothing to anchor on, and the SLIPPAGE stays None
            # rather than becoming a flattering zero.
            position.exit_slippage = slippage(decision_spread, filled_spread,
                                              position.side, closing=True)
            # ACCUMULATED. A position closed in pieces earned its P&L in
            # pieces, and the earlier pieces are already booked.
            position.realized_pnl = (
                earned if position.realized_pnl is None
                else (position.realized_pnl if earned is None
                      else position.realized_pnl + earned))
            position.quantity = 0.0
            for fill in (position.leg_a, position.leg_b):
                if fill is not None:
                    fill.volume = 0.0
            return

        # PART of it. The exit price and slippage are deliberately NOT
        # set: they describe the exit of a position, and this one has
        # not exited. Recording the first piece's price as "the" exit
        # would make the number wrong for the trade the moment the rest
        # goes.
        left = position.reduce_by(fraction, realized=earned)
        logging.warning(
            '%s: position %s closed %.1f%% (%s) and is STILL ON for %s '
            'spreads — both legs reduced together, so it is not naked.',
            pair.key, position.position_id, fraction * 100.0, reason, left)

    # -- bookkeeping --------------------------------------------------------

    def _book_position(self, pair, side, spreads, plan, fills, sides,
                       decision_spread, elapsed_ms):
        fill_a, fill_b = fills['a'], fills['b']
        leg_a = LegFill(pair.account_a, pair.symbol_a, sides['a'],
                        fill_a.get('filled_volume') or 0.0,
                        fill_a.get('price'),
                        order_ticket=fill_a.get('ticket'),
                        position_tickets=fill_a.get('position_tickets'),
                        contract_size=plan['leg_a_contract'], clock=self.clock,
                        segment=self._segment(pair, 'a'),
                        product=self._product(pair))
        leg_b = LegFill(pair.account_b, pair.symbol_b, sides['b'],
                        fill_b.get('filled_volume') or 0.0,
                        fill_b.get('price'),
                        order_ticket=fill_b.get('ticket'),
                        position_tickets=fill_b.get('position_tickets'),
                        contract_size=plan['leg_b_contract'], clock=self.clock,
                        segment=self._segment(pair, 'b'),
                        product=self._product(pair))

        # Anchored on the EXECUTED fills, never on the mid the decision
        # was taken at.
        entry_spread = None
        if leg_a.price is not None and leg_b.price is not None:
            entry_spread = leg_b.price - float(pair.hedge_ratio) * leg_a.price

        position = SpreadPosition(
            pair.key, side, spreads, leg_a, leg_b, entry_spread,
            OrderType.MARKET,
            sizing.spread_units(leg_b.volume, plan['leg_b_contract']),
            clock=self.clock)
        position.click_to_on_ms = elapsed_ms
        position.entry_slippage = slippage(decision_spread, entry_spread, side)
        return position


def closed_spread(results, hedge_ratio):
    """`B - beta x A` of the prices the CLOSE actually filled at.

    Volume-weighted, because a close can fill in pieces. None when
    either leg reported no price: half a spread is not a spread, and a
    number built from one leg would be reported as an exit price nobody
    traded.
    """
    prices = {}
    for leg in ('a', 'b'):
        fills = [fill for fill in (results.get(leg) or {}).get('closed') or ()
                 if fill.get('price') is not None]
        volume = sum(float(fill.get('volume') or 0.0) for fill in fills)
        if not fills or volume <= 0:
            return None
        prices[leg] = sum(float(fill['price']) * float(fill['volume'] or 0.0)
                          for fill in fills) / volume
    return prices['b'] - float(hedge_ratio) * prices['a']


def slippage(expected, filled, side, closing=False):
    """Positive is a COST, always — including on exits.

    A short buys the spread back to close, so the sign flips between
    entry and exit; getting that backwards reports every exit's cost as
    a gain. Unmeasured returns None, which the UI renders as an em dash.
    """
    if expected is None or filled is None:
        return None
    side = SpreadSide(getattr(side, 'value', side))
    buying = (side is SpreadSide.BUY) != bool(closing)
    return (filled - expected) if buying else (expected - filled)


def mark_position(position, md, settings):
    """(gross, net, closing_spread) for an open position, marked at the
    touches it would actually CLOSE at.

    Consequence, and it is correct: a position shows a loss the instant
    it opens, equal to one round turn of both legs' bid-ask. That is
    what closing immediately would cost, and the UI says so rather than
    hiding it.

    Only CHARGES are subtracted. The crossing is already in the two
    prices; subtracting the round trip again is the bid-ask twice.
    """
    closing_spread = executable_spread(md, position.side, closing=True)
    gross = position.mark(closing_spread)
    if gross is None:
        return None, None, closing_spread
    return gross, gross - costs.mark_fees(position, settings), closing_spread


def leg_marks(position, md):
    """The two prices this position would be closed at, per leg.

    Agrees with `mark_position` by construction: `B - beta x A` of these
    two IS the closing executable spread, and a test pins that.
    """
    return closing_prices(md, position.side)
