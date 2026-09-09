"""The spread definition, ported and pinned.

`spread = P_B - beta x P_A`, from the MID OF THE BOOK, and a level read
on the EXECUTABLE side for its own direction.
"""

import pytest

from arrowtrader.models import SpreadSide
from arrowtrader.spread import (LevelSigma, QuoteAgeTracker, SpreadJumpTracker,
                                closing_prices, compute_spread,
                                executable_spread, stale_quote)


class Pair:
    key = 'GOLD05DEC25F|GOLD05FEB26F'


def tick(bid, ask, at=1.0, last=None):
    return {'bid': bid, 'ask': ask, 'last': last if last is not None else bid,
            'time': at}


# -- the definition ----------------------------------------------------------

def test_the_spread_is_B_minus_beta_times_A_from_the_MID():
    md = compute_spread(Pair(), tick(74999, 75001), tick(75499, 75501))
    assert md['leg_a_mid'] == 75000.0
    assert md['leg_b_mid'] == 75500.0
    assert md['spread'] == 500.0
    assert md['formula'] == 'spread = B - 1 x A'


def test_the_mid_is_NEVER_the_last_trade():
    """A print above the offer puts the 'mid' above the long spread —
    above the best price anyone can buy it at — and `short <= mid <=
    long` stops being true exactly when it is being relied on."""
    md = compute_spread(Pair(), tick(74999, 75001, last=99999),
                        tick(75499, 75501, last=99999))
    assert md['spread'] == 500.0
    assert md['short_spread'] <= md['spread'] <= md['long_spread']


def test_the_executable_touches_are_the_two_crossings():
    md = compute_spread(Pair(), tick(100, 101), tick(200, 202))
    assert md['short_spread'] == 200 - 101      # sell B's bid, lift A's ask
    assert md['long_spread'] == 202 - 100       # lift B's ask, hit A's bid
    assert md['spread_cost'] == 3.0             # one round turn of both


def test_beta_scales_leg_A_on_every_touch():
    md = compute_spread(Pair(), tick(100, 102), tick(500, 504),
                        hedge_ratio=2.0)
    assert md['spread'] == 502 - 2 * 101
    assert md['short_spread'] == 500 - 2 * 102
    assert md['long_spread'] == 504 - 2 * 100


# -- reading the right side --------------------------------------------------

def test_a_position_reads_the_OPPOSITE_side_to_close():
    """Reading the favourable side at both ends is worse than using
    the mid: every trade then looks like it cleared its costs."""
    md = compute_spread(Pair(), tick(100, 101), tick(200, 202))
    assert executable_spread(md, SpreadSide.BUY) == md['long_spread']
    assert executable_spread(md, SpreadSide.BUY, closing=True) \
        == md['short_spread']
    assert executable_spread(md, SpreadSide.SELL) == md['short_spread']
    assert executable_spread(md, SpreadSide.SELL, closing=True) \
        == md['long_spread']


def test_the_per_leg_close_prices_agree_with_the_spread():
    md = compute_spread(Pair(), tick(100, 101), tick(200, 202))
    for side in (SpreadSide.BUY, SpreadSide.SELL):
        a, b = closing_prices(md, side)
        assert b - a == pytest.approx(executable_spread(md, side,
                                                        closing=True))


def test_buying_the_spread_buys_leg_B_and_sells_leg_A():
    assert SpreadSide.BUY.leg_sides()[0].value == 'SELL'    # leg A
    assert SpreadSide.BUY.leg_sides()[1].value == 'BUY'     # leg B


# -- the Arrow-specific refusal ----------------------------------------------

def test_a_leg_with_no_book_prices_NOTHING():
    full = tick(100, 101)
    assert compute_spread(Pair(), {'bid': None, 'ask': None, 'last': 100,
                                   'time': 1}, full) is None
    assert compute_spread(Pair(), full, {'bid': 100, 'ask': None,
                                         'time': 1}) is None
    # CONTROL: two books present, and it prices.
    assert compute_spread(Pair(), full, full) is not None


# -- the guards ---------------------------------------------------------------

def test_a_pair_is_only_as_good_as_its_WORSE_leg():
    # `observe` reads the clock ONCE per call, not once per leg.
    clock = iter([0.0, 1.0, 20.0])
    ages = QuoteAgeTracker(clock=lambda: next(clock))
    md = compute_spread(Pair(), tick(100, 101, at=1), tick(200, 202, at=1))
    ages.observe('k', md)                       # first sight: no opinion
    assert stale_quote(md, 15.0) is None

    md2 = compute_spread(Pair(), tick(100, 101, at=1), tick(200, 203, at=2))
    ages.observe('k', md2)                      # leg B moved, leg A did not
    md3 = compute_spread(Pair(), tick(100, 101, at=1), tick(200, 204, at=3))
    ages.observe('k', md3)
    reason = stale_quote(md3, 15.0)
    assert reason is not None and 'Leg A' in reason
    # CONTROL: the guard turned off holds no opinion at all.
    assert stale_quote(md3, 0) is None


def test_the_jump_guard_catches_ONE_leg_lagging_a_fast_move():
    """Both legs ticking hard, one a moment behind, printing a spread
    neither book is offering. In the stat-arb system that cost $20.40."""
    now = [0.0]
    jumps = SpreadJumpTracker(clock=lambda: now[0])
    calm = compute_spread(Pair(), tick(100, 101, at=1), tick(200, 202, at=1))
    assert jumps.observe('k', calm, sigma=0.5, max_sigmas=5.0,
                         settle_sec=2.0) is None
    shock = compute_spread(Pair(), tick(100, 101, at=2), tick(280, 282, at=2))
    reason = jumps.observe('k', shock, sigma=0.5, max_sigmas=5.0,
                           settle_sec=2.0)
    assert reason is not None and 'lagging' in reason
    # It stays unusable until the series has been quiet: a disturbance
    # jumps twice, out and back, and one quote of quiet is not the end.
    now[0] = 1.0
    assert jumps.observe('k', shock, 0.5, 5.0, 2.0) is not None
    now[0] = 3.0
    assert jumps.observe('k', shock, 0.5, 5.0, 2.0) is None
    # CONTROL: the guard turned off never fires.
    fresh = SpreadJumpTracker(clock=lambda: 0.0)
    fresh.observe('k', calm, 0.5, 0, 2.0)
    assert fresh.observe('k', shock, 0.5, 0, 2.0) is None


def test_sigma_is_none_until_it_is_measured():
    """Unmeasured is not zero: a sigma of 0 would make every move an
    infinite number of sigmas and withhold every order."""
    sigma = LevelSigma(window=600)
    for n in range(10):
        sigma.observe({'quote_id': str(n), 'spread': float(n)})
    assert sigma.sigma is None
    for n in range(10, 40):
        sigma.observe({'quote_id': str(n), 'spread': float(n)})
    assert sigma.sigma > 0


def test_sigma_counts_quote_EVENTS_not_poll_iterations():
    sigma = LevelSigma()
    for _ in range(50):
        sigma.observe({'quote_id': 'same', 'spread': 1.0})
    assert sigma.samples == 1
