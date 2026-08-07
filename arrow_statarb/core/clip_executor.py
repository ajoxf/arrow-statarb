"""Clip execution — limit-first, sliced, hedge-sized-to-actual-fill.

Ported from the W3 pair_executor. Two Arrow adaptations:
  • Indian F&O is a NETTING book, not MT5 hedging-mode, so a plain opposite
    order closes/reduces — the position-ticket close machinery is dropped.
  • Both "legs" are the SAME Arrow connection, routed by symbol; ``leg_a`` and
    ``leg_b`` may be one object.

Execution ladder per child order (limit-first — a limit fill saves crossing the
spread):
  1. rest a limit at the peg (buy at bid / sell at ask), partials keep working;
  2. re-peg via order-MODIFY when the market drifts (no cancel/replace window);
  3. on timeout: cancel, ALWAYS re-read fills (a cancel can carry a partial),
     then cross the remainder at market (ON_TIMEOUT='cross') or abort.
Stops and unwinds never rest — straight to market.

Hedge policy: leg B (the contract/hedge leg) is sized to leg A's ACTUAL fill and
both CONTRACT SIZES (sizing.hedge_lots, not lots×beta); if the hedge under-fills
below MIN_MATCHED_FRACTION of the clip both legs are unwound, otherwise the
unmatched leg-A excess is unwound and the matched size kept. A failed unwind is
CRITICAL (naked exposure).

The ``leg`` interface (the porting seam) must expose: name; ensure_symbol,
tick, market_order, place_limit, order_state, modify_order, cancel_order,
pending_orders.
"""

from __future__ import annotations

import logging
import math
import uuid
import time as time_mod

from . import sizing
from . import slippage
from .exits import SELL_BASIS, BUY_BASIS, _dir

logger = logging.getLogger(__name__)

EPS = 1e-9
URGENT_REASONS = {"STOP_LOSS", "DOLLAR_STOP", "Z_STOP", "SYSTEM_SHUTDOWN",
                  "MANUAL_CLOSE", "MANUAL_STOP"}
BUY, SELL = "BUY", "SELL"


def _opp(side):
    return SELL if str(side).upper() == BUY else BUY


