"""Scenario catalogue + round-trip runner (ported, leg-interface, no real orders)."""

from arrow_statarb.core.scenarios import (
    build_catalogue, CATALOGUE, ScenarioRunner, SCENARIO_TYPES,
)


def test_catalogue_is_40_scenarios():
    cat = build_catalogue()
    assert len(cat) == 40
    assert sum(1 for s in cat if s["mode"] == "LIMIT") == 18
    assert sum(1 for s in cat if s["mode"] == "MARKET") == 22   # 18 + 4 partials
    assert sum(1 for s in cat if s["variant"].startswith("partial")) == 4
    assert [s["id"] for s in cat] == list(range(40))


class FakeLeg:
    """Netting leg whose resting limits fill only when MARKETABLE (buy≥ask /
    sell≤bid); market orders fill fully. Lets partial/cancel variants behave."""
    def __init__(self, name, price=100.0):
        self.name, self.price = name, price
        self.orders = []
        self._t = 0
        self._pending = {}

    def ensure_symbol(self, s):
        return {"ok": True, "volume_min": 1.0, "volume_step": 1.0, "point": 0.05}

    def tick(self, s):
        return {"bid": self.price - 0.05, "ask": self.price + 0.05}

    def market_order(self, s, side, lots, comment=""):
        self.orders.append((s, side, lots))
        return {"ok": True, "filled_volume": lots, "price": self.price}

    def place_limit(self, s, side, lots, price, comment=""):
        self._t += 1
        marketable = (price >= self.price + 0.05) if side == "BUY" else (price <= self.price - 0.05)
        self._pending[self._t] = {"lots": lots if marketable else 0.0, "price": price}
        if marketable:
            self.orders.append((s, side, lots))
        return {"ok": True, "ticket": self._t}

    def order_state(self, ticket):
        p = self._pending.get(ticket, {})
        return {"filled_volume": p.get("lots", 0.0), "price": p.get("price"),
                "still_open": p.get("lots", 0.0) <= 0}

    def cancel_order(self, ticket):
        p = self._pending.get(ticket, {})
        return {"filled_volume": p.get("lots", 0.0), "price": p.get("price")}


def _runner():
    return ScenarioRunner(FakeLeg("A"), FakeLeg("B"), "SPOT", "FUT")


def test_single_leg_market_round_trip():
    r = _runner().run("BUY_SPOT", "MARKET", "normal")
    assert r["ok"] and "flat" in r["detail"]


def test_single_leg_limit_round_trip():
    r = _runner().run("SELL_FUT", "LIMIT", "normal")
    assert r["ok"]


def test_limit_cancel_leaves_nothing():
    r = _runner().run("BUY_FUT", "LIMIT", "cancel")
    assert r["ok"] and "cancelled clean" in r["detail"]


def test_spread_round_trip_market():
    r = _runner().run("LONG_SPR", "MARKET", "normal")
    assert r["ok"] and "spread round trip" in r["detail"]


def test_spread_partial_spot_rolls_back():
    # partial_spot: spot fills, futures fails → spot rolled back (expected success)
    r = _runner().run("SHORT_SPR", "MARKET", "partial_spot")
    assert r["ok"]
    assert any(step[0] == "rollback_spot" for step in r["steps"])


def test_spread_partial_futures_rolls_back():
    r = _runner().run("LONG_SPR", "MARKET", "partial_futures")
    assert r["ok"]
    assert any(step[0] == "rollback_fut" for step in r["steps"])


def test_every_catalogue_scenario_runs_without_error():
    run = _runner()
    for s in CATALOGUE:
        out = run.run(s["type"], s["mode"], s["variant"])
        assert "ok" in out and out["type"] == s["type"]
