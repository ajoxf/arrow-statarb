"""The engine: one loop, every guard, and one snapshot per pass.

Ported from MT5-Trader's `coordinator.py`. It holds the legs, polls the
book, keeps the guards, drains clicks on their own thread, and writes
ONE status snapshot that every panel renders from.

Three things are different here and each of them shows on the screen.

**There is one session, not two processes.** MT5-Trader runs a leg
runner per account over localhost because the MetaTrader5 package holds
one connection per process; this coordinator holds one Arrow session
and hands the same object to both legs. The `dark_accounts` machinery
survives, because a session whose token has expired is exactly as
absent as a runner that never started — and reporting it as "quiet" is
the confusion that gets orders clicked into a screen with nothing
behind it.

**A guard withholding an order is not the only refusal any more.** An
order can also come back UNRESOLVED, and that is neither a fill nor a
refusal: it needs a person. It gets its own field in the snapshot and
its own banner, above the naked-leg one, because it is worse — a naked
leg is a known exposure and an unresolved order is an unknown one.

**The sweep runs at startup as well as shutdown, and now it finds
things.** On MT5 nothing at the broker knew what a spread was, so no
working order survived the process and a startup sweep was a formality.
Here a leg order rests at the exchange and outlives us: a resting entry
that fills while we are down leaves an outright, not a hedge.
"""

import logging
import threading
import time

from . import session as session_mod, sizing
from .book import Book, reduce_first
from .executor import PairExecutor, mark_position
from .models import OrderType, SpreadSide, TimeInForce
from .quoter import Quoter, quoting_leg
from .reconcile import Reconciler
from .spread import (LevelSigma, QuoteAgeTracker, SpreadJumpTracker,
                     compute_spread, executable_spread, stale_quote)


#: How long a dead order stays on the ladder with its reason. A rejected
#: order used to vanish in the same instant the click was accepted: the
#: trader saw a green toast and then an empty Work column, with the
#: refusal nowhere on the screen.
DEAD_ORDER_MEMORY_SEC = 20.0


