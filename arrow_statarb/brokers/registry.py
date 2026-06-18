"""Broker registry — maps a config name to a broker class.

Only Arrow is implemented today, but the algo and web layers go through this
registry so adding another broker (Kotak, etc.) later is just one entry here.
A single broker is active at a time, chosen from ``broker.name`` in config.
"""

from __future__ import annotations

import threading
from typing import Dict, Optional, Type

from loguru import logger

from arrow_statarb.brokers.base_broker import BaseBroker
from arrow_statarb.brokers.arrow_broker import ArrowBroker

# name → broker class
BROKER_REGISTRY: Dict[str, Type[BaseBroker]] = {
    "arrow": ArrowBroker,
}


def get_broker_class(name: str) -> Type[BaseBroker]:
    """Return the broker class registered under ``name`` (case-insensitive)."""
    key = (name or "").strip().lower()
    if key not in BROKER_REGISTRY:
        raise KeyError(
            f"Unknown broker '{name}'. Registered: {', '.join(BROKER_REGISTRY)}"
        )
    return BROKER_REGISTRY[key]


def create_broker(name: str, config: Dict) -> BaseBroker:
    """Instantiate the broker registered under ``name`` with ``config``."""
    return get_broker_class(name)(config=config)


class ActiveBroker:
    """Thread-safe holder for the one connected broker instance.

    The web app keeps a process-wide instance of this so every request /
    the algo all see the same connected broker.
    """

    def __init__(self):
        self._broker: Optional[BaseBroker] = None
        self._lock = threading.RLock()

    def set(self, broker: Optional[BaseBroker]) -> None:
        with self._lock:
            self._broker = broker

    def get(self) -> Optional[BaseBroker]:
        """Return the active broker only if it's currently connected."""
        with self._lock:
            if self._broker and getattr(self._broker, "connected", False):
                return self._broker
            return None

    def get_any(self) -> Optional[BaseBroker]:
        """Return the active broker even if disconnected (for status/teardown)."""
        with self._lock:
            return self._broker

    def clear(self) -> None:
        with self._lock:
            if self._broker:
                try:
                    self._broker.disconnect()
                except Exception as exc:
                    logger.warning("ActiveBroker: disconnect on clear failed — {}", exc)
            self._broker = None
