"""Safe two-leg spread executor for LIVE trading.

A spread is only ever a hedge while BOTH legs are on. The naive path — fire two
market orders and hope — degrades into a naked futures position the moment one
leg is rejected or lags. This executor closes that gap:

  * places each leg as a LIMIT order, priced from live LTP with a capped offset
    so slippage is bounded (set ``use_limit_orders: false`` to force market);
  * polls each order's real fill state via ``broker.get_order_status``;
  * amends the resting limit toward the market to chase the fill;
  * on timeout, escalates a still-unfilled limit to MARKET (``limit_to_market``);
  * **detects an orphaned leg** (one filled, the other not) and **recovers** it
    by cancelling the laggard and flattening the filled leg — so we never carry
    one-sided exposure from a failed entry;
  * optionally verifies we are flat on both legs before an entry
    (``verify_flat_before_entry``) so signals never stack onto a stale position.

Dependency-injected to stay testable and free of web/broker-construction logic:

  broker_fn()            -> the active broker (or None)
  price_fn(seg, sym)     -> latest LTP for a leg (or None)
  params_fn()            -> execution params dict (offsets, timeouts, flags)

``clock`` / ``sleep`` are injectable so tests drive timeouts deterministically.
Dry-run never reaches here — the web layer simulates above this executor, so any
order this places is real.
"""

from __future__ import annotations

import time
from typing import Callable, Dict, List, Optional

from loguru import logger

# Lifecycle states we treat as "still working" (worth polling/amending).
_PENDING = {"NEW", "PENDING", "OPEN", "PARTIAL"}


class LegOrder:
    """One leg of the spread plus its live execution state."""

    __slots__ = ("segment", "symbol", "side", "units", "token",
                 "order_id", "status", "order_type", "filled", "avg_price",
                 "limit_price", "ref_price", "error", "unconfirmed", "amend_count",
                 "recovery", "escalated")

    def __init__(self, segment: str, symbol: str, side: str, units: int, token: str = ""):
        self.segment = segment
        self.symbol = symbol
        self.side = side                  # "buy" | "sell"
        self.units = int(units)           # already lots × lot_size
        self.token = token
        self.order_id: Optional[str] = None
        self.status = "NEW"
        self.order_type = ""
        self.filled = 0
        self.avg_price = 0.0
        self.limit_price: Optional[float] = None
        self.ref_price: Optional[float] = None   # LTP at placement (slippage baseline)
        self.error = ""
        self.unconfirmed = False          # filled assumed (broker has no status API)
        self.amend_count = 0
        self.recovery: Optional[Dict] = None
        self.escalated = False            # limit timed out → re-sent as MARKET

    @property
    def working(self) -> bool:
        return self.status in _PENDING

    @property
    def complete(self) -> bool:
        return self.status == "COMPLETE"

    def view(self) -> Dict:
        return {"order_id": self.order_id, "symbol": self.symbol, "side": self.side,
                "status": self.status, "filled": self.filled, "avg_price": self.avg_price,
                "limit_price": self.limit_price, "ref_price": self.ref_price,
                "order_type": self.order_type, "amend_count": self.amend_count,
                "escalated": self.escalated, "unconfirmed": self.unconfirmed,
                "error": self.error}


