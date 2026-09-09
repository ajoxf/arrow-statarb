"""The Arrow session and the leg above it, against a fake that says no.

Every refusal here is one the live API makes. A fake that agreed to
everything would prove nothing.
"""

import sys
import types

import pytest

from tests import fake_arrow as F


@pytest.fixture
def arrow_sdk(monkeypatch):
    """Install the fake in place of `pyarrow_client`, for this test only."""
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


@pytest.fixture
def session(arrow_sdk):
    from arrowtrader.broker import ArrowSession
    from arrowtrader.segments import SegmentTable
    now = [0.0]
    built = ArrowSession(F.Account(), SegmentTable(),
                         clock=lambda: now[0],
                         sleep=lambda s: now.__setitem__(0, now[0] + s))
    assert built.initialize() is True
    return built


@pytest.fixture
def leg(session):
    from arrowtrader.legs import make_legs
    return make_legs(['leg_a', 'leg_b'], session)


# -- connecting ---------------------------------------------------------------

def test_the_master_is_loaded_BEFORE_the_session_is_usable(session):
    """The stat-arb layer loads it on a background thread and answers
    orders meanwhile, which means an order can be sized from a lot size
    that has not arrived. Here, no master means no session."""
    assert session.connected is True
    assert session.master.rows == 7
    assert session.master.lot_size('GOLD05DEC25F') == 100


def test_an_empty_master_REFUSES_the_connection(arrow_sdk, monkeypatch):
    from arrowtrader.broker import ArrowSession
    from arrowtrader.segments import SegmentTable
    monkeypatch.setattr(F.FakeArrowClient, 'get_instruments',
                        lambda self: [])
    built = ArrowSession(F.Account(), SegmentTable())
    assert built.initialize() is False
    assert 'Nothing can be sized' in built.last_error
    assert built.connected is False


def _refused(sdk, **answers):
    """Initialize against a login step that answers with `answers`."""
    from arrowtrader.broker import ArrowSession
    from arrowtrader.segments import SegmentTable
    for field, body in answers.items():
        setattr(F.FakeArrowClient, field, body)
    try:
        built = ArrowSession(F.Account(), SegmentTable())
        assert built.initialize() is False
        return built.last_error
    finally:
        for field in answers:
            setattr(F.FakeArrowClient, field, None)


def test_a_login_refusal_names_the_STEP_and_quotes_ARROW(arrow_sdk):
    """The bug the operator actually hit, in one test.

    The SDK reads `resp["redirectUrl"]` out of the 2FA response and its
    transport only raises on a body carrying `status: error`. A refusal
    shaped any other way therefore surfaced as the bare string
    `'redirectUrl'` — a KeyError's argument, on the screen, naming
    neither the step nor the reason.

    A refusal carries the broker's own words. This is that rule applied
    to the login.
    """
    said = _refused(arrow_sdk, twofa_answer={
        'message': 'Invalid OTP', 'status': 'failure'})
    assert 'redirectUrl' in said            # which key was missing
    assert 'validate-2fa' in said           # WHICH STEP
    assert 'Invalid OTP' in said            # ARROW'S OWN WORDS
    assert 'ARROW_TOTP_SECRET' in said      # and the thing to check
    # And the class it was recognised as, which is what carries the fix.
    assert 'base32' in said


def test_the_FIRST_step_failing_is_not_reported_as_the_second(arrow_sdk):
    """The control. The three steps fail with three different fixes —
    a password, a TOTP seed and an API secret — and a message that
    named the wrong one would send the operator to the wrong field."""
    said = _refused(arrow_sdk, login_answer={
        'message': 'required validation for field userID failed'})
    assert 'auth/app/login' in said
    assert 'requestId' in said
    assert 'User ID and password' in said
    assert 'validate-2fa' not in said
    assert 'ARROW_TOTP_SECRET' not in said


