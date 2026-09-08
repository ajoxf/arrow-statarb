"""The bridge between the web process and the engine.

Ported from MT5-Trader's `commands.py`, essentially unchanged — the
problem it solves has nothing to do with which broker is underneath.

**The watermark is PRIMED at startup, and that is the whole point.** In
the system this is ported from every watermark initialised to 0, so on
restart the engine replayed the ENTIRE history of commands in half a
second — opening an unintended live position and placing real orders.
A command is not state: replaying "place this order" is placing another
order.

So: persistent STATE (which ladders exist, what is configured) lives in
the config; a COMMAND (place this, cancel that) is executed once, by
the process that was running when it was written, and never again.
"""

import json
import logging
import os
import time
import uuid

from . import atomicfile
from .models import OrderType, OvernightMode, TimeInForce


class CommandLog:
    """Append-only commands, written by the web process."""

    def __init__(self, path, clock=time.time):
        self.path = path
        self.clock = clock

    def submit(self, kind, payload=None):
        command = {'id': uuid.uuid4().hex[:12], 'kind': kind,
                   'at': self.clock(), 'payload': payload or {}}
        with open(self.path, 'a', encoding='utf-8') as handle:
            handle.write(json.dumps(command) + '\n')
            handle.flush()
            os.fsync(handle.fileno())
        return command['id']

    def read_all(self):
        try:
            with open(self.path, 'r', encoding='utf-8') as handle:
                lines = handle.read().splitlines()
        except OSError:
            return []
        commands = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                commands.append(json.loads(line))
            except ValueError:
                # A half-written line from the instant we read it. It
                # will be complete next pass; guessing at it would be
                # guessing at an order.
                logging.debug('skipping a partial command line')
        return commands


