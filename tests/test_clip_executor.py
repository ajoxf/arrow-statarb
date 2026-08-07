"""Clip/slice execution + hedge-to-fill (ported, netting-adapted)."""

from arrow_statarb.core.clip_executor import ClipExecutor
from arrow_statarb.core.exits import SELL_BASIS, BUY_BASIS


class FakeLeg:
    """Simulates a netting broker leg. ``fill_frac`` per symbol caps how much of
    each order fills (to exercise partial-hedge policy)."""
    def __init__(self, name="arrow", price=100.0, fill_frac=None):
        self.name = name
        self.price = price
        self.fill_frac = fill_frac or {}
        self.orders = []            # (symbol, side, lots) actually sent
        self._t = 1

    def ensure_symbol(self, symbol):
        return {"ok": True, "volume_min": 0.01, "volume_max": 1000.0,
                "volume_step": 0.01, "point": 0.05, "tick_size": 0.05}

    def tick(self, symbol):
        return {"bid": self.price - 0.05, "ask": self.price + 0.05}

    def market_order(self, symbol, side, lots, slippage_points=0, comment=""):
        frac = self.fill_frac.get(symbol, 1.0)
        got = round(lots * frac, 8)
        self.orders.append((symbol, side, got))
        return {"ok": got > 0, "filled_volume": got, "price": self.price}

    # limit path (not exercised in market-style tests)
    def place_limit(self, symbol, side, lots, price, comment=""):
        self._t += 1
        self._pending = {"ticket": self._t, "symbol": symbol, "side": side,
                         "lots": lots, "price": price, "filled": 0.0}
        return {"ok": True, "ticket": self._t}

    def order_state(self, ticket):
        # fills fully on the first poll
        p = self._pending
        p["filled"] = p["lots"]
        self.orders.append((p["symbol"], p["side"], p["lots"]))
        return {"filled_volume": p["lots"], "price": p["price"], "still_open": False}

    def modify_order(self, ticket, price):
        return {"ok": True}

    def cancel_order(self, ticket):
        return {"filled_volume": 0.0, "price": None}

    def pending_orders(self, symbol):
        return []


EX = {"ENTRY_STYLE": "market", "SLIPPAGE_TOLERANCE": 1.0,
      "LIMIT_TIMEOUT_SEC": 5, "HEDGE_TIMEOUT_SEC": 2, "EXIT_TIMEOUT_SEC": 5,
      "MIN_MATCHED_FRACTION": 0.4, "ON_TIMEOUT": "cross"}


def test_full_pair_entry_market():
    leg = FakeLeg()
    ex = ClipExecutor(EX, leg, leg)
    ok, res = ex.execute_pair(BUY_BASIS, "SPOT", "FUT", lot_size=2.0,
                              contract_a=1, contract_b=1, hedge_ratio=1.0)
    assert ok
    # BUY_BASIS → sell spot (leg A), buy futures (leg B)
    assert res["side_a"] == "SELL" and res["side_b"] == "BUY"
    assert res["leg_a_lots"] == 2.0 and res["leg_b_lots"] == 2.0
    assert res["k"] == 2.0                              # lots_b * contract_b


def test_slicing_splits_into_children():
    leg = FakeLeg()
    ex = ClipExecutor(EX, leg, leg, slice_lots=1.0)
    ok, res = ex.execute_pair(SELL_BASIS, "SPOT", "FUT", lot_size=3.0,
                              contract_a=1, contract_b=1)
    assert ok
    # leg A (SPOT) sent in 1-lot children → 3 child orders on SPOT
    spot_children = [o for o in leg.orders if o[0] == "SPOT"]
    assert len(spot_children) == 3 and all(o[2] == 1.0 for o in spot_children)


def test_hedge_partial_keeps_matched_and_unwinds_excess():
    # Futures only fills 50% → matched leg-A = 50% of fill; excess unwound.
    leg = FakeLeg(fill_frac={"FUT": 0.5})
    ex = ClipExecutor(EX, leg, leg)
    ok, res = ex.execute_pair(BUY_BASIS, "SPOT", "FUT", lot_size=2.0,
                              contract_a=1, contract_b=1, hedge_ratio=1.0)
    assert ok
    assert abs(res["leg_b_lots"] - 1.0) < 1e-6         # hedge got 1 of 2
    assert abs(res["leg_a_lots"] - 1.0) < 1e-6         # leg A trimmed to match
    # an unwind (opposite side) was sent on SPOT for the excess
    assert any(o[0] == "SPOT" and o[1] == "SELL" for o in leg.orders) or \
           any(o[0] == "SPOT" and o[1] == "BUY" for o in leg.orders)


def test_hedge_below_min_fraction_unwinds_both():
    # Futures fills only 10% → matched below MIN_MATCHED_FRACTION 0.4 → fail.
    leg = FakeLeg(fill_frac={"FUT": 0.1})
    ex = ClipExecutor(EX, leg, leg)
    ok, res = ex.execute_pair(BUY_BASIS, "SPOT", "FUT", lot_size=2.0,
                              contract_a=1, contract_b=1, hedge_ratio=1.0)
    assert ok is False and "below" in res["error"]


def test_hedge_nothing_unwinds_spot():
    leg = FakeLeg(fill_frac={"FUT": 0.0})
    ex = ClipExecutor(EX, leg, leg)
    ok, res = ex.execute_pair(BUY_BASIS, "SPOT", "FUT", lot_size=2.0,
                              contract_a=1, contract_b=1)
    assert ok is False and "hedge filled nothing" in res["error"]


def test_close_pair_sends_opposite_orders():
    leg = FakeLeg()
    ex = ClipExecutor(EX, leg, leg)
    ok, res = ex.execute_pair(BUY_BASIS, "SPOT", "FUT", lot_size=1.0,
                              contract_a=1, contract_b=1)
    leg.orders.clear()
    ok2, cres = ex.close_pair(res, reason="TAKE_PROFIT")
    assert ok2
    # opposite of entry: entry was SELL spot / BUY fut → close BUY spot / SELL fut
    assert ("SPOT", "BUY", 1.0) in leg.orders
    assert ("FUT", "SELL", 1.0) in leg.orders


def test_limit_style_pegs_then_fills():
    leg = FakeLeg()
    ex = ClipExecutor(dict(EX, ENTRY_STYLE="limit"), leg, leg,
                      clock=lambda: 0.0, sleep=lambda s: None)
    ok, res = ex.execute_pair(BUY_BASIS, "SPOT", "FUT", lot_size=1.0,
                              contract_a=1, contract_b=1)
    assert ok and res["leg_a_lots"] == 1.0
