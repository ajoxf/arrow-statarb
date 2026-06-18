"""Generic broker interface.

Every broker implementation inherits from :class:`BaseBroker`. Only Arrow is
implemented today, but the interface is broker-agnostic so others (Kotak, etc.)
can be added later via the registry without touching the algo or web layers.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Callable, Dict, List, Optional


class BaseBroker(ABC):
    """Abstract base class for all broker connections."""

    def __init__(self, name: str, config: Dict):
        self.name = name
        self.config = config
        self.connected = False
        self._callbacks: Dict[str, List[Callable]] = {}

    # ── connection ───────────────────────────────────────────────────────────
    @abstractmethod
    def connect(self) -> bool:
        """Authenticate with the broker. Returns True on success."""

    @abstractmethod
    def disconnect(self) -> None:
        """Disconnect / invalidate the session."""

    # ── orders ───────────────────────────────────────────────────────────────
    @abstractmethod
    def submit_order(
        self,
        symbol: str,
        side: str,
        quantity: int,
        order_type: str = "market",
        price: Optional[float] = None,
        exchange_segment: str = "nse_fo",
        product: str = "NRML",
        validity: str = "DAY",
        token: str = "",
    ) -> Dict:
        """Place an order. ``quantity`` is in units (lots × lot_size)."""

    @abstractmethod
    def cancel_order(self, order_id: str) -> bool:
        """Cancel a pending order. Returns True on success."""

    # ── order lifecycle (fill confirmation / amendment) ──────────────────────
    # These power the live SpreadExecutor's safety net (fill polling, limit
    # amendment, orphan detection). Brokers that cannot report order state
    # inherit the safe defaults below: status ``UNKNOWN`` and amend unsupported.
    def get_order_status(self, order_id: str) -> Dict:
        """Normalized order state:
        ``{order_id, status, filled_qty, pending_qty, avg_price, raw}`` where
        ``status`` ∈ ``PENDING | OPEN | PARTIAL | COMPLETE | REJECTED |
        CANCELLED | UNKNOWN``. A broker without an order-status API returns
        ``UNKNOWN`` so callers can degrade gracefully."""
        return {"order_id": order_id, "status": "UNKNOWN", "filled_qty": 0,
                "pending_qty": 0, "avg_price": 0.0, "raw": {}}

    def amend_order(
        self,
        order_id: str,
        price: Optional[float] = None,
        quantity: Optional[int] = None,
        order_type: Optional[str] = None,
    ) -> bool:
        """Modify a pending order's price / quantity / type in place. Returns
        True on success, False if the broker has no amend capability."""
        return False

    # ── market data ──────────────────────────────────────────────────────────
    @abstractmethod
    def get_ltp(self, instruments: List[Dict]) -> Dict[str, float]:
        """REST last-traded-price lookup → ``{symbol: ltp}``."""

    def start_price_stream(self, symbols: List[str]) -> bool:
        """Subscribe symbols to a live (WebSocket) price feed. Optional —
        brokers without a stream can leave this returning False."""
        return False

    def get_streamed_ltp(self, symbols: List[str]) -> Dict[str, float]:
        """Latest streamed prices from the live feed cache → ``{symbol: ltp}``."""
        return {}

    def stop_price_stream(self) -> None:
        """Tear down the live price feed. Optional."""

    # ── account / positions ──────────────────────────────────────────────────
    @abstractmethod
    def get_positions(self) -> List[Dict]:
        """Open positions, each with net qty / avg / ltp / pnl."""

    @abstractmethod
    def get_account_info(self) -> Dict:
        """Account balance, margin, etc."""

    # ── instrument resolution / picker ───────────────────────────────────────
    @abstractmethod
    def resolve_lot_size(self, exchange_segment: str, symbol: str) -> int:
        """Lot size (units per lot) for a trading symbol."""

    @abstractmethod
    def resolve_token(self, exchange_segment: str, symbol: str) -> str:
        """Resolve a symbol to whatever identifier the broker's orders need."""

    def list_underlyings(self, exchange: str, kind: str) -> List[str]:
        """Underlyings available for an exchange + kind (future/option/cash)."""
        return []

    def list_contracts(self, exchange: str, kind: str, underlying: str) -> List[Dict]:
        """Contracts for an underlying, sorted chronologically by expiry."""
        return []

    # ── pub/sub helpers ──────────────────────────────────────────────────────
    def subscribe_market_data(self, symbols: List[str], callback: Callable) -> None:
        self._callbacks.setdefault("market_data", []).append(callback)

    def subscribe_order_updates(self, callback: Callable) -> None:
        self._callbacks.setdefault("order_update", []).append(callback)

    def _notify(self, event_type: str, data: Dict) -> None:
        for callback in self._callbacks.get(event_type, []):
            try:
                callback(data)
            except Exception:
                pass

    @property
    def is_connected(self) -> bool:
        return self.connected
