"""Entry-signal gates for one asset (per-asset state keyed by asset).

Ported ZSignalGenerator from the W3 basis system, decoupled: direction strings
(SELL_BASIS/BUY_BASIS) instead of the MT5 SignalType enum, and the edge filter
injected as ``edge_fn`` so Arrow's Indian cost stack supplies it.

All gates must pass: warm stats, |z| ≥ ENTRY_Z, |z| < MAX_ENTRY_Z (entry
ceiling), trend filter (never fight the tape), entry/stop cooldowns, z-reset
after a stop, and the edge filter. z earns ENTRIES; exits act on money.
"""

from __future__ import annotations

import logging

from .exits import SELL_BASIS, BUY_BASIS

logger = logging.getLogger(__name__)


def _default_edge(z, sigma, lots, contract_size, market_data):
    return True, 0.0, 0.0


class ZSignalGenerator:
    def __init__(self, signals_cfg, clock):
        self.cfg = signals_cfg
        self.clock = clock
        self.last_close_time = {}       # asset -> t
        self.last_stop_time = {}        # asset -> t
        self.blocked_direction = {}     # asset -> direction blocked until z-reset
        self._blocking = {}             # asset -> gate now blocking (log dedup)

    def notify_close(self, asset, reason, direction):
        now = self.clock()
        self.last_close_time[asset] = now
        if (reason or "").upper() in ("STOP_LOSS", "DOLLAR_STOP", "Z_STOP"):
            self.last_stop_time[asset] = now
            self.blocked_direction[asset] = direction
            logger.info("%s: %s stop — same-direction re-entry blocked until z "
                        "re-enters the exit band", asset, reason)

    def update(self, asset, z):
        """Clear the z-reset block once z returns inside the exit band."""
        if asset in self.blocked_direction and z is not None:
            if abs(z) <= self.cfg["EXIT_Z"]:
                logger.info("%s: z-reset — re-entry unblocked", asset)
                del self.blocked_direction[asset]

    def _blocked(self, asset, gate, message, *args):
        if self._blocking.get(asset) != gate:
            self._blocking[asset] = gate
            logger.info(message, *args)
        return None

    def _unblocked(self, asset):
        if self._blocking.pop(asset, None) is not None:
            logger.info("%s: entry gates clear", asset)

    def entry_signal(self, asset, stats, market_data, active_positions,
                     lots, contract_size, edge_fn=None):
        cfg = self.cfg
        edge_fn = edge_fn or _default_edge
        if active_positions or not stats.warm:
            return None

        z = stats.z
        if z is None or abs(z) < cfg["ENTRY_Z"]:
            self._blocking.pop(asset, None)     # normal resting state, not a block
            return None

        ceiling = cfg.get("MAX_ENTRY_Z", cfg["STOP_Z"])
        if abs(z) >= ceiling:
            return self._blocked(asset, "ceiling",
                                 "%s: |z|=%.2f beyond entry ceiling %.2f — a "
                                 "momentum spike, not a better entry", asset,
                                 abs(z), ceiling)

        direction = SELL_BASIS if z > 0 else BUY_BASIS

        if cfg.get("TREND_FILTER", True):
            slope = stats.trend_slope()
            if direction == SELL_BASIS and slope > 0:
                return self._blocked(asset, "trend-up", "%s: trend filter — "
                                     "spread rising, SELL_BASIS blocked", asset)
            if direction == BUY_BASIS and slope < 0:
                return self._blocked(asset, "trend-down", "%s: trend filter — "
                                     "spread falling, BUY_BASIS blocked", asset)

        now = self.clock()
        if now - self.last_close_time.get(asset, -1e18) < cfg["ENTRY_COOLDOWN_SEC"]:
            return None
        if now - self.last_stop_time.get(asset, -1e18) < cfg["STOP_COOLDOWN_SEC"]:
            return None
        if self.blocked_direction.get(asset) == direction:
            return None

        passes, capture, cost = edge_fn(z, stats.sigma, lots, contract_size,
                                        market_data)
        if not passes:
            return self._blocked(asset, "edge", "%s: edge filter — capture "
                                 "₹%.0f below cost multiple of ₹%.0f", asset,
                                 capture, cost)

        self._unblocked(asset)
        logger.info("%s: ENTRY %s — z=%.2f, capture ₹%.0f vs cost ₹%.0f",
                    asset, direction, z, capture, cost)
        return direction