class Coordinator:

    def __init__(self, config, legs, status_path='status.json',
                 store=None, clock=time.time, sleep=time.sleep):
        self.config = config
        self.legs = legs
        self.status_path = status_path
        self.store = store
        self.clock = clock
        self.sleep = sleep
        self.book = Book()
        self.executor = PairExecutor(config, legs, clock=clock, sleep=sleep)
        self.quoter = Quoter(config, legs, self.executor, self.book,
                             clock=clock)
        self.reconciler = Reconciler(config, legs, self.book, self.executor,
                                     clock=clock)
        self.session = session_mod.SessionClock(
            config, offset=self.exchange_offset)
        self.market = {}            # pair key -> the last snapshot
        self.errors = {}            # pair key -> reasons
        self.ages = QuoteAgeTracker()
        self.jumps = SpreadJumpTracker()
        self.sigmas = {}            # pair key -> LevelSigma
        #: Orders we do not know the outcome of. They are NOT failures
        #: and NOT fills, they need a person, and they stay here until
        #: one has looked.
        self.unresolved = []
        self.recovery = {'complete': False, 'note': 'not started'}
        self.session_events = []
        self._offset = None
        self._offset_at = 0.0
        self._loop_interval = None
        self._ladder_locked = {}
        self._ladder_anchor = {}
        self._stop = threading.Event()
        self._lock = threading.RLock()

    # -- starting and stopping ------------------------------------------------

    def start(self):
        """Connect, recover the book, and sweep before anything else.

        THE ORDER MATTERS AND IT IS NOT THE MT5 ORDER. There, recovery
        could take its time: nothing was resting at the broker, so
        nothing could fill while we were getting ready. Here a resting
        order from the last run is live at the exchange RIGHT NOW and
        can fill before we have read the book it belongs to — so it is
        swept first, and the book is recovered against a quiet account.
        """
        for name, leg in self.legs.items():
            if not leg.connect():
                logging.error("could not connect leg '%s'", name)
        self.sweep_resting('startup')
        self.recover()
        self.resolve_symbols()
        return self

    def recover(self):
        """Bring the book back from disk, and gate the reconciler on it."""
        if self.store is None:
            self.recovery = {
                'complete': False,
                'note': ('no database, so the book starts EMPTY and may not '
                         'be. Nothing will be auto-closed and every position '
                         'at the exchange will read as unexplained.')}
            self.reconciler.book_complete = False
            return self.recovery
        from .database import recover
        self.recovery = recover(self.store, self.book, clock=self.clock)
        self.reconciler.book_complete = bool(self.recovery.get('complete'))
        return self.recovery

    def sweep_resting(self, reason):
        """Pull every resting order we can find, scoped as tightly as
        the account allows — and SAY what scope that was.

        On a dedicated account every resting order is ours and the
        sweep is complete. On a shared one it is not, and the sweep
        touches only what THIS PROCESS placed — which after a restart
        is nothing at all. That gap is real and is reported rather than
        glossed: an order from the previous run is still live at the
        exchange and this process no longer knows its id.
        """
        pulled = []
        for pair in self.config.pairs.values():
            pulled.extend(self.quoter.sweep(pair, reason))
        dedicated = getattr(self.config.account, 'dedicated', False)
        note = None
        if not dedicated:
            note = ('this account is not declared dedicated, so the sweep '
                    'pulled only orders THIS process placed. A resting order '
                    'from a previous run is still live at the exchange and '
                    'cannot be told from your own — check the broker\'s own '
                    'order book.')
        detail = {'reason': reason, 'pulled': len(pulled),
                  'scope': 'the account' if dedicated else 'this process',
                  'note': note}
        event = dict(detail, at=self.clock(), kind='sweep')
        self.session_events.append(event)
        if self.store is not None:
            self.store.event('sweep', **detail)
        return event

    def stop(self):
        self._stop.set()
        self.sweep_resting('shutdown')
        for leg in self.legs.values():
            leg.close()

    # -- symbols ---------------------------------------------------------------

    def resolve_symbols(self):
        """Read every leg's specs from the master and cache them.

        A pair whose lot size cannot be resolved is left with EMPTY
        metadata rather than plausible defaults, so `clip_plan` refuses
        it by name instead of sizing it wrongly.
        """
        for key, pair in self.config.pairs.items():
            problems = []
            for leg_key, symbol in (('a', pair.symbol_a),
                                    ('b', pair.symbol_b)):
                runner = self.executor._leg(pair, leg_key)
                if runner is None or not symbol:
                    problems.append(f'leg {leg_key.upper()} is not configured')
                    continue
                found = runner.ensure_symbol(symbol)
                if not found.get('ok'):
                    problems.append(found.get('error'))
                    continue
                meta = dict(found)
                override = (pair.contract_size_a if leg_key == 'a'
                            else pair.contract_size_b)
                if override:
                    # LOUD, because every money figure runs through it.
                    meta['contract_size'] = override
                    meta['contract_size_overridden'] = True
                    problems.append(
                        f'leg {leg_key.upper()} lot size is OVERRIDDEN to '
                        f'{override:g}; the master says '
                        f'{found.get("contract_size")}')
                setattr(pair, f'meta_{leg_key}', meta)
            self.errors[key] = [p for p in problems if p]
        return self.errors

    # -- the clock ---------------------------------------------------------------

    def exchange_offset(self):
        """Seconds the exchange's clock runs ahead of ours, or None.

        Cached: it does not drift on the scale of a poll and it is a
        round trip. None is UNKNOWN, and unknown means the session
        cutoff does not fire.
        """
        ttl = float(self.config.get('BROKER_CLOCK_TTL_SEC', 300.0))
        if self._offset is not None and self.clock() - self._offset_at < ttl:
            return self._offset
        for leg in self.legs.values():
            found = leg.server_offset()
            if found is not None:
                self._offset = found
                self._offset_at = self.clock()
                return found
        return None

    # -- the poll ------------------------------------------------------------------

    def poll_once(self):
        with self._lock:
            return self._poll_once()

    def _poll_once(self):
        started = self.clock()
        for key, pair in self.config.enabled_pairs().items():
            md = self._read_pair(pair)
            self.market[key] = md
            if md is not None:
                self.quoter.work(pair, md)
        self._loop_interval = self.clock() - started
        return self.market

    def _read_pair(self, pair):
        """Both legs' books into one spread snapshot, with its guards."""
        runner_a = self.executor._leg(pair, 'a')
        runner_b = self.executor._leg(pair, 'b')
        if runner_a is None or runner_b is None:
            return None
        tick_a = runner_a.tick(pair.symbol_a)
        tick_b = runner_b.tick(pair.symbol_b)
        md = compute_spread(pair, tick_a, tick_b, pair.hedge_ratio,
                            clock=self.clock)
        if md is None:
            # A leg with no book prices NOTHING. Say which, rather than
            # drawing a ladder around a number nobody can trade at.
            from .quotes import missing_book_reason
            self.errors.setdefault(pair.key, [])
            reason = missing_book_reason(tick_a, tick_b, pair.symbol_a,
                                         pair.symbol_b)
            if reason and reason not in self.errors[pair.key]:
                self.errors[pair.key] = [reason]
            return None
        self.ages.observe(pair.key, md)
        sigma = self.sigmas.setdefault(
            pair.key, LevelSigma(self.config.get('SIGMA_WINDOW_QUOTES', 600)))
        sigma.observe(md)
        reasons = []
        stale = stale_quote(md, pair.max_quote_age_sec
                            if pair.max_quote_age_sec is not None
                            else self.config.get('MAX_QUOTE_AGE_SEC'))
        if stale:
            reasons.append(stale)
        jumped = self.jumps.observe(
            pair.key, md, sigma.sigma,
            self.config.get('MAX_SPREAD_JUMP_SIGMA'),
            self.config.get('JUMP_SETTLE_SEC'))
        if jumped:
            reasons.append(jumped)
        # ONE field the whole system reads, so the ladder, the executor
        # and the quoter cannot disagree about whether a price is
        # usable.
        md['guard_reason'] = reasons[0] if reasons else None
        md['guard_reasons'] = reasons
        md['tender'] = self._tender(pair)
        return md

    def _tender(self, pair):
        """How close either leg is to delivery. MCX settles physically."""
        notes = []
        for leg_key in ('a', 'b'):
            meta = (pair.meta_a if leg_key == 'a' else pair.meta_b) or {}
            days = meta.get('days_to_expiry')
            if days is None:
                continue
            warn = float(self.config.get('TENDER_WARN_DAYS', 7) or 7)
            if days <= warn:
                symbol = pair.symbol_a if leg_key == 'a' else pair.symbol_b
                notes.append(
                    f'{symbol} expires in {days} day'
                    f'{"" if days == 1 else "s"} — inside the tender window. '
                    f'A position carried in can be assigned for DELIVERY.')
        return notes or None

    # -- clicks ----------------------------------------------------------------------

    def click(self, pair_key, side, level, quantity=None, order_type=None):
        """One click: one order. Drained on its own thread."""
        with self._lock:
            return self._click(pair_key, side, level, quantity, order_type)

    def _click(self, pair_key, side, level, quantity=None, order_type=None):
        pair = self.config.pairs.get(pair_key)
        if pair is None:
            return {'ok': False, 'reason': f'{pair_key} is not a pair'}
        md = self.market.get(pair_key)
        side = SpreadSide(getattr(side, 'value', side))
        spreads = float(quantity if quantity is not None
                        else pair.default_quantity)
        kind = OrderType(getattr(order_type, 'value', order_type)
                         or pair.order_type)

        refusal = self._tender_refusal(pair)
        if refusal:
            return {'ok': False, 'reason': refusal, 'refused': True}

        if kind is OrderType.MARKET:
            if self._away_from_the_market(pair, side, md, level):
                if not self.config.get('CLICK_AWAY_RESTS', True):
                    return {'ok': False, 'refused': True,
                            'reason': (f'{level:g} cannot be crossed from '
                                       f'here — a buy under the offer fills '
                                       f'at no price')}
                kind = OrderType.LIMIT      # it RESTS, and the toast says so

        if kind is OrderType.MARKET:
            return self._market_click(pair, side, level, spreads, md)
        return self._rest_click(pair, side, level, spreads)

    def _market_click(self, pair, side, level, spreads, md):
        closed, left, failure = reduce_first(
            self.book, self.executor, pair, side, spreads, md,
            on_closed=lambda position: self.quoter.disarm(
                position.position_id, 'its position was closed'))
        if failure:
            # NOTHING opens on top of a close that did not happen.
            return {'ok': False, 'reason': failure, 'closed': closed}
        if left <= 0:
            self.remember_all()
            return {'ok': True, 'closed': closed, 'opened': None}
        result = self.executor.market_entry(pair, side, md, spreads=left,
                                            clicked_level=level)
        if result.unresolved:
            self._note_unresolved(pair, result)
        if result.ok and result.position is not None:
            self.book.add_position(result.position)
            self._auto_route(pair, result.position, md)
            self.remember(result.position)
        return dict(result.to_dict(), closed=closed)

    def _rest_click(self, pair, side, level, spreads):
        order = self.book.add_order(pair, side, level, spreads,
                                    order_type=OrderType.LIMIT)
        self.quoter.group_for(pair, order)
        return {'ok': True, 'order_id': order.order_id,
                'resting': True, 'level': level}

    def _away_from_the_market(self, pair, side, md, level):
        """A click that cannot cross at any price."""
        if md is None or level is None:
            return False
        touch = executable_spread(md, side)
        if touch is None:
            return False
        return (level < touch - 1e-9) if side is SpreadSide.BUY \
            else (level > touch + 1e-9)

    def _tender_refusal(self, pair):
        """Withhold a new OPEN inside the tender window, if asked to.

        Only ever an OPEN. A guard never prevents a close, so this is
        not consulted anywhere on the exit path.
        """
        if not self.config.get('REFUSE_OPEN_IN_TENDER'):
            return None
        notes = self._tender(pair)
        if not notes:
            return None
        return (f'{notes[0]} Opening inside the tender window is turned off '
                f'in settings.')

    def _auto_route(self, pair, position, md):
        """On a fill, rest a close at the take-profit. A target, no stop."""
        if not (pair.auto_route and self.config.get('AUTO_ROUTE_ENABLED')):
            return None
        settings = pair.exit_settings(self.config.settings)
        target = self.take_profit(pair, position, settings)
        if target is None:
            return None
        return self.quoter.arm(pair, position, target, auto=True)

    def take_profit(self, pair, position, settings):
        """Break-even plus a target on the MARGIN one spread ties up.

        None where the margin could not be priced — which is the normal
        case until Arrow's own calculator is wired up. It is NEVER
        derived from notional: margin here is SPAN plus exposure, set
        by the exchange, and a number computed from notional would be a
        guess presented as a figure.
        """
        if position.entry_spread is None or not position.spread_units:
            return None
        margin = self.margin_per_spread(pair)
        if margin is None:
            return None
        pct = float(settings.get('TP_TARGET_PCT_OF_MARGIN', 0.0) or 0.0)
        move = (margin * pct / 100.0) / position.spread_units
        return (position.entry_spread + move
                if position.side is SpreadSide.BUY
                else position.entry_spread - move)

    def margin_per_spread(self, pair):
        """SPAN + exposure for ONE spread, both legs priced TOGETHER.

        Together, because a calendar spread attracts a margin BENEFIT
        and pricing the legs separately misses it entirely — which is
        the main economic reason to trade the spread as a spread.
        """
        runner = self.executor._leg(pair, 'a')
        if runner is None:
            return None
        units_a = sizing.units(pair.clip_lots_a,
                               (pair.meta_a or {}).get('contract_size'))
        units_b = sizing.units(pair.clip_lots_b,
                               (pair.meta_b or {}).get('contract_size'))
        if not units_a or not units_b:
            return None
        return runner.margin_for([(pair.symbol_a, 'SELL', units_a),
                                  (pair.symbol_b, 'BUY', units_b)])

    def _note_unresolved(self, pair, result):
        entry = dict(result.unresolved, at=self.clock(), pair_key=pair.key,
                     reason=result.reason)
        self.unresolved.append(entry)
        if self.store is not None:
            # `pair_key` is a COLUMN on the events table and also a key
            # in this detail; passing both is a duplicate-argument
            # TypeError at the worst possible moment — the one code
            # path that runs when we do not know what an order did.
            detail = {name: value for name, value in entry.items()
                      if name != 'pair_key'}
            self.store.event('unresolved', pair_key=pair.key, **detail)

    def clear_unresolved(self, ticket):
        """A person has looked at it. Only they can clear it."""
        before = len(self.unresolved)
        self.unresolved = [row for row in self.unresolved
                           if row.get('ticket') != ticket]
        return before != len(self.unresolved)

    # -- closing ----------------------------------------------------------------------

    def flatten(self, pair_key, reason='flatten'):
        """Close everything on one pair, now. Never withheld."""
        pair = self.config.pairs.get(pair_key)
        if pair is None:
            return {'ok': False, 'reason': f'{pair_key} is not a pair'}
        md = self.market.get(pair_key)
        results = []
        for position in list(self.book.positions(pair_key)):
            self.quoter.disarm(position.position_id, 'flattened')
            results.append(self.executor.close_position(pair, position, md,
                                                        reason=reason))
        self.remember_all()
        return {'ok': all(r.get('ok') for r in results), 'closed': results}

    def cancel_all(self, pair_key=None, side=None):
        for order in self.book.cancel_where(pair_key, side):
            self.quoter.cancel(order)
        return True

    # -- housekeeping -------------------------------------------------------------------

    def remember(self, position):
        if self.store is not None:
            self.store.save_position(position)
        return position

    def remember_all(self):
        for position in self.book.positions(open_only=False):
            self.remember(position)

    def run_session_cutoff(self):
        """The one cutoff: DAY orders die, the overnight rule decides.

        Per SEGMENT, because MCX and NSE close hours apart — and a
        missed cutoff is REPORTED, because MCX's 23:30 close leaves
        only thirty minutes to midnight and a restarting engine walks
        straight over it.
        """
        fired = []
        for key, pair in self.config.pairs.items():
            missed = self.session.missed(key, pair.segment_b)
            if missed:
                self.session_events.append({'at': self.clock(),
                                            'kind': 'missed_cutoff',
                                            'pair_key': key, 'note': missed})
                if self.store is not None:
                    self.store.event('missed_cutoff', pair_key=key,
                                     note=missed)
            self.session.seen(key)
            if not self.session.due(key, pair.segment_b):
                continue
            for order in session_mod.day_orders(self.book.orders(key)):
                self.book.cancel(order.order_id, 'the session cutoff')
                self.quoter.cancel(order)
            md = self.market.get(key)
            settings = pair.exit_settings(self.config.settings)
            now = self.session.exchange_now()
            hour, minute, _source = self.session.cutoff_for(pair.segment_b)
            for position in list(self.book.positions(key)):
                _gross, net, _closing = mark_position(position, md, settings)
                if session_mod.overnight_action(pair.overnight, net, now,
                                                hour, minute):
                    self.quoter.disarm(position.position_id, 'the cutoff')
                    self.executor.close_position(pair, position, md,
                                                 reason='OVERNIGHT_CLOSE')
            self.session.mark(key)
            fired.append(key)
        if fired:
            self.remember_all()
        return fired

    def reconcile_if_due(self):
        return self.reconciler.run()

    def dark_accounts(self):
        """Legs that are not answering. NAMED, not omitted.

        An absent account looks exactly like a quiet one on a screen
        that does not say so — and that is the confusion that gets
        orders clicked into a ladder with nothing behind it.
        """
        return [name for name, leg in self.legs.items() if not leg.ping()]

    # -- the snapshot ---------------------------------------------------------------------

    def snapshot(self):
        """Everything every panel needs, from one poll's data."""
        pairs = {}
        for key, pair in self.config.pairs.items():
            md = self.market.get(key)
            settings = pair.exit_settings(self.config.settings)
            net, avg_entry = self.book.net_position(key)
            buys, sells = self.book.working_counts(key)
            positions = []
            open_pnl = 0.0
            for position in self.book.positions(key):
                gross, net_pnl, closing = mark_position(position, md, settings)
                positions.append(dict(position.to_dict(),
                                      gross_pnl=gross, net_pnl=net_pnl,
                                      closing_spread=closing))
                # UNMEASURED IS NOT ZERO. One position that cannot be
                # marked makes the TOTAL unknown, rather than an
                # authoritative-looking number that is silently short.
                if net_pnl is None:
                    open_pnl = None
                elif open_pnl is not None:
                    open_pnl += net_pnl
            pairs[key] = {
                'key': key, 'name': pair.name, 'enabled': pair.enabled,
                'symbol_a': pair.symbol_a, 'symbol_b': pair.symbol_b,
                'segment_a': pair.segment_a, 'segment_b': pair.segment_b,
                'product': pair.product,
                'pair_type': pair.pair_type,
                'hedge_ratio': pair.hedge_ratio,
                'hedge_ratio_for': pair.hedge_ratio_for,
                'increment': pair.effective_increment(),
                'increment_derived': pair.derived_increment(),
                'order_type': pair.order_type.value,
                'exit_type': pair.exit_type.value,
                'time_in_force': pair.time_in_force.value,
                'overnight': pair.overnight.value,
                'auto_route': pair.auto_route,
                'auto_route_on': bool(pair.auto_route and self.config.get(
                    'AUTO_ROUTE_ENABLED', False)),
                'default_quantity': pair.default_quantity,
                'max_qty': sizing.max_qty(pair, pair.meta_a, pair.meta_b),
                'row_count': pair.rows,
                'clip_lots_a': pair.clip_lots_a,
                'clip_lots_b': pair.clip_lots_b,
                # LOT SIZE — units per lot, from the master. Named
                # `contract_*` so the front end reads it unchanged.
                'contract_a': (pair.meta_a or {}).get('contract_size'),
                'contract_b': (pair.meta_b or {}).get('contract_size'),
                'contract_a_overridden': (pair.meta_a or {}).get(
                    'contract_size_overridden', False),
                'contract_b_overridden': (pair.meta_b or {}).get(
                    'contract_size_overridden', False),
                # What ACTUALLY goes on the wire for one Qty.
                'units_a': sizing.units(
                    pair.clip_lots_a, (pair.meta_a or {}).get('contract_size')),
                'units_b': sizing.units(
                    pair.clip_lots_b, (pair.meta_b or {}).get('contract_size')),
                'freeze_qty_a': (pair.meta_a or {}).get('freeze_qty'),
                'freeze_qty_b': (pair.meta_b or {}).get('freeze_qty'),
                'expiry_a': (pair.meta_a or {}).get('expiry'),
                'expiry_b': (pair.meta_b or {}).get('expiry'),
                'days_to_expiry_a': (pair.meta_a or {}).get('days_to_expiry'),
                'days_to_expiry_b': (pair.meta_b or {}).get('days_to_expiry'),
                'tender': (md or {}).get('tender'),
                'square_off': session_mod.squares_off(pair,
                                                      self.config.settings),
                'spread_units': sizing.spread_units(
                    pair.clip_lots_b,
                    (pair.meta_b or {}).get('contract_size')),
                'market': md,
                'short_spread': (md or {}).get('short_spread'),
                'long_spread': (md or {}).get('long_spread'),
                'errors': self.errors.get(key) or [],
                'orders': [order.to_dict()
                           for order in self.book.orders(key)],
                'dead_orders': [
                    order.to_dict()
                    for order in self.book.orders(key, working_only=False)
                    if not order.is_working and order.reason
                    and self.clock() - order.created_at
                    < DEAD_ORDER_MEMORY_SEC],
                'quotes': self.quoter.snapshot(key),
                'quoting_leg': pair.quoting_leg,
                'quoting_leg_effective': quoting_leg(pair),
                'leg_a_width': (pair.meta_a or {}).get('width'),
                'leg_b_width': (pair.meta_b or {}).get('width'),
                'working_buys': buys, 'working_sells': sells,
                'resting_closes': sum(1 for order in self.book.orders(key)
                                      if order.position_id),
                'positions': positions,
                'net_position': net, 'avg_entry': avg_entry,
                'open_pnl': open_pnl if positions else None,
                'last_print': self.book.last_print(key),
                'session': self.session.describe(pair.segment_b),
            }
        return {
            'at': self.clock(),
            'confirm_market_clicks': bool(
                self.config.get('CONFIRM_MARKET_CLICKS', False)),
            'row_height_px': self.config.get('ROW_HEIGHT_PX', 17),
            'click_convention': self.config.get('CLICK_CONVENTION', 'TOUCH'),
            'recentre_sec': self.config.get('RECENTRE_SEC', 5.0),
            'click_away_rests': bool(self.config.get('CLICK_AWAY_RESTS',
                                                     True)),
            'command_poll_sec': self.config.get('COMMAND_POLL_SEC', 0.02),
            'loop_interval_sec': self._loop_interval,
            'poll_target_sec': self.config.get('POLL_INTERVAL_SEC'),
            'currency': 'INR',
            'accounts': {name: leg.account_info()
                         for name, leg in self.legs.items()},
            'dark_accounts': self.dark_accounts(),
            #: ONE ACCOUNT, ONE MARGIN POOL. MT5-Trader's "the pair can
            #: only be carried by the weaker of the two brokers" does
            #: not apply and must not be shown by habit.
            'single_pool': True,
            'dedicated': getattr(self.config.account, 'dedicated', False),
            'reconciler': self.reconciler.snapshot(),
            'recovery': dict(self.recovery),
            #: ABOVE the naked-leg banner on the screen. A naked leg is
            #: a KNOWN exposure; an unresolved order is an unknown one,
            #: and nothing may act on it automatically.
            'unresolved': list(self.unresolved),
            'session_events': self.session_events[-50:],
            'hedge_times_ms': list(self.quoter.hedge_times[-50:]),
            'click_to_on_ms': list(self.executor.timings[-50:]),
            'pairs': pairs,
        }

    def publish(self):
        """Write the snapshot through a tmp file and `os.replace`."""
        from . import atomicfile
        import json
        payload = self.snapshot()
        tmp = self.status_path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as handle:
            json.dump(payload, handle, default=str)
        atomicfile.replace(tmp, self.status_path)
        return payload

    def run(self):
        interval = float(self.config.get('POLL_INTERVAL_SEC', 0.3))
        while not self._stop.is_set():
            try:
                self.poll_once()
                self.run_session_cutoff()
                self.publish()
            except Exception as error:                  # noqa: BLE001
                logging.exception('poll failed: %s', error)
            self.sleep(interval)
