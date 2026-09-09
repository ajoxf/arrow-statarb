"""The Arrow session: the only module that touches `pyarrow_client`.

Every Arrow quirk lives here, the way every MT5 quirk lives in
MT5-Trader's `broker.py`. Above this line nothing knows what a
`TradingSymbol` is, that quantities go on the wire in units, or that
a market order needs `mpp=True`.

FOUR STRUCTURAL DIFFERENCES FROM MT5, EACH OF WHICH CHANGES CALLERS:

1. **One session trades both legs.** The MetaTrader5 package holds one
   global connection per process, which is why MT5-Trader runs a leg
   runner per account and a coordinator above them. Arrow has no such
   limit: one authenticated client trades every contract. So there is
   one of these objects, and both `ArrowLeg`s share it.

2. **An order does not return its fill.** `mt5.order_send` comes back
   with `result.price` — the trade is done when the call returns. Arrow
   returns an `order_id` and nothing else; the fill is discovered by
   POLLING the order book. `send_market_order` therefore has a
   deadline, and a market order that has not resolved when the deadline
   passes is reported as UNRESOLVED — which is not "rejected" and must
   never be unwound as though it were.

3. **Positions net.** There are no position tickets. `net_positions`
   returns one row per (symbol, product) and the reconciler compares it
   against our own ledger. A close is an opposite order, not a
   `position=` field.

4. **There is no slippage parameter.** MT5 takes `deviation` and the
   server enforces it. Arrow takes nothing, so the clicked price is the
   only guard and it is enforced above this module, in the executor.
   What this module owes the executor is an honest `executed_price`.

And one rule kept exactly: **a refusal carries the broker's own words.**
Every error string that leaves here has been through `arrow_errors`.
"""

import logging
import threading
import time

from . import arrow_errors, instruments as instr, quotes
from .models import OrderSide

try:
    import pyarrow_client as arrow
except ImportError:      # not installed off the trading box; tests fake it
    arrow = None


#: How long a market order may go unresolved before we stop waiting and
#: say so. It is NOT a rejection window: an order still working at the
#: deadline is reported unresolved, and the caller reads the order book
#: rather than assuming.
MARKET_RESOLVE_SEC = 5.0

#: How often the order book is re-read while waiting for a fill.
FILL_POLL_SEC = 0.2


def scales_from(settings):
    """The three price scales, from config, as kwargs for `ArrowSession`.

    THREE, DECLARED SEPARATELY, and that is not caution for its own
    sake. A quote, a streamed tick and an order's own price come back
    from different endpoints and there is no reason they must agree on
    a scale — the REST quote turned out to be in PAISE while the
    default said rupees, which put every ladder price a hundred times
    too high and every one of them looking perfectly plausible.

    `quote` and `stream` are paise, measured. `order` is RUPEES and is
    NOT measured: nothing read-only can settle what scale a limit price
    goes out in, and the way to find out is one lot, watched. Until
    then it is a declaration a config line can correct, not a fact.
    """
    settings = settings or {}

    def scale(name, fallback):
        value = settings.get(name)
        if value in (None, ''):
            return fallback
        if str(value).strip().upper() in ('PAISE', 'PAISA'):
            return quotes.PAISE
        if str(value).strip().upper() in ('RUPEES', 'RUPEE', 'INR'):
            return quotes.RUPEES
        return float(value)

    return {
        'quote_scale': scale('QUOTE_SCALE', quotes.PAISE),
        'stream_scale': scale('STREAM_SCALE', quotes.PAISE),
        'order_scale': scale('ORDER_SCALE', quotes.RUPEES),
    }


#: "Not asked yet", which is not None — None is a real answer meaning
#: this build takes no tag at all.
_UNASKED = object()


#: Keys that must never reach a log line or the screen. `_readable`
#: strips them from a response body before that body is quoted back.
#: Compared with `-` and `_` stripped, so `api_secret`, `api-secret`
#: and `apiSecret` are all the same key.
_SECRET_KEYS = ('password', 'token', 'apisecret', 'appsecret', 'totp',
                'totpsecret', 'code', 'checksum', 'requesttoken',
                'sessiontoken', 'secret')


def _dig(body, *keys):
    """First present, non-empty value under any of `keys`, case-blind.

    Arrow's own responses are already inconsistent about case
    (`requestId`, `redirectUrl`, `token`), and the SDK unwraps a `data`
    envelope on some routes and not others. Reading one exact spelling
    is how a working response gets mistaken for a refusal.
    """
    if isinstance(body, dict):
        inner = body.get('data')
        low = {str(k).lower(): v for k, v in body.items()}
        for key in keys:
            value = low.get(str(key).lower())
            if value not in (None, ''):
                return value
        if isinstance(inner, (dict, list)):
            return _dig(inner, *keys)
    elif isinstance(body, list):
        for item in body:
            found = _dig(item, *keys)
            if found not in (None, ''):
                return found
    return None


def _readable(body):
    """Arrow's answer, quotable — with every secret taken out of it.

    THE CREDENTIALS RULE APPLIES TO ERROR TEXT. A login body carries
    the password that was sent, and this string goes on the screen and
    into the log, so the redaction happens here and not at the call
    sites.

    The result is a sentence, not a dict repr: the operator reads it on
    a panel, and `{'message': 'Invalid OTP'}` makes them parse Python
    to find the one word that matters.
    """
    if isinstance(body, dict):
        if not body:
            # UNMEASURED IS NOT ZERO. A step that answered with nothing
            # at all is a different fault from one that refused, and an
            # empty `{}` on the screen says neither.
            return 'nothing at all — an empty body'
        parts = []
        for key, value in body.items():
            if str(key).lower().replace('-', '').replace('_', '') \
                    in _SECRET_KEYS:
                parts.append(f'{key}: (hidden)')
            elif isinstance(value, (dict, list)):
                parts.append(f'{key}: {_readable(value)}')
            else:
                parts.append(f'{key}: {value}')
        return ' | '.join(parts)[:400]
    if isinstance(body, list):
        if not body:
            return 'nothing at all — an empty body'
        return ' | '.join(_readable(item) for item in body)[:400]
    if body in (None, ''):
        return 'nothing at all — an empty body'
    return str(body)[:400]


def _totp(seed):
    """The 6-digit code for a base32 seed."""
    import pyotp
    return pyotp.TOTP(seed).now()


def _request_token(redirect_url):
    """The `request-token` query parameter out of Arrow's redirect."""
    from urllib.parse import parse_qs, urlparse
    try:
        found = parse_qs(urlparse(str(redirect_url)).query)
        return (found.get('request-token') or found.get('request_token')
                or [None])[0]
    except Exception:                                   # noqa: BLE001
        return None


class OrderResult:
    """Outcome of an order, decoupled from the SDK's return values."""

    def __init__(self, success, requested_price=None, executed_price=None,
                 ticket=None, error=None, volume=0.0, unresolved=False):
        self.success = success
        self.requested_price = requested_price
        self.executed_price = executed_price
        self.ticket = ticket
        self.error = error
        #: FILLED volume, in the units the caller passed in.
        self.volume = volume
        #: True where we do not KNOW what happened — a timeout, or a
        #: deadline reached with the order still working. Distinct from
        #: failure, and callers must not unwind on it.
        self.unresolved = unresolved


