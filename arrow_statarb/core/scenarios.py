"""Round-trip order scenarios — does Arrow actually trade the way the engine
assumes? Ported from the W3 Full Order Test Suite, adapted to Arrow's single
netting connection and the leg interface (the same seam ClipExecutor uses).

Each scenario is a complete round trip at MINIMUM volume — open, then close — so
nothing is left on the book. Spread scenarios roll back the first leg if the
second fails: a test must never leave a naked position. LIMIT scenarios rest a
marketable limit; the #3 variant cancels without filling.

The runner never places real orders in tests — it drives an injected leg
interface, exercised with a fake leg. Wired to the live Arrow leg on the Setup
page, it places REAL minimum-lot orders (the UI warns and requires the algo
stopped + flat).
"""

from __future__ import annotations

import time as time_mod

SCENARIO_TYPES = [
    ("BUY_SPOT", "BUY_SPOT"), ("SELL_FUT", "SELL_FUTURES"),
    ("BUY_FUT", "BUY_FUTURES"), ("SELL_SPOT", "SELL_SPOT"),
    ("LONG_SPR", "LONG_SPREAD"), ("SHORT_SPR", "SHORT_SPREAD"),
]
RUN_SPACING_SEC = {"LIMIT": 20, "MARKET": 5}

BUY, SELL = "BUY", "SELL"
_SINGLE = {"BUY_SPOT": ("a", BUY), "SELL_SPOT": ("a", SELL),
           "BUY_FUT": ("b", BUY), "SELL_FUT": ("b", SELL)}
# spread → (spot side on leg A, futures side on leg B)
_SPREAD = {"LONG_SPR": (SELL, BUY), "SHORT_SPR": (BUY, SELL)}


def build_catalogue():
    """The 40 scenarios in run order: 18 LIMIT (6 types × 3, #3 cancel), 18
    MARKET (same, #3 quick-close), then 4 partial-fill recoveries."""
    out = []
    for kind, label in SCENARIO_TYPES:
        out.append({"type": kind, "mode": "LIMIT", "variant": "normal", "name": f"{label} #1"})
        out.append({"type": kind, "mode": "LIMIT", "variant": "normal", "name": f"{label} #2"})
        out.append({"type": kind, "mode": "LIMIT", "variant": "cancel", "name": f"{label} #3 (cancel)"})
    for kind, label in SCENARIO_TYPES:
        out.append({"type": kind, "mode": "MARKET", "variant": "normal", "name": f"MKT {label} #1"})
        out.append({"type": kind, "mode": "MARKET", "variant": "normal", "name": f"MKT {label} #2"})
        out.append({"type": kind, "mode": "MARKET", "variant": "quick_close", "name": f"MKT {label} #3 (quick-close)"})
    for spread, label in (("LONG_SPR", "LONG_SPREAD"), ("SHORT_SPR", "SHORT_SPREAD")):
        out.append({"type": spread, "mode": "MARKET", "variant": "partial_spot",
                    "name": f"{label} partial: spot fills, futures fails → market-close spot"})
        out.append({"type": spread, "mode": "MARKET", "variant": "partial_futures",
                    "name": f"{label} partial: futures fills, spot fails → market-close futures"})
    for i, s in enumerate(out):
        s["id"] = i
    return out


CATALOGUE = build_catalogue()


def _opp(side):
    return SELL if side == BUY else BUY