def test_the_TOKEN_step_failing_points_at_the_API_SECRET(arrow_sdk):
    said = _refused(arrow_sdk, token_answer={'message': 'checksum mismatch'})
    assert 'authenticate-token' in said
    assert 'ARROW_API_SECRET' in said
    assert 'checksum mismatch' in said


def test_a_login_refusal_NEVER_carries_the_password(arrow_sdk):
    """CREDENTIALS LIVE ONLY IN .env — never in code, config, chat or a
    log line. This message goes to both a log line and the screen, and
    the body it quotes is the login body, which carries the password
    that was sent."""
    said = _refused(arrow_sdk, twofa_answer={
        'message': 'no', 'password': 'secret', 'token': 'abc',
        'apiSecret': 'apisecret'})
    for secret in ('secret', 'abc', 'apisecret'):
        assert secret not in said, f'the refusal leaked {secret!r}'
    assert '(hidden)' in said


def test_an_EMPTY_answer_says_so_rather_than_reading_as_nothing_wrong(arrow_sdk):
    """UNMEASURED IS NOT ZERO. A step that answered with nothing at all
    is a different fault from one that answered with a refusal, and a
    message that rendered it as an empty string said neither."""
    said = _refused(arrow_sdk, twofa_answer={})
    assert 'empty body' in said


def test_MCX_is_fetched_from_its_OWN_route_when_all_does_not_carry_it(
        arrow_sdk, monkeypatch):
    """pyarrow-client says so in the enum it ships:

        # MCX is used for user-permission checks and instrument segment
        # downloads (GET /mcx).

    ...and then wraps no method for it. `get_instruments()` is `/all`.
    So a master fetched the documented way can be complete for NSE and
    BSE and carry not one commodity contract — and the Exchanges page
    would report that the account is not entitled to MCX. A WRONG
    diagnosis is worse than none: it sends the operator to Arrow's
    support desk to ask for a segment they already have.
    """
    from arrowtrader.broker import ArrowSession
    from arrowtrader.segments import SegmentTable
    nse_only = [row for row in F.MASTER if row['ExchSeg'] != 'MCXFO']
    monkeypatch.setattr(F.FakeArrowClient, 'get_instruments',
                        lambda self: list(nse_only))
    monkeypatch.setattr(F.FakeArrowClient, 'mcx_rows',
                        [row for row in F.MASTER if row['ExchSeg'] == 'MCXFO'])
    built = ArrowSession(F.Account(), SegmentTable())
    assert built.initialize() is True
    assert 'MCXFO' in built.master.exch_segs
    assert built.master.lot_size('GOLD05DEC25F') == 100
    assert built.master_sources['/mcx'] == 6


def test_MCX_is_fetched_when_all_has_its_OPTIONS_but_NO_FUTURES(
        arrow_sdk, monkeypatch):
    """The condition that was wrong, and the case it lets through.

    Asking "did `/all` mention this segment at all" is satisfied by a
    single option. A master carrying MCX's entire option chain and not
    one future therefore looked complete, `/mcx` was never asked, and
    the contracts this terminal actually trades never arrived — on a
    segment the Exchanges page had just called ready.
    """
    from arrowtrader.broker import ArrowSession
    from arrowtrader.segments import SegmentTable
    options_only = [row for row in F.MASTER
                    if row['ExchSeg'] != 'MCXFO' or 'C1' in
                    row['TradingSymbol'] or 'P1' in row['TradingSymbol']]
    assert any(row['ExchSeg'] == 'MCXFO' for row in options_only), (
        'the fixture must still mention MCXFO, or it proves nothing')
    monkeypatch.setattr(F.FakeArrowClient, 'get_instruments',
                        lambda self: list(options_only))
    monkeypatch.setattr(
        F.FakeArrowClient, 'mcx_rows',
        [row for row in F.MASTER if row['TradingSymbol'].endswith('F')])
    built = ArrowSession(F.Account(), SegmentTable())
    assert built.initialize() is True
    assert '/mcx' in built.master_sources, (
        'a master with MCX options and no MCX futures looked complete')
    assert built.master.lot_size('GOLD05DEC25F') == 100


