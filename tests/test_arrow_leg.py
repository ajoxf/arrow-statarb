"""Arrow leg adapter (the seam) — lot↔unit conversion + interface conformance."""

from arrow_statarb.core.arrow_leg import ArrowLeg
from arrow_statarb.core.scenarios import ScenarioRunner


class FakeArrowBroker:
    """Mimics the Arrow broker surface the adapter uses. Lot size 50; market
    orders fill fully; limits fill if marketable (price crosses the touch)."""
    def __init__(self, price=100.0, lot=50):
        self.price, self.lot = price, lot
        self.orders = {}
        self._n = 0
        self.submitted = []

    def resolve_lot_size(self, seg, sym):
        return self.lot

    def resolve_tick_size(self, seg, sym):
        return 0.05

    def get_quote(self, seg, sym):
        return {"ltp": self.price, "bid": self.price - 0.05, "ask": self.price + 0.05}

    def submit_order(self, symbol, side, quantity, order_type, exchange_segment,
                     product, price=None):
        self._n += 1
        oid = f"O{self._n}"
        self.submitted.append((symbol, side, quantity, order_type, price))
        if order_type == "market":
            filled = quantity
        else:
            marketable = (price >= self.price + 0.05) if side == "buy" \
                else (price <= self.price - 0.05)
            filled = quantity if marketable else 0
        self.orders[oid] = {"filled_qty": filled, "avg_price": self.price,
                            "status": "COMPLETE" if filled else "OPEN"}
        return {"order_id": oid, "status": "submitted"}

    def get_order_status(self, oid):
        o = self.orders.get(oid, {})
        return {"order_id": oid, "status": o.get("status", "UNKNOWN"),
                "filled_qty": o.get("filled_qty", 0), "pending_qty": 0,
                "avg_price": o.get("avg_price", 0.0)}

    def cancel_order(self, oid):
        if oid in self.orders and self.orders[oid]["status"] == "OPEN":
            self.orders[oid]["status"] = "CANCELLED"
        return True

    def amend_order(self, oid, price=None):
        return True


SEG = {"NIFTY30JUN26F": "nse_fo"}


def _leg():
    return ArrowLeg(FakeArrowBroker(), SEG)


def test_ensure_symbol_reports_lot_and_tick():
    meta = _leg().ensure_symbol("NIFTY30JUN26F")
    assert meta["ok"] and meta["lot_size"] == 50 and meta["tick_size"] == 0.05
    assert meta["volume_min"] == 1 and meta["volume_step"] == 1


def test_market_order_converts_lots_to_units_and_back():
    b = FakeArrowBroker(lot=50)
    leg = ArrowLeg(b, SEG)
    r = leg.market_order("NIFTY30JUN26F", "BUY", 2)      # 2 lots
    assert r["ok"] and r["filled_volume"] == 2.0         # reported back in lots
    # broker received units = 2 × 50 = 100
    assert b.submitted[-1][2] == 100


def test_limit_marketable_fills_far_rests():
    leg = _leg()
    tick = leg.tick("NIFTY30JUN26F")
    # marketable buy limit at the ask → fills
    p = leg.place_limit("NIFTY30JUN26F", "BUY", 1, tick["ask"])
    assert p["ok"]
    assert leg.order_state(p["ticket"])["filled_volume"] == 1.0
    # far buy limit → rests unfilled, cancel returns 0 filled
    p2 = leg.place_limit("NIFTY30JUN26F", "BUY", 1, tick["bid"] * 0.9)
    assert leg.order_state(p2["ticket"])["filled_volume"] == 0.0
    assert leg.order_state(p2["ticket"])["still_open"] is True
    assert leg.cancel_order(p2["ticket"])["filled_volume"] == 0.0


def test_tick_returns_bid_ask():
    t = _leg().tick("NIFTY30JUN26F")
    assert t["bid"] < t["ask"]


def test_scenario_round_trip_through_adapter():
    # The ported ScenarioRunner drives the REAL adapter over a fake broker —
    # proving the seam end-to-end (open → close, flat).
    b = FakeArrowBroker()
    leg = ArrowLeg(b, {"A": "nse_fo", "B": "nse_fo"})
    runner = ScenarioRunner(leg, leg, "A", "B")
    r = runner.run("LONG_SPR", "MARKET", "normal")
    assert r["ok"] and "flat" in r["detail"]
