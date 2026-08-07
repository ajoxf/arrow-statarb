"""Exit ladder — levels in RUPEES, frozen at entry from actual fills.

Ported from the W3 basis system. z is for ENTRIES; exits act on money —
post-entry the rolling z is a drifting statistic and never the thing that
pays or stops you.

Priority each tick (risk first):
  1. MANUAL_STOP    — operator's own stop spread (manual trades only).
  1a DOLLAR_STOP    — ungated, GROSS P&L, straight to market. Tighter of
                      per-lot ₹, %-of-capital and TP/RR.
  1b MANUAL_TARGET  — operator's own take-profit spread.
  2. TAKE_PROFIT    — ungated, NET money (BE + target). Precedence:
                      sigma-fraction > %-of-capital > fixed ₹, cost-floored.
  3. REVERSION_EXIT — gated: spread 'home' AND net ≥ floor. The gate DEFERS
                      to max-hold: past 1× it decays to break-even, past 2×
                      it releases entirely (reversion edge spent). Fail-open
                      if P&L can't be priced.
  4. MAX_HOLD       — after N× half-life, exit only if net > 0; suppressed
                      while z-progress ≥ 50% toward home ONLY when a TP exists.
  5. TIME_STOP      — hard clock (× max-hold and/or fixed minutes), any P&L —
                      the sideways loser's only exit.
  6. Z_STOP         — demoted: fires only when explicitly enabled OR no ₹ stop
                      is armed (a trade must always have a stop). Otherwise the
                      would-have-fired occasion is logged for scoring.

Decoupled from the reference's config object: the constructor takes plain
dicts, and ``build_plan`` takes the round-trip cost as input so the caller's
Indian cost stack (core/costs.py) supplies it.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# Spread direction. SELL_BASIS = short the spread (basis rich, z>0), profits
# when the spread FALLS; BUY_BASIS = long the spread (z<0), profits when it
# RISES. Arrow's engine names the same two LONG_SPREAD / SHORT_SPREAD.
SELL_BASIS = "SELL_BASIS"
BUY_BASIS = "BUY_BASIS"
_ALIAS = {"SHORT_SPREAD": SELL_BASIS, "LONG_SPREAD": BUY_BASIS,
          SELL_BASIS: SELL_BASIS, BUY_BASIS: BUY_BASIS}


def _dir(direction):
    return _ALIAS.get(str(direction).upper(), BUY_BASIS)


def overnight_exit(mode, net_pnl, now, close_hour, close_minute):
    """Overnight handling for manual trades: ALLOW keeps the position,
    EXIT_IF_PROFIT flattens only in profit, EXIT_ALWAYS flattens regardless —
    all at the session cutoff."""
    if not mode or mode == "ALLOW":
        return None
    cutoff = now.replace(hour=int(close_hour), minute=int(close_minute),
                         second=0, microsecond=0)
    if now < cutoff:
        return None
    if mode == "EXIT_ALWAYS":
        return "OVERNIGHT_CLOSE"
    if mode == "EXIT_IF_PROFIT" and net_pnl is not None and net_pnl > 0:
        return "OVERNIGHT_CLOSE"
    return None


def outcome_tag(close_reason, z_reverted):
    """Deterministic post-trade outcome label (numbers-first review)."""
    reason = (close_reason or "").upper()
    if reason in ("TAKE_PROFIT", "MANUAL_TARGET"):
        return "TARGET_HIT"
    if reason in ("OVERNIGHT_CLOSE", "MANUAL_CLOSE", "MAX_HOLD", "TIME_STOP"):
        return "TIME_EXIT"
    if reason == "REVERSION_EXIT":
        return "REVERSION_BANKED"
    if reason in ("DOLLAR_STOP", "Z_STOP", "STOP_LOSS", "MANUAL_STOP"):
        return "STOPPED_AFTER_FULL_REVERSION" if z_reverted else "STOPPED_IN_TREND"
    return reason


class ExitLadder:
    def __init__(self, exits: dict, signals: dict, target_fraction: float = 0.5):
        self.exits = exits or {}
        self.signals = signals or {}
        self.target_fraction = float(target_fraction or 0.0)
        self._z_stop_logged = set()

    def build_plan(self, lots, contract_size, entry_z, sigma, half_life_sec,
                   rt_cost, capital=None, entry_mu=None):
        """Compute the frozen exit levels (₹). ``rt_cost`` is the round-trip
        cost from the caller's cost model; ``capital`` is capital-at-risk for
        the %-of-capital forms. Returns None when the trade can never win
        (cost floor above plausible full reversion) — the entry must be blocked.
        """
        exits = self.exits
        oz = lots * contract_size

        # Take-profit precedence: sigma-fraction > %-capital > fixed ₹
        tp = None
        if exits.get("USE_SIGMA_TARGET", True) and sigma and entry_z:
            tp = self.target_fraction * abs(entry_z) * sigma * oz
        elif exits.get("TP_CAPITAL_PCT", 0) > 0 and capital:
            tp = exits["TP_CAPITAL_PCT"] / 100.0 * capital
        elif exits.get("TP_INR_PER_LOT", 0) > 0:
            tp = exits["TP_INR_PER_LOT"] * lots

        if tp is not None:
            floor = exits.get("COST_FLOOR_MULT", 1.0) * rt_cost
            tp = max(tp, floor)
            plausible = abs(entry_z or 0) * (sigma or 0) * oz
            if plausible > 0 and tp > plausible:
                logger.info("Exit plan not viable: cost floor ₹%.0f exceeds "
                            "plausible full reversion ₹%.0f — blocking entry",
                            tp, plausible)
                return None

        # Stop: the TIGHTER of every armed form
        candidates = [exits.get("STOP_INR_PER_LOT", 0) * lots]
        if exits.get("STOP_CAPITAL_PCT", 0) > 0 and capital:
            candidates.append(exits["STOP_CAPITAL_PCT"] / 100.0 * capital)
        rr = exits.get("RR", 0)
        if tp and rr > 0:
            candidates.append(tp / rr)
        armed = [c for c in candidates if c > 0]
        stop = min(armed) if armed else 0.0

        if half_life_sec:
            max_hold = exits.get("MAX_HOLD_HALF_LIVES", 4) * half_life_sec
        else:
            max_hold = exits.get("MAX_HOLD_FALLBACK_MIN", 240) * 60

        return {
            "tp_inr": tp,
            "stop_inr": stop,
            "gate_floor_inr": exits.get("GATE_FLOOR_INR", 0.0),
            "max_hold_sec": max_hold,
            "entry_z": entry_z,
            "entry_sigma": sigma,
            "entry_mu": entry_mu,
            "rt_cost_inr": rt_cost,
            "capital_at_risk": capital,
            "half_life_sec": half_life_sec,
        }

    @staticmethod
    def spread_levels(plan, entry_spread, oz, direction):
        """Translate the ₹ ladder into absolute SPREAD levels for the in-position
        card: BE, EX (gate release), TP, SL. d = -1 when profit needs the spread
        to FALL (SELL_BASIS), +1 when it needs it to rise."""
        if not oz:
            return None
        d = -1.0 if _dir(direction) == SELL_BASIS else 1.0
        fees = plan.get("rt_cost_inr", 0.0)
        sl = (entry_spread - d * plan["stop_inr"] / oz
              if plan.get("stop_inr") else None)
        tp = (entry_spread + d * (plan["tp_inr"] + fees) / oz
              if plan.get("tp_inr") else None)
        manual_tp = plan.get("manual_exit_spread")
        manual_sl = plan.get("manual_stop_spread")
        tp = ExitLadder._nearest(tp, manual_tp, entry_spread)
        sl = ExitLadder._nearest(sl, manual_sl, entry_spread)
        return {
            "entry_spread": entry_spread,
            "be": entry_spread + d * fees / oz,
            "sl": sl,
            "tp": tp,
            "ex": entry_spread + d * (plan.get("gate_floor_inr", 0) + fees) / oz,
            "favorable": "down" if d < 0 else "up",
            "manual_tp": manual_tp,
            "manual_sl": manual_sl,
        }

    @staticmethod
    def _nearest(a, b, anchor):
        if a is None:
            return b
        if b is None:
            return a
        return a if abs(a - anchor) <= abs(b - anchor) else b

    def _reversion_home(self, plan, z, spread, direction):
        """Has the spread 'come home'? EXIT_MODE: zscore (z inside band),
        spread (crossed the entry mean), or hybrid (either)."""
        cfg = self.signals
        z_home = z is not None and abs(z) <= cfg.get("EXIT_Z", 0.5)
        spread_home = False
        entry_mu = plan.get("entry_mu")
        if spread is not None and entry_mu is not None:
            if _dir(direction) == SELL_BASIS:
                spread_home = spread <= entry_mu
            else:
                spread_home = spread >= entry_mu
        mode = cfg.get("EXIT_MODE", "zscore")
        if mode == "spread":
            return spread_home
        if mode == "hybrid":
            return z_home or spread_home
        return z_home

    def evaluate(self, direction, position_id, plan, z, gross_pnl, age_sec,
                 spread=None):
        """Return an exit reason string, or None to keep holding. ``gross_pnl``
        is the mark-to-market price move; profit decisions act on NET =
        gross − round-trip cost, the dollar stop on GROSS."""
        exits = self.exits
        cfg = self.signals
        d = _dir(direction)
        max_hold = plan["max_hold_sec"]
        fees = plan.get("rt_cost_inr", 0.0)
        net_pnl = gross_pnl - fees if gross_pnl is not None else None

        # 1. Manual stop spread — outranks everything but is joined by the ₹ stop
        stop_level = plan.get("manual_stop_spread")
        if stop_level is not None and spread is not None:
            hit = (spread >= stop_level if d == SELL_BASIS else spread <= stop_level)
            if hit:
                return "MANUAL_STOP"

        # 1a. Dollar stop — ungated, gross move
        if plan["stop_inr"] and gross_pnl is not None \
                and gross_pnl <= -plan["stop_inr"]:
            return "DOLLAR_STOP"

        # 1b. Manual take-profit spread
        target = plan.get("manual_exit_spread")
        if target is not None and spread is not None:
            reached = (spread <= target if d == SELL_BASIS else spread >= target)
            if reached:
                return "MANUAL_TARGET"

        # 2. Take profit — ungated, NET money (BE + target)
        if plan["tp_inr"] and net_pnl is not None and net_pnl >= plan["tp_inr"]:
            return "TAKE_PROFIT"

        # 3. Reversion exit — gate floor decays with age (deadlock fix)
        if self._reversion_home(plan, z, spread, d):
            if net_pnl is None:
                return "REVERSION_EXIT"                 # fail-open
            if age_sec >= 2 * max_hold:
                return "REVERSION_EXIT"                 # gate released
            floor = plan["gate_floor_inr"]
            if age_sec >= max_hold:
                floor = 0.0                             # decayed to break-even
            if net_pnl >= floor:
                return "REVERSION_EXIT"

        # 4. Max hold — only walk away with a NET profit; suppressed while still
        # travelling toward an EXISTING take-profit
        if age_sec >= max_hold and net_pnl is not None and net_pnl > 0:
            suppressed = False
            entry_z = plan.get("entry_z")
            if plan["tp_inr"] and entry_z and z is not None and abs(entry_z) > 0:
                progress = 1.0 - abs(z) / abs(entry_z)
                suppressed = progress >= exits.get("MAX_HOLD_PROGRESS_SUPPRESS", 0.5)
            if not suppressed:
                return "MAX_HOLD"

        # 5. Hard time stops — ANY P&L. Two clocks: × max-hold, and fixed minutes
        time_stop_mult = exits.get("HARD_TIME_STOP_MULT", 0)
        if time_stop_mult and age_sec >= time_stop_mult * max_hold:
            return "TIME_STOP"
        hard_minutes = exits.get("HARD_MAX_HOLD_MIN", 0)
        if hard_minutes and age_sec >= hard_minutes * 60:
            return "TIME_STOP"

        # 6. z-stop — demoted to entry-ceiling duty
        if z is not None:
            adverse = ((d == SELL_BASIS and z >= cfg.get("STOP_Z", 4.5))
                       or (d == BUY_BASIS and z <= -cfg.get("STOP_Z", 4.5)))
            if adverse:
                dollar_stop_armed = plan.get("stop_inr", 0) > 0
                if exits.get("Z_STOP_EXIT_ENABLED", False) or not dollar_stop_armed:
                    return "Z_STOP"                     # fail-safe: always a stop
                if position_id not in self._z_stop_logged:
                    self._z_stop_logged.add(position_id)
                    logger.warning("Z-STOP WOULD HAVE FIRED for %s at z=%.2f "
                                   "(gross ₹%.2f, stop -₹%.0f) — disabled while "
                                   "the ₹ stop is armed; logged for scoring",
                                   position_id, z, gross_pnl or 0, plan["stop_inr"])
        return None