def test_the_extra_route_is_NOT_asked_for_when_all_already_has_it(
        arrow_sdk, monkeypatch):
    """The control. `/all` on a live account is ~223k rows and the
    commodity rows are in it or they are not; asking a second time for
    something already held is a slower connect for nothing."""
    from arrowtrader.broker import ArrowSession
    from arrowtrader.segments import SegmentTable
    asked = []
    monkeypatch.setattr(F.FakeArrowClient, '_get',
                        lambda self, url, **kw: asked.append(url) or [])
    built = ArrowSession(F.Account(), SegmentTable())
    assert built.initialize() is True
    assert asked == []
    assert built.master_sources == {'/all': 7}


def test_a_broker_with_NO_such_route_still_connects(arrow_sdk, monkeypatch):
    """A supplement that is not there is not a connection failure.
    `/all` succeeded; whether this account has MCX is then a question
    for the Segments page, answered in Arrow's own words."""
    from arrowtrader.broker import ArrowSession
    from arrowtrader.segments import SegmentTable
    nse_only = [row for row in F.MASTER if row['ExchSeg'] != 'MCXFO']
    monkeypatch.setattr(F.FakeArrowClient, 'get_instruments',
                        lambda self: list(nse_only))
    # `mcx_rows` is None by default: the route 404s.
    built = ArrowSession(F.Account(), SegmentTable())
    assert built.initialize() is True
    assert 'MCXFO' not in built.master.exch_segs
    assert '/mcx' not in built.master_sources


def test_an_order_carries_DISCLOSED_QUANTITY(session):
    """It is a REQUIRED argument with no default in the SDK.

    Leaving it out is a TypeError raised inside `place_order` before
    the call reaches the wire — so every order failed, and what the
    operator saw where the broker's words belong was
    `place_order() missing 1 required positional argument`. Zero means
    the whole order is visible, which is what a spread leg wants: an
    iceberg leg fills slower than the leg it hedges, and a hedge that
    fills at two different speeds is not a hedge.
    """
    session.send_market_order('GOLD05DEC25F', 'BUY', 100)
    sent = session._client.placed[-1]
    assert sent['disclosed_quantity'] == 0


def test_the_tag_field_is_read_from_the_SIGNATURE_not_guessed_at(session):
    """Trying each spelling and catching TypeError looks equivalent and
    is not: a MISSING REQUIRED argument raises the same TypeError as an
    unknown keyword. The loop swallowed a real signature error once per
    candidate name and reported the last one — which is how a missing
    `disclosed_quantity` surfaced as a tag problem."""
    session.send_market_order('GOLD05DEC25F', 'BUY', 100, comment='AT-1')
    sent = session._client.placed[-1]
    # `remarks` is what pyarrow-client 1.8.0 actually takes.
    assert sent['remarks'] == 'AT-1'


def test_a_build_with_NO_tag_field_still_places_the_order(arrow_sdk,
                                                          monkeypatch):
    """The control. A tag scopes our own PENDING orders for the sweep;
    it is not worth failing an order over."""
    from arrowtrader.broker import ArrowSession
    from arrowtrader.segments import SegmentTable

    def untagged(self, exchange, symbol, quantity, disclosed_quantity,
                 product, order_type, variety, transaction_type, price,
                 validity, mpp=False):
        self.placed.append({'symbol': symbol, 'quantity': quantity})
        return 'ORDER-NO-TAG'

    monkeypatch.setattr(F.FakeArrowClient, 'place_order', untagged)
    built = ArrowSession(F.Account(), SegmentTable())
    assert built.initialize() is True
    result = built.send_market_order('GOLD05DEC25F', 'BUY', 100,
                                     comment='AT-1')
    assert built._client.placed[-1]['symbol'] == 'GOLD05DEC25F'
    assert result.ticket == 'ORDER-NO-TAG'


