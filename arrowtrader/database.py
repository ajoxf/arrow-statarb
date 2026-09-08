"""The local database: crash-safe state, and the trade journal.

Ported from MT5-Trader's `database.py`. Two jobs, different in kind:

- **State.** Open positions live here as well as in memory, so a
  restart recovers what is actually on at the exchange. Without it the
  book comes back empty, the reconciler reads every real position as a
  difference, and the screen fills with findings about our own
  positions.

  IT MATTERS MORE HERE THAN IT DID ON MT5. There, a lost book could be
  partly rebuilt from the broker: every position carried our magic
  number, so the tickets could be recognised as ours. On a netting
  venue the exchange holds one number per contract and can tell us
  nothing about who opened it, at what price, or as half of which
  spread. **This file is the only copy of that.** Lose it and the
  information is gone, not merely inconvenient.

- **The journal.** Every fill the broker reports, ours and the trader's
  own dealing alike, keyed by the trade id. The record that survives
  us: an audit trail read back from what actually happened rather than
  written from our own intentions.

Mechanics carried across because they were paid for:

- **WAL and a 30-second busy timeout, through ONE `_connect()`.** The
  web process reads this file while the engine writes it, and the
  default raises `database is locked` immediately — which once stopped
  an exit loop for 30 seconds with a live position open.
- **A fill is written once**, by `INSERT OR REPLACE` on the trade id.
  The trade book is re-read every pass and the same fill arrives many
  times; a journal that grows a row per read is not a journal.
- **Keyed by (account, trade id), never the id alone.** Ids are unique
  per broker, not across them.
- **Exchange time is stored beside our own**, so a row can be lined up
  against the broker's own contract note rather than sitting hours away
  from it.
"""

import json
import logging
import os
import sqlite3
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS positions (
    position_id     TEXT PRIMARY KEY,
    pair_key        TEXT NOT NULL,
    side            TEXT NOT NULL,
    quantity        REAL NOT NULL,
    entry_spread    REAL,
    exit_spread     REAL,
    spread_units    REAL,
    order_type      TEXT,
    opened_at       REAL,
    closed_at       REAL,
    close_reason    TEXT,
    realized_pnl    REAL,
    entry_slippage  REAL,
    exit_slippage   REAL,
    click_to_on_ms  REAL,
    leg_a           TEXT,       -- JSON: symbol, side, volume, price,
    leg_b           TEXT        --       lot size, segment, product
);

CREATE INDEX IF NOT EXISTS positions_open
    ON positions (closed_at, pair_key);

-- The journal. One row per BROKER trade.
--
-- `is_ours` is the field that carries the whole difficulty of this
-- venue. On MT5 it is read straight off the deal's magic number and is
-- a fact. Here it is an INFERENCE — from our own order ids, from a tag
-- if the SDK carried one — and on a shared account it can be wrong.
-- `ours_source` records HOW it was decided, so a report can say
-- "attributed by order id" rather than presenting a guess as a fact.
CREATE TABLE IF NOT EXISTS fills (
    trade_id        TEXT NOT NULL,
    account         TEXT NOT NULL,
    order_id        TEXT,
    symbol          TEXT,
    segment         TEXT,
    product         TEXT,
    side            TEXT,        -- BUY / SELL
    units           REAL,        -- what went on the wire
    lots            REAL,        -- ...and what that was in lots
    price           REAL,
    charges         REAL,        -- brokerage + taxes, where reported
    exchange_time   REAL,        -- the EXCHANGE's own stamp
    seen_at         REAL,        -- our clock, when we first read it
    is_ours         INTEGER,
    ours_source     TEXT,        -- how that was decided
    tag             TEXT,
    pair_key        TEXT,
    leg             TEXT,        -- 'A' or 'B' on that pair
    PRIMARY KEY (account, trade_id)
);

CREATE INDEX IF NOT EXISTS fills_time ON fills (exchange_time);
CREATE INDEX IF NOT EXISTS fills_pair ON fills (pair_key, exchange_time);

-- Everything else worth being able to answer "what happened at 14:32?"
-- with: refusals, sweeps, reconciler findings, session cutoffs, and the
-- unresolved orders that need a person.
CREATE TABLE IF NOT EXISTS events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    at              REAL NOT NULL,
    kind            TEXT NOT NULL,
    pair_key        TEXT,
    detail          TEXT
);

