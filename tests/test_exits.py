"""Exit ladder (ported from the W3 basis system, INR) — priority + gating."""

from datetime import datetime

from arrow_statarb.core.exits import (
    ExitLadder, overnight_exit, outcome_tag, BUY_BASIS, SELL_BASIS,
)

SIGNALS = {"EXIT_Z": 0.5, "STOP_Z": 4.5, "EXIT_MODE": "zscore"}


def _ladder(**exit_over):
    exits = {"USE_SIGMA_TARGET": True, "COST_FLOOR_MULT": 1.2,
             "STOP_INR_PER_LOT": 10.0, "RR": 0, "GATE_FLOOR_INR": 0.0,
             "MAX_HOLD_HALF_LIVES": 4, "MAX_HOLD_FALLBACK_MIN": 240,
             "HARD_TIME_STOP_MULT": 3, "HARD_MAX_HOLD_MIN": 0,
             "MAX_HOLD_PROGRESS_SUPPRESS": 0.5, "Z_STOP_EXIT_ENABLED": False}
    exits.update(exit_over)
    return ExitLadder(exits, SIGNALS, target_fraction=0.5)


def _plan(lad, **over):
    p = lad.build_plan(lots=1, contract_size=1, entry_z=3.0, sigma=1.0,
                       half_life_sec=100.0, rt_cost=1.0, capital=None,
                       entry_mu=None)
    p.update(over)
    return p


def test_build_plan_tp_precedence_and_stop():
    lad = _ladder()
    p = _plan(lad)
    assert p["tp_inr"] == 1.5              # sigma-frac 0.5*3*1*1, above cost floor 1.2
    assert p["stop_inr"] == 10.0          # STOP_INR_PER_LOT
    assert p["max_hold_sec"] == 400.0     # 4 * half_life 100


def test_build_plan_blocks_unviable_entry():
    # cost floor above plausible full reversion → None (entry must be blocked)
    lad = _ladder(COST_FLOOR_MULT=1.2)
    p = lad.build_plan(lots=1, contract_size=1, entry_z=3.0, sigma=1.0,
                       half_life_sec=100.0, rt_cost=10.0)   # floor 12 > plausible 3
    assert p is None


def test_dollar_stop_is_gross_and_first():
    lad = _ladder()
    p = _plan(lad)
    # gross -10 ≤ -stop 10 → DOLLAR_STOP (even though z would be 'home')
    assert lad.evaluate(BUY_BASIS, "pid", p, z=0.0, gross_pnl=-10.0,
                        age_sec=1.0) == "DOLLAR_STOP"


def test_take_profit_is_net_of_cost():
    lad = _ladder()
    p = _plan(lad)                        # tp 1.5, fees 1.0
    # gross 3 → net 2 ≥ 1.5 → TAKE_PROFIT
    assert lad.evaluate(BUY_BASIS, "pid", p, z=2.0, gross_pnl=3.0,
                        age_sec=1.0) == "TAKE_PROFIT"
    # gross 2 → net 1 < 1.5, z not home → keep holding
    assert lad.evaluate(BUY_BASIS, "pid", p, z=2.0, gross_pnl=2.0,
                        age_sec=1.0) is None


def test_reversion_gate_and_age_decay():
    lad = _ladder(GATE_FLOOR_INR=100.0)
    p = _plan(lad)                        # max_hold 400
    # z home, small net below gate, young → HOLD
    assert lad.evaluate(BUY_BASIS, "pid", p, z=0.1, gross_pnl=1.5,
                        age_sec=10.0) is None
    # past 1× max-hold → floor decays to break-even (net ≥ 0) → fires
    assert lad.evaluate(BUY_BASIS, "pid", p, z=0.1, gross_pnl=1.5,
                        age_sec=401.0) == "REVERSION_EXIT"
    # past 2× → released regardless of net
    assert lad.evaluate(BUY_BASIS, "pid", p, z=0.1, gross_pnl=-5.0,
                        age_sec=801.0) == "REVERSION_EXIT"


def test_max_hold_only_in_profit_and_time_stop_any_pnl():
    lad = _ladder(GATE_FLOOR_INR=0.0)
    p = _plan(lad)                        # max_hold 400, tp 1.5
    # losing & past max-hold, z NOT home → no max-hold (needs net>0); but time
    # stop (3× max-hold) catches it regardless of P&L
    assert lad.evaluate(BUY_BASIS, "pid", p, z=3.0, gross_pnl=-5.0,
                        age_sec=1300.0) == "TIME_STOP"
    # net-positive but below TP, z far from home (no progress) → MAX_HOLD
    # gross 2 → net 1 (< tp 1.5); progress 1-3/3=0 < 0.5 → not suppressed
    assert lad.evaluate(BUY_BASIS, "pid", p, z=3.0, gross_pnl=2.0,
                        age_sec=401.0) == "MAX_HOLD"


def test_z_stop_demoted_when_dollar_stop_armed():
    lad = _ladder()
    p = _plan(lad)                        # stop_inr 10 armed
    # adverse z beyond STOP_Z, but ₹ stop armed and z-stop disabled → HOLD
    assert lad.evaluate(BUY_BASIS, "pid", p, z=-5.0, gross_pnl=-2.0,
                        age_sec=1.0) is None
    # no ₹ stop armed → fail-safe z-stop fires
    p2 = _plan(lad, stop_inr=0.0)
    assert lad.evaluate(BUY_BASIS, "pid2", p2, z=-5.0, gross_pnl=-2.0,
                        age_sec=1.0) == "Z_STOP"


def test_spread_levels_translation_both_directions():
    lad = _ladder()
    p = _plan(lad)                        # tp 1.5, stop 10, fees 1, oz 1
    sell = ExitLadder.spread_levels(p, entry_spread=100.0, oz=1, direction=SELL_BASIS)
    assert sell["tp"] == 97.5 and sell["sl"] == 110.0 and sell["be"] == 99.0
    buy = ExitLadder.spread_levels(p, entry_spread=100.0, oz=1, direction=BUY_BASIS)
    assert buy["tp"] == 102.5 and buy["sl"] == 90.0 and buy["be"] == 101.0


def test_manual_stop_and_target_spread():
    lad = _ladder()
    p = _plan(lad, manual_stop_spread=90.0, manual_exit_spread=105.0)
    # BUY_BASIS: spread ≤ 90 → manual stop
    assert lad.evaluate(BUY_BASIS, "pid", p, z=1.0, gross_pnl=0.0,
                        age_sec=1.0, spread=89.0) == "MANUAL_STOP"
    # spread ≥ 105 → manual target
    assert lad.evaluate(BUY_BASIS, "pid", p, z=1.0, gross_pnl=0.0,
                        age_sec=1.0, spread=106.0) == "MANUAL_TARGET"


def test_overnight_and_outcome_tag():
    now = datetime(2026, 8, 7, 17, 0)
    assert overnight_exit("EXIT_ALWAYS", -5.0, now, 16, 55) == "OVERNIGHT_CLOSE"
    assert overnight_exit("EXIT_IF_PROFIT", -5.0, now, 16, 55) is None
    assert overnight_exit("ALLOW", 5.0, now, 16, 55) is None
    assert outcome_tag("TAKE_PROFIT", False) == "TARGET_HIT"
    assert outcome_tag("DOLLAR_STOP", True) == "STOPPED_AFTER_FULL_REVERSION"
    assert outcome_tag("DOLLAR_STOP", False) == "STOPPED_IN_TREND"
