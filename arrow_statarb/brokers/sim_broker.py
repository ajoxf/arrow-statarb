"""Simulated broker for the ``live_sim`` mode.

Implements just enough of the broker surface for the real :class:`SpreadExecutor`
to run end-to-end — limit placement, fill polling, amendment, limit→market
escalation and orphan recovery — against simulated fills, so execution can be
exercised and measured with ZERO capital at risk.

Realistic fills: a buy fills at the **ask**, a sell at the **bid** (the real
spread cost), using live bid/ask depth from Arrow when available, or a
configurable synthetic spread (``spread_ticks``) when only LTP is known. A limit
only fills when it is marketable (buy ≥ ask / sell ≤ bid) — otherwise it rests
until the executor amends it more aggressively or escalates to market, which is
how fill-probability is modelled. ``extra_slip_ticks`` adds optional latency/queue
slippage on top of the touch.

Tunable knobs (no hidden magic): slow-fill / reject / orphan probabilities for
exercising the safety paths.
"""

from __future__ import annotations

import random
import threading
from typing import Callable, Dict, List, Optional

from loguru import logger


class SimBroker:
    def __init__(
        self,
        *,
        real_price_fn: Optional[Callable[[str, str], Optional[float]]] = None,
        quote_fn: Optional[Callable[[str, str], Optional[Dict]]] = None,
        lot_size_fn: Optional[Callable[[str, str], int]] = None,
        tick_size: float = 0.05,
        spread_ticks: float = 2.0,        # synthetic full bid-ask spread (ticks)
        extra_slip_ticks: float = 0.0,    # extra adverse slip beyond the touch
        slow_prob: float = 0.25,
        reject_prob: float = 0.0,
        orphan_prob: float = 0.0,
        orphan_symbols: Optional[set] = None,
        default_lot_size: int = 75,
        seed: Optional[int] = None,
        slippage_pct: float = 0.0,        # accepted for back-compat; unused
    ):
        self._real_price = real_price_fn
        self._quote_fn = quote_fn
        self._lot_size_fn = lot_size_fn
        self.tick_size = float(tick_size) or 0.05
        self.spread_ticks = float(spread_ticks)
        self.extra_slip_ticks = float(extra_slip_ticks)
        self.slow_prob = float(slow_prob)
        self.reject_prob = float(reject_prob)
        self.orphan_prob = float(orphan_prob)
        # symbols whose orders NEVER fill (even on market) — forces a deterministic
        # orphan so recovery can be demonstrated/tested.
        self.orphan_symbols = {s.upper() for s in (orphan_symbols or ())}
        self.default_lot_size = int(default_lot_size)
        self._rng = random.Random(seed)
        self._lock = threading.RLock()
        self._orders: Dict[str, Dict] = {}
        self._positions: Dict[str, Dict] = {}   # symbol -> {net, avg}
        self._base: Dict[str, float] = {}        # synthetic price per symbol
        self._counter = 0
        self.connected = True

    # ── prices / quotes ──────────────────────────────────────────────────────
    def _ltp(self, segment: str, symbol: str) -> float:
        if self._real_price:
            try:
                v = self._real_price(segment, symbol)
                if v:
                    return float(v)
            except Exception:
                pass
        b = self._base.get(symbol)
        if b is None:
            b = 25000.0 + self._rng.uniform(-500, 500)
        b *= (1 + self._rng.uniform(-0.0004, 0.0004))
        self._base[symbol] = b
        return round(b, 2)

    def _quote(self, segment: str, symbol: str):
        """Return (bid, ask, ltp). Uses real depth when ``quote_fn`` provides
        bid/ask; otherwise synthesizes a ``spread_ticks``-wide book around LTP."""
        q = None
        if self._quote_fn:
            try:
                q = self._quote_fn(segment, symbol)
            except Exception:
                q = None
        ltp = float((q or {}).get("ltp") or self._ltp(segment, symbol))
        bid = (q or {}).get("bid")
        ask = (q or {}).get("ask")
        try:
            bid = float(bid) if bid else None
            ask = float(ask) if ask else None
        except (TypeError, ValueError):
            bid = ask = None
        if not bid or not ask or ask < bid:
            half = (self.spread_ticks * self.tick_size) / 2.0
            bid, ask = round(ltp - half, 2), round(ltp + half, 2)
        return bid, ask, ltp

    def get_quote(self, exchange_segment: str, symbol: str) -> Dict:
        """Public {ltp, bid, ask} for the leg adapter (scenario/clip execution
        in live_sim). Wraps the synthetic book from ``_quote``."""
        bid, ask, ltp = self._quote(exchange_segment, symbol)
        return {"ltp": ltp, "bid": bid, "ask": ask}

    def _marketable(self, side: str, price, bid: float, ask: float, order_type: str) -> bool:
        if order_type == "market" or price is None:
            return True
        return price >= ask if side == "buy" else price <= bid

    def _touch_fill(self, side: str, bid: float, ask: float) -> float:
        extra = self.extra_slip_ticks * self.tick_size
        return round((ask + extra) if side == "buy" else (bid - extra), 2)

    def get_streamed_ltp(self, symbols: List[str]) -> Dict[str, float]:
        return {s.upper(): self._ltp("", s) for s in symbols}

    def get_ltp(self, instruments: List[Dict]) -> Dict[str, float]:
        out = {}
        for ins in instruments:
            sym = ins.get("instrument_token", "")
            out[sym.upper()] = self._ltp(ins.get("exchange_segment", ""), sym)
        return out

    def start_price_stream(self, symbols: List[str]) -> bool:
        return True

    # ── instrument resolution ────────────────────────────────────────────────
    def resolve_lot_size(self, exchange_segment: str, symbol: str) -> int:
        if self._lot_size_fn:
            try:
                ls = int(self._lot_size_fn(exchange_segment, symbol))
                if ls > 0:
                    return ls
            except Exception:
                pass
        return self.default_lot_size

    def resolve_tick_size(self, exchange_segment: str, symbol: str) -> float:
        """Same tick the sim books quotes on, so the executor's tick-rounded
        limits line up with the simulated bid/ask grid."""
        return float(self.tick_size)

    def resolve_token(self, exchange_segment: str, symbol: str) -> str:
        return symbol

    # ── orders ───────────────────────────────────────────────────────────────
    def submit_order(self, symbol: str, side: str, quantity: int,
                     order_type: str = "market", price: Optional[float] = None,
                     exchange_segment: str = "", product: str = "NRML",
                     token: str = "") -> Dict:
        with self._lock:
            if order_type != "market" and self._rng.random() < self.reject_prob:
                logger.info("SimBroker: REJECT {} {} {}", side, quantity, symbol)
                return {"order_id": None, "status": "error",
                        "message": "simulated reject", "symbol": symbol}
            self._counter += 1
            oid = f"SIM{self._counter}"
            never = (symbol.upper() in self.orphan_symbols) or (
                (order_type != "market") and (self._rng.random() < self.orphan_prob))
            slow = (order_type != "market") and (self._rng.random() < self.slow_prob)
            o = {
                "symbol": symbol, "seg": exchange_segment, "side": side,
                "units": int(quantity), "order_type": order_type, "price": price,
                "status": "PENDING", "fill_price": 0.0, "polls": 0,
                "polls_needed": (self._rng.randint(2, 4) if slow else 0),
                "never": never,
            }
            self._orders[oid] = o
            # A (non-stuck) MARKET order fills immediately at the touch — so
            # orphan-recovery flattening updates positions without a status poll.
            if order_type == "market" and not never:
                bid, ask, _ = self._quote(exchange_segment, symbol)
                o["fill_price"] = self._touch_fill(side, bid, ask)
                o["status"] = "COMPLETE"
                self._apply_fill(o)
            return {"order_id": oid, "status": "submitted", "symbol": symbol}

    def get_order_status(self, order_id: str) -> Dict:
        with self._lock:
            o = self._orders.get(order_id)
            if not o:
                return {"order_id": order_id, "status": "UNKNOWN", "filled_qty": 0,
                        "pending_qty": 0, "avg_price": 0.0, "raw": {}}
            if o["status"] in ("COMPLETE", "REJECTED", "CANCELLED"):
                pass
            elif o["never"]:
                o["status"] = "OPEN"
            else:
                o["polls"] += 1
                bid, ask, _ = self._quote(o["seg"], o["symbol"])
                marketable = self._marketable(o["side"], o["price"], bid, ask, o["order_type"])
                if marketable and o["polls"] > o["polls_needed"]:
                    o["fill_price"] = self._touch_fill(o["side"], bid, ask)
                    o["status"] = "COMPLETE"
                    self._apply_fill(o)
                else:
                    o["status"] = "OPEN"
            filled = o["units"] if o["status"] == "COMPLETE" else 0
            return {"order_id": order_id, "status": o["status"], "filled_qty": filled,
                    "pending_qty": o["units"] - filled,
                    "avg_price": o["fill_price"] if filled else 0.0, "raw": dict(o)}

    def amend_order(self, order_id: str, price: Optional[float] = None,
                    quantity: Optional[int] = None, order_type: Optional[str] = None) -> bool:
        with self._lock:
            o = self._orders.get(order_id)
            if not o or o["status"] in ("COMPLETE", "REJECTED", "CANCELLED"):
                return False
            if price is not None:
                o["price"] = price                                  # chasing → marketable sooner
            o["polls_needed"] = max(0, o["polls_needed"] - 1)
            return True

    def cancel_order(self, order_id: str) -> bool:
        with self._lock:
            o = self._orders.get(order_id)
            if o and o["status"] not in ("COMPLETE",):
                o["status"] = "CANCELLED"
            return True

    # ── positions ────────────────────────────────────────────────────────────
    def _apply_fill(self, o: Dict) -> None:
        sym = o["symbol"]
        signed = o["units"] if o["side"] == "buy" else -o["units"]
        pos = self._positions.setdefault(sym, {"net": 0, "avg": 0.0})
        new_net = pos["net"] + signed
        if pos["net"] == 0 or (pos["net"] > 0) == (signed > 0):
            tot = abs(pos["net"]) + abs(signed)
            pos["avg"] = round((pos["avg"] * abs(pos["net"]) + o["fill_price"] * abs(signed)) / tot, 2) if tot else 0.0
        pos["net"] = new_net
        if new_net == 0:
            pos["avg"] = 0.0

    def get_positions(self) -> List[Dict]:
        with self._lock:
            out = []
            for sym, p in self._positions.items():
                if not p["net"]:
                    continue
                ltp = self._ltp("", sym)
                out.append({"symbol": sym, "net_quantity": p["net"],
                            "average_price": p["avg"], "ltp": ltp,
                            "pnl": round((ltp - p["avg"]) * p["net"], 2)})
            return out

    def get_account_info(self) -> Dict:
        return {}

    def get_funds(self) -> Dict:
        return {"available": None, "used": None, "equity": None, "cash": None, "raw": {}}