def test_the_feed_is_asked_for_FULL_because_that_is_where_the_BOOK_is(session):
    """The SDK's modes are ltp (13 bytes), ltpc (17), quote (93) and
    full (249). The five levels a side live in the last 140 bytes of
    the full packet ALONE — QUOTE carries total buy and sell quantity
    and no prices, so a ladder built on it has no touch at all.

    There is no DEPTH mode. The test fake invented one and the broker
    asked for it first, so every test streamed in a mode the live SDK
    does not have.
    """
    assert session._data_mode() is F.DataMode.FULL


def test_a_six_digit_totp_is_refused_with_the_actual_fix(arrow_sdk):
    """The seed, not the code. It is the mistake everybody makes."""
    from arrowtrader.broker import ArrowSession
    from arrowtrader.segments import SegmentTable
    account = F.Account()
    account.totp_secret = '123456'
    built = ArrowSession(account, SegmentTable())
    assert built.initialize() is False
    assert 'base32' in built.last_error


def test_the_session_token_never_leaves_the_broker_module(session):
    """Not into a log line, not into the status file, not the browser."""
    report = session.terminal_report()
    assert 'FAKE-SESSION-TOKEN' not in repr(report)
    info = session.account_info()
    assert 'FAKE-SESSION-TOKEN' not in repr(info)


def test_both_legs_share_ONE_session(leg):
    """Two legs on one login is the normal case here, not a red banner."""
    assert leg['leg_a'].session is leg['leg_b'].session
    assert leg['leg_b'].connect() is True


# -- prices -------------------------------------------------------------------

def test_the_full_quote_gives_a_book_and_the_ladder_can_price_it(leg):
    tick = leg['leg_a'].tick('GOLD05DEC25F')
    assert tick['bid'] == 74999.0 and tick['ask'] == 75001.0
    assert tick['executable'] is True
    assert len(tick['depth']) == 10          # five a side


def test_an_LTP_ONLY_build_produces_a_tick_that_prices_NOTHING(session):
    """The degraded case is representable and visibly degraded, rather
    than papered over by copying `last` into both sides."""
    # The scale is the SESSION'S, unchanged: REST is in paise, and a
    # degraded feed is degraded in the same units as a good one.
    session._quote_mode = lambda: F.QuoteMode.LTP
    session.stop_stream()
    session._subscribe = lambda contract: False
    tick = session.symbol_tick('GOLD05DEC25F')
    assert tick['last'] == 75000.0
    assert tick['bid'] is None and tick['ask'] is None
    assert tick['executable'] is False


def test_the_stream_is_in_PAISE_and_is_converted_once(session):
    # Subscribe FIRST — the stream only exists once a symbol is read —
    # and use prices REST does not carry, so a tick that quietly fell
    # back to REST cannot pass this by coincidence.
    session.symbol_tick('GOLD05DEC25F')
    F.FakeStreams.last.push(218124, bid=74111.0, ask=74113.0, last=74112.0)
    tick = session.symbol_tick('GOLD05DEC25F')
    assert tick['bid'] == 74111.0        # not 7411100
    assert tick['ask'] == 74113.0
    assert tick['last'] == 74112.0


def test_a_last_only_tick_does_not_WIPE_a_bid_and_ask_we_already_have(session):
    """A QUOTE tick and a DEPTH tick arrive separately. Merging forward
    is what stops the book flickering out between them."""
    session.symbol_tick('GOLD05DEC25F')          # subscribe
    F.FakeStreams.last.push(218124, bid=74111.0, ask=74113.0)
    F.FakeStreams.last.push(218124, last=74112.5)
    tick = session.symbol_tick('GOLD05DEC25F')
    assert tick['bid'] == 74111.0 and tick['ask'] == 74113.0
    assert tick['last'] == 74112.5


