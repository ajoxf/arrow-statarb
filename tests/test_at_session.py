"""The session clock — IST, MCX's moving close, and physical delivery.

The discipline is MT5-Trader's: measured, never configured, and
unmeasured is not zero. The numbers are all different.
"""

import datetime

import pytest

from arrowtrader import session as S
from arrowtrader.models import OvernightMode, TimeInForce, SyntheticOrder


class Config:
    def __init__(self, **settings):
        self.settings = dict(settings)

    def get(self, name, default=None):
        return self.settings.get(name, default)


def clock_at(*args):
    return lambda: datetime.datetime(*args, tzinfo=datetime.timezone.utc)


# -- unmeasured is not zero ---------------------------------------------------

def test_with_NO_measured_offset_the_cutoff_does_not_fire():
    """A session rule on the wrong clock is worse than one waiting for
    the right one."""
    clock = S.SessionClock(Config(), now=clock_at(2026, 1, 5, 20, 0),
                           offset=lambda: None)
    assert clock.exchange_now() is None
    assert clock.due('pair', 'mcx_fo') is False
    assert 'will NOT fire' in clock.describe('mcx_fo')['note']
    # CONTROL: measured, and it fires.
    measured = S.SessionClock(Config(), now=clock_at(2026, 1, 5, 18, 5),
                              offset=lambda: 0)
    assert measured.due('pair', 'mcx_fo') is True


def test_the_offset_is_reported_as_hours_from_THIS_machine():
    clock = S.SessionClock(Config(), now=clock_at(2026, 1, 5, 12, 0),
                           offset=lambda: 3600.0)
    described = clock.describe('mcx_fo')
    assert described['offset_sec'] == 3600.0
    assert '+1.0h' in described['note']


# -- one cutoff will not do ---------------------------------------------------

def test_MCX_and_NSE_close_at_different_times():
    """A single OVERNIGHT_CLOSE_HOUR works for a CFD account with one
    rolling session. It does not work here."""
    mcx, _ = S.session_for('mcx_fo')
    nse, _ = S.session_for('nse_fo')
    assert mcx.close_hm == (23, 30)
    assert nse.close_hm == (15, 30)


def test_the_same_moment_is_past_one_close_and_not_the_other():
    # 18:05 UTC = 23:35 IST — past MCX's 23:30, long past NSE's 15:30.
    clock = S.SessionClock(Config(), now=clock_at(2026, 1, 5, 18, 5),
                           offset=lambda: 0)
    assert clock.due('mcx-pair', 'mcx_fo') is True
    assert clock.due('nse-pair', 'nse_fo') is True
    # 12:00 UTC = 17:30 IST — past NSE's close, not MCX's.
    earlier = S.SessionClock(Config(), now=clock_at(2026, 1, 5, 12, 0),
                             offset=lambda: 0)
    assert earlier.due('nse-pair', 'nse_fo') is True
    assert earlier.due('mcx-pair', 'mcx_fo') is False


def test_a_DEFAULT_close_says_it_is_a_default():
    """MCX's evening close moves with US daylight saving, so a
    hardcoded time is wrong twice a year and the operator should know
    before it matters."""
    clock = S.SessionClock(Config(), now=clock_at(2026, 1, 5, 12, 0),
                           offset=lambda: 0)
    described = clock.describe('mcx_fo')
    assert described['cutoff_source'] == 'default'
    assert 'daylight saving' in described['note']


def test_a_CONFIGURED_close_wins_and_says_so():
    clock = S.SessionClock(
        Config(SESSIONS={'mcx_fo': {'open': (9, 0), 'close': (23, 55)}}),
        now=clock_at(2026, 1, 5, 12, 0), offset=lambda: 0)
    assert clock.cutoff_for('mcx_fo')[:2] == (23, 55)
    assert clock.describe('mcx_fo')['cutoff_source'] == 'configured'
    assert 'daylight saving' not in clock.describe('mcx_fo')['note']


def test_an_unknown_segment_falls_back_to_the_desk_default():
    clock = S.SessionClock(Config(SESSION_CLOSE_HOUR=20,
                                  SESSION_CLOSE_MINUTE=15),
                           now=clock_at(2026, 1, 5, 12, 0), offset=lambda: 0)
    assert clock.cutoff_for('ncdex_fo') == (20, 15, 'desk default')


# -- fired once a day, on the EXCHANGE's date ---------------------------------

def test_the_cutoff_fires_once_per_exchange_day():
    """A rule re-firing every poll would cancel an order the trader
    deliberately placed a minute later."""
    now = [datetime.datetime(2026, 1, 5, 18, 5,
                             tzinfo=datetime.timezone.utc)]
    clock = S.SessionClock(Config(), now=lambda: now[0], offset=lambda: 0)
    assert clock.due('pair', 'mcx_fo') is True
    clock.mark('pair')
    assert clock.due('pair', 'mcx_fo') is False
    # The next exchange day it is due again.
    now[0] += datetime.timedelta(days=1)
    assert clock.due('pair', 'mcx_fo') is True


# -- the 30-minute window MT5 cannot have -------------------------------------

