"""The session clock — IST, MCX's moving close, and physical delivery.

Ported from MT5-Trader's `session.py`. The DISCIPLINE is kept exactly:
the cutoff runs on the EXCHANGE's clock, the offset is MEASURED rather
than configured, and **unmeasured is not zero** — with no measurement
the cutoff does not fire, and the screen says which clock it is on. A
session rule on the wrong clock is worse than one waiting for the right
one.

What changes is everything the numbers describe.

## ONE CUTOFF WILL NOT DO

MT5-Trader configures a single `OVERNIGHT_CLOSE_HOUR`. That works for a
CFD account with one rolling session. It does not work here:

- **MCX non-agri** runs from the morning into the late evening, and the
  evening close MOVES with US daylight saving, because the contracts
  track US benchmarks. Twice a year every hardcoded time is an hour
  wrong.
- **MCX agri** closes in the afternoon.
- **NSE F&O** is a single, much shorter session.

So a contract's session is read from its own schedule where it has one
and falls back to the desk-wide setting where it does not — and the
ladder shows which of the two it is using, because "the cutoff did not
fire" and "the cutoff fired an hour early" have the same cause and
different fixes.

## TWO THINGS THE MT5 VERSION HAS NO CONCEPT OF

**MIS auto square-off.** The broker flattens intraday positions near
the close, without asking and without telling us first. That is not a
cutoff we fire — it is one that fires AT us, and the only correct
responses are to show the countdown and to make sure the reconciler
reads the resulting disappearance as a close rather than as a ghost.

**Tender, and physical delivery.** MCX futures are settled by delivery.
A position carried into the tender period can be assigned, which is a
different kind of event from a losing trade: it involves a warehouse.
The ladder warns as it approaches, and `REFUSE_OPEN_IN_TENDER` can
withhold a new OPEN — never a close, because a guard never prevents a
close.

## AND ONE CAVEAT THAT CHANGED ITS MEANING

MT5-Trader's GTC caveat is that no working order survives the process,
because nothing at the broker knows what a spread is. Here a LEG order
does rest at the exchange and does survive us. What does not survive is
the SPREAD: the second leg is only crossed while this is running. The
caveat is still true and is still on the screen; it says something
different now.
"""

import datetime

from .models import OvernightMode, TimeInForce


#: Everything on this venue is reckoned in IST, which has no daylight
#: saving of its own — but MCX's evening close tracks US benchmarks and
#: therefore MOVES twice a year with THEIRS.
IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))


class Session:
    """One contract's trading hours, as the exchange publishes them."""

    def __init__(self, key, open_hm, close_hm, label='', days=(0, 1, 2, 3, 4)):
        self.key = key
        self.open_hm = open_hm            # (hour, minute) IST
        self.close_hm = close_hm
        self.label = label or key
        self.days = tuple(days)           # Monday = 0

    def closes_at(self, day):
        return datetime.datetime.combine(
            day, datetime.time(*self.close_hm, tzinfo=IST))

    def opens_at(self, day):
        return datetime.datetime.combine(
            day, datetime.time(*self.open_hm, tzinfo=IST))

    def is_trading_day(self, day):
        return day.weekday() in self.days

    def to_dict(self):
        return {'key': self.key, 'label': self.label,
                'open': '%02d:%02d' % self.open_hm,
                'close': '%02d:%02d' % self.close_hm}


#: Fallback schedules, by segment. THESE ARE DEFAULTS, NOT FACTS: the
#: exchange publishes the real thing and a holiday calendar besides, and
#: MCX's evening close moves with US daylight saving. Anything the
#: master or config carries WINS over these, and where neither exists
#: the screen says the session is a default rather than a reading.
#:
#: They exist so a fresh install is approximately right rather than
#: silently trading against midnight.
FALLBACK = {
    'mcx_fo': Session('mcx_fo', (9, 0), (23, 30), 'MCX (non-agri)'),
    'nse_fo': Session('nse_fo', (9, 15), (15, 30), 'NSE F&O'),
    'nse_cm': Session('nse_cm', (9, 15), (15, 30), 'NSE cash'),
    'bse_fo': Session('bse_fo', (9, 15), (15, 30), 'BSE F&O'),
    'bse_cm': Session('bse_cm', (9, 15), (15, 30), 'BSE cash'),
}


def session_for(segment, settings=None):
    """The session in force, and whether it was READ or assumed.

    Returns (Session, source) where source is 'configured' or
    'default'. The distinction reaches the screen: a cutoff running on
    an assumed close is a cutoff that can be an hour wrong twice a
    year, and the operator should know before it matters rather than
    after.
    """
    table = (settings or {}).get('SESSIONS') or {}
    raw = table.get(segment)
    if raw:
        return Session(segment,
                       tuple(raw.get('open', (9, 0))),
                       tuple(raw.get('close', (23, 30))),
                       raw.get('label') or segment,
                       tuple(raw.get('days', (0, 1, 2, 3, 4)))), 'configured'
    found = FALLBACK.get(segment)
    if found is not None:
        return found, 'default'
    return None, 'unknown'