def test_session_stats_report_a_missing_field_as_none_not_zero(session,
                                                               monkeypatch):
    """A missing high must render as an em dash, never as a real high
    of 0.00. Brokers fill in different subsets of these fields."""
    monkeypatch.setattr(
        F.FakeArrowClient, 'get_quotes',
        lambda self, mode, pairs: [{'TradingSymbol': 'GOLD05DEC25F',
                                    'Ltp': 7500000, 'Volume': 4321}])
    stats = session.session_stats('GOLD05DEC25F')
    assert stats['high'] is None and stats['open'] is None
    # CONTROL: a field that IS published comes through.
    assert stats['volume'] == 4321.0


def test_a_quote_call_that_fails_is_none_not_a_stale_price(session):
    session.stop_stream()
    session._subscribe = lambda contract: False
    session._client.raise_on_quotes = True
    assert session.symbol_tick('GOLD05DEC25F') is None


# -- orders: lots in, units out ----------------------------------------------

def test_an_order_carries_UNITS_not_lots(leg):
    result = leg['leg_a'].order('GOLD05DEC25F', 'BUY', 2)
    assert result['ok'] is True
    sent = leg['leg_a'].session._client.placed[-1]
    assert sent['quantity'] == 200          # 2 lots x LotSize 100
    assert result['filled_volume'] == 2.0   # ...and LOTS come back
    assert result['filled_units'] == 200.0


def test_a_market_order_carries_price_zero_AND_mpp(leg):
    """Plain MKT is disabled on Arrow. Both halves, or it is rejected."""
    leg['leg_a'].order('GOLD05DEC25F', 'BUY', 1)
    sent = leg['leg_a'].session._client.placed[-1]
    assert sent['mpp'] is True
    assert sent['price'] == 0.0


def test_an_unknown_lot_size_REFUSES_rather_than_sizing_at_one(leg,
                                                               monkeypatch):
    session = leg['leg_a'].session
    monkeypatch.setattr(session.master, 'lot_size', lambda symbol: None)
    result = leg['leg_a'].order('GOLD05DEC25F', 'BUY', 2)
    assert result['ok'] is False
    assert 'no lot size' in result['error']
    assert session._client.placed == []      # nothing reached the exchange


def test_the_touch_is_recorded_so_the_executor_can_measure_slippage(leg):
    """Arrow takes no deviation parameter, so the clicked-price guard
    above is the ONLY slippage protection. It needs this number."""
    result = leg['leg_a'].order('GOLD05DEC25F', 'BUY', 1)
    assert result['requested_price'] == 75001.0     # the offer
    assert result['price'] == 75001.0


# -- the exchange says no ------------------------------------------------------

def test_over_the_freeze_quantity_is_refused_in_the_exchanges_words(leg):
    result = leg['leg_a'].order('GOLD05DEC25F', 'BUY', 20)   # 2,000 units
    assert result['ok'] is False
    assert 'freeze quantity' in result['error']
    # ...and the fix is appended.
    assert 'Reduce the Qty' in result['error']
    # CONTROL: at the cap it goes through.
    assert leg['leg_a'].order('GOLD05DEC25F', 'BUY', 10)['ok'] is True


def test_an_rms_block_names_the_broker_not_the_exchange(leg):
    leg['leg_a'].session._client.rms_blocked.add('GOLD05DEC25F')
    result = leg['leg_a'].order('GOLD05DEC25F', 'BUY', 1)
    assert 'rms:blocked' in result['error']
    assert "risk system" in result['error']


def test_a_limit_off_the_tick_is_snapped_and_the_note_says_so(leg):
    result = leg['leg_a'].place_limit('GOLD05DEC25F', 'BUY', 1, 74999.4)
    assert result['ok'] is True
    assert result['price'] == 74999.0
    assert 'not a multiple' in result['price_note']


def test_a_price_outside_the_daily_range_is_NOT_clamped_into_it(leg):
    """A DPR breach is a refusal the operator must see, not something
    to be quietly moved into range under their click."""
    result = leg['leg_a'].place_limit('GOLD05DEC25F', 'BUY', 1, 50000.0)
    assert result['ok'] is False
    assert 'price range' in result['error']


