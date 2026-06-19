"""SimBroker + ExecutionLog + the live_sim execution path."""

from arrow_statarb.brokers.sim_broker import SimBroker
from arrow_statarb.core.execution_log import ExecutionLog, _slippage
from arrow_statarb.core.executor import SpreadExecutor, LegOrder


def _legs():
    return [LegOrder("nse_fo", "AAA", "buy", 75), LegOrder("nse_fo", "BBB", "sell", 75)]


PARAMS = {"use_limit_orders": True, "limit_offset_pct": 0.05, "amend_step_pct": 0.05,
          "fill_timeout_sec": 1.0, "amend_interval_sec": 0.1, "poll_interval_sec": 0.02,
          "limit_to_market": True, "verify_flat_before_entry": True, "product": "NRML"}


def _exec(sim):
    return SpreadExecutor(
        broker_fn=lambda: sim,
        price_fn=lambda seg, sym: sim.get_streamed_ltp([sym]).get(sym.upper()),
        params_fn=lambda: dict(PARAMS))


def test_sim_both_legs_fill_with_slippage():
    sim = SimBroker(slow_prob=0, reject_prob=0, orphan_prob=0, seed=1)
    res = _exec(sim).execute(_legs())
    assert res["success"] is True and res["orphan"] is False
    # both legs report a fill price near the reference (slipped slightly)
    for r in res["results"]:
        assert r["avg_price"] > 0 and r["ref_price"] > 0
    assert res["elapsed_sec"] is not None


def test_sim_orphan_triggers_recovery():
    # BBB never fills (even on market) → orphan; AAA fills and must be flattened.
    sim = SimBroker(slow_prob=0, orphan_symbols={"BBB"}, seed=2)
    res = _exec(sim).execute(_legs())
    assert res["success"] is False
    assert res["orphan"] is True
    assert res["recovered"] is True
    # AAA (the filled leg) is net-zero after recovery flattened it
    assert all(p["symbol"] != "AAA" for p in sim.get_positions())


def test_execution_log_records_slippage():
    sim = SimBroker(slow_prob=0, seed=3)
    res = _exec(sim).execute(_legs(), label="Order")
    log = ExecutionLog()
    ev = log.record(res, "Order", "live_sim")
    assert ev["mode"] == "live_sim"
    assert len(ev["legs"]) == 2
    assert log.stats()["count"] == 1
    # slippage is computed for filled legs
    assert any(l["slippage_pct"] is not None for l in ev["legs"])


def test_slippage_sign():
    # buy filled above ref = adverse (positive); sell below ref = adverse (positive)
    sp, _ = _slippage("buy", 100.0, 101.0)
    assert sp == 1.0
    sp2, _ = _slippage("sell", 100.0, 99.0)
    assert sp2 == 1.0
    sp3, _ = _slippage("buy", 100.0, 99.0)
    assert sp3 == -1.0
