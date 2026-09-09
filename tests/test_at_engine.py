"""The quoter, the database and the coordinator, wired together.

The rule this file exists to pin: **a resting CLOSE is a real order at
the exchange.** On MT5 it could not be — a closing limit rested as an
ordinary limit and opened a second position facing the other way (live
2026-09-02, ticket 2092). The exchange nets here, so there is one path
instead of two.
"""

import sys
import types

import pytest

from tests import fake_arrow as F


@pytest.fixture
def arrow_sdk(monkeypatch):
    module = types.ModuleType('pyarrow_client')
    for name in ('Exchange', 'OrderType', 'ProductType', 'TransactionType',
                 'Retention', 'Variety', 'QuoteMode', 'DataMode'):
        setattr(module, name, getattr(F, name))
    module.ArrowClient = F.FakeArrowClient
    module.ArrowStreams = F.FakeStreams
    monkeypatch.setitem(sys.modules, 'pyarrow_client', module)
    from arrowtrader import broker
    monkeypatch.setattr(broker, 'arrow', module)
    return module


RAW = {
    'account': {'name': 'arrow', 'app_id': 'A', 'user_id': 'U',
                'dedicated': True},
    'pairs': {
        'GOLD05DEC25F|GOLD05FEB26F': {
            'name': 'Gold Dec/Feb',
            'leg_a': {'account': 'arrow', 'symbol': 'GOLD05DEC25F',
                      'segment': 'mcx_fo'},
            'leg_b': {'account': 'arrow', 'symbol': 'GOLD05FEB26F',
                      'segment': 'mcx_fo'},
            'pair_type': 'FUTURE_FUTURE', 'order_type': 'MARKET',
            'clip_lots_a': 1, 'clip_lots_b': 1, 'default_quantity': 1,
        },
    },
    'settings': {'MARKET_PROTECTION_TICKS': 0, 'AUTO_ROUTE_ENABLED': False},
}

KEY = 'GOLD05DEC25F|GOLD05FEB26F'


@pytest.fixture
def engine(arrow_sdk, tmp_path):
    from arrowtrader.broker import ArrowSession
    from arrowtrader.config import TraderConfig
    from arrowtrader.coordinator import Coordinator
    from arrowtrader.database import Store
    from arrowtrader.legs import make_legs
    from arrowtrader.segments import SegmentTable

    config = TraderConfig.from_raw(RAW)
    now = [0.0]

    def tick():
        now[0] += 0.001
        return now[0]

    session = ArrowSession(F.Account(), SegmentTable(), clock=tick,
                           sleep=lambda s: None)
    legs = make_legs(['arrow'], session)
    store = Store(str(tmp_path / 'engine.db'), clock=tick)
    built = Coordinator(config, legs, status_path=str(tmp_path / 'status.json'),
                        store=store, clock=tick, sleep=lambda s: None)
    built.start()
    built.poll_once()
    return built


def pair_of(engine):
    return engine.config.pairs[KEY]


# -- the whole path ------------------------------------------------------------

def test_a_market_click_crosses_both_legs_and_books_a_position(engine):
    md = engine.market[KEY]
    result = engine.click(KEY, 'BUY', md['long_spread'], quantity=1)
    assert result['ok'] is True
    assert len(engine.book.positions(KEY)) == 1
    client = engine.legs['arrow'].session._client
    assert len(client.placed) == 2                    # one per leg
    # ...and in UNITS, not lots.
    assert all(order['quantity'] == 100 for order in client.placed)


def test_the_position_survives_a_restart_with_its_OWN_id(engine, tmp_path):
    from arrowtrader.book import Book
    from arrowtrader.database import recover
    md = engine.market[KEY]
    engine.click(KEY, 'BUY', md['long_spread'], quantity=1)
    original = engine.book.positions(KEY)[0]
    fresh = Book()
    report = recover(engine.store, fresh)
    assert report['complete'] is True
    assert fresh.positions()[0].position_id == original.position_id
    assert fresh.positions()[0].leg_b.contract_size == 100


