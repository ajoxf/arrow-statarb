"""Sizing — and the one conversion that can be wrong by 100x.

`test_units_are_lots_times_lot_size` guards the arithmetic that turns
a click into a number the exchange acts on. It may not be deleted.
"""

import pytest

from arrowtrader import sizing


class Pair:
    """The fields `clip_plan` reads. Both leg sizes are TYPED."""

    def __init__(self, beta=1.0, lots_a=1.0, lots_b=1.0):
        self.hedge_ratio = beta
        self.clip_lots_a = lots_a
        self.clip_lots_b = lots_b


#: GOLD: 100 g a lot on MCX's own numbering, freeze 1,000 units.
GOLD = {'contract_size': 100, 'freeze_qty': 1000}
#: GOLDM: a tenth of it. A different instrument, not a smaller GOLD.
GOLDM = {'contract_size': 10, 'freeze_qty': 10000}


# -- the one that may not be deleted -----------------------------------------

def test_units_are_lots_times_lot_size():
    """UNITS = LOTS x LOTSIZE, and nothing else.

    This is what goes on the wire. Arrow's `place_order` takes
    `quantity` in units; the ladder, the book, the P&L and every panel
    speak LOTS. If this conversion is wrong the first live order does
    not fail — it FILLS, at LotSize times the intended size.
    """
    assert sizing.units(1, 100) == 100
    assert sizing.units(3, 30) == 90          # 3 lots of SILVER
    assert sizing.units(10, 10) == 100        # 10 lots of GOLDM

    # Unknown lot size REFUSES. It is never 1, and never the lots.
    assert sizing.units(3, None) is None
    assert sizing.units(None, 100) is None

    # A fractional lot cannot be silently truncated into units.
    assert sizing.units(0.5, 3) is None
    # ...but a fractional lot that lands on a whole number of units is
    # still a fractional lot, and the caller has already refused it.
    assert sizing.units(0, 100) is None


def test_the_plan_carries_units_and_says_them_out_loud():
    plan = sizing.clip_plan(Pair(lots_a=1, lots_b=10), GOLD, GOLDM,
                            75000.0, 7500.0, spreads=2)
    assert plan['reason'] is None
    assert plan['leg_a_lots'] == 2 and plan['leg_b_lots'] == 20
    assert plan['leg_a_units'] == 200      # 2 lots x 100
    assert plan['leg_b_units'] == 200      # 20 lots x 10
    assert '200 units' in plan['derivation']
    assert 'Rs' in plan['derivation']


# -- unknown is not one ------------------------------------------------------

def test_a_missing_lot_size_refuses_and_names_the_leg():
    plan = sizing.clip_plan(Pair(), {'contract_size': None}, GOLDM,
                            75000.0, 7500.0, spreads=1)
    assert 'leg A has no lot size' in plan['reason']
    assert plan['leg_a_units'] is None
    # CONTROL: with the lot size present the same click is accepted.
    ok = sizing.clip_plan(Pair(), GOLD, GOLDM, 75000.0, 7500.0, spreads=1)
    assert ok['reason'] is None


def test_both_legs_missing_names_both():
    plan = sizing.clip_plan(Pair(), {}, {}, 1.0, 1.0, spreads=1)
    assert 'leg A and leg B' in plan['reason']


# -- k, the one multiplier ---------------------------------------------------

def test_k_is_leg_b_lots_times_leg_b_lot_size():
    assert sizing.spread_units(20, 10) == 200
    # Missing either input gives 0.0, which every caller renders as an
    # em dash rather than as a money figure of zero.
    assert sizing.spread_units(20, None) == 0.0
    assert sizing.spread_units(None, 10) == 0.0


def test_k_matches_the_plan():
    plan = sizing.clip_plan(Pair(lots_a=1, lots_b=10), GOLD, GOLDM,
                            75000.0, 7500.0, spreads=2)
    assert plan['spread_units'] == sizing.spread_units(plan['leg_b_lots'], 10)
    # And per ONE unit of Qty, which is what the exit levels are priced
    # per and what does not move when the keypad is touched.
    assert plan['spread_units_per_qty'] == sizing.spread_units(10, 10)


# -- whole lots ---------------------------------------------------------------

def test_lots_are_whole_and_leg_b_rounds_DOWN():
    """Short is the recoverable error on the leg that is crossed."""
    assert sizing.round_step(2.6, 1.0, 1.0) == 3.0            # nearest
    assert sizing.round_step(2.6, 1.0, 1.0, down=True) == 2.0  # leg B
    assert sizing.round_step(0.4, 1.0, 1.0, down=True) == 0.0  # refuses


def test_a_sub_lot_click_is_refused_not_rounded_to_zero_silently():
    plan = sizing.clip_plan(Pair(lots_a=1, lots_b=1), GOLD, GOLDM,
                            75000.0, 7500.0, spreads=0.5)
    assert plan['reason'] is not None
    assert 'minimum' in plan['reason']


# -- the freeze quantity is the exchange's, and it is in UNITS ---------------

def test_freeze_quantity_becomes_a_lot_cap():
    assert sizing.volume_max(GOLD) == 10        # 1,000 units / 100
    assert sizing.volume_max(GOLDM) == 1000     # 10,000 units / 10
    # Unknown is None — unbounded is not the same as zero, and neither
    # is the same as "we did not read it".
    assert sizing.volume_max({'contract_size': 100}) is None
    assert sizing.volume_max({'freeze_qty': 1000}) is None
    assert sizing.volume_max(None) is None


def test_over_the_freeze_quantity_is_refused_WITH_the_size_that_fits():
    plan = sizing.clip_plan(Pair(), GOLD, GOLDM, 75000.0, 7500.0, spreads=20)
    assert 'freeze quantity' in plan['reason']
    assert 'Qty 10 or less fits' in plan['reason']
    # CONTROL: exactly at the cap is accepted.
    assert sizing.clip_plan(Pair(), GOLD, GOLDM, 75000.0, 7500.0,
                            spreads=10)['reason'] is None


def test_the_keypad_stops_offering_a_guaranteed_refusal():
    assert sizing.max_qty(Pair(lots_a=1, lots_b=10), GOLD, GOLDM) == 10
    # Leg B binds when its own ratio is the tighter one.
    assert sizing.max_qty(Pair(lots_a=1, lots_b=200), GOLD, GOLDM) == 5
    # Nothing known about either cap: unbounded, and the keypad offers
    # everything rather than inventing a limit.
    assert sizing.max_qty(Pair(), {'contract_size': 100},
                          {'contract_size': 10}) is None


# -- the ratio, as the trader typed it ---------------------------------------

def test_the_trader_types_both_legs_and_nothing_is_derived():
    """GOLD vs GOLDM at 1:10 is the pair this control exists for."""
    plan = sizing.clip_plan(Pair(lots_a=1, lots_b=10), GOLD, GOLDM,
                            75000.0, 7500.0, spreads=1)
    assert (plan['leg_a_lots'], plan['leg_b_lots']) == (1, 10)
    # ...and it is NOT quietly matched by notional or by beta.
    assert sizing.leg_ratio(Pair(lots_a=1, lots_b=10)) == 10.0


def test_tidy_sweeps_the_binary_dust():
    assert sizing.tidy(0.15 - 0.1) == 0.05