def test_a_closed_market_says_so_rather_than_failing_silently(leg):
    leg['leg_a'].session._client.market_closed = True
    result = leg['leg_a'].order('GOLD05DEC25F', 'BUY', 1)
    assert 'closed' in result['error'].lower()


# -- the unresolved order, which is not a rejection ---------------------------

def test_an_order_still_working_at_the_deadline_is_UNRESOLVED(leg):
    """`mt5.order_send` returns the fill. Arrow returns an id, so an
    order that has not resolved is a THIRD outcome — and unwinding on
    it is how a position gets doubled instead of cancelled."""
    leg['leg_a'].session._client.rest_market_orders = True
    result = leg['leg_a'].order('GOLD05DEC25F', 'BUY', 1, deadline_sec=1.0)
    assert result['ok'] is False
    assert result['unresolved'] is True
    assert 'must not be unwound' in result['error']
    assert result['ticket'] is not None      # ...and it is nameable
    # CONTROL: a real rejection is resolved, and IS safe to act on.
    leg['leg_a'].session._client.rest_market_orders = False
    leg['leg_a'].session._client.reject_next = 'Insufficient margin'
    refused = leg['leg_a'].order('GOLD05DEC25F', 'BUY', 1)
    assert refused['unresolved'] is False


def test_an_unknown_status_is_unresolved_not_unfilled(session):
    session._client.orders['999'] = {'orderNo': '999', 'status': '',
                                     'filledQty': 0}
    state = session.await_fill('999', deadline_sec=0.5)
    assert state['status'] == 'UNKNOWN'
    assert state['unresolved'] is True


# -- cancelling ---------------------------------------------------------------

def test_a_cancel_ALWAYS_reports_what_filled_first(leg):
    """A cancelled order can carry partial fills, and a cancel can lose
    the race outright. Reporting 'cancelled' on an order that filled is
    how the book comes to believe a leg is flat."""
    placed = leg['leg_a'].place_limit('GOLD05DEC25F', 'BUY', 2, 74999.0)
    client = leg['leg_a'].session._client
    client.fill_resting(placed['ticket'], units=100)      # 1 lot of 2
    result = leg['leg_a'].cancel_order(placed['ticket'])
    assert result['filled_volume'] == 100.0
    assert result['leaked_fill'] is True


def test_a_clean_cancel_reports_no_leak(leg):
    placed = leg['leg_a'].place_limit('GOLD05DEC25F', 'BUY', 1, 74999.0)
    result = leg['leg_a'].cancel_order(placed['ticket'])
    assert result['cancelled'] is True
    assert result['filled_volume'] == 0.0
    assert result['leaked_fill'] is False


# -- re-pegging ---------------------------------------------------------------

def test_a_repeg_MODIFIES_and_keeps_the_order_id(leg):
    """Cancel-and-replace loses queue position AND changes the id, so
    the book cannot follow the order through its own life."""
    placed = leg['leg_a'].place_limit('GOLD05DEC25F', 'BUY', 1, 74999.0)
    result = leg['leg_a'].modify_order(placed['ticket'], 75000.0,
                                       symbol='GOLD05DEC25F')
    assert result['ok'] is True
    client = leg['leg_a'].session._client
    assert client.modified[-1]['order_id'] == placed['ticket']
    assert client.cancelled == []


def test_a_build_with_no_amend_SAYS_SO_rather_than_silently_replacing(
        session, monkeypatch):
    """It changes what the peg costs, and the screen has to say it."""
    for name in ('modify_order', 'amend_order', 'update_order'):
        monkeypatch.delattr(F.FakeArrowClient, name, raising=False)
    result = session.modify_pending('123', 75000.0)
    assert result['ok'] is False
    assert result['amend_unsupported'] is True
    assert 'back of the queue' in result['error']


# -- positions net ------------------------------------------------------------

