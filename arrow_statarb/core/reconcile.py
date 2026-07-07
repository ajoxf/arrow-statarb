"""Self-healing reconciliation between engine state and the exchange.

Runs on a cadence and compares what the engine believes against the exchange's
actual bot-instrument positions. Two failure modes, each acted on only after N
*consecutive* mismatches (so a single stale read never triggers a close):

  • engine_ghost   — engine thinks it is IN-TRADE but the exchange is FLAT.
                     Safe fix: force-clear the engine's in-memory position.
  • exchange_orphan — exchange holds bot legs but the engine is FLAT.
                     Fix (only when ``auto_close`` is on): flatten each leg and
                     book the cost to the untracked ledger.

It NEVER touches non-bot instruments — the caller supplies only bot-leg
positions.
"""

from __future__ import annotations

from typing import Callable, Dict, List

from loguru import logger


class ReconcileGuard:
    def __init__(self, *, engine_in_trade: Callable[[], bool],
                 exchange_bot_positions: Callable[[], List[Dict]],
                 clear_engine: Callable[[], bool],
                 flatten_leg: Callable[[Dict], None],
                 threshold: int = 3, auto_close: bool = False):
        self._engine_in_trade = engine_in_trade
        self._exchange_bot_positions = exchange_bot_positions
        self._clear_engine = clear_engine
        self._flatten_leg = flatten_leg
        self._threshold = max(1, int(threshold))
        self._auto_close = bool(auto_close)
        self._consec = 0
        self._kind: str = ""
        self.last: Dict = {"state": "ok", "consec": 0}

    @staticmethod
    def _has_bot_position(positions: List[Dict]) -> bool:
        return any(int(p.get("net_quantity", 0) or 0) != 0 for p in (positions or []))

    def check(self) -> Dict:
        """Run one reconciliation cycle; returns the resulting state dict."""
        in_trade = bool(self._engine_in_trade())
        bot_pos = self._exchange_bot_positions() or []
        has_bot = self._has_bot_position(bot_pos)

        if in_trade and not has_bot:
            kind = "engine_ghost"
        elif (not in_trade) and has_bot:
            kind = "exchange_orphan"
        else:
            self._consec = 0; self._kind = ""
            self.last = {"state": "ok", "consec": 0}
            return self.last

        self._consec = self._consec + 1 if kind == self._kind else 1
        self._kind = kind
        acted = None
        if self._consec >= self._threshold:
            if kind == "engine_ghost":
                if self._clear_engine():
                    acted = "cleared_engine"
                self._consec = 0; self._kind = ""
            elif kind == "exchange_orphan":
                if self._auto_close:
                    for p in bot_pos:
                        if int(p.get("net_quantity", 0) or 0) != 0:
                            try:
                                self._flatten_leg(p)
                            except Exception as exc:            # noqa: BLE001
                                logger.error("ReconcileGuard: flatten failed for {} — {}",
                                             p.get("symbol"), exc)
                    acted = "closed_orphan"
                    self._consec = 0; self._kind = ""
                else:
                    acted = "orphan_detected_alert"             # surfaced, not auto-closed
        self.last = {"state": kind, "consec": self._consec, "acted": acted,
                     "auto_close": self._auto_close}
        return self.last