def past_cutoff(now, close_hour, close_minute):
    """Is `now` at or past today's cutoff?

    `now` is the EXCHANGE-local time; the caller converts, so this
    stays a pure comparison with nothing to get wrong about clocks.
    """
    cutoff = now.replace(hour=int(close_hour), minute=int(close_minute),
                         second=0, microsecond=0)
    return now >= cutoff


def overnight_action(mode, net_pnl, now, close_hour, close_minute):
    """'OVERNIGHT_CLOSE' or None, for one position at one moment."""
    mode = OvernightMode(getattr(mode, 'value', mode) or 'ALLOW')
    if mode is OvernightMode.ALLOW:
        return None
    if not past_cutoff(now, close_hour, close_minute):
        return None
    if mode is OvernightMode.EXIT_ALWAYS:
        return 'OVERNIGHT_CLOSE'
    # EXIT_IF_PROFIT: unmeasured P&L is NOT a profit. A position whose
    # mark could not be taken is left alone rather than flattened on a
    # number nobody has.
    if net_pnl is not None and net_pnl > 0:
        return 'OVERNIGHT_CLOSE'
    return None


class SessionClock:
    """The EXCHANGE's clock, and the cutoff fired once a day per pair.

    Once, because a rule that re-fires every poll after the cutoff would
    cancel a working order the trader deliberately placed a minute later
    — and would keep trying to flatten a position whose close failed.
    """

    def __init__(self, config, now=None, offset=None):
        self.config = config
        #: OUR clock, in UTC. Converted to IST only for display and
        #: comparison, never stored naive — a naive datetime is how a
        #: time zone bug survives a test suite.
        self.now = now or (lambda: datetime.datetime.now(datetime.timezone.utc))
        #: Seconds the EXCHANGE's clock runs ahead of ours, or None.
        self.offset = offset or (lambda: None)
        self._fired = {}                # pair key -> date it last fired

    def exchange_now(self):
        """What time it is at the exchange, in IST, or None if unknown."""
        offset = self.offset()
        if offset is None:
            return None
        return (self.now()
                + datetime.timedelta(seconds=offset)).astimezone(IST)

    def cutoff_for(self, segment=None):
        """(hour, minute, source) for one segment's close."""
        found, source = session_for(segment, self.config.settings
                                    if hasattr(self.config, 'settings')
                                    else None)
        if found is not None:
            return found.close_hm[0], found.close_hm[1], source
        return (int(self.config.get('SESSION_CLOSE_HOUR', 23)),
                int(self.config.get('SESSION_CLOSE_MINUTE', 25)),
                'desk default')

    def describe(self, segment=None):
        """Which clock the cutoff runs on, for the screen."""
        offset = self.offset()
        hour, minute, source = self.cutoff_for(segment)
        cutoff = f'{hour:02d}:{minute:02d} IST'
        if offset is None:
            return {'exchange_time': None, 'offset_sec': None,
                    'cutoff': cutoff, 'cutoff_source': source,
                    'note': ('the exchange clock has not been measured yet '
                             '— the session cutoff will NOT fire until it '
                             'is, and nothing here is running on it')}
        at = self.exchange_now()
        hours = offset / 3600.0
        note = (f'exchange time, {hours:+.1f}h from this machine — the '
                f'{cutoff} cutoff runs on it')
        if source == 'default':
            note += ('. This close is a DEFAULT, not a reading: MCX\'s '
                     'evening close moves with US daylight saving, so '
                     'confirm it against the exchange.')
        return {'exchange_time': at.strftime('%H:%M:%S'),
                'offset_sec': offset, 'cutoff': cutoff,
                'cutoff_source': source, 'note': note}

    def due(self, pair_key, segment=None):
        now = self.exchange_now()
        if now is None:
            # Unmeasured is not zero: without the exchange's clock we do
            # not know whether its day has reached the cutoff.
            return False
        hour, minute, _source = self.cutoff_for(segment)
        if not past_cutoff(now, hour, minute):
            return False
        # The exchange's DATE too: a cutoff either side of midnight
        # belongs to the exchange's trading day, not to ours. MCX's
        # non-agri session runs to 23:30, so this is not hypothetical.
        return self._fired.get(pair_key) != now.date()

    def mark(self, pair_key):
        now = self.exchange_now()
        if now is not None:
            self._fired[pair_key] = now.date()

    def missed(self, pair_key, segment=None):
        """Did a cutoff go past unfired? A sentence, or None.

        THIS IS AN MCX PROBLEM AND MT5-Trader CANNOT HAVE IT. Its
        cutoff is 16:55, so the window between the cutoff and midnight
        is seven hours and a poll cannot plausibly step over the whole
        of it. MCX non-agri closes at 23:30. The window is THIRTY
        MINUTES, and an engine that is restarting, wedged, or simply
        started late walks straight over it — after which
        `past_cutoff` compares against TODAY'S 23:30, which is still
        hours away, and the DAY orders and the overnight rule for
        yesterday never fire at all. Silently.

        It is reported rather than acted on, and deliberately. Firing
        yesterday's cutoff at 00:05 would send closes into a shut
        market: they would fail, and the failure would be the first
        anyone heard of it. What a person can act on is being told
        that a session ended with the rule unapplied.
        """
        now = self.exchange_now()
        if now is None:
            return None
        hour, minute, _source = self.cutoff_for(segment)
        last = self._fired.get(pair_key)
        if last is None or last >= now.date():
            return None
        # The cutoff belonged to a day we have now left. If it had
        # fired we would have stamped that date.
        missed_on = now.date() - datetime.timedelta(days=1)
        if last >= missed_on:
            return None
        return (f'the {hour:02d}:{minute:02d} IST cutoff was not applied on '
                f'{missed_on.isoformat()} — this ladder\'s DAY orders were '
                f'not cancelled and its overnight rule did not run. Nothing '
                f'is being done about it now: the market is shut, and a '
                f'close sent into it would only fail.')

    def seen(self, pair_key):
        """Stamp the exchange day WITHOUT firing the cutoff.

        Called every poll, so `missed` has a day to compare against. A
        ladder this process has never seen cannot have missed anything,
        and must not claim it did on the first poll after a restart.
        """
        now = self.exchange_now()
        if now is not None:
            self._fired.setdefault(pair_key, now.date())