class CommandRunner:
    """Executes commands on the engine side, exactly once."""

    def __init__(self, coordinator, log_path, results_path, clock=time.time):
        self.coordinator = coordinator
        self.log = CommandLog(log_path, clock=clock)
        self.results_path = results_path
        self.clock = clock
        self.seen = set()
        self.results = {}
        self.primed = False

    def prime(self):
        """Everything already written happened in a previous life."""
        history = self.log.read_all()
        self.seen = {command['id'] for command in history}
        self.primed = True
        if history:
            logging.info('primed past %d command(s) from a previous run — '
                         'they are history, not instructions', len(history))
        return len(history)

    def drain(self):
        if not self.primed:
            # Refusing is the safe answer: an unprimed drain is the
            # replay this module exists to prevent.
            raise RuntimeError('CommandRunner.drain() before prime() — that '
                               'would replay the whole command history')
        done = []
        for command in self.log.read_all():
            if command['id'] in self.seen:
                continue
            self.seen.add(command['id'])
            result = self.execute(command)
            self.results[command['id']] = result
            done.append(result)
        if done:
            self.publish()
        return done

    def execute(self, command):
        kind = command.get('kind')
        payload = command.get('payload') or {}
        try:
            handler = getattr(self, f'_do_{kind}', None)
            if handler is None:
                return self._result(command, False, f'unknown command: {kind}')
            return self._result(command, True, None, handler(payload))
        except Exception as error:              # never die on a command
            logging.exception('command %s failed: %s', kind, error)
            return self._result(command, False, str(error))

    def _result(self, command, ok, error=None, data=None):
        return {'id': command['id'], 'kind': command.get('kind'), 'ok': ok,
                'error': error, 'data': data, 'at': self.clock()}

    def publish(self):
        tmp = self.results_path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as handle:
            # Bounded: this is a UI convenience, not the audit trail.
            json.dump(dict(list(self.results.items())[-200:]), handle,
                      default=str)
        atomicfile.replace(tmp, self.results_path)

    # -- the commands --------------------------------------------------------

    #: The per-ladder settings the grid and the ladder both edit, each
    #: with the type it must be. An enum coercion is also the
    #: validation: 'MARKETT' raises here rather than arming a mode
    #: nothing understands.
    EDITABLE = {
        'order_type': OrderType,
        'exit_type': OrderType,
        'time_in_force': TimeInForce,
        'overnight': OvernightMode,
        #: NRML or MIS. Part of a POSITION's identity here, because the
        #: exchange nets per (symbol, product) — and MIS is squared off
        #: by the broker without asking, which makes it a deliberate
        #: choice rather than a display preference.
        'product': lambda value: ('MIS' if str(value).upper() == 'MIS'
                                  else 'NRML'),
        'increment': lambda value: (float(value) if value not in (None, '')
                                    else None),
        'default_quantity': float,
        'quoting_leg': lambda value: value if value in ('a', 'b') else None,
        'rows': int,
        'clip_lots_a': lambda value: float(value or 1.0),
        'clip_lots_b': lambda value: float(value or 1.0),
        #: An override of the master's LotSize. Blank is the master's,
        #: which is right almost always — and units are lots x LotSize,
        #: so a wrong one here is a hundredfold order.
        'contract_size_a': lambda value: (float(value)
                                          if value not in (None, '') else None),
        'contract_size_b': lambda value: (float(value)
                                          if value not in (None, '') else None),
        'max_quote_age_sec': lambda value: (float(value)
                                            if value not in (None, '')
                                            else None),
        'auto_route': bool,
        'algo_window': bool,
        'pair_type': lambda value: str(value or 'FUTURE_FUTURE').upper(),
    }

    #: Desk-wide settings that take effect NOW rather than at a
    #: restart. Everything else says "restart required" instead of
    #: looking applied.
    HOT_SETTINGS = {
        'MARKET_PROTECTION_TICKS': float,
        'CONFIRM_MARKET_CLICKS': bool,
        'CLICK_AWAY_RESTS': bool,
        'CLICK_CONVENTION': str,
        'CLOSE_FIRST': bool,
        'RECENTRE_SEC': float,
        'ROW_HEIGHT_PX': int,
        'REPEG_DEAD_BAND_TICKS': float,
        'MAX_QUOTE_AGE_SEC': float,
        'MAX_SPREAD_JUMP_SIGMA': float,
        'JUMP_SETTLE_SEC': float,
        'AUTO_ROUTE_ENABLED': bool,
        'TP_TARGET_PCT_OF_MARGIN': float,
        'BREAK_EVEN_NIGHTS': float,
        'TENDER_WARN_DAYS': float,
        'REFUSE_OPEN_IN_TENDER': bool,
        'RECONCILE_INTERVAL_SEC': float,
        'SHUTDOWN_CLOSE_POSITIONS': str,
    }

    def _do_click(self, payload):
        return self.coordinator.click(payload['pair'], payload['side'],
                                      float(payload['level']),
                                      payload.get('quantity'),
                                      payload.get('order_type'))

    def _do_cancel_order(self, payload):
        order = self.coordinator.book.order(payload['order_id'])
        if order is None:
            return {'ok': False, 'error': 'that order is already gone'}
        self.coordinator.book.cancel(order.order_id)
        self.coordinator.quoter.cancel(order)
        return {'ok': True, 'order_id': order.order_id}

    def _do_cancel_where(self, payload):
        self.coordinator.cancel_all(payload.get('pair'), payload.get('side'))
        return {'ok': True}

    def _do_flatten_pair(self, payload):
        return self.coordinator.flatten(payload['pair'], reason='flatten')

    def _do_close_position(self, payload):
        pair = self.coordinator.config.pairs.get(payload['pair'])
        position = self.coordinator.book.position(payload['position_id'])
        if pair is None or position is None:
            return {'ok': False, 'error': 'that position is already gone'}
        self.coordinator.quoter.disarm(position.position_id, 'closed by hand')
        return self.coordinator.executor.close_position(
            pair, position, self.coordinator.market.get(payload['pair']),
            reason='closed by hand', quantity=payload.get('quantity'))

    def _do_close_at_limit(self, payload):
        """Rest a closing order per open position at one level.

        REAL orders at the exchange here, unlike MT5 where a closing
        limit could not exist and the level had to be held in memory.
        """
        pair = self.coordinator.config.pairs.get(payload['pair'])
        if pair is None:
            return {'ok': False, 'error': 'no such pair'}
        armed = []
        for position in self.coordinator.book.positions(payload['pair']):
            order = self.coordinator.quoter.arm(
                pair, position, float(payload['level']),
                quantity=payload.get('quantity'), auto=False)
            if order is not None:
                armed.append(order.order_id)
        return {'ok': bool(armed), 'armed': armed}

    def _do_kill(self, payload):
        """Everything flat, everything cancelled. Asked once, upstream."""
        out = []
        for key in list(self.coordinator.config.pairs):
            self.coordinator.cancel_all(key)
            out.append(self.coordinator.flatten(key, reason='KILL'))
        return {'ok': all(row.get('ok') for row in out), 'pairs': out}

    def _do_clear_unresolved(self, payload):
        """A person has looked at an order we did not know the fate of.

        ONLY a person can clear one. Nothing in the engine does it,
        because nothing in the engine knows what happened.
        """
        return {'ok': self.coordinator.clear_unresolved(payload['ticket'])}

    def _do_adopt_unclaimed(self, payload):
        return self.coordinator.reconciler.adopt(
            payload['symbol'], payload.get('product', 'NRML'))

    def _do_close_unclaimed(self, payload):
        return self.coordinator.reconciler.close_excess(
            payload['symbol'], payload.get('product', 'NRML'),
            payload.get('lots'))

    def _do_lock_ladder(self, payload):
        self.coordinator._ladder_locked[payload['pair']] = bool(
            payload.get('locked'))
        return {'ok': True}

    def _do_recentre_ladder(self, payload):
        self.coordinator._ladder_anchor.pop(payload['pair'], None)
        return {'ok': True}

    def _do_refresh_feed(self, payload):
        pair = self.coordinator.config.pairs.get(payload['pair'])
        if pair is None:
            return {'ok': False, 'error': 'no such pair'}
        out = {}
        for leg_key, symbol in (('a', pair.symbol_a), ('b', pair.symbol_b)):
            runner = self.coordinator.executor._leg(pair, leg_key)
            out[leg_key] = bool(runner and runner.resubscribe(symbol))
        # Whether it WORKED, rather than a claim that it did.
        return {'ok': any(out.values()), 'legs': out}

    def _do_set_setting(self, payload):
        applied = {}
        for name, value in (payload.get('fields') or {}).items():
            coerce = self.HOT_SETTINGS.get(name)
            if coerce is None:
                continue
            self.coordinator.config.settings[name] = coerce(value)
            applied[name] = self.coordinator.config.settings[name]
        return {'ok': True, 'applied': applied}

    def _do_set_pair(self, payload):
        pair = self.coordinator.config.pairs.get(payload['pair'])
        if pair is None:
            return {'ok': False, 'error': 'no such pair'}
        changed = []
        for name, value in (payload.get('fields') or {}).items():
            coerce = self.EDITABLE.get(name)
            if coerce is None:
                continue
            setattr(pair, name, coerce(value))
            changed.append(name)
        return {'ok': True, 'changed': changed}