class SpreadExecutor:
    def __init__(
        self,
        *,
        broker_fn: Callable[[], object],
        price_fn: Callable[[str, str], Optional[float]],
        params_fn: Callable[[], Dict],
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self._broker_fn = broker_fn
        self._price_fn = price_fn
        self._params_fn = params_fn
        self._clock = clock
        self._sleep = sleep

    # ── public API ───────────────────────────────────────────────────────────
    def execute(self, legs: List[LegOrder], label: str = "Order",
                verify_flat: bool = False) -> Dict:
        """Place and confirm both legs. Returns a result dict shaped like the
        manual order path (``success``/``message``/``results``) plus
        ``orphan``/``recovered`` flags."""
        broker = self._broker_fn()
        if broker is None:
            return self._fail(legs, "No broker connected")
        p = self._params_fn()
        start = self._clock()

        if verify_flat and p.get("verify_flat_before_entry", True):
            clash = self._verify_flat(broker, legs)
            if clash:
                logger.warning("{}: blocked — {}", label, clash)
                return dict(self._fail(legs, clash), elapsed_sec=0.0)

        for leg in legs:
            self._place(broker, leg, p)

        self._await_fills(broker, legs, p)
        self._escalate(broker, legs, p)

        elapsed = round(self._clock() - start, 2)
        filled = [l for l in legs if l.complete]
        failed = [l for l in legs if not l.complete]

        if not failed:
            ids = ", ".join(str(l.order_id) for l in legs)
            unconf = any(l.unconfirmed for l in legs)
            tag = " (fills unconfirmed)" if unconf else ""
            logger.info("{}: both legs filled — {}{}", label, ids, tag)
            return dict(self._ok(legs, f"[LIVE] Filled: {ids}{tag}"), elapsed_sec=elapsed)

        if not filled:
            err = "; ".join(l.error or f"{l.symbol} not filled" for l in failed)
            logger.error("{}: no legs filled — {}", label, err)
            return dict(self._fail(legs, err), elapsed_sec=elapsed)

        # ── ORPHAN: at least one leg filled, at least one did not ────────────
        recovered = self._recover_orphan(broker, filled, p)
        stuck = "; ".join(l.error or f"{l.symbol} not filled" for l in failed)
        msg = (f"ORPHAN on {label}: filled {', '.join(l.symbol for l in filled)} but "
               f"{stuck}. " + ("Flattened filled leg(s)." if recovered
                               else "RECOVERY FAILED — check positions NOW."))
        logger.error("{}", msg)
        return dict(self._fail(legs, msg, orphan=True, recovered=recovered), elapsed_sec=elapsed)

    # ── placement / pricing ──────────────────────────────────────────────────
    @staticmethod
    def _limit_price(side: str, ltp: Optional[float], offset: float) -> Optional[float]:
        """A marketable limit: buy slightly above / sell slightly below LTP so it
        fills quickly while still capping slippage at ``offset``."""
        if ltp is None:
            return None
        return round(ltp * (1 + offset), 2) if side == "buy" else round(ltp * (1 - offset), 2)

    def _place(self, broker, leg: LegOrder, p: Dict, force_market: bool = False) -> None:
        use_limit = bool(p.get("use_limit_orders", True)) and not force_market
        order_type = "limit" if use_limit else "market"
        # Reference price (slippage baseline) is captured on every placement.
        ltp = self._price_fn(leg.segment, leg.symbol)
        if ltp is not None:
            leg.ref_price = ltp
        price = None
        if order_type == "limit":
            price = self._limit_price(leg.side, ltp, float(p.get("limit_offset_pct", 0.05)) / 100.0)
            if price is None:                       # no live price → market fallback
                order_type = "market"
                logger.warning("Executor: no LTP for {} — placing MARKET", leg.symbol)
        if force_market:
            leg.escalated = True

        res = broker.submit_order(
            symbol=leg.symbol, side=leg.side, quantity=leg.units,
            order_type=order_type, price=price, exchange_segment=leg.segment,
            product=str(p.get("product", "NRML")), token=leg.token,
        ) or {}

        if res.get("status") == "error" or res.get("order_id") in (None, ""):
            leg.status = "REJECTED"
            leg.order_id = None
            leg.error = res.get("message", "order rejected")
            logger.error("Executor: {} {} {} REJECTED — {}",
                         leg.side, leg.units, leg.symbol, leg.error)
        else:
            leg.order_id = str(res.get("order_id"))
            leg.status = "PENDING"
            leg.order_type = order_type
            leg.limit_price = price

    # ── fill polling + amendment ─────────────────────────────────────────────
    def _refresh(self, broker, leg: LegOrder) -> None:
        if not leg.order_id:
            return
        st = broker.get_order_status(leg.order_id) or {}
        status = str(st.get("status", "UNKNOWN")).upper()
        if status == "UNKNOWN":
            # Broker can't report fills — assume the accepted order is on.
            # Orphan detection is impossible in this mode, so flag it loudly.
            if not leg.unconfirmed:
                logger.warning("Executor: {} has no order-status API — treating {} "
                               "as filled (UNCONFIRMED)", broker.__class__.__name__, leg.symbol)
            leg.status = "COMPLETE"
            leg.unconfirmed = True
            leg.filled = leg.units
            return
        leg.status = status
        leg.filled = int(st.get("filled_qty") or 0)
        if st.get("avg_price"):
            leg.avg_price = float(st["avg_price"])

    def _await_fills(self, broker, legs: List[LegOrder], p: Dict) -> None:
        timeout = float(p.get("fill_timeout_sec", 5.0))
        poll = float(p.get("poll_interval_sec", 0.4))
        amend_every = float(p.get("amend_interval_sec", 1.5))
        use_limit = bool(p.get("use_limit_orders", True))

        deadline = self._clock() + timeout
        next_amend = self._clock() + amend_every
        while self._clock() < deadline:
            working = [l for l in legs if l.working]
            if not working:
                break
            for leg in working:
                self._refresh(broker, leg)
            working = [l for l in legs if l.working]
            if not working:
                break
            if use_limit and self._clock() >= next_amend:
                for leg in working:
                    self._amend_more_aggressive(broker, leg, p)
                next_amend = self._clock() + amend_every
            self._sleep(poll)

    def _amend_more_aggressive(self, broker, leg: LegOrder, p: Dict) -> None:
        """Walk the resting limit further through the market to chase the fill."""
        if not leg.order_id or leg.order_type != "limit":
            return
        ltp = self._price_fn(leg.segment, leg.symbol)
        if ltp is None:
            return
        leg.amend_count += 1
        offset = (float(p.get("limit_offset_pct", 0.05))
                  + float(p.get("amend_step_pct", 0.05)) * leg.amend_count) / 100.0
        price = self._limit_price(leg.side, ltp, offset)
        if price is not None and broker.amend_order(leg.order_id, price=price):
            leg.limit_price = price
            logger.info("Executor: amended {} limit → {}", leg.symbol, price)

    # ── timeout escalation (limit → market) ──────────────────────────────────
    def _escalate(self, broker, legs: List[LegOrder], p: Dict) -> None:
        working = [l for l in legs if l.working]
        if not working:
            return
        if not bool(p.get("limit_to_market", True)):
            for leg in working:
                if leg.order_id:
                    broker.cancel_order(leg.order_id)
                leg.status = "CANCELLED"
                leg.error = leg.error or "limit unfilled (limit_to_market disabled)"
            return

        for leg in working:
            if leg.order_id:
                try:
                    broker.cancel_order(leg.order_id)
                except Exception as exc:        # noqa: BLE001
                    logger.warning("Executor: cancel {} failed — {}", leg.order_id, exc)
            leg.order_id = None
            leg.status = "NEW"
            logger.warning("Executor: {} limit timed out — re-placing as MARKET", leg.symbol)
            self._place(broker, leg, p, force_market=True)

        # Short confirmation window for the market re-placements.
        timeout = max(float(p.get("fill_timeout_sec", 5.0)), 2.0)
        poll = float(p.get("poll_interval_sec", 0.4))
        deadline = self._clock() + timeout
        while self._clock() < deadline:
            still = [l for l in working if l.working]
            if not still:
                break
            for leg in still:
                self._refresh(broker, leg)
            if not [l for l in working if l.working]:
                break
            self._sleep(poll)

    # ── orphan recovery / pre-entry verification ─────────────────────────────
    def _recover_orphan(self, broker, filled: List[LegOrder], p: Dict) -> bool:
        """Flatten every leg that DID fill, with an opposing market order, so a
        partial entry never leaves one-sided exposure."""
        ok = True
        for leg in filled:
            opp = "sell" if leg.side == "buy" else "buy"
            qty = leg.filled or leg.units
            try:
                res = broker.submit_order(
                    symbol=leg.symbol, side=opp, quantity=qty, order_type="market",
                    exchange_segment=leg.segment, product=str(p.get("product", "NRML")),
                    token=leg.token,
                ) or {}
                leg.recovery = res
                if res.get("status") == "error" or res.get("order_id") in (None, ""):
                    ok = False
                    logger.error("Executor: ORPHAN recovery FAILED for {} — {}",
                                 leg.symbol, res.get("message"))
                else:
                    logger.error("Executor: ORPHAN recovery — flattened {} {} via MARKET (id={})",
                                 opp, leg.symbol, res.get("order_id"))
            except Exception as exc:            # noqa: BLE001
                ok = False
                logger.error("Executor: ORPHAN recovery EXCEPTION for {} — {}", leg.symbol, exc)
        return ok

    def _verify_flat(self, broker, legs: List[LegOrder]) -> str:
        """Return a reason string if either leg already has an open exchange
        position (so we don't stack a new entry), else ``""``."""
        try:
            positions = broker.get_positions() or []
        except Exception as exc:                # noqa: BLE001
            logger.warning("Executor: verify_flat could not read positions — {}", exc)
            return ""                           # can't verify → don't block
        held = {str(pos.get("symbol", "")).upper(): int(pos.get("net_quantity", 0) or 0)
                for pos in positions}
        clashes = [l.symbol for l in legs if held.get(l.symbol.upper(), 0) != 0]
        if clashes:
            return ("verify_exchange_position: existing position in "
                    + ", ".join(clashes) + " — entry skipped")
        return ""

    # ── result shaping ───────────────────────────────────────────────────────
    @staticmethod
    def _ok(legs: List[LegOrder], message: str) -> Dict:
        return {"success": True, "message": message, "error": "", "dry_run": False,
                "orphan": False, "recovered": False,
                "results": [l.view() for l in legs]}

    @staticmethod
    def _fail(legs: List[LegOrder], error: str, *, orphan: bool = False,
              recovered: bool = False) -> Dict:
        return {"success": False, "message": "", "error": error, "dry_run": False,
                "orphan": orphan, "recovered": recovered,
                "results": [l.view() for l in legs]}