def test_an_opposite_order_REDUCES_the_net_instead_of_stacking(leg):
    """On MT5's hedging accounts this opens a SECOND position. Here it
    is what it looks like: a close."""
    leg['leg_a'].order('GOLD05DEC25F', 'BUY', 2)
    assert leg['leg_a'].positions('GOLD05DEC25F')[0]['net_units'] == 200
    leg['leg_a'].close_reduce('GOLD05DEC25F', 'BUY', 2)
    assert leg['leg_a'].positions('GOLD05DEC25F') == []


def test_a_close_crosses_the_OTHER_way_from_the_entry_side(leg):
    leg['leg_a'].order('GOLD05DEC25F', 'SELL', 1)
    leg['leg_a'].close_reduce('GOLD05DEC25F', 'SELL', 1)
    assert leg['leg_a'].session._client.placed[-1]['transactionType'] == 'BUY'


def test_a_partial_close_leaves_the_rest_on(leg):
    leg['leg_a'].order('GOLD05DEC25F', 'BUY', 3)
    leg['leg_a'].close_reduce('GOLD05DEC25F', 'BUY', 1)
    held = leg['leg_a'].positions('GOLD05DEC25F')[0]
    assert held['net_units'] == 200
    assert held['volume'] == 2.0            # lots


def test_a_position_reports_lots_AND_units(leg):
    leg['leg_a'].order('GOLDM05DEC25F', 'BUY', 4)     # LotSize 10
    held = leg['leg_a'].positions('GOLDM05DEC25F')[0]
    assert held['net_units'] == 40
    assert held['volume'] == 4.0
    assert held['lot_size'] == 10
    assert held['ticket'] is None           # there ARE none


# -- None is unknown, and it is not flat --------------------------------------

def test_positions_are_NONE_when_the_call_fails_not_an_empty_list(leg):
    """An empty list says 'flat' and would have the reconciler sweep a
    live account clean in its own report."""
    leg['leg_a'].order('GOLD05DEC25F', 'BUY', 1)
    leg['leg_a'].session._client.raise_on_positions = True
    assert leg['leg_a'].positions() is None
    # CONTROL: working again, and genuinely flat, IS an empty list.
    leg['leg_a'].session._client.raise_on_positions = False
    leg['leg_a'].close_reduce('GOLD05DEC25F', 'BUY', 1)
    assert leg['leg_a'].positions() == []


def test_pending_orders_are_NONE_when_unknown(leg, monkeypatch):
    monkeypatch.delattr(F.FakeArrowClient, 'get_order_book')
    monkeypatch.delattr(F.FakeArrowClient, 'get_orders', raising=False)
    assert leg['leg_a'].pending_orders() is None


def test_a_disconnected_session_is_unknown_not_flat(leg):
    leg['leg_a'].close()
    assert leg['leg_a'].positions() is None
    assert leg['leg_a'].pending_orders() is None


def test_the_server_offset_is_NONE_when_it_cannot_be_measured(leg):
    """Unmeasured is not zero: with no measurement the session cutoff
    does not fire, and the screen says so."""
    assert leg['leg_a'].server_offset() is None


# -- margin --------------------------------------------------------------------

def test_margin_is_NONE_where_the_sdk_has_no_calculator(leg):
    """Never derived from notional. The screen shows an em dash and
    the take-profit target is disabled."""
    assert leg['leg_a'].margin_for(
        [('GOLD05DEC25F', 'BUY', 100)]) is None


def test_margin_asks_for_BOTH_legs_together(leg, monkeypatch):
    """A calendar spread's margin BENEFIT only appears when the two
    legs are priced as one basket."""
    seen = {}

    def calculator(self, basket):
        seen['legs'] = len(basket)
        return {'totalMargin': 62000.0}

    monkeypatch.setattr(F.FakeArrowClient, 'get_margin', calculator,
                        raising=False)
    total = leg['leg_a'].margin_for([('GOLD05DEC25F', 'BUY', 100),
                                     ('GOLD05FEB26F', 'SELL', 100)])
    assert total == 62000.0
    assert seen['legs'] == 2


