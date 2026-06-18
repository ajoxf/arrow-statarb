"""
Trading signal generation based on Z-score
"""

import time
from typing import Dict, Optional

from loguru import logger


class SignalGenerator:
    """
    Generates entry/exit signals based on Z-score thresholds.

    Signal logic:
    - ENTRY LONG spread: Z-score < -entry_threshold
    - ENTRY SHORT spread: Z-score > +entry_threshold
    - EXIT: Z-score crosses exit_threshold toward zero
    - STOP LOSS: Z-score exceeds stop_threshold
    """

    def __init__(
        self,
        entry_threshold: float = 2.0,
        exit_threshold: float = 0.0,
        stop_threshold: float = 4.0,
        cooldown_period: float = 300.0,
        confirmation_ticks: int = 3,
    ):
        self.entry_threshold = entry_threshold
        self.exit_threshold = exit_threshold
        self.stop_threshold = stop_threshold
        self.cooldown_period = cooldown_period
        self.confirmation_ticks = confirmation_ticks

        self._last_signal_time = 0.0
        self._consecutive_above = 0  # consecutive ticks above entry
        self._consecutive_below = 0  # consecutive ticks below -entry
        self._last_zscore = 0.0

    def generate_signal(
        self, zscore: float, current_position: str = "flat"
    ) -> Dict:
        """
        Generate trading signal.

        Args:
            zscore: Current Z-score
            current_position: 'long', 'short', or 'flat'

        Returns:
            Signal dict with keys: action, direction, strength, reason
        """
        now = time.time()
        self._last_zscore = zscore
        current_position = current_position.lower()  # normalise "LONG"/"SHORT"/"FLAT"

        # Track consecutive ticks for confirmation
        if zscore > self.entry_threshold:
            self._consecutive_above += 1
            self._consecutive_below = 0
        elif zscore < -self.entry_threshold:
            self._consecutive_below += 1
            self._consecutive_above = 0
        else:
            self._consecutive_above = 0
            self._consecutive_below = 0

        # Check cooldown
        in_cooldown = (now - self._last_signal_time) < self.cooldown_period

        # --- STOP LOSS ---
        if current_position == "long" and zscore < -self.stop_threshold:
            self._last_signal_time = now
            return self._make_signal("EXIT", "flat", abs(zscore), "stop_loss")

        if current_position == "short" and zscore > self.stop_threshold:
            self._last_signal_time = now
            return self._make_signal("EXIT", "flat", abs(zscore), "stop_loss")

        # --- EXIT SIGNALS ---
        if current_position == "long" and zscore >= self.exit_threshold:
            self._last_signal_time = now
            return self._make_signal("EXIT", "flat", abs(zscore), "target_reached")

        if current_position == "short" and zscore <= self.exit_threshold:
            self._last_signal_time = now
            return self._make_signal("EXIT", "flat", abs(zscore), "target_reached")

        # --- ENTRY SIGNALS ---
        if current_position == "flat" and not in_cooldown:
            # Short spread: Z-score above entry threshold
            if (
                zscore > self.entry_threshold
                and self._consecutive_above >= self.confirmation_ticks
            ):
                self._last_signal_time = now
                return self._make_signal(
                    "ENTRY", "short", min(abs(zscore), 5.0), "zscore_above_threshold"
                )

            # Long spread: Z-score below negative entry threshold
            if (
                zscore < -self.entry_threshold
                and self._consecutive_below >= self.confirmation_ticks
            ):
                self._last_signal_time = now
                return self._make_signal(
                    "ENTRY", "long", min(abs(zscore), 5.0), "zscore_below_threshold"
                )

        # No signal
        return self._make_signal("HOLD", current_position, 0.0, "no_signal")

    def _make_signal(
        self, action: str, direction: str, strength: float, reason: str
    ) -> Dict:
        """Create signal dictionary."""
        return {
            "action": action,
            "direction": direction,
            "strength": strength,
            "reason": reason,
            "zscore": self._last_zscore,
            "timestamp": time.time(),
        }

    def update_thresholds(
        self,
        entry: Optional[float] = None,
        exit_: Optional[float] = None,
        stop: Optional[float] = None,
    ):
        """Update signal thresholds at runtime."""
        if entry is not None:
            self.entry_threshold = entry
        if exit_ is not None:
            self.exit_threshold = exit_
        if stop is not None:
            self.stop_threshold = stop
        logger.info(
            f"Thresholds updated: entry={self.entry_threshold}, "
            f"exit={self.exit_threshold}, stop={self.stop_threshold}"
        )