class ArrowSession:
    """One authenticated Arrow connection, shared by both legs."""

    def __init__(self, account, segments, freeze_quantities=None,
                 tick_sizes=None, clock=time.time, sleep=time.sleep,
                 quote_scale=quotes.PAISE, stream_scale=quotes.PAISE,
                 order_scale=quotes.RUPEES):
        self.account = account
        self.segments = segments
        self.connected = False
        self.master = None
        self.last_error = None
        self.clock = clock
        self.sleep = sleep
        #: WHICH FIELDS ARE IN PAISE. Blocker 3.2's second half: the
        #: WebSocket is documented as paise, REST is per build. Both
        #: are declared rather than guessed, so a wrong answer is one
        #: config line instead of a hundredfold error in the parser.
        #: PAISE, MEASURED. `--probe` on CRUDEOIL21SEP26F answered
        #: bid 908800 / ask 908900 on a contract whose option strikes
        #: run 5950 to 10100 — so the REST quote is in paise and a
        #: ladder built on it unscaled is a hundred times too high,
        #: with every price on it perfectly plausible-looking.
        self.quote_scale = quote_scale
        self.stream_scale = stream_scale
        #: ...AND THE ORDER SIDE IS A THIRD ANSWER. An average fill
        #: price, a resting order's price and a position's average come
        #: back from different endpoints than the quotes do, and there
        #: is no reason they must agree on a scale. Reusing
        #: `quote_scale` for them would silently divide a rupee price by
        #: a hundred the moment REST quotes turn out to be paise —
        #: which is exactly the question blocker 3.2 leaves open. Three
        #: fields, three declarations, all verifiable against one live
        #: fill.
        self.order_scale = order_scale
        self._client = None
        #: Which tag parameter this SDK build takes, asked once.
        self._tag_name = _UNASKED
        self._streams = None
        self._stream_lock = threading.RLock()
        self._stream_ticks = {}
        self._stream_tokens = set()
        #: Symbols we have asked the feed for, and whether it took them.
        #: Mirrors MT5-Trader's `last_visible`: a leg that looks frozen
        #: is either not subscribed or subscribed and receiving
        #: nothing, and those are two faults with two different fixes.
        self.subscribed = {}
        self._freeze_quantities = freeze_quantities or {}
        self._tick_sizes = tick_sizes or {}
        #: Set when the account is declared to be used by nothing else.
        #: It is what restores MT5-Trader's "never touch the trader's
        #: own clicks" guarantee on a netting broker — see models.py.
        self.dedicated = bool(getattr(account, 'dedicated', False))

    # -- connection -------------------------------------------------------

    def initialize(self):
        """Authenticate, then load the instrument master.

        The master is loaded SYNCHRONOUSLY and the session is not
        connected without it. The stat-arb layer loads it on a
        background thread and answers orders in the meantime, which
        means an order can be sized from a lot size that has not
        arrived yet. Here, no master means no session.
        """
        if arrow is None:
            self.last_error = ('pyarrow-client is not installed — '
                               'pip install pyarrow-client')
            return False
        try:
            self._client = arrow.ArrowClient(app_id=self.account.app_id)
            ok = self._login(self._client)
        except Exception as error:                      # noqa: BLE001
            self.last_error = arrow_errors.refusal(error, 'Arrow login')
            self._client = None
            return False
        if not ok:
            # `_login` has already written the reason, in Arrow's words.
            self._client = None
            return False
        if not self.load_master():
            return False
        self.connected = True
        self.last_error = None
        return True

    #: The three steps of Arrow's login, named. A refusal has to say
    #: WHICH one failed: a bad password, a bad TOTP seed and an
    #: unregistered IP all end the same way, and they have three
    #: different fixes.
    LOGIN_STEPS = (
        ('login', 'POST /auth/app/login', 'requestId',
         'App ID, User ID and password'),
        ('2fa', 'POST /auth/validate-2fa', 'redirectUrl',
         'the TOTP seed (ARROW_TOTP_SECRET) and this machine\'s clock'),
        ('token', 'POST /auth/app/authenticate-token', 'token',
         'the API secret (ARROW_API_SECRET)'),
    )

    def _login(self, client):
        """Log in, and say what Arrow ACTUALLY answered when it fails.

        The SDK's own `auto_login` walks three steps and reads one key
        out of each response — `requestId`, then `redirectUrl`, then
        `token`. Its transport only raises when the body carries
        `status: error` or an `errorCode`, so a refusal shaped any
        other way falls straight through and the step's `resp[key]`
        raises a bare `KeyError`. What the operator then sees is the
        word `'redirectUrl'` and nothing else — no status, no message,
        and no indication that the TOTP was the thing Arrow rejected.

        That is precisely the failure this system is not allowed to
        have. So we walk the same three steps ourselves, through the
        SDK's own client and its own URLs, and when a step does not
        return the key it owes us we report the step, the fix, and the
        body Arrow sent — verbatim, minus anything secret.
        """
        steps = dict((name, (where, key, fix))
                     for name, where, key, fix in self.LOGIN_STEPS)

        def refuse(step, body):
            where, key, fix = steps[step]
            said = _readable(body)
            self.last_error = arrow_errors.refusal(
                f'{where} answered without "{key}". Check {fix}. '
                f'Arrow said: {said}', 'Arrow login')
            return False

        post = getattr(client, '_post', None)
        if not callable(post):
            # An SDK build without the private surface: fall back to its
            # own login and take whatever error it gives.
            got = client.auto_login(
                user_id=self.account.user_id,
                password=self.account.password,
                # The installed SDK's parameter is `api_secret`. The
                # docs say `app_secret` and the docs are wrong.
                api_secret=self.account.api_secret,
                # The base32 SEED, never the 6-digit code.
                totp_secret=self.account.totp_secret)
            if not got:
                self.last_error = arrow_errors.refusal(
                    'Arrow refused the login', 'Arrow login')
                return False
            return True

        first = post(client.DEFAULT_LOGIN_URL, params={
            'userID': self.account.user_id,
            'password': self.account.password,
            'captchaValue': '', 'captchaID': None,
            'appID': self.account.app_id, 'isAppLogin': True})
        request_id = _dig(first, 'requestId')
        if not request_id:
            return refuse('login', first)

        # The base32 SEED, never the 6-digit code.
        try:
            code = _totp(self.account.totp_secret)
        except Exception as error:                      # noqa: BLE001
            # NOT "login failed". A seed that will not generate a code
            # is a typo in `.env`, and saying which of the three things
            # is wrong is the whole point of walking the steps.
            self.last_error = arrow_errors.refusal(
                'No TOTP could be generated from ARROW_TOTP_SECRET. It must '
                'be the base32 SEED from your authenticator setup, not the '
                '6-digit code: ' + str(error), 'Arrow login')
            return False
        second = post(client.VALIDATE_2FA_URL, params={
            'code': code, 'requestId': request_id,
            'userID': self.account.user_id})
        redirect = _dig(second, 'redirectUrl', 'redirect_url')
        if not redirect:
            return refuse('2fa', second)

        token = _request_token(redirect)
        if not token:
            self.last_error = arrow_errors.refusal(
                'Arrow returned a redirect with no request-token in it: '
                + str(redirect)[:200], 'Arrow login')
            return False

        # The installed SDK's parameter is `api_secret`. The docs say
        # `app_secret` and the docs are wrong.
        third = client.login(request_token=token,
                             api_secret=self.account.api_secret)
        if not _dig(third, 'token'):
            return refuse('token', third)
        if not client.token:
            client.set_token(_dig(third, 'token'))
        return True

    def load_master(self):
        """Fetch and index the instrument master. ~223k rows."""
        try:
            rows = instr.parse_master(self._client.get_instruments())
        except Exception as error:                      # noqa: BLE001
            self.last_error = arrow_errors.refusal(error, 'Instrument master')
            return False
        #: Where each batch of rows came from, for the log and the page.
        self.master_sources = {'/all': len(rows)}
        rows = rows + self._extra_segment_rows(rows)
        if not rows:
            self.last_error = (
                'The instrument master came back empty. Nothing can be '
                'sized or priced without it — check the account is '
                'entitled to a segment, and reload.')
            return False
        self.master = instr.Master(
            rows, segments=self.segments,
            freeze_quantities=self._freeze_quantities,
            tick_sizes=self._tick_sizes)
        logging.info('Arrow: master loaded — %d contracts across %s (%s)',
                     self.master.rows, sorted(self.master.exch_segs),
                     ', '.join(f'{route} {count}' for route, count
                               in self.master_sources.items()))
        if self.master.unknown_exch_segs:
            # Named, not dropped silently: a master full of MCXFO rows
            # and a build that knows no MCX segment is a one-line fix.
            logging.warning('Arrow: %s carry segments this build does not '
                            'know — they cannot be grouped in the picker',
                            self.master.unknown_exch_segs)
        return True

    #: Segment downloads that are NOT on `/all`, as {route: segment key}.
    #:
    #: pyarrow-client's own source says so, in the enum it ships:
    #:
    #:     # MCX is used for user-permission checks and instrument
    #:     # segment downloads (GET /mcx).
    #:
    #: and then provides no method for it — `get_instruments()` is
    #: `/all` and nothing else. So a master fetched the documented way
    #: can be complete for NSE and BSE and carry not one commodity
    #: contract, and the Exchanges page would report that the account is
    #: not entitled to MCX. That is a WRONG DIAGNOSIS, which is worse
    #: than no diagnosis: it sends the operator to Arrow's support desk
    #: to ask for a segment they already have.
    SEGMENT_ROUTES = (('/mcx', 'mcx_fo'),)

    def _extra_segment_rows(self, have):
        """Rows for segments `/all` did not carry, from their own routes.

        Asked for where `/all` came back with no FUTURES for the
        segment — not merely no rows.

        THE DIFFERENCE IS THE WHOLE POINT. Testing "did `/all` mention
        this segment at all" is satisfied by a single option, so a
        master carrying MCX's entire option chain and not one future
        looks complete, `/mcx` is never asked, and the contracts this
        terminal actually trades never arrive. What the operator sees
        is an empty futures list on a segment the page has just called
        ready.

        A route that is not there is still not an error: it means this
        account's `/all` is the whole story.
        """
        client, extra = self._client, []
        get = getattr(client, '_get', None)
        routes = getattr(client, '_routes', None)
        if not callable(get) or routes is None:
            return extra
        root = getattr(routes, '_root_url', '') or ''
        seen = set()
        for row in have:
            exch = str(instr.field(row, 'ExchSeg', 'exch_seg', 'exchseg')
                       or '').strip().upper()
            if not exch:
                continue
            symbol = instr.field(row, 'TradingSymbol', 'trading_symbol',
                                 'tsym')
            kind = instr.classify(
                exch, instr.field(row, 'OptionType', 'option_type',
                                  'optiontype'),
                symbol, instr.field(row, 'StrikePrice', 'strike'))
            if kind == 'future':
                seen.add(exch)
        for route, key in self.SEGMENT_ROUTES:
            segment = self.segments.get(key)
            if segment is None or seen & set(segment.spellings()):
                continue
            try:
                found = instr.parse_master(get(root + route))
            except Exception as error:                  # noqa: BLE001
                # NOT a connection failure. `/all` succeeded; this is a
                # supplement, and a broker that does not serve it is
                # answered by the Segments page, in its own words.
                logging.info('Arrow: %s carries no extra instruments (%s)',
                             route, error)
                continue
            if found:
                logging.info('Arrow: %s added %d %s contracts that /all did '
                             'not carry', route, len(found), segment.label)
                self.master_sources[route] = len(found)
                extra.extend(found)
        return extra

    def shutdown(self):
        self.stop_stream()
        if self._client is not None:
            try:
                self._client.invalidate_session()
            except Exception:                           # noqa: BLE001
                pass
        self._client = None
        self.connected = False

    def is_alive(self):
        """Is the session usable RIGHT NOW?

        The token lasts about 24 hours, and an expired one fails every
        call with the same shape as a network problem. Asking a cheap
        endpoint is the only way to tell them apart.
        """
        if not self.connected or self._client is None:
            return False
        try:
            return bool(self._client.get_user_details())
        except Exception as error:                      # noqa: BLE001
            self.last_error = arrow_errors.refusal(error, 'Arrow session')
            return False

    def sdk_exchanges(self):
        """The `Exchange` enum values this SDK build carries, or None.

        None is 'could not be read', which is NOT 'none of them' — see
        `segments.available_segments`, which reports the two cases
        differently because they have different fixes.
        """
        if arrow is None or not hasattr(arrow, 'Exchange'):
            return None
        try:
            return [member.value for member in arrow.Exchange]
        except Exception:                               # noqa: BLE001
            return None

    # -- instruments ------------------------------------------------------

    def contract(self, symbol):
        return self.master.contract(symbol) if self.master else None

    def ensure_symbol(self, symbol):
        """The specs the sizing depends on, or a refusal naming the fix.

        Shaped like MT5-Trader's `ensure_symbol`, with `contract_size`
        carrying `LotSize` and `volume_step`/`volume_min` both 1 —
        there is no fractional lot on an Indian exchange.
        """
        if self.master is None:
            return {'ok': False, 'error': 'the instrument master is not '
                                          'loaded — reconnect'}
        contract = self.master.contract(symbol)
        if contract is None:
            return {'ok': False,
                    'error': f'{symbol} is not in the instrument master'}
        if contract.lot_size is None:
            # UNKNOWN IS NOT ONE. Nothing may be sized on this.
            return {'ok': False,
                    'error': f'{symbol} has no LotSize in the master, so '
                             f'nothing can be sized on it'}
        self._subscribe(contract)
        return {
            'ok': True,
            'volume_min': 1.0,
            'volume_max': None,          # the freeze quantity is the cap
            'volume_step': 1.0,
            'point': contract.tick_size,
            'tick_size': contract.tick_size,
            'contract_size': contract.lot_size,
            'freeze_qty': contract.freeze_qty,
            'expiry': contract.expiry.isoformat() if contract.expiry else None,
            'days_to_expiry': contract.days_to_expiry(),
            'token': contract.token,
            'segment': contract.segment,
        }

    def find_symbols(self, pattern, limit=40, segment=None, kind=None):
        if self.master is None:
            return None
        return [contract.to_dict()
                for contract in self.master.search(pattern, segment=segment,
                                                   kind=kind, limit=limit)]

    def symbol_report(self, symbol):
        if self.master is None:
            return {'symbol': symbol, 'found': False,
                    'error': 'the instrument master is not loaded'}
        report = self.master.report(symbol)
        tick = self.symbol_tick(symbol)
        report['priced'] = bool(tick and tick.get('executable'))
        report['bid'] = (tick or {}).get('bid')
        report['ask'] = (tick or {}).get('ask')
        if tick and not tick.get('executable'):
            report.setdefault('problems', []).append(
                quotes.missing_book_reason(tick, tick, symbol, symbol)
                or f'{symbol} is not quoting a tradeable book')
            report['ok'] = False
        return report

    # -- prices -----------------------------------------------------------

    def symbol_tick(self, symbol):
        """The current book for one contract, or None.

        The STREAM is preferred where it has a tick: it is ~50ms and
        costs nothing per read. REST is the fallback and the warm-up.
        Either way what comes back has been through `quotes`, so a
        missing side is None and an absent book is None.
        """
        contract = self.contract(symbol)
        if contract is None:
            return None
        self._subscribe(contract)
        streamed = self._stream_ticks.get(contract.token)
        if streamed is not None:
            return dict(streamed, visible=True)
        raw = self._rest_quote(contract)
        if raw is None:
            return None
        tick = quotes.normalise_quote(raw, scale=self.quote_scale)
        if tick is not None:
            tick['visible'] = self.subscribed.get(symbol)
        return tick

    def _rest_quote(self, contract):
        if not self.connected or self._client is None:
            return None
        exchange = self._exchange(contract.segment)
        if exchange is None:
            return None
        try:
            # FULL, not LTP. An LTP-only quote cannot price a ladder —
            # see quotes.py and blocker 3.2.
            response = self._client.get_quotes(
                self._quote_mode(), [(contract.trading_symbol, exchange)])
        except Exception as error:                      # noqa: BLE001
            self.last_error = arrow_errors.refusal(error, 'Quote')
            return None
        if isinstance(response, dict):
            return response
        for row in (response or ()):
            if isinstance(row, dict):
                return row
        return None

    def _quote_mode(self):
        """`QuoteMode.FULL` where this build has it, LTP where it does not.

        An LTP-only build is a DEGRADED session, not a working one:
        `quotes.normalise_quote` will produce a tick with no bid and no
        ask, `compute_spread` will refuse it, and the ladder will say
        so. That is the correct outcome — it is far better than a
        ladder drawn around a last trade.
        """
        mode = getattr(arrow, 'QuoteMode', None)
        for name in ('FULL', 'QUOTE', 'DEPTH', 'OHLC', 'LTP'):
            found = getattr(mode, name, None)
            if found is not None:
                return found
        return None

    def depth(self, symbol):
        """This contract's order book, or None where there is none."""
        tick = self.symbol_tick(symbol)
        return (tick or {}).get('depth')

    def session_stats(self, symbol):
        """The contract's own session O/H/L/V, as the exchange reports it."""
        contract = self.contract(symbol)
        if contract is None:
            return None
        raw = self._rest_quote(contract)
        if not raw:
            return None

        def value(*names):
            found = instr.field(raw, *names)
            try:
                number = float(found)
            except (TypeError, ValueError):
                return None
            # Unmeasured is not zero: a missing high must render as an
            # em dash, never as a real high of 0.00.
            if number <= 0:
                return None
            scale = self.quote_scale
            return number / scale if scale and scale != 1.0 else number

        return {
            'symbol': symbol,
            'open': value('Open', 'open', 'o'),
            'high': value('High', 'high', 'h'),
            'low': value('Low', 'low', 'l'),
            # Volume is a COUNT, never a price — it is not scaled.
            'volume': _number(instr.field(raw, 'Volume', 'volume', 'v')),
        }

    def resubscribe(self, symbol):
        """Drop the feed subscription and take it back.

        The one thing that reliably restarts a feed that has gone
        quiet. Returns the tick that came back, so the caller can say
        whether it worked rather than claim it did.
        """
        contract = self.contract(symbol)
        if contract is None or contract.token is None:
            return None
        with self._stream_lock:
            self._stream_tokens.discard(contract.token)
            self._stream_ticks.pop(contract.token, None)
            self.subscribed.pop(symbol, None)
        self._subscribe(contract)
        return self.symbol_tick(symbol)

    # -- the live feed ----------------------------------------------------

    def _subscribe(self, contract):
        """Idempotent: subscribing an already-subscribed token is a
        local set lookup, so this is done on the READ rather than
        hoped for at startup."""
        if contract.token is None or not self.connected:
            self.subscribed[contract.trading_symbol] = False
            return False
        with self._stream_lock:
            if contract.token in self._stream_tokens:
                return True
            if not self._start_stream():
                self.subscribed[contract.trading_symbol] = False
                return False
            try:
                self._streams.subscribe_market_data(
                    self._data_mode(), [int(contract.token)])
            except Exception as error:                  # noqa: BLE001
                logging.warning('Arrow: could not subscribe %s — %s',
                                contract.trading_symbol, error)
                self.subscribed[contract.trading_symbol] = False
                return False
            self._stream_tokens.add(contract.token)
            self.subscribed[contract.trading_symbol] = True
            return True

    def _data_mode(self):
        """FULL, because FULL is the only mode that carries the book.

        The SDK's four modes are ltp (13 bytes), ltpc (17), quote (93)
        and full (249), and the five levels a side live in the last
        140 bytes of the full packet alone. QUOTE has total buy and
        sell quantity and NO PRICES — a ladder built on it has no
        touch. The rest are listed as fallbacks for a build that
        renames them, and every one below FULL is a degraded feed that
        says so on the screen.
        """
        mode = getattr(arrow, 'DataMode', None)
        for name in ('FULL', 'DEPTH', 'QUOTE', 'LTPC', 'LTP'):
            found = getattr(mode, name, None)
            if found is not None:
                return found
        return None

    def _start_stream(self):
        if self._streams is not None:
            return True
        try:
            streams = arrow.ArrowStreams(appID=self.account.app_id,
                                         token=self._token(), debug=False)
            streams.data_stream.on_ticks = self._on_ticks
            streams.connect_data_stream()
        except Exception as error:                      # noqa: BLE001
            logging.warning('Arrow: price stream did not start — %s', error)
            return False
        self._streams = streams
        return True

    def _on_ticks(self, tick):
        """One tick off the WebSocket, normalised the same way REST is.

        Prices here are in PAISE — that is the one thing about this
        feed that is documented and confirmed. The scale is still
        declared rather than hardcoded at the call site, so REST and
        the stream cannot silently disagree.
        """
        try:
            token = int(getattr(tick, 'token', None)
                        or instr.field(_as_dict(tick), 'token') or 0)
        except (TypeError, ValueError):
            return
        if not token:
            return
        payload = _as_dict(tick)
        normalised = quotes.normalise_quote(payload, scale=self.stream_scale)
        if normalised is None:
            return
        previous = self._stream_ticks.get(token)
        if previous is not None:
            # A QUOTE tick and a DEPTH tick can arrive separately, and
            # a tick that carries only a last trade must not WIPE a bid
            # and ask we already have. Merge forward, field by field.
            for key in ('bid', 'ask', 'last', 'depth', 'bid_size',
                        'ask_size'):
                if normalised.get(key) is None:
                    normalised[key] = previous.get(key)
            normalised['executable'] = (normalised['bid'] is not None
                                        and normalised['ask'] is not None)
        self._stream_ticks[token] = normalised

    def stop_stream(self):
        with self._stream_lock:
            if self._streams is not None:
                try:
                    self._streams.disconnect_all()
                except Exception:                       # noqa: BLE001
                    pass
            self._streams = None
            self._stream_tokens.clear()
            self._stream_ticks.clear()
            self.subscribed.clear()

    # -- helpers ----------------------------------------------------------

    @staticmethod
    def _to_rupees(value, scale):
        """Divide by the declared scale, once. None stays None."""
        if value is None or not scale or scale == 1.0:
            return value
        return value / scale

    def _token(self):
        """The session token. IT NEVER LEAVES THIS MODULE — not into a
        log line, not into the status file, not into the browser."""
        return getattr(self._client, 'token', None)

    def _exchange(self, segment_key):
        value = self.segments.exchange_for(segment_key)
        if value is None or arrow is None:
            return None
        try:
            return arrow.Exchange(value)
        except (ValueError, AttributeError):
            self.last_error = (
                f"the SDK's Exchange enum has no {value} value — this build "
                f"of pyarrow-client cannot address that exchange")
            return None

    # -- orders -----------------------------------------------------------

    def _place(self, contract, side, units, order_type, price=None,
               product='NRML', validity='DAY', tag=''):
        """One `place_order` call, with the Arrow rules applied.

        THE MARKET-ORDER RULE: Arrow disables plain MKT, so a market
        order MUST carry `price=0` and `mpp=True` or it is rejected
        outright. That is not a preference — it is the API, and the
        pair is applied here so no caller can get it half right.
        """
        exchange = self._exchange(contract.segment)
        if exchange is None:
            return None, self.last_error or 'unknown exchange'
        is_market = str(order_type).upper() in ('MARKET', 'MKT')
        request = {
            'exchange': exchange,
            'symbol': contract.trading_symbol,
            # UNITS. Never lots. `sizing.units` made this number.
            'quantity': int(units),
            # REQUIRED, and it has no default in the SDK: leaving it
            # out is a TypeError before the call reaches the wire, and
            # every order fails with a Python error where the broker's
            # words should be. Zero means the whole order is visible,
            # which is what a spread leg wants — an iceberg leg fills
            # slower than the leg it is hedging, and a hedge that fills
            # at two different speeds is not a hedge.
            'disclosed_quantity': 0,
            'product': self._enum('ProductType', 'NRML' if product.upper()
                                  == 'NRML' else product.upper()),
            'order_type': self._enum('OrderType',
                                     'MKT' if is_market else 'LMT'),
            'variety': self._enum('Variety', 'REGULAR'),
            'transaction_type': self._enum(
                'TransactionType',
                'BUY' if OrderSide(side) is OrderSide.BUY else 'SELL'),
            'price': 0.0 if is_market else float(price or 0.0),
            'validity': self._enum('Retention', str(validity).upper()),
        }
        if is_market:
            request['mpp'] = True
        try:
            order_id = self._call_place(request, tag)
        except Exception as error:                      # noqa: BLE001
            return None, arrow_errors.refusal(
                error, f'{request["transaction_type"]} {units} '
                       f'{contract.trading_symbol}')
        if not order_id:
            return None, arrow_errors.refusal(
                'Arrow returned no order id',
                f'{side} {units} {contract.trading_symbol}')
        return str(order_id), None

    #: The SDK's free-text field, in the order builds have spelled it.
    TAG_FIELDS = ('remarks', 'tag', 'order_tag', 'user_remarks')

    def _tag_field(self):
        """Which tag parameter THIS build takes, asked once, from the
        signature — never discovered by calling and catching.

        Trying each name in turn and catching `TypeError` looks
        equivalent and is not: a missing REQUIRED argument raises the
        same `TypeError` as an unknown keyword, so the loop swallowed a
        real signature error four times over and reported the last one.
        That is how a missing `disclosed_quantity` — required, with no
        default — surfaced as a tag problem.
        """
        if self._tag_name is not _UNASKED:
            return self._tag_name
        self._tag_name = None
        try:
            import inspect
            takes = inspect.signature(self._client.place_order).parameters
        except (TypeError, ValueError):                 # noqa: BLE001
            return None
        if any(p.kind is p.VAR_KEYWORD for p in takes.values()):
            self._tag_name = self.TAG_FIELDS[0]
            return self._tag_name
        for name in self.TAG_FIELDS:
            if name in takes:
                self._tag_name = name
                break
        return self._tag_name

    def _call_place(self, request, tag):
        """Send it, with a tag where the build accepts one.

        A tag scopes OUR OWN ORDERS so a sweep never cancels the
        trader's. It does not scope positions — nothing can, on a
        netting account.
        """
        name = self._tag_field() if tag else None
        if name:
            return self._client.place_order(**request, **{name: tag})
        return self._client.place_order(**request)

    def _enum(self, name, value):
        holder = getattr(arrow, name, None)
        if holder is None:
            return value
        try:
            return holder(value)
        except (ValueError, AttributeError):
            return getattr(holder, value, value)

    def send_market_order(self, symbol, side, units, comment='',
                          product='NRML', deadline_sec=None):
        """Cross now, then WAIT to find out what happened.

        This is the shape difference that matters most against MT5.
        `mt5.order_send` returns `result.price` — the trade is done when
        the call returns, and a caller can compare it against the
        clicked price in the same breath. Arrow returns an order id, and
        the fill has to be read back out of the order book.

        So there are THREE outcomes here, not two:

        - filled (possibly in part), with a real average price;
        - REJECTED, with the exchange's own words — safe to unwind;
        - UNRESOLVED, because the deadline passed with the order still
          working. **This is not a rejection.** The order may fill a
          moment later, and unwinding on it is how a position gets
          doubled instead of cancelled.
        """
        contract = self.contract(symbol)
        if contract is None:
            return OrderResult(False, error=f'{symbol} is not in the master')
        # The touch we are aiming at, recorded BEFORE the order goes, so
        # the executor's slippage guard has something to measure
        # against. Arrow takes no deviation parameter — this is the
        # only slippage protection in the system.
        tick = self.symbol_tick(symbol) or {}
        requested = (tick.get('ask') if OrderSide(side) is OrderSide.BUY
                     else tick.get('bid'))
        order_id, error = self._place(contract, side, units, 'MARKET',
                                      product=product, tag=comment)
        if error:
            return OrderResult(False, requested_price=requested, error=error)
        state = self.await_fill(order_id, deadline_sec=deadline_sec)
        if state['status'] == 'REJECTED':
            return OrderResult(False, requested_price=requested,
                               ticket=order_id,
                               error=arrow_errors.refusal(
                                   state.get('error') or 'rejected',
                                   f'{side} {units} {symbol}'))
        if state['unresolved']:
            return OrderResult(False, requested_price=requested,
                               ticket=order_id, unresolved=True,
                               volume=state['filled_units'],
                               executed_price=state['price'],
                               error=(f'{symbol}: order {order_id} is still '
                                      f'working after {state["waited"]:.1f}s '
                                      f'— READ THE ORDER BOOK before doing '
                                      f'anything about it. It is not a '
                                      f'rejection and must not be unwound.'))
        return OrderResult(bool(state['filled_units']),
                           requested_price=requested,
                           executed_price=state['price'],
                           ticket=order_id,
                           volume=state['filled_units'],
                           error=None if state['filled_units'] else
                           f'{symbol}: order {order_id} filled nothing')

    def place_pending_limit(self, symbol, side, units, price, comment='',
                            product='NRML'):
        """Rest a real limit at the exchange.

        UNLIKE MT5, A RESTING ORDER HERE CAN CLOSE. MT5 honours
        `position` on a DEAL and ignores it on a PENDING, so a
        "closing" limit rested as an ordinary limit and, on a hedging
        account, opened a SECOND position facing the other way (live
        2026-09-02, ticket 2092). On a netting account an opposite
        resting limit REDUCES the net, which is what it looks like it
        does. So the quoter may rest closes here.

        The price is rounded to the contract's tick size — an exchange
        rejects anything else, which is the Indian analogue of MT5's
        10015 Invalid Price.
        """
        contract = self.contract(symbol)
        if contract is None:
            return {'ok': False, 'ticket': None,
                    'error': f'{symbol} is not in the master'}
        price, moved = self.legal_limit_price(symbol, price)
        order_id, error = self._place(contract, side, units, 'LIMIT',
                                      price=price, product=product,
                                      tag=comment)
        if error:
            return {'ok': False, 'ticket': None, 'error': error}
        return {'ok': True, 'ticket': order_id, 'error': None,
                'price': price, 'price_note': moved}

    def legal_limit_price(self, symbol, price):
        """A price the exchange will accept, and a note where it moved.

        Only the tick size is enforced here. The daily price range
        (DPR) is the other refusal and it is NOT enforced by rounding —
        a price outside the band is a refusal the operator must see,
        not something to be quietly clamped into range.
        """
        contract = self.contract(symbol)
        if contract is None or not contract.tick_size or price is None:
            return price, None
        tick = float(contract.tick_size)
        snapped = round(round(float(price) / tick) * tick, 8)
        if abs(snapped - float(price)) < 1e-9:
            return snapped, None
        return snapped, (f'{price:g} is not a multiple of {symbol}\'s '
                         f'{tick:g} tick — sent at {snapped:g}')

    def modify_pending(self, ticket, price, symbol=None):
        """Re-peg in place. MODIFY, never cancel-and-replace.

        Every re-price loses queue position; cancel-and-replace loses
        it AND changes the order id, so the book cannot follow the
        order through its own life. Where the SDK exposes no modify at
        all this returns a refusal that SAYS SO, rather than silently
        falling back to cancel-and-replace — because that changes what
        the peg costs, and the screen has to be able to say it.
        """
        if symbol:
            price, _moved = self.legal_limit_price(symbol, price)
        for name in ('modify_order', 'amend_order', 'update_order'):
            call = getattr(self._client, name, None)
            if call is None:
                continue
            try:
                call(ticket, price=float(price))
            except TypeError:
                try:
                    call(order_id=ticket, price=float(price))
                except Exception as error:              # noqa: BLE001
                    return {'ok': False,
                            'error': arrow_errors.refusal(error, 'Re-peg')}
            except Exception as error:                  # noqa: BLE001
                return {'ok': False,
                        'error': arrow_errors.refusal(error, 'Re-peg')}
            return {'ok': True, 'error': None, 'price': price}
        return {'ok': False, 'amend_unsupported': True,
                'error': 'this build of pyarrow-client exposes no '
                         'modify/amend, so a resting order cannot be '
                         're-priced in place — every re-peg would be a '
                         'cancel and replace, and would go to the back '
                         'of the queue'}

    def cancel_pending(self, ticket):
        """Pull an order, then ALWAYS report what filled first.

        A cancelled order can carry partial fills, and a cancel can
        lose the race with a fill outright. Reporting 'cancelled' on
        an order that filled is how the book comes to believe a leg is
        flat while the money is at the exchange.
        """
        error = None
        try:
            self._client.cancel_order(ticket)
        except Exception as failure:                    # noqa: BLE001
            error = arrow_errors.refusal(failure, 'Cancel')
        state = self.order_fill_state(ticket)
        filled = state.get('filled_volume') or 0.0
        return {
            'ok': error is None or bool(filled),
            'cancelled': error is None and not state.get('still_open'),
            'filled_volume': filled,
            'price': state.get('price'),
            'position_tickets': state.get('position_tickets') or [],
            'still_open': state.get('still_open'),
            #: A cancel that did not prevent a fill is its own event and
            #: has to stay visible in the report.
            'leaked_fill': bool(filled) and error is None,
            'error': error,
        }

    def await_fill(self, ticket, deadline_sec=None):
        """Poll the order book until the order resolves, or the deadline.

        `unresolved` is the field that matters: True means we do not
        know, and the caller must not treat it as a rejection.
        """
        deadline = (MARKET_RESOLVE_SEC if deadline_sec is None
                    else deadline_sec)
        started = self.clock()
        state = {}
        while True:
            state = self.order_fill_state(ticket)
            status = state.get('status')
            waited = self.clock() - started
            if status in ('COMPLETE', 'REJECTED', 'CANCELLED'):
                return {'status': status, 'filled_units': state['filled_volume'],
                        'price': state['price'], 'unresolved': False,
                        'waited': waited, 'error': state.get('error')}
            if waited >= deadline:
                return {'status': status or 'UNKNOWN',
                        'filled_units': state['filled_volume'],
                        'price': state['price'],
                        # UNKNOWN is unknown. A status we could not read
                        # is as unresolved as an order still working.
                        'unresolved': True, 'waited': waited,
                        'error': state.get('error')}
            self.sleep(FILL_POLL_SEC)

    def order_fill_state(self, ticket):
        """What the broker holds for one order id, normalised.

        `status` is one of PENDING / OPEN / PARTIAL / COMPLETE /
        REJECTED / CANCELLED / UNKNOWN. **UNKNOWN is unknown**, and is
        never read as "not filled".
        """
        raw = self._order_row(ticket)
        if raw is None:
            return {'ok': False, 'status': 'UNKNOWN', 'filled_volume': 0.0,
                    'price': None, 'position_tickets': [], 'still_open': None,
                    'error': self.last_error}
        status = _status(instr.field(raw, 'status', 'orderStatus', 'ordStatus',
                                     'report_type'))
        filled = _number(instr.field(raw, 'filledQty', 'filled_quantity',
                                     'fillshares', 'cumQty',
                                     'filledQuantity')) or 0.0
        average = _number(instr.field(raw, 'avgPrice', 'averagePrice',
                                      'avgprc', 'avg_price', 'tradedPrice'))
        average = self._to_rupees(average, self.order_scale)
        return {
            'ok': True,
            'status': status,
            'filled_volume': filled,
            'price': average if filled else None,
            'position_tickets': [str(ticket)],
            'still_open': status in ('PENDING', 'OPEN', 'PARTIAL'),
            'error': (instr.field(raw, 'rejectionReason', 'rejReason',
                                  'message', 'errorMessage')
                      if status == 'REJECTED' else None),
        }

    def _order_row(self, ticket):
        for name in ('get_order_status', 'get_order_history',
                     'single_order_history', 'order_history'):
            call = getattr(self._client, name, None)
            if call is None:
                continue
            try:
                raw = call(ticket)
            except Exception as error:                  # noqa: BLE001
                self.last_error = arrow_errors.refusal(error, 'Order status')
                continue
            row = _latest(raw)
            if row is not None:
                return row
        for name in ('get_order_book', 'get_orders'):
            call = getattr(self._client, name, None)
            if call is None:
                continue
            try:
                book = call() or []
            except Exception as error:                  # noqa: BLE001
                self.last_error = arrow_errors.refusal(error, 'Order book')
                return None
            for row in book:
                if not isinstance(row, dict):
                    continue
                if str(instr.field(row, 'orderNo', 'order_id',
                                   'nestOrderNumber', 'norenordno', 'id')
                       or '') == str(ticket):
                    return row
        return None

    def verify_ticket(self, ticket):
        """Proof the order reached the exchange — not just that the
        call returned. MT5-Trader's `verify_ticket`, same job."""
        row = self._order_row(ticket)
        if row is None:
            return {'ticket': ticket, 'confirmed': False,
                    'error': 'the broker has no record of this order id'}
        return {'ticket': ticket, 'confirmed': True,
                'status': _status(instr.field(row, 'status', 'orderStatus')),
                'symbol': instr.field(row, 'tradingSymbol', 'symbol'),
                'error': None}

    # -- positions --------------------------------------------------------

    def net_positions(self, symbol=None):
        """What the exchange says we are, per (symbol, product). Or None.

        **None MEANS UNKNOWN**, which is not "flat". A call that failed,
        a token that expired and an account that is genuinely flat all
        look identical to a caller that reads an empty list, and the
        reconciler acting on that would sweep a live account clean in
        its own report while the money sits at the exchange.

        There are no tickets. Volume comes back in LOTS — computed from
        the exchange's units and the master's LotSize — with the units
        beside it, because units is the number that can be wrong.
        """
        if not self.connected or self._client is None:
            return None
        try:
            raw = self._client.get_positions()
        except Exception as error:                      # noqa: BLE001
            self.last_error = arrow_errors.refusal(error, 'Positions')
            return None
        if raw is None:
            return None
        out = []
        for row in raw:
            if not isinstance(row, dict):
                continue
            name = str(instr.field(row, 'tradingSymbol', 'tradingsymbol',
                                   'symbol') or '').upper()
            if symbol and name != str(symbol).upper():
                continue
            buys = _number(instr.field(row, 'buyQty', 'buyQuantity',
                                       'cfBuyQty')) or 0.0
            sells = _number(instr.field(row, 'sellQty', 'sellQuantity',
                                        'cfSellQty')) or 0.0
            net_units = _number(instr.field(row, 'netQty', 'netQuantity',
                                            'quantity', 'netqty'))
            if net_units is None:
                net_units = buys - sells
            average = self._to_rupees(
                _number(instr.field(row, 'avgPrice', 'averagePrice',
                                    'netAvgPrice', 'buyAvgPrice')),
                self.order_scale)
            lot_size = self.master.lot_size(name) if self.master else None
            out.append({
                # There is no ticket. Kept as None so ported code that
                # reads the field gets an honest absence rather than a
                # number that means something else.
                'ticket': None,
                'symbol': name,
                'product': str(instr.field(row, 'product', 'productType')
                               or ''),
                'side': 'BUY' if net_units > 0 else
                        'SELL' if net_units < 0 else 'FLAT',
                'net_units': net_units,
                # LOTS, or None where the lot size is unknown — which
                # is not zero lots.
                'volume': (None if not lot_size
                           else abs(net_units) / float(lot_size)),
                'price_open': average,
                'lot_size': lot_size,
                'pnl': _number(instr.field(row, 'pnl', 'unrealizedPnl',
                                           'mtm', 'mtom', 'netPnl')),
            })
        return out

    def close_by_reduction(self, symbol, side, units, comment='',
                           product='NRML', deadline_sec=None):
        """Get OUT of `units` on this contract, by crossing the other way.

        This replaces MT5-Trader's `close_position_ticket`. There is
        nothing to name: the exchange nets, so an opposite order for
        the same units on the same product reduces what is open.

        `side` is the side the position was ENTERED on; the order goes
        the other way. Naming it that way round is deliberate — every
        caller in the ported code already holds the entry side, and a
        function that took the CLOSING side would invert silently the
        first time somebody passed the wrong one.

        A guard never prevents a close, so nothing in this path can
        withhold it.
        """
        return self.send_market_order(
            symbol, OrderSide(side).opposite, units,
            comment=comment, product=product, deadline_sec=deadline_sec)

    def working_orders(self, symbol=None):
        """Our resting orders, or **None** for unknown.

        Scoped by tag where the build carried one through; otherwise
        every resting order on the account is ours to the extent that
        the account is dedicated, and `scope` says which it was so the
        sweep can be honest about what it swept.
        """
        if not self.connected or self._client is None:
            return None
        call = (getattr(self._client, 'get_order_book', None)
                or getattr(self._client, 'get_orders', None))
        if call is None:
            return None
        try:
            book = call()
        except Exception as error:                      # noqa: BLE001
            self.last_error = arrow_errors.refusal(error, 'Order book')
            return None
        if book is None:
            return None
        out = []
        for row in book:
            if not isinstance(row, dict):
                continue
            status = _status(instr.field(row, 'status', 'orderStatus'))
            if status not in ('PENDING', 'OPEN', 'PARTIAL'):
                continue
            name = str(instr.field(row, 'tradingSymbol', 'symbol') or '')
            if symbol and name.upper() != str(symbol).upper():
                continue
            price = self._to_rupees(
                _number(instr.field(row, 'price', 'orderPrice')),
                self.order_scale)
            out.append({
                'ticket': str(instr.field(row, 'orderNo', 'order_id',
                                          'nestOrderNumber', 'id') or ''),
                'symbol': name,
                'volume': _number(instr.field(row, 'quantity', 'qty',
                                              'pendingQty')) or 0.0,
                'price': price,
                'tag': instr.field(row, 'tag', 'remarks', 'user_remarks'),
                'status': status,
            })
        return out

    def order_log(self, hours=24):
        """Recent activity, ours and the trader's own. None for unknown."""
        call = (getattr(self._client, 'get_trade_book', None)
                or getattr(self._client, 'get_order_book', None))
        if call is None or not self.connected:
            return None
        try:
            rows = call()
        except Exception as error:                      # noqa: BLE001
            self.last_error = arrow_errors.refusal(error, 'Trade book')
            return None
        if rows is None:
            return None
        return [row for row in rows if isinstance(row, dict)]

    # -- the account ------------------------------------------------------

    def account_info(self):
        """Funds and margin, in rupees. None where it cannot be read.

        SPAN and exposure are reported separately where the broker
        gives them, because a calendar spread's margin BENEFIT is the
        main economic reason to trade it as a spread, and it cannot be
        shown from a single total.
        """
        if not self.connected or self._client is None:
            return None
        try:
            raw = self._client.get_user_limits()
        except Exception as error:                      # noqa: BLE001
            self.last_error = arrow_errors.refusal(error, 'Funds')
            return None
        if isinstance(raw, list):
            raw = raw[0] if raw else None
        if not isinstance(raw, dict):
            return None

        def money(*names):
            return _number(instr.field(raw, *names))

        return {
            'account': self.account.name,
            'login': self.account.user_id,
            'server': 'Arrow',
            'currency': 'INR',
            'balance': money('cash', 'cashBalance', 'openingBalance'),
            'equity': money('equity', 'netWorth', 'net', 'balance'),
            'margin': money('marginUsed', 'usedMargin', 'utilizedMargin',
                            'marginUtilized', 'utilisedMargin'),
            'margin_free': money('availableMargin', 'availablecash',
                                 'cashAvailable', 'marginAvailable',
                                 'availableBalance'),
            'span': money('spanMargin', 'span'),
            'exposure': money('exposureMargin', 'exposure'),
            'profit': money('unrealizedMtm', 'mtm', 'pnl'),
            # There is ONE account, so there is one margin pool. The
            # 'weaker of two brokers' reading that MT5-Trader shows does
            # not apply and must not be carried across.
            'single_pool': True,
            'dedicated': self.dedicated,
        }

    def margin_for(self, legs):
        """SPAN + exposure for a basket, as the BROKER computes it.

        `legs` is a list of (symbol, side, units) — passed TOGETHER, so
        a calendar spread's margin benefit is included. Asked rather
        than derived: margin depends on the exchange's own SPAN file
        and a number computed here would be a guess presented as a
        figure.

        None where the SDK exposes no calculator. The screen then shows
        an em dash and the take-profit target is disabled, rather than
        being priced off notional — which is not what margin is.
        """
        if not self.connected or self._client is None:
            return None
        call = None
        for name in ('get_margin', 'calculate_margin', 'order_margin',
                     'span_calculator', 'get_span_margin'):
            call = getattr(self._client, name, None)
            if call is not None:
                break
        if call is None:
            return None
        basket = []
        for symbol, side, units in legs:
            contract = self.contract(symbol)
            if contract is None:
                return None
            exchange = self._exchange(contract.segment)
            if exchange is None:
                return None
            basket.append({'exchange': exchange, 'symbol': symbol,
                           'quantity': int(units),
                           'transaction_type': self._enum(
                               'TransactionType',
                               'BUY' if OrderSide(side) is OrderSide.BUY
                               else 'SELL')})
        try:
            raw = call(basket)
        except Exception as error:                      # noqa: BLE001
            self.last_error = arrow_errors.refusal(error, 'Margin')
            return None
        if isinstance(raw, list):
            raw = raw[0] if raw else None
        if not isinstance(raw, dict):
            return _number(raw)
        return _number(instr.field(raw, 'totalMargin', 'total', 'margin',
                                   'marginRequired', 'span'))

    def server_time_offset_sec(self):
        """Seconds the EXCHANGE's clock runs ahead of ours, or None.

        The session cutoff, the DAY cancel and the tender warnings all
        run on the exchange's day, not this machine's. A box in one
        time zone and an exchange in another is the normal case.

        **None where it cannot be established, and None means the
        cutoff does not fire.** A guess here would be worse than an
        honest blank — see MT5-Trader, same rule.
        """
        if not self.connected or self._client is None:
            return None
        for name in ('get_server_time', 'server_time', 'get_time'):
            call = getattr(self._client, name, None)
            if call is None:
                continue
            try:
                raw = call()
            except Exception:                           # noqa: BLE001
                continue
            stamp = _number(raw if not isinstance(raw, dict)
                            else instr.field(raw, 'time', 'serverTime',
                                             'timestamp'))
            if stamp:
                if stamp > 10 ** 11:
                    stamp /= 1000.0
                return stamp - self.clock()
        return None

    def terminal_report(self):
        """Is the session there, and can it trade? The Exchanges page."""
        return {
            'account': self.account.name,
            'library': arrow is not None,
            'terminal': self.connected,
            'connected': self.connected,
            'master_rows': self.master.rows if self.master else None,
            'master_segments': (sorted(self.master.exch_segs)
                                if self.master else []),
            'dedicated': self.dedicated,
            'error': self.last_error,
        }