def test_an_opposite_click_REDUCES_before_it_opens(engine):
    md = engine.market[KEY]
    engine.click(KEY, 'SELL', md['short_spread'], quantity=2)
    result = engine.click(KEY, 'BUY', md['long_spread'], quantity=3)
    assert len(result['closed']) == 1
    assert engine.book.net_position(KEY)[0] == 1.0


# -- guards --------------------------------------------------------------------

def test_a_stale_leg_withholds_an_ORDER(engine):
    engine.market[KEY]['guard_reason'] = 'Leg A has not moved for 30s'
    md = engine.market[KEY]
    result = engine.click(KEY, 'BUY', md['long_spread'], quantity=1)
    assert result['ok'] is False
    assert 'not moved' in result['reason']
    # CONTROL: cleared, and the same click goes through.
    engine.market[KEY]['guard_reason'] = None
    assert engine.click(KEY, 'BUY', md['long_spread'], quantity=1)['ok']


def test_a_guard_NEVER_prevents_a_close(engine):
    md = engine.market[KEY]
    engine.click(KEY, 'BUY', md['long_spread'], quantity=1)
    engine.market[KEY]['guard_reason'] = 'Leg A has not moved for 30s'
    assert engine.flatten(KEY)['ok'] is True
    assert engine.book.positions(KEY) == []


def test_a_leg_with_NO_BOOK_prices_nothing_and_says_which(engine,
                                                          monkeypatch):
    """An LTP-only feed draws a plausible ladder around a number nobody
    can trade at."""
    session = engine.legs['arrow'].session
    session.stop_stream()
    session._subscribe = lambda contract: False
    monkeypatch.setattr(
        F.FakeArrowClient, 'get_quotes',
        lambda self, mode, pairs: [{'TradingSymbol': symbol, 'Ltp': 75000.0}
                                   for symbol, _exchange in pairs])
    engine.poll_once()
    assert engine.market[KEY] is None
    assert any('LTP mode' in reason for reason in engine.errors[KEY])


def test_a_click_AWAY_from_the_market_RESTS(engine):
    md = engine.market[KEY]
    result = engine.click(KEY, 'BUY', md['long_spread'] - 50.0, quantity=1)
    assert result['resting'] is True
    assert engine.book.orders(KEY)


def test_click_away_can_be_REFUSED_instead(engine):
    engine.config.settings['CLICK_AWAY_RESTS'] = False
    md = engine.market[KEY]
    result = engine.click(KEY, 'BUY', md['long_spread'] - 50.0, quantity=1)
    assert result['ok'] is False
    assert 'fills at no price' in result['reason']


# -- the resting order that MT5 could not have ---------------------------------

def test_a_resting_ENTRY_puts_a_REAL_order_at_the_exchange(engine):
    md = engine.market[KEY]
    engine.click(KEY, 'BUY', md['long_spread'] - 50.0, quantity=1)
    engine.poll_once()
    client = engine.legs['arrow'].session._client
    resting = [order for order in client.orders.values()
               if order['status'] == 'OPEN']
    assert len(resting) == 1
    assert engine.quoter.snapshot(KEY)[0]['ticket'] is not None


def test_a_resting_CLOSE_is_a_REAL_order_TOO(engine):
    """THE CHANGE. On MT5 a closing limit rested as an ordinary limit
    and, on a hedging account, opened a second position facing the
    other way. The exchange nets here, so it reduces."""
    md = engine.market[KEY]
    engine.click(KEY, 'BUY', md['long_spread'], quantity=1)
    position = engine.book.positions(KEY)[0]
    engine.quoter.arm(pair_of(engine), position,
                      position.entry_spread + 50.0, auto=False)
    engine.poll_once()
    group = [row for row in engine.quoter.snapshot(KEY)
             if row['intent'] == 'CLOSE'][0]
    assert group['ticket'] is not None
    assert group['rests_at_exchange'] is True
    client = engine.legs['arrow'].session._client
    assert client.orders[group['ticket']]['status'] == 'OPEN'


