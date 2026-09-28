"""How many lots each leg trades — contract-aware sizing and hedging.

Ported from the W3 basis system (currency = INR here). Two modes:

    lots      CLIP_LOTS is the anchor (what the current engine does).
    notional  NOTIONAL_PER_LEG_INR is the anchor; lots follow from the live
              price — the only mode where "balanced" means anything across
              two different instruments.

## Why the hedge is not simply ``leg_a_lots * HEDGE_RATIO``

The spread is ``S = P_b - beta * P_a``. Holding L_A lots of leg A and L_B of
leg B with contract sizes C_A, C_B, matching the P&L coefficients of a price
move to ``-dS * k`` gives

    L_A * C_A = beta * L_B * C_B          (units, not lots)

so ``L_B * C_B = L_A * C_A / beta``. The old ``L_B = L_A * beta`` is correct
ONLY at beta=1 with equal contract sizes — exactly the calendar case it has
run in, which is why the MCX pair (order-lot 1, no contract multiplier) showed
break-even-Z 4.11: the ₹-per-point ``k = L_B * C_B`` was 1 instead of the real
contract size. This module makes ``k`` contract-aware (``spread_units``).
"""

from __future__ import annotations

import math


def round_step(volume, step, minimum=0.0, down=False):
    """Snap to a tradable volume. NEAREST by default (a notional target is not
    a ceiling; flooring lands up to a fifth low at small sizes). ``down=True``
    where overshooting is unsafe (the hedge)."""
    if step and step > 0:
        scaled = volume / step
        volume = (math.floor(scaled + 1e-9) if down
                  else math.floor(scaled + 0.5 + 1e-9)) * step
        volume = round(volume, 8)
    return volume if volume >= minimum - 1e-9 else 0.0


def lots_for_notional(notional_inr, contract_size, price, step=0.0, minimum=0.0):
    """Lots whose notional is as close as a tradable step allows, or None when
    it cannot be computed (a missing price must not become a zero/full order)."""
    if not notional_inr or not contract_size or not price:
        return None
    if notional_inr < 0 or contract_size <= 0 or price <= 0:
        return None
    return round_step(notional_inr / (contract_size * price), step, minimum)


def hedge_lots(leg_a_lots, contract_a, contract_b, beta, step=0.0, minimum=0.0,
               mode="units", price_a=None, price_b=None):
    """Leg B lots that hedge leg A.

    units (default): ``L_B*C_B = L_A*C_A / beta`` — equal units weighted by
        beta; the pair's P&L is exactly the spread move (right for a basis
        trade). notional: ``L_B*C_B*P_B = L_A*C_A*P_A`` — equal money (trades
        the return spread; right for two related instruments). Rounds DOWN so
        the hedge never overshoots; the executor trims leg A to the matched size.
    """
    beta = float(beta or 1.0)
    if not leg_a_lots or not contract_a or not contract_b:
        return 0.0
    if str(mode).lower() == "notional":
        if not price_a or not price_b:
            return 0.0
        target = leg_a_lots * contract_a * price_a / (contract_b * price_b)
    else:
        if beta == 0:
            return 0.0
        target = leg_a_lots * contract_a / (beta * contract_b)
    return round_step(target, step, minimum, down=True)


def spread_units(leg_b_lots, contract_b):
    """₹ per 1.00 of spread movement — ``k = L_B * C_B`` (leg B's units).
    Everything converting a spread distance into money (exit levels, slippage,
    edge, live net P&L) multiplies by this."""
    if not leg_b_lots or not contract_b:
        return 0.0
    return leg_b_lots * contract_b


def notional(lots, contract_size, price):
    if not lots or not contract_size or not price:
        return 0.0
    return lots * contract_size * price


def margin(notional_inr, leverage):
    """Capital the broker locks for that notional. Leverage is broker-side;
    the config only mirrors it. 0/None leverage → unlevered (whole amount)."""
    if not notional_inr:
        return 0.0
    if not leverage or leverage <= 0:
        return notional_inr
    return notional_inr / float(leverage)


def minimum_notional(contract_a, contract_b, price_a, price_b, beta,
                     min_a=0.0, min_b=0.0, mode="units"):
    """Smallest per-leg notional this PAIR can actually trade (usually bound by
    leg B's minimum lot), so the UI can state a target rather than reject."""
    beta = float(beta or 1.0)
    if not contract_a or not contract_b or not price_a or not price_b:
        return None
    needs = [min_a * contract_a * price_a] if min_a else []
    if min_b:
        if str(mode).lower() == "notional":
            lots_a_needed = min_b * contract_b * price_b / (contract_a * price_a)
        else:
            lots_a_needed = min_b * beta * contract_b / contract_a
        needs.append(lots_a_needed * contract_a * price_a)
    return max(needs) if needs else None