def day_orders(orders):
    """The working orders the cutoff cancels — DAY only."""
    return [order for order in orders
            if order.time_in_force is TimeInForce.DAY]


def gtc_caveat():
    """What GTC actually means here. On the screen, not only in code.

    DIFFERENT FROM MT5-Trader, and worth reading twice. There, no
    working order survived the process, because nothing at the broker
    knew what a spread was. Here a LEG order rests at the exchange and
    does survive us — but the SPREAD does not: the second leg is only
    crossed while this is running, so a resting entry that fills after
    a shutdown leaves an outright, not a hedge.
    """
    return ('GTC: the leg order rests at the exchange and outlives this '
            'process — but the SPREAD does not. The second leg is only '
            'crossed while this is running, so a fill while it is down '
            'is an outright, not a hedge. Sweep before you stop.')


def tender_state(contract, settings=None, today=None):
    """How close this contract is to delivery, and what to say about it.

    MCX futures are settled by DELIVERY. A position carried into the
    tender period can be assigned, which involves a warehouse and is
    not a thing to discover from a contract note.

    Returns None where there is nothing to say. `refuse` is only ever
    True for an OPEN — a guard never prevents a close.
    """
    if contract is None:
        return None
    days = contract.days_to_expiry(today) if hasattr(
        contract, 'days_to_expiry') else None
    if days is None:
        # Unmeasured is not zero, and it is certainly not "safe".
        return {'days_to_expiry': None, 'warn': False, 'refuse': False,
                'note': (f'{getattr(contract, "trading_symbol", "this leg")} '
                         f'has no expiry in the master, so how close it is '
                         f'to delivery cannot be said')}
    warn_days = float((settings or {}).get('TENDER_WARN_DAYS', 7) or 7)
    inside = days <= warn_days
    refuse = bool(inside and (settings or {}).get('REFUSE_OPEN_IN_TENDER'))
    note = None
    if days < 0:
        note = (f'{contract.trading_symbol} EXPIRED {abs(days)} days ago')
    elif inside:
        note = (f'{contract.trading_symbol} expires in {days} day'
                f'{"" if days == 1 else "s"} — inside the tender window. A '
                f'position carried in can be assigned for DELIVERY.')
    return {'days_to_expiry': days, 'warn': inside or days < 0,
            'refuse': refuse, 'note': note}


def squares_off(pair, settings=None):
    """When the BROKER will flatten this pair, if it will.

    MIS positions are squared off near the close, without asking. This
    is not a cutoff we fire — it is one that fires at us, and the only
    correct responses are to show the countdown and to make sure the
    reconciler reads the disappearance as a close rather than a ghost.

    None for NRML, which is the default and the reason it is.
    """
    if getattr(pair, 'product', 'NRML') != 'MIS':
        return None
    found, source = session_for(getattr(pair, 'segment_b', None), settings)
    close = found.close_hm if found else (23, 25)
    # Brokers square off BEFORE the close, not at it. The margin is
    # theirs to choose and it is not published as an API field, so it is
    # a setting — and it says so rather than pretending to be measured.
    minutes = float((settings or {}).get('MIS_SQUARE_OFF_MINUTES', 25) or 25)
    at = (datetime.datetime.combine(datetime.date.today(),
                                    datetime.time(*close, tzinfo=IST))
          - datetime.timedelta(minutes=minutes))
    return {'product': 'MIS',
            'at': at.strftime('%H:%M IST'),
            'source': source,
            'note': (f'This ladder is on MIS: the broker squares it off '
                     f'around {at.strftime("%H:%M")} IST, without asking. '
                     f'A spread half-squared-off is an outright. NRML is '
                     f'the default for that reason.')}
