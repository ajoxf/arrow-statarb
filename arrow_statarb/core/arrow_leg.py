"""Arrow leg adapter — the porting seam.

Implements the leg interface used by ClipExecutor and ScenarioRunner over the
Arrow broker (real or sim). The interface speaks in LOTS; Arrow orders in UNITS
(units = lots × lot_size), so this adapter does the conversion both ways and
reports fills back in lots.

Methods provided: name, ensure_symbol, tick, market_order, place_limit,
order_state, modify_order, cancel_order, pending_orders.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

_OPEN_STATES = {"OPEN", "PENDING", "PARTIAL", "TRIGGER_PENDING"}


class ArrowLeg:
    def __init__(self, broker, symbol_segments, product="NRML", name="arrow"):
        self.broker = broker
        self.segs = {str(k).upper(): v for k, v in (symbol_segments or {}).items()}
        self.product = product
        self.name = name
        self._order_symbol = {}          # order_id → symbol (for lot conversion)

    # ── helpers ───────────────────────────────────────────────────────────────
    def _seg(self, symbol):
        return self.segs.get(str(symbol).upper())

    def _lot(self, symbol):
        try:
            return max(1, int(self.broker.resolve_lot_size(self._seg(symbol), symbol) or 1))
        except Exception:
            return 1

    # ── interface ─────────────────────────────────────────────────────────────
    def ensure_symbol(self, symbol):
        seg = self._seg(symbol)
        lot = self._lot(symbol)
        tick = 0.05
        try:
            tick = float(self.broker.resolve_tick_size(seg, symbol) or 0.0) or 0.05
        except Exception:
            pass
        return {"ok": bool(seg), "volume_min": 1, "volume_step": 1,
                "volume_max": 1_000_000, "point": tick, "tick_size": tick,
                "lot_size": lot}

    def tick(self, symbol):
        try:
            q = self.broker.get_quote(self._seg(symbol), symbol) or {}
        except Exception:
            return None
        bid = q.get("bid") if q.get("bid") is not None else q.get("ltp")
        ask = q.get("ask") if q.get("ask") is not None else q.get("ltp")
        if bid is None or ask is None:
            return None
        return {"bid": float(bid), "ask": float(ask)}

    def market_order(self, symbol, side, lots, slippage_points=0.0, comment=""):
        lot = self._lot(symbol)
        res = self.broker.submit_order(
            symbol=symbol, side=str(side).lower(), quantity=int(lots * lot),
            order_type="market", exchange_segment=self._seg(symbol),
            product=self.product) or {}
        if res.get("status") != "submitted":
            return {"ok": False, "filled_volume": 0.0, "price": None,
                    "error": res.get("message") or res.get("status")}
        oid = res.get("order_id")
        st = self.broker.get_order_status(oid) if oid else {}
        filled_units = float((st or {}).get("filled_qty") or 0)
        return {"ok": filled_units > 0, "order_id": oid,
                "filled_volume": filled_units / lot,
                "price": (st or {}).get("avg_price") or None,
                "error": None if filled_units > 0 else (st or {}).get("status")}

    def place_limit(self, symbol, side, lots, price, comment=""):
        lot = self._lot(symbol)
        res = self.broker.submit_order(
            symbol=symbol, side=str(side).lower(), quantity=int(lots * lot),
            order_type="limit", price=float(price),
            exchange_segment=self._seg(symbol), product=self.product) or {}
        if res.get("status") != "submitted" or not res.get("order_id"):
            return {"ok": False, "ticket": None,
                    "error": res.get("message") or res.get("status")}
        self._order_symbol[res["order_id"]] = symbol
        return {"ok": True, "ticket": res["order_id"]}

    def order_state(self, ticket):
        symbol = self._order_symbol.get(ticket)
        lot = self._lot(symbol) if symbol else 1
        st = self.broker.get_order_status(ticket) or {}
        status = str(st.get("status") or "UNKNOWN").upper()
        filled_units = float(st.get("filled_qty") or 0)
        return {"filled_volume": filled_units / lot,
                "price": st.get("avg_price") or None,
                "still_open": status in _OPEN_STATES,
                "error": st.get("status") if status == "REJECTED" else None}

    def modify_order(self, ticket, price):
        try:
            r = self.broker.amend_order(ticket, price=float(price))
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": bool(r if isinstance(r, bool) else (r or {}).get("ok", True))}

    def cancel_order(self, ticket):
        symbol = self._order_symbol.get(ticket)
        lot = self._lot(symbol) if symbol else 1
        st = self.broker.get_order_status(ticket) or {}
        filled_units = float(st.get("filled_qty") or 0)
        try:
            self.broker.cancel_order(ticket)
        except Exception as exc:
            logger.debug("ArrowLeg: cancel %s failed — %s", ticket, exc)
        return {"filled_volume": filled_units / lot,
                "price": st.get("avg_price") or None}

    def pending_orders(self, symbol):
        # Best-effort: Arrow's order-book scan lives in get_order_status; the
        # scenario/clip paths sweep by cancelling known tickets, so an empty
        # list here is safe (no stale-order pre-sweep). Extend if needed.
        return []
