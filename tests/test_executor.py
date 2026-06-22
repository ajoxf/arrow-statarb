"""SpreadExecutor — the LIVE order-execution safety net.

Exercises every scenario from the safety review: limit placement, fill polling,
limit amendment, limit→market escalation, orphan detection + recovery, the
pre-entry verify-flat gate, and graceful degradation when the broker exposes no
order-status API.
"""

import pytest

from arrow_statarb.core.executor import SpreadExecutor, LegOrder


class FakeClock:
    """Deterministic clock — time only advances when the executor sleeps, so
    timeouts resolve instantly and reproducibly."""
    def __init__(self):
        self.t = 0.0

    def now(self):
        return self.t

    def sleep(self, dt):
        self.t += max(float(dt), 0.001)


class MockBroker:
    """Configurable broker double covering the methods the executor calls."""
    def __init__(self, status_unknown=False):
        self.counter = 0
        self.orders = {}
        self.poll_counts = {}
        self.status_unknown = status_unknown
        # behaviour switches keyed by symbol
        self.reject_symbols = set()    # submit_order returns an error
        self.fill_after = {}           # symbol -> polls before COMPLETE (default 0)
        self.never_fill = set()        # stays OPEN forever…
        self.fill_on_market = set()    # …unless re-placed as a market order
        self.partial_fill = {}         # symbol -> units filled on a resting limit
        # recorders
        self.submits = []
        self.amends = []
        self.cancels = []
        self.positions = []

    def submit_order(self, symbol, side, quantity, order_type="market", price=None,
                     exchange_segment="", product="NRML", token=""):
        self.submits.append({"symbol": symbol, "side": side, "quantity": quantity,
                             "order_type": order_type, "price": price})
        if symbol in self.reject_symbols:
            return {"status": "error", "message": f"{symbol} rejected", "order_id": None}
        self.counter += 1
        oid = f"OID{self.counter}"
        self.orders[oid] = {"symbol": symbol, "side": side, "units": quantity,
                            "order_type": order_type, "price": price,
                            "status": "PENDING", "filled": 0, "avg": 0.0}
        return {"order_id": oid, "status": "submitted", "symbol": symbol}

    def get_order_status(self, order_id):
        if self.status_unknown:
            return {"order_id": order_id, "status": "UNKNOWN", "filled_qty": 0,
                    "pending_qty": 0, "avg_price": 0.0, "raw": {}}
        o = self.orders[order_id]
        self.poll_counts[order_id] = self.poll_counts.get(order_id, 0) + 1
        sym = o["symbol"]
        if o["status"] in ("REJECTED", "CANCELLED", "COMPLETE"):
            pass
        elif sym in self.never_fill:
            if o["order_type"] == "market" and sym in self.fill_on_market:
                o.update(status="COMPLETE", filled=o["units"], avg=o["price"] or 100.0)
            elif sym in self.partial_fill and o["order_type"] != "market":
                o.update(status="PARTIAL", filled=self.partial_fill[sym], avg=o["price"] or 100.0)
            else:
                o["status"] = "OPEN"
        elif self.poll_counts[order_id] > self.fill_after.get(sym, 0):
            o.update(status="COMPLETE", filled=o["units"], avg=o["price"] or 100.0)
        else:
            o["status"] = "OPEN"
        return {"order_id": order_id, "status": o["status"], "filled_qty": o["filled"],
                "pending_qty": o["units"] - o["filled"], "avg_price": o["avg"], "raw": o}

    def amend_order(self, order_id, price=None, quantity=None, order_type=None):
        self.amends.append({"order_id": order_id, "price": price})
        if order_id in self.orders and price is not None:
            self.orders[order_id]["price"] = price
        return True

    def cancel_order(self, order_id):
        self.cancels.append(order_id)
        if order_id in self.orders:
            self.orders[order_id]["status"] = "CANCELLED"
        return True

    def get_positions(self):
        return self.positions