CREATE INDEX IF NOT EXISTS events_at ON events (at);
"""


class Store:
    """One SQLite file, opened the same way everywhere."""

    def __init__(self, path='arrowtrader.db', clock=time.time):
        self.path = path
        self.clock = clock
        self._ensure()

    def _connect(self):
        """The ONE way this database is opened.

        WAL so a reader never blocks the writer, and a 30-second busy
        timeout so the one that does wait, waits — rather than raising
        `database is locked` while a position is open.
        """
        connection = sqlite3.connect(self.path, timeout=30.0)
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA journal_mode=WAL')
        connection.execute('PRAGMA busy_timeout=30000')
        connection.execute('PRAGMA synchronous=NORMAL')
        return connection

    def _ensure(self):
        directory = os.path.dirname(os.path.abspath(self.path))
        if directory and not os.path.isdir(directory):
            os.makedirs(directory, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(SCHEMA)

    # -- positions: the state a restart recovers ---------------------------

    def save_position(self, position):
        """Write a position, open or closed. Called on EVERY change.

        Cheap enough to do on every change, and far cheaper than the
        alternative — which here is not merely a restart that mistakes
        a live position for a difference, but the permanent loss of the
        only record that a net at the exchange was ever ours.
        """
        row = position.to_dict()
        with self._connect() as connection:
            connection.execute(
                """INSERT OR REPLACE INTO positions
                   (position_id, pair_key, side, quantity, entry_spread,
                    exit_spread, spread_units, order_type, opened_at,
                    closed_at, close_reason, realized_pnl, entry_slippage,
                    exit_slippage, click_to_on_ms, leg_a, leg_b)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (row['position_id'], row['pair_key'], row['side'],
                 row['quantity'], row['entry_spread'], row['exit_spread'],
                 row['spread_units'], row['order_type'], row['opened_at'],
                 row['closed_at'], row['close_reason'], row['realized_pnl'],
                 row['entry_slippage'], row['exit_slippage'],
                 row['click_to_on_ms'], json.dumps(row['leg_a']),
                 json.dumps(row['leg_b'])))
        return position

    def open_positions(self):
        """Every position that was open when we last wrote it.

        A position whose close FAILED is still open and comes back
        open: removing it from this list is how the money ends up at
        the exchange with the screen reading flat.
        """
        with self._connect() as connection:
            rows = connection.execute(
                'SELECT * FROM positions WHERE closed_at IS NULL '
                'ORDER BY opened_at').fetchall()
        return [_position_row(row) for row in rows]

    def closed_positions(self, limit=200):
        with self._connect() as connection:
            rows = connection.execute(
                'SELECT * FROM positions WHERE closed_at IS NOT NULL '
                'ORDER BY closed_at DESC LIMIT ?', (limit,)).fetchall()
        return [_position_row(row) for row in rows]

    def positions_between(self, start=None, end=None, pair_key=None):
        """Positions OPENED inside a window, open ones included.

        Anchored on `opened_at`, not on the close: a position belongs to
        the session it was PUT ON in, which is the session whose click
        the entry slippage was measured against. Anchoring on the close
        would move a trade carried overnight into the next day's report
        and credit its entry to a click nobody made that day.
        """
        where, params = [], []
        if start is not None:
            where.append('opened_at >= ?')
            params.append(start)
        if end is not None:
            where.append('opened_at <= ?')
            params.append(end)
        if pair_key:
            where.append('pair_key = ?')
            params.append(pair_key)
        clause = ('WHERE ' + ' AND '.join(where)) if where else ''
        with self._connect() as connection:
            rows = connection.execute(
                f'SELECT * FROM positions {clause} ORDER BY opened_at',
                params).fetchall()
        return [_position_row(row) for row in rows]

    # -- the journal --------------------------------------------------------

    def record_fills(self, account, rows, resolve=None, ours=None):
        """Write what the BROKER says happened. Idempotent by trade id.

        `rows` are `leg.order_log()` records — the broker's own trade
        book, which includes the trader's own dealing as well as ours.
        Both belong in the journal: a fill on the account is a fill on
        the account.

        `ours(order_id, tag)` returns `(is_ours, how)`. **`how` is
        recorded**, because on this venue ownership is an inference and
        the report must be able to say which kind. On MT5 the magic
        number makes it a fact; here the strongest evidence available is
        an order id we sent, and on a shared account even a confident
        answer can be wrong.
        """
        written = 0
        now = self.clock()
        with self._connect() as connection:
            for row in rows or ():
                trade_id = str(row.get('trade_id') or row.get('orderNo')
                               or row.get('order_id') or '')
                if not trade_id:
                    continue
                order_id = str(row.get('order_id') or row.get('orderNo') or '')
                tag = row.get('tag')
                is_ours, source = (ours(order_id, tag) if ours
                                   else (None, 'not attributed'))
                symbol = row.get('symbol') or row.get('tradingSymbol')
                pair_key, leg = (resolve(symbol) if resolve
                                 else (None, None))
                units = _number(row.get('units') or row.get('quantity'))
                lot_size = _number(row.get('lot_size'))
                connection.execute(
                    """INSERT OR REPLACE INTO fills
                       (trade_id, account, order_id, symbol, segment,
                        product, side, units, lots, price, charges,
                        exchange_time,
                        seen_at,
                        is_ours, ours_source, tag, pair_key, leg)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,
                               COALESCE((SELECT seen_at FROM fills
                                         WHERE account = ? AND trade_id = ?),
                                        ?),
                               ?,?,?,?,?)""",
                    (trade_id, account, order_id, symbol,
                     row.get('segment'), row.get('product'),
                     row.get('side'), units,
                     (units / lot_size) if (units and lot_size) else None,
                     _number(row.get('price') or row.get('avgPrice')),
                     _number(row.get('charges')),
                     _number(row.get('exchange_time')),
                     account, trade_id, now,
                     None if is_ours is None else int(bool(is_ours)),
                     source, tag, pair_key, leg))
                written += 1
        return written

    def fills(self, pair_key=None, ours_only=False, limit=500):
        where, params = [], []
        if pair_key:
            where.append('pair_key = ?')
            params.append(pair_key)
        if ours_only:
            where.append('is_ours = 1')
        clause = ('WHERE ' + ' AND '.join(where)) if where else ''
        params.append(limit)
        with self._connect() as connection:
            rows = connection.execute(
                f'SELECT * FROM fills {clause} '
                f'ORDER BY COALESCE(exchange_time, seen_at) DESC LIMIT ?',
                params).fetchall()
        return [dict(row) for row in rows]

    def fills_between(self, start=None, end=None, ours_only=True):
        """The journal over a window, on the EXCHANGE's clock.

        Falls back to our own stamp where the broker gave none — and
        the row says which, because a report cut on one clock and
        populated from another is a report that quietly loses trades at
        both ends.
        """
        where, params = [], []
        if ours_only:
            where.append('is_ours = 1')
        if start is not None:
            where.append('COALESCE(exchange_time, seen_at) >= ?')
            params.append(start)
        if end is not None:
            where.append('COALESCE(exchange_time, seen_at) <= ?')
            params.append(end)
        clause = ('WHERE ' + ' AND '.join(where)) if where else ''
        with self._connect() as connection:
            rows = connection.execute(
                f'SELECT * FROM fills {clause} '
                f'ORDER BY COALESCE(exchange_time, seen_at)',
                params).fetchall()
        return [dict(row) for row in rows]

    def unattributed_fills(self, limit=200):
        """Fills we could NOT decide the ownership of.

        Counted and shown rather than defaulted either way. Defaulted
        to ours they inflate our own P&L with the trader's dealing;
        defaulted to theirs they hide our own trades from the journal.
        """
        with self._connect() as connection:
            rows = connection.execute(
                'SELECT * FROM fills WHERE is_ours IS NULL '
                'ORDER BY COALESCE(exchange_time, seen_at) DESC LIMIT ?',
                (limit,)).fetchall()
        return [dict(row) for row in rows]

    # -- events ---------------------------------------------------------------

    def event(self, kind, pair_key=None, **detail):
        with self._connect() as connection:
            connection.execute(
                'INSERT INTO events (at, kind, pair_key, detail) '
                'VALUES (?,?,?,?)',
                (self.clock(), kind, pair_key, json.dumps(detail,
                                                          default=str)))

    def events(self, kind=None, limit=200):
        where, params = ('WHERE kind = ?', [kind]) if kind else ('', [])
        params.append(limit)
        with self._connect() as connection:
            rows = connection.execute(
                f'SELECT * FROM events {where} ORDER BY at DESC LIMIT ?',
                params).fetchall()
        return [dict(row, detail=_json(row['detail'])) for row in rows]