def test_an_entry_and_a_close_at_the_SAME_level_are_NOT_merged(engine):
    """One opens and one consumes; merging them would make a fill mean
    two different things at once."""
    md = engine.market[KEY]
    engine.click(KEY, 'BUY', md['long_spread'], quantity=1)
    position = engine.book.positions(KEY)[0]
    # ABOVE the bid, so a SELL rests rather than crossing — and it is
    # the same level the close against a long position is armed at.
    level = md['short_spread'] + 50.0
    engine.click(KEY, 'SELL', level, quantity=1)
    engine.quoter.arm(pair_of(engine), position, level, auto=False)
    engine.poll_once()
    groups = [row for row in engine.quoter.snapshot(KEY)
              if abs(row['level'] - level) < 1e-9]
    assert len(groups) == 2
    assert {row['intent'] for row in groups} == {'OPEN', 'CLOSE'}


def test_a_resting_order_is_repegged_off_the_OTHER_leg(engine):
    md = engine.market[KEY]
    engine.click(KEY, 'BUY', md['long_spread'] - 50.0, quantity=1)
    engine.poll_once()
    before = engine.quoter.snapshot(KEY)[0]['price']
    # Leg A moves; the peg on leg B must follow it.
    client = engine.legs['arrow'].session._client
    client.books['GOLD05DEC25F'] = (75299.0, 75301.0, 75300.0)
    engine.legs['arrow'].session.stop_stream()
    engine.legs['arrow'].session._subscribe = lambda contract: False
    engine.poll_once()
    after = engine.quoter.snapshot(KEY)[0]['price']
    assert after != before
    assert client.modified                      # MODIFY, not cancel-replace
    assert client.cancelled == []


def test_a_repeg_INSIDE_the_dead_band_does_nothing(engine):
    """Every modify loses queue position, and re-pricing three times a
    second guarantees you are never at the front of a queue."""
    engine.config.settings['REPEG_DEAD_BAND_TICKS'] = 1000.0
    md = engine.market[KEY]
    engine.click(KEY, 'BUY', md['long_spread'] - 50.0, quantity=1)
    engine.poll_once()
    client = engine.legs['arrow'].session._client
    client.books['GOLD05DEC25F'] = (75009.0, 75011.0, 75010.0)
    engine.legs['arrow'].session.stop_stream()
    engine.legs['arrow'].session._subscribe = lambda contract: False
    engine.poll_once()
    assert client.modified == []


def test_a_resting_fill_crosses_the_other_leg_and_books_the_position(engine):
    md = engine.market[KEY]
    engine.click(KEY, 'BUY', md['long_spread'] - 50.0, quantity=1)
    engine.poll_once()
    client = engine.legs['arrow'].session._client
    ticket = engine.quoter.snapshot(KEY)[0]['ticket']
    client.fill_resting(ticket)
    engine.poll_once()
    assert len(engine.book.positions(KEY)) == 1
    assert engine.quoter.hedge_times


def test_a_resting_CLOSE_filling_consumes_its_position(engine):
    md = engine.market[KEY]
    engine.click(KEY, 'BUY', md['long_spread'], quantity=1)
    position = engine.book.positions(KEY)[0]
    engine.quoter.arm(pair_of(engine), position,
                      position.entry_spread + 50.0, auto=False)
    engine.poll_once()
    group = [row for row in engine.quoter.snapshot(KEY)
             if row['intent'] == 'CLOSE'][0]
    engine.legs['arrow'].session._client.fill_resting(group['ticket'])
    engine.poll_once()
    assert position.is_open is False
    assert 'resting level' in position.close_reason


# -- the sweep that now finds things -------------------------------------------

def test_the_sweep_runs_at_STARTUP_as_well_as_shutdown(engine):
    """On MT5 no working order survived the process, so a startup sweep
    was a formality. A leg order outlives us here."""
    events = [row for row in engine.session_events
              if row['kind'] == 'sweep']
    assert events and events[0]['reason'] == 'startup'