# ── helpers ──────────────────────────────────────────────────────────────────
PARAMS = {
    "use_limit_orders": True, "limit_offset_pct": 0.05, "amend_step_pct": 0.05,
    "fill_timeout_sec": 1.0, "amend_interval_sec": 0.2, "poll_interval_sec": 0.1,
    "limit_to_market": True, "verify_flat_before_entry": True, "product": "NRML",
}


def _legs():
    return [LegOrder("nse_fo", "AAA", "buy", 75),
            LegOrder("nse_fo", "BBB", "sell", 75)]


def _executor(broker, params=None, price=100.0):
    fc = FakeClock()
    return SpreadExecutor(
        broker_fn=lambda: broker,
        price_fn=lambda seg, sym: price,
        params_fn=lambda: dict(params or PARAMS),
        clock=fc.now, sleep=fc.sleep,
    )


# ── happy path: both legs as limits, both fill ───────────────────────────────
def test_both_legs_fill_as_limit():
    b = MockBroker()
    res = _executor(b).execute(_legs(), label="Order")
    assert res["success"] is True
    assert res["orphan"] is False
    # both placed as LIMIT with a marketable price (buy above / sell below LTP)
    assert {s["order_type"] for s in b.submits} == {"limit"}
    buy = next(s for s in b.submits if s["side"] == "buy")
    sell = next(s for s in b.submits if s["side"] == "sell")
    assert buy["price"] > 100.0 and sell["price"] < 100.0


def test_market_mode_when_limits_disabled():
    b = MockBroker()
    p = dict(PARAMS, use_limit_orders=False)
    res = _executor(b, p).execute(_legs())
    assert res["success"] is True
    assert {s["order_type"] for s in b.submits} == {"market"}


# ── amendment: a slow limit gets re-priced toward the market ─────────────────
def test_slow_limit_is_amended_then_fills():
    b = MockBroker()
    b.fill_after = {"AAA": 3, "BBB": 3}      # need a few polls → amendments fire
    res = _executor(b).execute(_legs())
    assert res["success"] is True
    assert len(b.amends) >= 1                 # the resting limit was chased


# ── timeout escalation: limit never fills, re-placed as market ───────────────
def test_limit_times_out_and_escalates_to_market():
    b = MockBroker()
    b.never_fill = {"AAA", "BBB"}
    b.fill_on_market = {"AAA", "BBB"}         # only fill once re-sent as market
    res = _executor(b).execute(_legs())
    assert res["success"] is True
    assert b.cancels                          # limits were cancelled
    assert any(s["order_type"] == "market" for s in b.submits)


# ── ORPHAN: one leg rejected at placement → filled leg is flattened ──────────
def test_orphan_on_rejection_recovers_by_flattening():
    b = MockBroker()
    b.reject_symbols = {"BBB"}                # leg B never makes it to market
    res = _executor(b).execute(_legs())
    assert res["success"] is False
    assert res["orphan"] is True
    assert res["recovered"] is True
    # the filled buy on AAA must be flattened by an opposing market sell
    recovery = [s for s in b.submits if s["symbol"] == "AAA"
                and s["side"] == "sell" and s["order_type"] == "market"]
    assert len(recovery) == 1


# ── ORPHAN: one leg never fills (even on market) → filled leg flattened ───────
def test_orphan_on_no_fill_recovers():
    b = MockBroker()
    b.never_fill = {"BBB"}                    # B stays unfilled through escalation
    res = _executor(b).execute(_legs())
    assert res["success"] is False
    assert res["orphan"] is True
    assert res["recovered"] is True
    assert any(s["symbol"] == "AAA" and s["side"] == "sell"
               and s["order_type"] == "market" for s in b.submits)


# ── pre-entry verify: existing leg position blocks the entry ─────────────────
def test_verify_flat_blocks_when_position_exists():
    b = MockBroker()
    b.positions = [{"symbol": "AAA", "net_quantity": 75}]
    res = _executor(b).execute(_legs(), verify_flat=True)
    assert res["success"] is False
    assert "verify_exchange_position" in res["error"]
    assert b.submits == []                    # nothing was sent


