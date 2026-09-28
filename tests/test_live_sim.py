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


# ── fills & slippage: timeline, ₹, persistence ───────────────────────────────

def _res(ref_a, fill_a, ref_b, fill_b, t0=1000.0):
    return {"success": True, "elapsed_sec": 0.9, "results": [
        {"symbol": "CRUDEOIL19OCT26F", "side": "buy", "order_type": "limit",
         "status": "COMPLETE", "units": 2, "filled": 2,
         "ref_price": ref_a, "avg_price": fill_a,
         "sent_at": t0, "acked_at": t0 + 0.05, "filled_at": t0 + 0.30},
        {"symbol": "CRUDEOIL18DEC26F", "side": "sell", "order_type": "limit",
         "status": "COMPLETE", "units": 2, "filled": 2,
         "ref_price": ref_b, "avg_price": fill_b,
         "sent_at": t0 + 0.01, "acked_at": t0 + 0.06, "filled_at": t0 + 0.75},
    ]}


def _mcx_scale(_leg):
    # MCX: broker lot = 1 unit, a point is worth ₹100 per lot (CRUDEOIL)
    return {"inr_per_point": 100.0, "lot_size": 1}


def test_fills_record_timeline_and_rupee_slippage(tmp_path):
    log = ExecutionLog(path=tmp_path / "x.jsonl", leg_scale=_mcx_scale)
    ev = log.record(_res(9111.0, 9112.0, 8522.0, 8520.0), "Order", "live")
    a, b = ev["legs"]
    assert a["slippage"] == 1.0 and a["slippage_inr"] == 200.0   # 1 pt × 2 lots × ₹100
    assert b["slippage"] == 2.0 and b["slippage_inr"] == 400.0   # sold 2 pts lower
    assert a["fill_ms"] == 300 and b["fill_ms"] == 740 and a["ack_ms"] == 50
    assert ev["leg_gap_ms"] == 450 and ev["total_ms"] == 750
    assert ev["slippage_inr"] == 600.0
    st = log.stats()
    # per ORDER per LOT: (200/2 + 400/2) / 2 = ₹150 — comparable with slippage_per_lot
    assert st["avg_slippage_inr_per_lot_order"] == 150.0
    assert st["avg_leg_gap_ms"] == 450


def test_fills_survive_a_restart(tmp_path):
    path = tmp_path / "x.jsonl"
    ExecutionLog(path=path, leg_scale=_mcx_scale).record(
        _res(9111.0, 9112.0, 8522.0, 8520.0), "Order", "live")
    again = ExecutionLog(path=path, leg_scale=_mcx_scale)
    assert len(again.all()) == 1
    assert again.stats()["avg_slippage_inr_per_lot_order"] == 150.0


def test_price_improvement_is_negative_slippage(tmp_path):
    log = ExecutionLog(leg_scale=_mcx_scale)
    ev = log.record(_res(9111.0, 9110.0, 8522.0, 8523.0), "Close", "live")
    assert [l["slippage_inr"] for l in ev["legs"]] == [-200.0, -200.0]


def test_rupees_left_blank_when_scale_unknown():
    ev = ExecutionLog().record(_res(9111.0, 9112.0, 8522.0, 8520.0), "Order", "live")
    assert ev["legs"][0]["slippage_inr"] is None and ev["legs"][0]["slippage"] == 1.0