def test_the_account_reports_ONE_margin_pool(leg):
    """MT5-Trader's 'the weaker of the two brokers carries the pair' is
    wrong here and must not be carried across by habit."""
    info = leg['leg_a'].account_info()
    assert info['single_pool'] is True
    assert info['currency'] == 'INR'


# -- what the sweep can honestly claim ----------------------------------------

def test_a_dedicated_account_is_declared_so_the_banner_can_be_honest(session):
    """There is no magic number on a netting position. A dedicated
    account is what restores the guarantee, and the screen has to know
    whether it has one."""
    assert session.terminal_report()['dedicated'] is True


# -- three scales, three declarations -----------------------------------------

def test_quote_order_and_stream_scales_are_declared_INDEPENDENTLY(session):
    """Blocker 3.2's second half. The WebSocket is documented as paise;
    whether REST quotes and order/position prices are is per build, and
    they need not agree. One shared `scale` would divide a rupee fill by
    a hundred the moment quotes turned out to be paise."""
    session.order_scale = 100.0             # this build reports fills in paise
    session._client.orders['777'] = {'orderNo': '777', 'status': 'COMPLETE',
                                     'filledQty': 100, 'avgPrice': 7500100}
    state = session.order_fill_state('777')
    assert state['price'] == 75001.0
    # ...while the quote side, declared in rupees, is untouched.
    assert session.symbol_tick('GOLD05DEC25F')['ask'] == 75001.0


def test_a_position_average_uses_the_ORDER_scale_not_the_quote_scale(session):
    session.order_scale = 100.0
    session._client.net[('GOLD05DEC25F', 'NRML')] = {'units': 100,
                                                     'price': 7500100}
    held = session.net_positions('GOLD05DEC25F')[0]
    assert held['price_open'] == 75001.0


def test_the_REST_QUOTE_IS_IN_PAISE(session):
    """MEASURED on a live account, and it was wrong by a factor of 100.

    `--probe` on CRUDEOIL21SEP26F answered `BestBidPrice: 908800` for a
    contract whose option strikes run 5950 to 10100. The REST quote is
    in PAISE — and the default said rupees, which put every price on
    every ladder a hundred times too high with each one looking
    perfectly plausible. That is the worst shape a scale error can
    take: nothing about the screen says it is wrong.
    """
    tick = session.symbol_tick('GOLD05DEC25F')
    # The fake holds 74999 / 75001 in rupees and serves paise, as the
    # live API does.
    assert tick['bid'] == 74999.0
    assert tick['ask'] == 75001.0
    # ...and the BOOK is on the same scale as the touch. A depth level
    # priced a hundred times off puts size at prices nobody is showing.
    best = [level for level in tick['depth'] if level['type'] == 'bid'][0]
    assert best['price'] == 74999.0


def test_the_three_scales_are_SETTABLE_and_default_to_what_was_measured():
    """Three, declared separately, because a quote, a streamed tick and
    an order's own price come back from different endpoints and there
    is no reason they must agree.

    Quote and stream are paise, measured. ORDER IS NOT MEASURED —
    nothing read-only can settle what scale a limit price goes out in —
    so it stays a declaration a config line can correct rather than a
    fact.
    """
    from arrowtrader.broker import scales_from
    from arrowtrader.quotes import PAISE, RUPEES
    assert scales_from({}) == {'quote_scale': PAISE, 'stream_scale': PAISE,
                               'order_scale': RUPEES}
    assert scales_from({'ORDER_SCALE': 'paise'})['order_scale'] == PAISE
    assert scales_from({'QUOTE_SCALE': 'RUPEES'})['quote_scale'] == RUPEES
    # A number is taken as itself, for a venue that is neither.
    assert scales_from({'QUOTE_SCALE': 1000})['quote_scale'] == 1000.0
    # Blank means "the default", not zero — a zero scale divides by
    # nothing and would take the price with it.
    assert scales_from({'QUOTE_SCALE': ''})['quote_scale'] == PAISE