def test_verify_flat_allows_when_flat():
    b = MockBroker()
    res = _executor(b).execute(_legs(), verify_flat=True)
    assert res["success"] is True


# ── transient UNKNOWN status must NOT be assumed a fill (fail-safe default) ────
def test_unknown_status_fails_safe_not_phantom_fill():
    b = MockBroker(status_unknown=True)
    res = _executor(b).execute(_legs())
    # default: a persistent/transient UNKNOWN never fabricates a fill → no
    # phantom "filled" success; the trade fails safe instead.
    assert res["success"] is False
    assert all(not r["status"] == "COMPLETE" for r in res["results"])


# ── opt-in legacy mode: a broker with NO status API may assume the fill ───────
def test_assume_fill_on_unknown_opt_in():
    b = MockBroker(status_unknown=True)
    p = dict(PARAMS, assume_fill_on_unknown=True, unknown_status_grace_polls=1)
    res = _executor(b, p).execute(_legs())
    assert res["success"] is True
    assert all(r["unconfirmed"] for r in res["results"])


# ── partial fill on a limit must NOT be doubled on market escalation ──────────
def test_partial_fill_escalation_orders_only_residual():
    b = MockBroker()
    # AAA partially fills (30 of 75) on the limit, then needs escalation.
    b.never_fill = {"AAA"}
    b.fill_on_market = {"AAA"}
    b.partial_fill = {"AAA": 30}              # limit fills 30, rests
    b.fill_after = {"BBB": 0}
    res = _executor(b).execute(_legs())
    assert res["success"] is True
    # the escalation market order for AAA must be for the RESIDUAL 45, not 75
    aaa_market = [s for s in b.submits if s["symbol"] == "AAA" and s["order_type"] == "market"]
    assert aaa_market and aaa_market[-1]["quantity"] == 45


def test_no_broker_fails_cleanly():
    ex = SpreadExecutor(broker_fn=lambda: None,
                        price_fn=lambda s, y: 100.0, params_fn=lambda: dict(PARAMS))
    res = ex.execute(_legs())
    assert res["success"] is False
    assert "broker" in res["error"].lower()


# ── verify_flat fail-safe when positions can't be read (Arrow timeout) ────────
def test_verify_flat_fail_closed_blocks_when_positions_unavailable():
    b = MockBroker()
    fc = FakeClock()
    ex = SpreadExecutor(broker_fn=lambda: b, price_fn=lambda s, y: 100.0,
                        params_fn=lambda: dict(PARAMS, verify_flat_fail_open=False),
                        positions_fn=lambda: None,      # read failing
                        clock=fc.now, sleep=fc.sleep)
    res = ex.execute(_legs(), verify_flat=True)
    assert res["success"] is False
    assert "positions unavailable" in res["error"]
    assert b.submits == []                              # nothing was placed


def test_verify_flat_fail_open_allows_when_configured():
    b = MockBroker()
    fc = FakeClock()
    ex = SpreadExecutor(broker_fn=lambda: b, price_fn=lambda s, y: 100.0,
                        params_fn=lambda: dict(PARAMS, verify_flat_fail_open=True),
                        positions_fn=lambda: None,
                        clock=fc.now, sleep=fc.sleep)
    res = ex.execute(_legs(), verify_flat=True)
    assert res["success"] is True


def test_verify_flat_uses_cached_positions():
    b = MockBroker()
    fc = FakeClock()
    ex = SpreadExecutor(broker_fn=lambda: b, price_fn=lambda s, y: 100.0,
                        params_fn=lambda: dict(PARAMS),
                        positions_fn=lambda: [{"symbol": "AAA", "net_quantity": 75}],
                        clock=fc.now, sleep=fc.sleep)
    res = ex.execute(_legs(), verify_flat=True)
    assert res["success"] is False
    assert "existing position" in res["error"]          # AAA already held → blocked