def _number(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _json(text):
    try:
        return json.loads(text or '{}')
    except ValueError:
        return {}


def _position_row(row):
    """A database row back into the dict `SpreadPosition.from_dict` reads."""
    out = dict(row)
    out['leg_a'] = _json(out.get('leg_a'))
    out['leg_b'] = _json(out.get('leg_b'))
    return out


def recover(store, book, clock=time.time):
    """Bring the book back, and say whether it is COMPLETE.

    Returns a report. `complete` is what the reconciler gates on: while
    it is False nothing is auto-closed and no difference is called an
    orphan, because an orphan is only an orphan if we are sure it is
    not ours.

    A position comes back OPEN, under management, with its OWN id, its
    fills and its order ids exactly as they were. A recovered position
    under a new id would be a stranger to the reconciler and a
    duplicate to the book.
    """
    from .models import SpreadPosition
    report = {'at': clock(), 'complete': False, 'recovered': 0,
              'error': None, 'note': None}
    try:
        rows = store.open_positions()
    except Exception as error:                          # noqa: BLE001
        report['error'] = str(error)
        report['note'] = (
            'the database could not be read, so the book is EMPTY and may '
            'not be. Nothing will be auto-closed and every position at the '
            'exchange will be reported as unexplained until this is fixed.')
        logging.critical('recovery: %s', report['note'])
        return report
    for raw in rows:
        book.add_position(SpreadPosition.from_dict(raw))
        report['recovered'] += 1
    report['complete'] = True
    report['note'] = (
        f'{report["recovered"]} open position(s) recovered from disk'
        if report['recovered'] else
        'no open positions were recorded — the book starts flat')
    logging.info('recovery: %s', report['note'])
    return report