def plan(params, contract_a, contract_b, price_a, price_b,
         meta_a=None, meta_b=None, size_multiplier=1.0):
    """Resolve one entry's sizing. ``params`` is a plain dict with keys
    HEDGE_RATIO, SIZING_MODE ('lots'|'notional'), NOTIONAL_PER_LEG_INR,
    CLIP_LOTS, HEDGE_MODE ('units'|'notional'), and optional SPOT_LEVERAGE /
    FUT_LEVERAGE / LEVERAGE. ``meta_*`` carry volume_step / volume_min per leg.

    Returns a dict that is both the execution instruction (leg_a_lots,
    leg_b_lots) and the display block (notionals, margin, imbalance, k). A
    non-empty ``reason`` means the sizing could not be resolved and the caller
    must refuse the entry rather than guess a size."""
    beta = float(params.get("HEDGE_RATIO", 1.0) or 1.0)
    mode = str(params.get("SIZING_MODE", "lots") or "lots").lower()
    meta_a, meta_b = meta_a or {}, meta_b or {}
    step_a = meta_a.get("volume_step") or 0.0
    step_b = meta_b.get("volume_step") or 0.0
    min_a = meta_a.get("volume_min") or 0.0
    min_b = meta_b.get("volume_min") or 0.0

    target_notional = float(params.get("NOTIONAL_PER_LEG_INR", 0.0) or 0.0)
    reason = None

    if mode == "notional":
        lots_a = lots_for_notional(target_notional, contract_a, price_a,
                                   step_a, min_a)
        if lots_a is None:
            reason = ("notional sizing needs NOTIONAL_PER_LEG_INR, the contract "
                      "size and a live price")
            lots_a = 0.0
        elif lots_a <= 0:
            reason = (f"₹{target_notional:,.0f} per leg is below one tradable "
                      f"lot of leg A (₹{contract_a * price_a:,.0f} minimum)")
    else:
        lots_a = float(params.get("CLIP_LOTS", 1.0) or 0.0)

    hedge_mode = str(params.get("HEDGE_MODE", "units") or "units").lower()
    lots_a = round_step(lots_a * float(size_multiplier or 1.0), step_a, min_a)
    lots_b = hedge_lots(lots_a, contract_a, contract_b, beta, step_b, min_b,
                        mode=hedge_mode, price_a=price_a, price_b=price_b)
    floor = minimum_notional(contract_a, contract_b, price_a, price_b, beta,
                             min_a, min_b, mode=hedge_mode)
    if lots_a > 0 and lots_b <= 0 and not reason:
        reason = (f"the hedge for {lots_a:g} lots on leg A is under leg B's "
                  f"{min_b:g}-lot minimum"
                  + (f" — this pair needs at least ₹{floor:,.0f} per leg"
                     if floor else ""))
    elif lots_a <= 0 and mode == "notional" and floor and target_notional:
        reason = (f"₹{target_notional:,.0f} per leg is below this pair's "
                  f"minimum of ₹{floor:,.0f}")

    notional_a = notional(lots_a, contract_a, price_a)
    notional_b = notional(lots_b, contract_b, price_b)
    lev_a = params.get("SPOT_LEVERAGE") or params.get("LEVERAGE")
    lev_b = params.get("FUT_LEVERAGE") or params.get("LEVERAGE")
    margin_a, margin_b = margin(notional_a, lev_a), margin(notional_b, lev_b)
    bigger = max(notional_a, notional_b)

    step_inr = step_a * contract_a * price_a if (step_a and price_a) else None
    shortfall_pct = None
    if mode == "notional" and target_notional and notional_a:
        shortfall_pct = 100.0 * (notional_a - target_notional) / target_notional

    dollar_neutral_beta = (price_b / price_a) if (price_a and price_b) else None
    beta_gap_pct = (100.0 * (beta - dollar_neutral_beta) / dollar_neutral_beta
                    if dollar_neutral_beta else None)

    return {
        "mode": mode,
        "hedge_mode": hedge_mode,
        "dollar_neutral_beta": dollar_neutral_beta,
        "beta_gap_pct": beta_gap_pct,
        "target_notional_inr": target_notional if mode == "notional" else None,
        "hedge_ratio": beta,
        "leg_a_lots": lots_a, "leg_b_lots": lots_b,
        "leg_a_contract": contract_a, "leg_b_contract": contract_b,
        "leg_a_notional_inr": notional_a, "leg_b_notional_inr": notional_b,
        "leg_a_units": lots_a * (contract_a or 0),
        "leg_b_units": lots_b * (contract_b or 0),
        "leg_a_margin_inr": margin_a, "leg_b_margin_inr": margin_b,
        "leg_a_leverage": lev_a, "leg_b_leverage": lev_b,
        "margin_inr": margin_a + margin_b,
        "spread_units": spread_units(lots_b, contract_b),
        "imbalance_inr": notional_a - notional_b,
        "imbalance_pct": (100.0 * (notional_a - notional_b) / bigger
                          if bigger else 0.0),
        "min_notional_inr": floor,
        "lot_step_inr": step_inr,
        "notional_gap_pct": shortfall_pct,
        "reason": reason,
    }