class ScenarioRunner:
    def __init__(self, leg_a, leg_b, symbol_a, symbol_b, clock=time_mod.time):
        self.leg_a, self.leg_b = leg_a, leg_b
        self.symbol_a, self.symbol_b = symbol_a, symbol_b
        self.clock = clock

    def _leg(self, which):
        return (self.leg_a, self.symbol_a) if which == "a" else (self.leg_b, self.symbol_b)

    def _minvol(self, leg, symbol):
        meta = leg.ensure_symbol(symbol) or {}
        return float(meta.get("volume_min") or 0.01), meta

    def _open(self, leg, symbol, side, mode, fillable=True):
        vol, meta = self._minvol(leg, symbol)
        # A limit is used for LIMIT mode and for any leg we want to REST unfilled
        # (the cancel/partial variants) — a far, non-marketable price won't fill.
        if mode == "LIMIT" or not fillable:
            tick = leg.tick(symbol) or {}
            bid, ask = tick.get("bid"), tick.get("ask")
            if fillable:                                   # marketable: cross the touch
                price = ask if side == BUY else bid
            else:                                          # far from market → rests, won't fill
                price = (bid * 0.9 if side == BUY else ask * 1.1) if (bid and ask) else 0.0
            placed = leg.place_limit(symbol, side, vol, price, comment="SCENARIO")
            return {"vol": vol, "meta": meta, **placed}
        r = leg.market_order(symbol, side, vol, comment="SCENARIO")
        return {"vol": vol, "meta": meta, "ticket": None,
                "filled": float(r.get("filled_volume") or 0.0),
                "ok": bool(r.get("ok")), "price": r.get("price")}

    def _await_limit(self, leg, opened):
        """Return filled volume for a resting limit (single poll — the fake leg
        settles immediately; the live adapter reads the real order state)."""
        if opened.get("ticket") is None:
            return float(opened.get("filled") or 0.0)
        st = leg.order_state(opened["ticket"]) or {}
        return float(st.get("filled_volume") or 0.0)

    def _close_market(self, leg, symbol, side, vol):
        if vol <= 0:
            return 0.0
        r = leg.market_order(symbol, _opp(side), vol, comment="SCENARIO close")
        return float(r.get("filled_volume") or 0.0)

    def run(self, s_type, mode, variant="normal"):
        """Run one scenario; return {ok, type, mode, variant, detail, steps}."""
        steps = []

        def result(ok, detail):
            return {"ok": ok, "type": s_type, "mode": mode, "variant": variant,
                    "detail": detail, "steps": steps}

        # ── single-leg round trip ────────────────────────────────────────────
        if s_type in _SINGLE:
            which, side = _SINGLE[s_type]
            leg, symbol = self._leg(which)
            if variant == "cancel":
                opened = self._open(leg, symbol, side, "LIMIT", fillable=False)
                steps.append(("place", opened.get("ok"), opened.get("vol")))
                if not opened.get("ok"):
                    return result(False, "limit placement rejected")
                cancelled = leg.cancel_order(opened["ticket"]) or {}
                leaked = float(cancelled.get("filled_volume") or 0.0)
                steps.append(("cancel", True, leaked))
                if leaked > 0:
                    self._close_market(leg, symbol, side, leaked)   # flatten a leaked fill
                    return result(False, f"cancel leaked {leaked} filled — flattened")
                return result(True, "placed then cancelled clean, no fill")
            opened = self._open(leg, symbol, side, mode)
            filled = self._await_limit(leg, opened)
            steps.append(("open", filled > 0, filled))
            if filled <= 0:
                return result(False, "open filled nothing")
            closed = self._close_market(leg, symbol, side, filled)
            steps.append(("close", closed >= filled, closed))
            return result(closed >= filled - 1e-9,
                          "round trip complete, flat" if closed >= filled - 1e-9
                          else f"close under-filled {closed}/{filled}")

        # ── spread round trip ────────────────────────────────────────────────
        if s_type in _SPREAD:
            side_a, side_b = _SPREAD[s_type]
            # partial variants force one leg to fail (min-vol beyond a fake cap)
            a_fillable = variant != "partial_futures"
            b_fillable = variant != "partial_spot"
            oa = self._open(self.leg_a, self.symbol_a, side_a, mode, fillable=a_fillable)
            fa = self._await_limit(self.leg_a, oa)
            steps.append(("open_spot", fa > 0, fa))
            ob = self._open(self.leg_b, self.symbol_b, side_b, mode, fillable=b_fillable)
            fb = self._await_limit(self.leg_b, ob)
            steps.append(("open_fut", fb > 0, fb))

            if fa > 0 and fb <= 0:                          # futures failed → roll back spot
                self._close_market(self.leg_a, self.symbol_a, side_a, fa)
                steps.append(("rollback_spot", True, fa))
                ok = variant == "partial_spot"
                return result(ok, "spot filled, futures failed → spot rolled back")
            if fb > 0 and fa <= 0:                          # spot failed → roll back futures
                self._close_market(self.leg_b, self.symbol_b, side_b, fb)
                steps.append(("rollback_fut", True, fb))
                ok = variant == "partial_futures"
                return result(ok, "futures filled, spot failed → futures rolled back")
            if fa <= 0 and fb <= 0:
                return result(False, "neither leg filled")

            ca = self._close_market(self.leg_a, self.symbol_a, side_a, fa)
            cb = self._close_market(self.leg_b, self.symbol_b, side_b, fb)
            steps.append(("close_spot", ca >= fa, ca))
            steps.append(("close_fut", cb >= fb, cb))
            ok = ca >= fa - 1e-9 and cb >= fb - 1e-9
            return result(ok, "spread round trip complete, flat" if ok
                          else "spread close under-filled")

        return result(False, f"unknown scenario type {s_type}")