def test_the_sweep_SAYS_what_scope_it_had(engine):
    engine.config.account.dedicated = False
    event = engine.sweep_resting('startup')
    assert event['scope'] == 'this process'
    assert 'cannot be told from your own' in event['note']
    # CONTROL: a dedicated account gets the whole account, and no caveat.
    engine.config.account.dedicated = True
    assert engine.sweep_resting('startup')['note'] is None


def test_a_shutdown_sweep_pulls_the_resting_orders(engine):
    md = engine.market[KEY]
    engine.click(KEY, 'BUY', md['long_spread'] - 50.0, quantity=1)
    engine.poll_once()
    ticket = engine.quoter.snapshot(KEY)[0]['ticket']
    engine.sweep_resting('shutdown')
    assert engine.legs['arrow'].session._client.orders[ticket]['status'] \
        == 'CANCELLED'


# -- the third outcome reaches the screen ---------------------------------------

def test_an_unresolved_order_is_recorded_for_a_PERSON(engine):
    """A naked leg is a KNOWN exposure; an unresolved order is an
    unknown one, and nothing may act on it automatically."""
    engine.legs['arrow'].session._client.rest_market_orders = True
    md = engine.market[KEY]
    engine.click(KEY, 'BUY', md['long_spread'], quantity=1)
    assert engine.unresolved
    assert engine.snapshot()['unresolved']
    # Only a person clears it.
    ticket = engine.unresolved[0]['ticket']
    assert engine.clear_unresolved(ticket) is True
    assert engine.unresolved == []


def test_an_unresolved_order_is_written_to_the_journal(engine):
    engine.legs['arrow'].session._client.rest_market_orders = True
    md = engine.market[KEY]
    engine.click(KEY, 'BUY', md['long_spread'], quantity=1)
    assert engine.store.events('unresolved')


# -- the snapshot the UI reads ---------------------------------------------------

def test_the_snapshot_carries_UNITS_beside_the_lots(engine):
    row = engine.snapshot()['pairs'][KEY]
    assert row['clip_lots_a'] == 1.0
    assert row['units_a'] == 100          # what goes on the wire
    assert row['contract_a'] == 100


def test_the_snapshot_reports_ONE_margin_pool(engine):
    """MT5-Trader's 'the pair can only be carried by the weaker of the
    two brokers' does not apply and must not be shown by habit."""
    payload = engine.snapshot()
    assert payload['single_pool'] is True
    assert payload['currency'] == 'INR'


def test_an_unmarkable_position_makes_the_TOTAL_unknown(engine):
    """Rather than an authoritative-looking number silently one short."""
    md = engine.market[KEY]
    engine.click(KEY, 'BUY', md['long_spread'], quantity=1)
    engine.market[KEY] = None
    row = engine.snapshot()['pairs'][KEY]
    assert row['open_pnl'] is None


def test_the_snapshot_names_a_dark_account_rather_than_omitting_it(engine):
    engine.legs['arrow'].session.connected = False
    assert engine.snapshot()['dark_accounts'] == ['arrow']


def test_a_dead_order_keeps_its_REASON_on_the_ladder(engine):
    """A rejected order used to vanish in the same instant the click
    was accepted: a green toast, then an empty Work column."""
    md = engine.market[KEY]
    engine.click(KEY, 'BUY', md['long_spread'] - 50.0, quantity=1)
    order = engine.book.orders(KEY)[0]
    engine.book.cancel(order.order_id, 'rms:blocked for gold05dec25f')
    assert engine.snapshot()['pairs'][KEY]['dead_orders'][0]['reason'] \
        == 'rms:blocked for gold05dec25f'


def test_the_snapshot_publishes_atomically(engine):
    import json
    payload = engine.publish()
    with open(engine.status_path) as handle:
        assert json.load(handle)['pairs'][KEY]['key'] == payload['pairs'][KEY]['key']


