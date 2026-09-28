"""Contract-aware sizing / hedging (ported from the W3 basis system, INR)."""

from arrow_statarb.core import sizing


def test_spread_units_is_contract_aware():
    # k = leg_b_lots * contract_b — the ₹-per-point. For MCX GOLDM with a
    # 100-unit contract, 1 lot gives k=100, NOT 1 (the bug that made BE-Z 4.11).
    assert sizing.spread_units(1, 100) == 100
    assert sizing.spread_units(2, 50) == 100
    assert sizing.spread_units(0, 100) == 0.0


def test_hedge_units_mode_beta_one_equal_contracts():
    # Calendar case: beta=1, equal contract sizes → hedge == leg A lots.
    assert sizing.hedge_lots(3, 100, 100, beta=1.0, step=1) == 3


def test_hedge_units_mode_inverts_with_beta():
    # beta=2 → correct hedge is HALF the leg-A units (the old L_B=L_A*beta
    # doubled it). L_B*C_B = L_A*C_A/beta = 4*100/2 = 200 → 2 lots of C_B=100.
    assert sizing.hedge_lots(4, 100, 100, beta=2.0, step=1) == 2


def test_hedge_units_mode_different_contract_sizes():
    # C_A=1000, C_B=100, beta=1 → L_B = L_A*C_A/(beta*C_B) = 1*1000/100 = 10.
    assert sizing.hedge_lots(1, 1000, 100, beta=1.0, step=1) == 10


def test_hedge_notional_mode_equal_money():
    # notional mode: L_B*C_B*P_B = L_A*C_A*P_A → equal money on both legs.
    # 1*100*200 = L_B*100*100 → L_B = 2.
    lb = sizing.hedge_lots(1, 100, 100, beta=1.0, step=1, mode="notional",
                           price_a=200.0, price_b=100.0)
    assert lb == 2


def test_hedge_rounds_down_never_overshoots():
    # target 2.9 lots with step 1 → 2 (down), so the hedge is never larger
    # than the position it hedges.
    assert sizing.hedge_lots(29, 10, 100, beta=1.0, step=1) == 2


def test_lots_for_notional_nearest_not_floor():
    # ₹20,000 / (contract 1 * price 4293) = 4.66 → nearest 5 (floor would be 4).
    assert sizing.lots_for_notional(20000, 1, 4293, step=1) == 5
    # missing price → None (must not become a zero/full order)
    assert sizing.lots_for_notional(20000, 1, 0) is None


def test_plan_lots_mode_calendar():
    p = {"HEDGE_RATIO": 1.0, "SIZING_MODE": "lots", "CLIP_LOTS": 2,
         "HEDGE_MODE": "units"}
    out = sizing.plan(p, contract_a=100, contract_b=100, price_a=5000.0,
                      price_b=5010.0, meta_a={"volume_step": 1},
                      meta_b={"volume_step": 1})
    assert out["leg_a_lots"] == 2 and out["leg_b_lots"] == 2
    assert out["spread_units"] == 200          # k = 2 * 100
    assert out["reason"] is None


def test_plan_notional_mode_and_min_floor():
    p = {"HEDGE_RATIO": 1.0, "SIZING_MODE": "notional",
         "NOTIONAL_PER_LEG_INR": 500000, "HEDGE_MODE": "units"}
    out = sizing.plan(p, contract_a=100, contract_b=100, price_a=5000.0,
                      price_b=5010.0, meta_a={"volume_step": 1, "volume_min": 1},
                      meta_b={"volume_step": 1, "volume_min": 1})
    # 500000 / (100*5000) = 1 lot
    assert out["leg_a_lots"] == 1
    assert out["target_notional_inr"] == 500000
    assert out["min_notional_inr"] is not None


def test_plan_reports_beta_gap_when_not_dollar_neutral():
    # beta 1.0 but prices differ → dollar-neutral beta = P_b/P_a, gap reported.
    p = {"HEDGE_RATIO": 1.0, "SIZING_MODE": "lots", "CLIP_LOTS": 1,
         "HEDGE_MODE": "units"}
    out = sizing.plan(p, contract_a=1, contract_b=1, price_a=100.0,
                      price_b=200.0)
    assert out["dollar_neutral_beta"] == 2.0
    assert out["beta_gap_pct"] is not None      # 1.0 vs 2.0 → -50%
