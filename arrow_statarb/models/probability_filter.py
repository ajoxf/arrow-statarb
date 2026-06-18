"""
OU-based probability filter for trade entry and hold decisions.

Blocks low-quality entries and triggers time-stops / negative-EV exits,
yielding fewer but more profitable trades.

Win probability uses the Ornstein-Uhlenbeck gambler's-ruin formula
(OU scale function), which answers: given the spread is at z₀ σ, what is
the probability that it reverts to the exit level before blowing out to
the stop-loss level?  This is mathematically exact for a normalized OU
process with unit stationary variance.

P_win = 1 - erfi(|z₀|/√2) / erfi(z_stop/√2)

where erfi is the imaginary error function.  Typical entries (z₀=2–3.5,
z_stop=4) yield P_win > 80 %, ensuring the 60 % threshold blocks only
trades that are already dangerously close to the stop.
"""

import math
from typing import Dict, Tuple

from loguru import logger

try:
    from scipy.special import erfi as _scipy_erfi

    def _erfi(x: float) -> float:
        return float(_scipy_erfi(x))

except ImportError:
    def _erfi(x: float) -> float:
        """Fallback erfi via asymptotic + series without scipy."""
        x = abs(x)
        if x == 0:
            return 0.0
        if x > 4.0:
            # Asymptotic: erfi(x) ≈ exp(x²)/(√π × x)
            return math.exp(min(x * x, 700)) / (math.sqrt(math.pi) * x)
        # Series: erfi(x) = (2/√π) Σ x^(2k+1) / (k! (2k+1))
        result, term, k = x, x, 0
        x2 = x * x
        while k < 60:
            k += 1
            term *= x2 / k
            contrib = term / (2 * k + 1)
            result += contrib
            if abs(contrib) < 1e-12 * abs(result):
                break
        return (2.0 / math.sqrt(math.pi)) * result


def _norm_cdf(x: float) -> float:
    return 0.5 * math.erfc(-x / math.sqrt(2))


class ProbabilityFilter:
    """
    Applies four quality checks before entry / during hold:

    1. Break-even z-score  — |z| must exceed the minimum spread move needed
       to cover round-trip commission + slippage.
    2. OU gambler's-ruin win probability  — P(revert to exit_z before
       hitting stop_z) ≥ min_win_probability.
    3. Expected value  — EV > min_expected_value after weighting profit
       vs stop-loss payoff.
    4. Time stop  — exit if days held > max_half_lives × half_life.
    """

    def __init__(
        self,
        *,
        commission_per_lot: float = 10.0,   # INR per lot (or per order when basis="per_order"), one way
        slippage_per_lot: float = 5.0,       # INR per lot, one way
        commission_basis: str = "per_lot",   # "per_lot" or "per_order"
        lot_multiplier: float = 100.0,        # spread σ units → INR per contract
        min_win_probability: float = 0.60,
        min_expected_value: float = 0.0,
        max_half_lives: float = 3.0,
        exit_zscore: float = 0.0,
        stop_zscore: float = 4.0,
        enabled: bool = True,
    ):
        self.commission_per_lot = commission_per_lot
        self.slippage_per_lot = slippage_per_lot
        self.commission_basis = commission_basis
        self.lot_multiplier = lot_multiplier
        self.min_win_probability = min_win_probability
        self.min_expected_value = min_expected_value
        self.max_half_lives = max_half_lives
        self.exit_zscore = exit_zscore
        self.stop_zscore = stop_zscore
        self.enabled = enabled

    # ------------------------------------------------------------------ public

    def check_entry(
        self,
        z_score: float,
        std: float,
        half_life: float,
        contracts: int = 1,
    ) -> Tuple[bool, str, Dict]:
        """
        Decide whether an ENTRY signal should be allowed.

        Returns:
            (allow, reason, metrics)
        """
        if not self.enabled:
            return True, "filter_disabled", {}

        if std <= 0:
            return False, "insufficient_data", {}

        metrics = self._compute_metrics(z_score, std, contracts)

        if abs(z_score) < metrics["breakeven_z"]:
            return False, "below_breakeven", metrics

        if metrics["win_probability"] < self.min_win_probability:
            return False, "low_win_probability", metrics

        if metrics["expected_value"] < self.min_expected_value:
            return False, "negative_ev", metrics

        return True, "all_checks_passed", metrics

    def check_hold(
        self,
        z_entry: float,
        z_current: float,
        std: float,
        half_life: float,
        days_held: float,
        contracts: int = 1,
    ) -> Tuple[bool, str, Dict]:
        """
        Decide whether an open position should be exited early.

        Returns:
            (should_exit, reason, metrics)
        """
        if not self.enabled:
            return False, "filter_disabled", {}

        if std <= 0 or half_life <= 0:
            return False, "insufficient_data", {}

        metrics = self._compute_metrics(z_current, std, contracts)
        metrics["z_entry"] = z_entry
        metrics["days_held"] = days_held

        max_days = self.max_half_lives * half_life
        metrics["max_days"] = max_days

        if days_held >= max_days:
            return True, "time_stop", metrics

        if metrics["expected_value"] < self.min_expected_value:
            return True, "negative_ev_hold", metrics

        return False, "hold", metrics

    # ----------------------------------------------------------------- private

    def _round_trip_cost(self, contracts: int) -> float:
        if self.commission_basis == "per_order":
            # Flat commission per order (entry + exit); slippage still scales with lots
            return self.commission_per_lot * 2 + self.slippage_per_lot * 2 * contracts
        return (self.commission_per_lot + self.slippage_per_lot) * 2 * contracts

    def _win_probability(self, z: float) -> float:
        """
        P(hit exit_zscore before stop_zscore | normalized OU process at |z|).

        Uses the OU scale function S(x) = √(π/2) × erfi(x/√2), giving
        P_win = 1 - S(|z|) / S(z_stop)  = 1 - erfi(|z|/√2) / erfi(z_stop/√2).

        For typical entries (z=2–3.5, stop=4): P_win = 80–99 %.
        Probability drops sharply only when z approaches z_stop.
        """
        z_abs = abs(z)
        if z_abs >= self.stop_zscore:
            return 0.0
        if z_abs <= self.exit_zscore:
            return 1.0
        try:
            sqrt2 = math.sqrt(2)
            erfi_z = _erfi(z_abs / sqrt2)
            erfi_stop = _erfi(self.stop_zscore / sqrt2)
            if erfi_stop <= 0:
                return 1.0
            return max(0.0, 1.0 - erfi_z / erfi_stop)
        except Exception:
            # Linear fallback
            span = self.stop_zscore - self.exit_zscore
            return max(0.0, (self.stop_zscore - z_abs) / span)

    def _compute_metrics(self, z: float, std: float, contracts: int) -> Dict:
        rt_cost = self._round_trip_cost(contracts)
        scale = std * self.lot_multiplier * contracts

        breakeven_z = rt_cost / scale if scale > 0 else float("inf")

        p_win = self._win_probability(z)
        p_stop = 1.0 - p_win

        profit_if_win = max(0.0, (abs(z) - self.exit_zscore) * scale)
        loss_if_stop = max(0.0, (self.stop_zscore - abs(z)) * scale)
        ev = p_win * profit_if_win - p_stop * loss_if_stop - rt_cost

        return {
            "z_score": z,
            "std": std,
            "breakeven_z": breakeven_z,
            "win_probability": p_win,
            "expected_value": ev,
            "round_trip_cost": rt_cost,
            "profit_if_win": profit_if_win,
            "loss_if_stop": loss_if_stop,
        }