# -- lot sizes are never guessed --------------------------------------------------

def test_an_OVERRIDDEN_lot_size_is_loud(engine):
    pair = pair_of(engine)
    pair.contract_size_a = 50
    engine.resolve_symbols()
    assert any('OVERRIDDEN' in reason for reason in engine.errors[KEY])
    assert engine.snapshot()['pairs'][KEY]['contract_a_overridden'] is True


def test_margin_is_NONE_rather_than_derived_from_notional(engine):
    """Margin here is SPAN plus exposure, set by the exchange. A number
    computed from notional would be a guess presented as a figure."""
    assert engine.margin_per_spread(pair_of(engine)) is None


def test_the_take_profit_is_withheld_when_margin_cannot_be_priced(engine):
    md = engine.market[KEY]
    engine.click(KEY, 'BUY', md['long_spread'], quantity=1)
    position = engine.book.positions(KEY)[0]
    settings = pair_of(engine).exit_settings(engine.config.settings)
    assert engine.take_profit(pair_of(engine), position, settings) is None


# -- tender --------------------------------------------------------------------------

def test_a_contract_near_delivery_WARNS(engine):
    pair = pair_of(engine)
    pair.meta_a = dict(pair.meta_a, days_to_expiry=3)
    engine.poll_once()
    assert any('DELIVERY' in note
               for note in engine.snapshot()['pairs'][KEY]['tender'])


def test_tender_can_withhold_an_OPEN_and_never_a_close(engine):
    pair = pair_of(engine)
    md = engine.market[KEY]
    engine.click(KEY, 'BUY', md['long_spread'], quantity=1)
    pair.meta_a = dict(pair.meta_a, days_to_expiry=3)
    engine.config.settings['REFUSE_OPEN_IN_TENDER'] = True
    engine.poll_once()
    refused = engine.click(KEY, 'BUY', engine.market[KEY]['long_spread'],
                           quantity=1)
    assert refused['refused'] is True
    assert 'tender window' in refused['reason']
    # A guard NEVER prevents a close.
    assert engine.flatten(KEY)['ok'] is True


# -- the session cutoff ----------------------------------------------------------------

def test_with_NO_measured_clock_the_cutoff_does_not_fire(engine):
    md = engine.market[KEY]
    engine.click(KEY, 'BUY', md['long_spread'] - 50.0, quantity=1)
    assert engine.exchange_offset() is None
    assert engine.run_session_cutoff() == []
    assert engine.book.orders(KEY)              # the DAY order still stands


# -- the ladder, end to end ------------------------------------------------------

def test_the_snapshot_CARRIES_A_LADDER_at_all(engine):
    """The front end draws `row.rows` and nothing else builds it.

    Nothing produced this key. `var rows = row.rows || []` then drew an
    empty tbody, three times a second, on a ladder whose prices, sizes
    and market line were all correct in the engine and never reached
    the screen. The two ladder commands the engine already accepts —
    lock and recentre — had nothing to act on for the same reason.
    """
    row = engine.snapshot()['pairs'][KEY]
    assert row['rows'], 'the snapshot carries no ladder'
    assert len(row['rows']) == row['row_count']
    levels = [line['level'] for line in row['rows']]
    assert levels == sorted(levels, reverse=True)
    assert sum(1 for line in row['rows'] if line['is_best_bid']) == 1
    assert sum(1 for line in row['rows'] if line['is_best_ask']) == 1


def test_the_ladder_carries_SIZES_from_the_two_legs_DOM(engine):
    """The fake serves five levels a side on both legs, so the spread's
    size columns are derivable — and the number at the touch is the
    smaller of the two legs, in CLIPS, not in units."""
    row = engine.snapshot()['pairs'][KEY]
    sizes = [line['ask_size'] for line in row['rows']
             if line['ask_size'] is not None]
    assert sizes, 'the ladder drew no size at all from two full books'
    touch = next(line for line in row['rows'] if line['is_best_ask'])
    assert touch['ask_size'] is not None
    # A gold lot is 100 units. The fake's books hold single-digit
    # units a level, so in clips this is small — and a ladder reading
    # units as clips would show hundreds.
    assert touch['ask_size'] < 10