def test_a_cutoff_stepped_OVER_is_reported_not_silently_lost():
    """MT5-Trader's cutoff is 16:55, so the window to midnight is seven
    hours and a poll cannot step over it. MCX closes at 23:30 — the
    window is THIRTY MINUTES, and a restarting engine walks straight
    over it, after which past_cutoff compares against today's 23:30
    which is still hours away."""
    now = [datetime.datetime(2026, 1, 5, 12, 0,
                             tzinfo=datetime.timezone.utc)]     # 17:30 IST
    clock = S.SessionClock(Config(), now=lambda: now[0], offset=lambda: 0)
    clock.seen('pair')
    assert clock.missed('pair', 'mcx_fo') is None
    # ...the engine is down over 23:30 and comes back at 00:30 IST.
    now[0] = datetime.datetime(2026, 1, 6, 19, 0,
                               tzinfo=datetime.timezone.utc)
    warning = clock.missed('pair', 'mcx_fo')
    assert warning is not None
    assert 'was not applied' in warning
    assert 'DAY orders were not cancelled' in warning
    # It reports rather than acting: a close into a shut market only
    # fails, and the failure would be the first anyone heard of it.
    assert 'the market is shut' in warning


def test_a_ladder_this_process_has_never_seen_has_missed_NOTHING():
    """It must not claim it did on the first poll after a restart."""
    clock = S.SessionClock(Config(), now=clock_at(2026, 1, 6, 19, 0),
                           offset=lambda: 0)
    assert clock.missed('brand-new', 'mcx_fo') is None


def test_missed_is_silent_while_the_cutoff_HAS_been_applied():
    now = [datetime.datetime(2026, 1, 5, 18, 5,
                             tzinfo=datetime.timezone.utc)]
    clock = S.SessionClock(Config(), now=lambda: now[0], offset=lambda: 0)
    clock.mark('pair')
    now[0] = datetime.datetime(2026, 1, 5, 19, 0,
                               tzinfo=datetime.timezone.utc)
    assert clock.missed('pair', 'mcx_fo') is None


# -- the overnight rule --------------------------------------------------------

def test_ALLOW_never_closes():
    now = datetime.datetime(2026, 1, 5, 23, 45, tzinfo=S.IST)
    assert S.overnight_action(OvernightMode.ALLOW, -100.0, now, 23, 30) is None


def test_EXIT_ALWAYS_closes_regardless_of_pnl():
    now = datetime.datetime(2026, 1, 5, 23, 45, tzinfo=S.IST)
    assert S.overnight_action('EXIT_ALWAYS', -500.0, now, 23, 30) \
        == 'OVERNIGHT_CLOSE'
    # CONTROL: before the cutoff it does nothing.
    early = datetime.datetime(2026, 1, 5, 17, 0, tzinfo=S.IST)
    assert S.overnight_action('EXIT_ALWAYS', -500.0, early, 23, 30) is None


def test_UNMEASURED_pnl_is_not_a_profit():
    """A position whose mark could not be taken is left alone rather
    than flattened on a number nobody has."""
    now = datetime.datetime(2026, 1, 5, 23, 45, tzinfo=S.IST)
    assert S.overnight_action('EXIT_IF_PROFIT', None, now, 23, 30) is None
    assert S.overnight_action('EXIT_IF_PROFIT', 0.0, now, 23, 30) is None
    assert S.overnight_action('EXIT_IF_PROFIT', 1.0, now, 23, 30) \
        == 'OVERNIGHT_CLOSE'


def test_only_DAY_orders_are_cancelled_at_the_cutoff():
    day = SyntheticOrder('k', 'BUY', 1.0, 1, 'LIMIT', 'DAY')
    gtc = SyntheticOrder('k', 'BUY', 1.0, 1, 'LIMIT', 'GTC')
    assert S.day_orders([day, gtc]) == [day]


def test_the_GTC_caveat_says_what_is_TRUE_HERE():
    """MT5-Trader's caveat is that no working order survives the
    process. A leg order does survive here — the SPREAD does not."""
    said = S.gtc_caveat()
    assert 'rests at the exchange' in said
    assert 'outright, not a hedge' in said


# -- delivery ------------------------------------------------------------------

class Contract:
    trading_symbol = 'GOLD05DEC25F'

    def __init__(self, days):
        self._days = days

    def days_to_expiry(self, today=None):
        return self._days


def test_tender_warns_before_delivery():
    """MCX futures are settled by DELIVERY. A position carried in can
    be assigned, which involves a warehouse."""
    state = S.tender_state(Contract(3), {'TENDER_WARN_DAYS': 7})
    assert state['warn'] is True
    assert 'DELIVERY' in state['note']
    # CONTROL: far from expiry there is nothing to say.
    assert S.tender_state(Contract(60), {'TENDER_WARN_DAYS': 7})['warn'] is False


def test_tender_can_withhold_an_OPEN_but_the_flag_is_off_by_default():
    inside = {'TENDER_WARN_DAYS': 7, 'REFUSE_OPEN_IN_TENDER': True}
    assert S.tender_state(Contract(3), inside)['refuse'] is True
    assert S.tender_state(Contract(3), {'TENDER_WARN_DAYS': 7})['refuse'] \
        is False


def test_an_unknown_expiry_is_not_reported_as_SAFE():
    """Unmeasured is not zero, and it is certainly not 'far away'."""
    state = S.tender_state(Contract(None))
    assert state['days_to_expiry'] is None
    assert state['refuse'] is False
    assert 'cannot be said' in state['note']


def test_an_expired_contract_says_so():
    assert 'EXPIRED' in S.tender_state(Contract(-2))['note']


# -- the cutoff that fires at US ----------------------------------------------

class Pair:
    def __init__(self, product='NRML'):
        self.product = product
        self.segment_b = 'mcx_fo'


def test_NRML_is_not_squared_off_by_anybody():
    assert S.squares_off(Pair('NRML')) is None


def test_MIS_carries_the_brokers_own_square_off_time():
    """Not a cutoff we fire — one that fires AT us."""
    found = S.squares_off(Pair('MIS'), {'MIS_SQUARE_OFF_MINUTES': 25})
    assert found['at'].endswith('IST')
    assert 'without asking' in found['note']
    assert 'half-squared-off is an outright' in found['note']