class ClipExecutor:
    def __init__(self, execution, leg_a, leg_b, slice_lots=0.0,
                 clock=time_mod.time, sleep=time_mod.sleep):
        self.execution = execution or {}
        self.leg_a = leg_a               # "spot" leg
        self.leg_b = leg_b               # "futures"/contract leg (hedge)
        self.slice_lots = slice_lots
        self.clock = clock
        self.sleep = sleep
        self._meta_cache = {}

    # ── volume helpers ────────────────────────────────────────────────────────
    def _meta(self, leg, symbol):
        key = (leg.name, symbol)
        if key not in self._meta_cache:
            meta = leg.ensure_symbol(symbol)
            if not meta or not meta.get("ok"):
                meta = {"ok": False, "volume_min": 0.01, "volume_max": 1000.0,
                        "volume_step": 0.01, "point": 0.01}
            self._meta_cache[key] = meta
        return self._meta_cache[key]

    @staticmethod
    def _round_step(volume, step):
        if step <= 0:
            return volume
        return math.floor(volume / step + EPS) * step

    # ── one child order ───────────────────────────────────────────────────────
    def _peg_price(self, leg, symbol, side, meta):
        """Peg from a FRESH tick, tick-rounded, kept strictly inside the book
        (a buy limit at/above the ask is rejected off-tick)."""
        tick = leg.tick(symbol)
        if not tick:
            return None
        point = meta.get("point", 0.01)
        offset = self.execution.get("PEG_OFFSET_POINTS", 0.0) * point
        if str(side).upper() == BUY:
            price = tick["bid"] + offset
            if price >= tick["ask"]:
                price = tick["bid"]
        else:
            price = tick["ask"] - offset
            if price <= tick["bid"]:
                price = tick["ask"]
        tick_size = meta.get("tick_size") or point or 0.01
        return round(round(price / tick_size) * tick_size, 10)

    def _market_child(self, leg, symbol, side, volume, comment):
        r = leg.market_order(symbol, side, volume,
                             slippage_points=self.execution.get("SLIPPAGE_TOLERANCE", 1.0),
                             comment=comment)
        return {"filled": float(r.get("filled_volume") or 0.0),
                "price": r.get("price"), "ok": bool(r.get("ok")),
                "error": r.get("error")}

    def _limit_child(self, leg, symbol, side, volume, comment, timeout,
                     escalate=None):
        ex = self.execution
        meta = self._meta(leg, symbol)
        poll = ex.get("ORDER_POLL_SEC", 0.5)
        repeg_every = ex.get("REPEG_INTERVAL_SEC", 2.0)
        point = meta.get("point", 0.01)

        price = self._peg_price(leg, symbol, side, meta)
        if price is None:
            return self._market_child(leg, symbol, side, volume, comment)

        placed = leg.place_limit(symbol, side, volume, price, comment=comment)
        if not placed.get("ok"):
            logger.warning("[%s] limit rejected (%s) — market fallback",
                           leg.name, placed.get("error"))
            return self._market_child(leg, symbol, side, volume, comment)

        ticket = placed["ticket"]
        deadline = self.clock() + timeout
        last_repeg = self.clock()
        while self.clock() < deadline:
            self.sleep(poll)
            state = leg.order_state(ticket)
            filled = float(state.get("filled_volume") or 0.0)
            if filled >= volume - EPS or not state.get("still_open", True):
                return {"filled": filled, "price": state.get("price"),
                        "ok": filled > EPS, "error": state.get("error")}
            if self.clock() - last_repeg >= repeg_every:
                new_price = self._peg_price(leg, symbol, side, meta)
                if new_price is not None and abs(new_price - price) > point / 2:
                    if leg.modify_order(ticket, new_price).get("ok"):
                        price = new_price
                last_repeg = self.clock()

        cancelled = leg.cancel_order(ticket)
        filled = float(cancelled.get("filled_volume") or 0.0)
        vwap = cancelled.get("price")
        remaining = volume - filled
        if remaining > EPS and ex.get("ON_TIMEOUT", "cross") == "cross":
            crossed = (escalate(remaining) if escalate
                       else self._market_child(leg, symbol, side, remaining, comment))
            got = crossed["filled"]
            if got > EPS:
                total = filled + got
                vwap = ((vwap or 0.0) * filled + (crossed["price"] or 0.0) * got) / total
                filled = total
        return {"filled": filled, "price": vwap, "ok": filled > EPS,
                "error": None if filled > EPS else "no fill before timeout"}

    def sweep_stale_orders(self, targets):
        """Cancel our leftover pendings on these symbols before a new execution
        (orphan pendings otherwise fill into untracked naked positions)."""
        seen = set()
        for leg, symbol in targets:
            key = (leg.name, symbol)
            if key in seen:
                continue
            seen.add(key)
            for order in (leg.pending_orders(symbol) or []):
                state = leg.cancel_order(order["ticket"])
                if float(state.get("filled_volume") or 0.0) > EPS:
                    logger.critical("[%s] stale order %s on %s had FILLED — "
                                    "reconciler will adopt it as an orphan",
                                    leg.name, order["ticket"], symbol)

    # ── sliced send ───────────────────────────────────────────────────────────
    def _send_sliced(self, leg, symbol, side, total_lots, comment,
                     style="market", timeout=None):
        meta = self._meta(leg, symbol)
        step = meta.get("volume_step") or 0.01
        vmax = meta.get("volume_max") or total_lots
        slice_lots = min(self.slice_lots or total_lots, vmax)
        timeout = timeout or self.execution.get("LIMIT_TIMEOUT_SEC", 15)

        remaining, filled, notional = total_lots, 0.0, 0.0
        while remaining > EPS:
            volume = self._round_step(min(slice_lots, remaining), step)
            if volume <= 0:
                break
            result = (self._limit_child(leg, symbol, side, volume, comment, timeout)
                      if style == "limit"
                      else self._market_child(leg, symbol, side, volume, comment))
            got = result["filled"]
            if got > EPS:
                filled += got
                notional += got * float(result["price"] or 0.0)
            if not result["ok"]:
                logger.warning("[%s] %s %s %.2f failed: %s", leg.name, side,
                               symbol, volume, result.get("error"))
                break
            if got < volume - EPS:
                logger.warning("[%s] %s %s partial %.2f/%.2f — not chasing "
                               "liquidity", leg.name, side, symbol, got, volume)
                break
            remaining -= got
        vwap = notional / filled if filled > EPS else None
        return filled, vwap

    def _unwind(self, leg, symbol, entry_side, lots, comment):
        """Reverse an entry fill AT MARKET; CRITICAL on failure."""
        if lots <= EPS:
            return True
        filled, _ = self._send_sliced(leg, symbol, _opp(entry_side), lots,
                                      comment, style="market")
        if filled < lots - EPS:
            logger.critical("UNHEDGED EXPOSURE on [%s]: unwound %.2f/%.2f of %s "
                            "— MANUAL INTERVENTION", leg.name, filled, lots, symbol)
            return False
        return True

    # ── pair entry ────────────────────────────────────────────────────────────
    def execute_pair(self, direction, symbol_a, symbol_b, lot_size,
                     contract_a, contract_b, hedge_ratio=1.0, tag="BASIS",
                     reference=None):
        """Open the spread. Returns (ok, result) where result carries per-leg
        fills, the frozen entry spread, k (=lots_b×contract_b) and the slippage
        report. Leg A patient; leg B hedge sized to leg A's actual fill."""
        d = _dir(direction)
        # SELL_BASIS (short spread): long spot, short futures. BUY_BASIS: opposite.
        side_a, side_b = (BUY, SELL) if d == SELL_BASIS else (SELL, BUY)
        ex = self.execution
        style = ex.get("ENTRY_STYLE", "market")
        comment = f"{tag}_{uuid.uuid4().hex[:8]}"

        self.sweep_stale_orders([(self.leg_a, symbol_a), (self.leg_b, symbol_b)])

        a_meta = self._meta(self.leg_a, symbol_a)
        b_meta = self._meta(self.leg_b, symbol_b)
        if not a_meta.get("ok") or not b_meta.get("ok"):
            return False, {"error": "a leg symbol is unavailable"}

        # Leg A clip — patient (nothing at risk while resting)
        a_filled, a_vwap = self._send_sliced(
            self.leg_a, symbol_a, side_a, lot_size, comment, style=style,
            timeout=ex.get("LIMIT_TIMEOUT_SEC", 15))
        if a_filled <= EPS:
            return False, {"error": "leg A filled nothing"}

        # Leg B hedge — sized to A's fill and both contract sizes; short patience
        b_step = b_meta.get("volume_step") or 0.01
        hedge_target = sizing.hedge_lots(a_filled, contract_a, contract_b,
                                         hedge_ratio, b_step)
        b_filled, b_vwap = self._send_sliced(
            self.leg_b, symbol_b, side_b, hedge_target, comment, style=style,
            timeout=ex.get("HEDGE_TIMEOUT_SEC", 4))

        if b_filled <= EPS:
            logger.error("Hedge filled nothing — unwinding %.2f leg-A lots", a_filled)
            self._unwind(self.leg_a, symbol_a, side_a, a_filled, comment)
            return False, {"error": "hedge filled nothing"}

        if b_filled < hedge_target - EPS:
            a_step = a_meta.get("volume_step") or 0.01
            matched_a = sizing.hedge_lots(
                b_filled, contract_b, contract_a,
                (1.0 / hedge_ratio) if hedge_ratio else 1.0, a_step)
            min_fraction = ex.get("MIN_MATCHED_FRACTION", 0.0)
            if matched_a < lot_size * min_fraction - EPS:
                logger.warning("Matched %.2f < %.0f%% of %.2f clip — unwinding "
                               "both legs", matched_a, min_fraction * 100, lot_size)
                self._unwind(self.leg_a, symbol_a, side_a, a_filled, comment)
                self._unwind(self.leg_b, symbol_b, side_b, b_filled, comment)
                return False, {"error": f"matched {matched_a:.2f} below "
                               f"{min_fraction:.0%} of clip"}
            excess = a_filled - matched_a
            logger.warning("Hedge partial %.2f/%.2f — unwinding %.2f excess "
                           "leg-A lots, keeping matched", b_filled, hedge_target, excess)
            self._unwind(self.leg_a, symbol_a, side_a, excess, comment)
            a_filled = matched_a

        k = b_filled * contract_b
        entry_spread = ((b_vwap or 0.0) - hedge_ratio * (a_vwap or 0.0))
        report = None
        if reference:
            report = slippage.build(d, False, hedge_ratio, k, side_a, side_b,
                                    reference, a_vwap, b_vwap, symbol_a, symbol_b)
        logger.info("Pair OPEN %s %s — A %.2f@%.2f, B %.2f@%.2f (k=%.2f)",
                    d, direction, a_filled, a_vwap or 0, b_filled, b_vwap or 0, k)
        return True, {
            "direction": d, "leg_a_lots": a_filled, "leg_b_lots": b_filled,
            "leg_a_price": a_vwap, "leg_b_price": b_vwap, "k": k,
            "hedge_ratio": hedge_ratio, "entry_spread": entry_spread,
            "side_a": side_a, "side_b": side_b, "symbol_a": symbol_a,
            "symbol_b": symbol_b, "comment": comment, "slippage": report,
        }

    # ── pair close (netting: plain opposite orders) ───────────────────────────
    def close_pair(self, position, reason=None, reference=None):
        """Flatten both legs. Netting book → opposite orders close/reduce.
        Urgent reasons go straight to market; else limit-first."""
        d = _dir(position.get("direction"))
        urgent = (reason or "").upper() in URGENT_REASONS
        style = "market" if urgent else self.execution.get("ENTRY_STYLE", "market")
        timeout = self.execution.get("EXIT_TIMEOUT_SEC", 15)
        comment = f"BASIS_CX_{uuid.uuid4().hex[:6]}"

        a_filled, a_vwap = self._send_sliced(
            self.leg_a, position["symbol_a"], _opp(position["side_a"]),
            position["leg_a_lots"], comment, style=style, timeout=timeout)
        b_filled, b_vwap = self._send_sliced(
            self.leg_b, position["symbol_b"], _opp(position["side_b"]),
            position["leg_b_lots"], comment, style=style, timeout=timeout)

        a_ok = a_filled >= position["leg_a_lots"] - EPS
        b_ok = b_filled >= position["leg_b_lots"] - EPS
        if not (a_ok and b_ok):
            logger.critical("INCOMPLETE CLOSE: A %.2f/%.2f, B %.2f/%.2f — "
                            "residual exposure, MANUAL INTERVENTION",
                            a_filled, position["leg_a_lots"], b_filled,
                            position["leg_b_lots"])
            return False, {"leg_a_lots": a_filled, "leg_b_lots": b_filled}

        report = None
        if reference:
            report = slippage.build(
                d, True, position.get("hedge_ratio", 1.0), position.get("k", 0.0),
                _opp(position["side_a"]), _opp(position["side_b"]), reference,
                a_vwap, b_vwap, position["symbol_a"], position["symbol_b"])
        return True, {"leg_a_price": a_vwap, "leg_b_price": b_vwap,
                      "leg_a_lots": a_filled, "leg_b_lots": b_filled,
                      "slippage": report}