def test_a_leg_in_LTP_MODE_draws_NO_LADDER_rather_than_a_plausible_one(engine):
    """The control, and the rule this whole system turns on. A leg with
    a last trade and no book prices nothing: the ladder is empty and
    the error line says which leg, instead of thirty rows of fiction
    around a number nobody can trade at."""
    engine.legs['arrow'].session._client.books = {
        'GOLD05DEC25F': None, 'GOLD05FEB26F': None}
    engine.poll_once()
    row = engine.snapshot()['pairs'][KEY]
    assert row['rows'] == []
    assert row['errors']


def test_LOCKING_the_ladder_pins_it_and_RECENTRING_lets_it_go(engine):
    """The two commands that had nothing to act on."""
    before = [line['level'] for line in engine.snapshot()['pairs'][KEY]['rows']]
    engine._ladder_locked[KEY] = True
    engine.poll_once()
    locked = engine.snapshot()['pairs'][KEY]
    assert locked['ladder_locked'] is True
    assert any(line['is_anchor'] for line in locked['rows'])
    assert [line['level'] for line in locked['rows']] == before

    # The market moves; the LOCKED window does not.
    engine.legs['arrow'].session._client.books['GOLD05FEB26F'] = (
        76499.0, 76502.0, 76500.0)
    engine.poll_once()
    still = engine.snapshot()['pairs'][KEY]
    assert [line['level'] for line in still['rows']] == before, (
        'the locked ladder moved with the market')

    engine._ladder_locked[KEY] = False
    engine.poll_once()
    freed = engine.snapshot()['pairs'][KEY]
    assert [line['level'] for line in freed['rows']] != before
    assert not any(line['is_anchor'] for line in freed['rows'])


# -- the ladder's header ---------------------------------------------------------

def test_the_FEED_LIGHT_actually_says_something(engine):
    """`market.feed_badge` is read in two places on the screen — the
    indicator on every ladder and the Feed column of the Market Grid —
    and nothing ever wrote it. Both were permanently an em dash, so a
    DEAD FEED and a QUIET MARKET looked identical, which is the single
    most expensive thing a trading screen can fail to distinguish.
    """
    md = engine.market[KEY]
    assert md['feed_badge'] is not None
    # On the first observation nothing has been seen twice yet. That is
    # not stale — a red light one second after startup is a light
    # nobody believes by the end of the week.
    assert md['feed_badge'] in ('warming up',) or \
        md['feed_badge'].startswith('OK')


def test_a_FROZEN_leg_turns_the_feed_light_and_the_ladder_says_which(engine):
    """The control. A spread is only as good as its worse leg."""
    from arrowtrader.spread import feed_badge
    frozen = dict(engine.market[KEY], leg_a_quote_age_sec=9.0,
                  leg_b_quote_age_sec=0.0)
    assert feed_badge(frozen, 5.0) == 'stale 9.0s'
    # ...and with the guard turned off it is not called stale.
    assert feed_badge(frozen, 0).startswith('OK')


def test_the_SESSION_STRIP_is_ours_and_says_so(engine):
    """The exchange publishes a high, low and open for each CONTRACT
    and nothing for the difference — and the difference of two highs is
    not the high of the difference, because the legs reach their
    extremes at different moments. So the range is ours, and the screen
    says `ours` rather than borrowing the exchange's word for it."""
    strip = engine.market[KEY]['session']
    assert strip is not None
    assert strip['ours'] is True
    assert strip['high'] is not None and strip['low'] is not None
    # Volume is per LEG and never added: one lot of the near month and
    # one of the far are not two lots of anything.
    assert 'volume_a' in strip and 'volume_b' in strip