# -- module helpers ----------------------------------------------------------

#: Broker status strings -> our lifecycle. Anything unmapped but
#: non-empty is still WORKING (a status we do not recognise is not a
#: finished order); empty is UNKNOWN.
_STATUS = {
    'COMPLETE': 'COMPLETE', 'COMPLETED': 'COMPLETE', 'FILLED': 'COMPLETE',
    'EXECUTED': 'COMPLETE', 'TRADED': 'COMPLETE',
    'REJECTED': 'REJECTED', 'REJECT': 'REJECTED',
    'CANCELLED': 'CANCELLED', 'CANCELED': 'CANCELLED',
    'PARTIALLY FILLED': 'PARTIAL', 'PARTIAL': 'PARTIAL',
    'OPEN': 'OPEN',
    'VALIDATION PENDING': 'PENDING', 'PUT ORDER REQ RECEIVED': 'PENDING',
    'MODIFY VALIDATION PENDING': 'PENDING', 'TRIGGER PENDING': 'PENDING',
    'TRIGGER_PENDING': 'PENDING', 'OPEN PENDING': 'PENDING',
    'PENDING': 'PENDING', 'AFTER MARKET ORDER REQ RECEIVED': 'PENDING',
}


def _status(raw):
    text = str(raw or '').upper().strip()
    if not text:
        return 'UNKNOWN'
    return _STATUS.get(text, 'OPEN')


def _number(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _latest(raw):
    """An order's history is a list of states, newest last."""
    if isinstance(raw, list):
        rows = [row for row in raw if isinstance(row, dict)]
        return rows[-1] if rows else None
    return raw if isinstance(raw, dict) else None


def _as_dict(tick):
    """A stream tick as a dict, whether the SDK sends an object or one."""
    if isinstance(tick, dict):
        return tick
    return {name: getattr(tick, name)
            for name in dir(tick)
            if not name.startswith('_') and not callable(getattr(tick, name))}