def test_the_session_HIGH_and_LOW_follow_what_was_actually_SEEN(engine):
    first = engine.market[KEY]['session']
    low_before = first['low']
    engine.legs['arrow'].session._client.books['GOLD05FEB26F'] = (
        75399.0, 75402.0, 75400.0)
    engine.poll_once()
    after = engine.market[KEY]['session']
    assert after['low'] < low_before
    assert after['high'] == first['high'], 'the high moved on a fall'


def test_NET_CHANGE_is_NONE_when_the_legs_publish_no_session_open(engine):
    """UNMEASURED IS NOT ZERO, and change-since-this-process-started is
    a different number from change on the day. Printing one under the
    other's label is the quiet substitution this system does not
    make."""
    md = engine.market[KEY]
    # The fake publishes an Open, so there IS a real one.
    assert md['session']['open'] is not None
    assert md['net_change'] is not None
    from arrowtrader.spread import SpreadSession
    blind = SpreadSession().observe(KEY, md, {'open': None}, {'open': None})
    assert blind['open'] is None
    assert blind['net_change'] is None


# -- the journal ------------------------------------------------------------------

def test_the_JOURNAL_is_actually_WRITTEN(engine):
    """`Store.record_fills` and `ArrowLeg.order_log` were both built and
    never connected, so the Fills tab and every report drawn from it
    were permanently empty — on a system whose whole cost model is
    charges read back from the contract note."""
    md = engine.market[KEY]
    engine.click(KEY, 'BUY', md['long_spread'], quantity=1)
    assert engine.journal() > 0
    rows = engine.store.fills()
    assert rows, 'nothing reached the fills table'
    symbols = {row['symbol'] for row in rows}
    assert {'GOLD05DEC25F', 'GOLD05FEB26F'} <= symbols


def test_the_journal_records_HOW_ownership_was_decided_not_just_whether(engine):
    """There is no magic number here and a netted position carries no
    marker, so ownership is an INFERENCE. The report has to be able to
    say which kind."""
    md = engine.market[KEY]
    engine.click(KEY, 'BUY', md['long_spread'], quantity=1)
    engine.journal()
    ours = [row for row in engine.store.fills() if row['is_ours']]
    assert ours
    assert all(row['ours_source'] for row in ours)


def test_the_journal_records_the_SIDE_and_the_LOTS(engine):
    """Two columns that came back blank on a live run.

    Arrow answers `transactionType` where the store read `side`, so the
    side column of the journal — on a report whose whole point is which
    way a trade went — was empty. And the trade book carries no lot
    size, so `lots` was blank too, on the unit the trader actually
    thinks in: 1 lot, not 100 units.
    """
    md = engine.market[KEY]
    engine.click(KEY, 'BUY', md['long_spread'], quantity=1)
    engine.journal()
    rows = {row['symbol']: row for row in engine.store.fills()}
    near = rows['GOLD05DEC25F']
    assert near['side'] in ('BUY', 'SELL')
    assert near['units'] == 100
    assert near['lots'] == 1, 'units were reported where lots belong'


def test_an_UNKNOWN_lot_size_leaves_lots_BLANK_rather_than_reporting_units(
        engine):
    """The control. Read as 1 it would report 100 lots for one."""
    engine.click(KEY, 'BUY', engine.market[KEY]['long_spread'], quantity=1)
    engine.legs['arrow'].session.master = None
    engine.journal()
    rows = engine.store.fills()
    assert rows
    assert all(row['lots'] is None for row in rows)
    assert all(row['units'] for row in rows), 'units must still be recorded'


def test_a_leg_that_CANNOT_BE_READ_is_not_journalled_as_a_quiet_day(engine):
    """None means unknown, never 'no activity'. A journal that recorded
    a failed read as no trades would show a clean session on the day
    the broker was unreachable."""
    engine.click(KEY, 'BUY', engine.market[KEY]['long_spread'], quantity=1)
    engine.journal()
    before = len(engine.store.fills())
    engine.legs['arrow'].order_log = lambda hours=24: None
    assert engine.journal() == 0
    assert len(engine.store.fills()) == before
