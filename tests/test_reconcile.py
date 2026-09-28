"""Self-healing reconciliation guard + untracked-close ledger."""

from arrow_statarb.core.reconcile import ReconcileGuard
from arrow_statarb.core.untracked_ledger import UntrackedLedger


def _guard(in_trade, positions, auto_close=False, threshold=3):
    calls = {"clear": 0, "flatten": []}
    g = ReconcileGuard(
        engine_in_trade=lambda: in_trade["v"],
        exchange_bot_positions=lambda: positions["v"],
        clear_engine=lambda: (calls.__setitem__("clear", calls["clear"] + 1) or True),
        flatten_leg=lambda p: calls["flatten"].append(p["symbol"]),
        threshold=threshold, auto_close=auto_close)
    return g, calls


def test_ok_when_states_agree():
    g, calls = _guard({"v": True}, {"v": [{"symbol": "A", "net_quantity": 65}]})
    assert g.check()["state"] == "ok"          # engine in-trade, exchange has position
    g2, _ = _guard({"v": False}, {"v": [{"symbol": "A", "net_quantity": 0}]})
    assert g2.check()["state"] == "ok"          # both flat


def test_engine_ghost_clears_after_threshold():
    g, calls = _guard({"v": True}, {"v": [{"symbol": "A", "net_quantity": 0}]}, threshold=3)
    g.check(); g.check()
    assert calls["clear"] == 0                   # not yet
    res = g.check()
    assert res["acted"] == "cleared_engine" and calls["clear"] == 1


def test_exchange_orphan_alerts_when_auto_close_off():
    g, calls = _guard({"v": False}, {"v": [{"symbol": "A", "net_quantity": 65}]},
                      auto_close=False, threshold=2)
    g.check()
    res = g.check()
    assert res["acted"] == "orphan_detected_alert"
    assert calls["flatten"] == []                # no real orders when auto_close off


def test_exchange_orphan_auto_closes_when_enabled():
    g, calls = _guard({"v": False}, {"v": [{"symbol": "A", "net_quantity": 65},
                                           {"symbol": "B", "net_quantity": -65}]},
                      auto_close=True, threshold=2)
    g.check()
    res = g.check()
    assert res["acted"] == "closed_orphan"
    assert calls["flatten"] == ["A", "B"]        # both bot legs flattened


def test_mismatch_counter_resets_on_agreement():
    positions = {"v": [{"symbol": "A", "net_quantity": 0}]}
    g, calls = _guard({"v": True}, positions, threshold=3)
    g.check(); g.check()                         # 2 ghost mismatches
    positions["v"] = [{"symbol": "A", "net_quantity": 65}]  # now agree (in-trade + pos)
    g.check()
    assert g.last["state"] == "ok"
    positions["v"] = [{"symbol": "A", "net_quantity": 0}]   # ghost again — counter restarts
    g.check(); g.check()
    assert calls["clear"] == 0                   # only 2 → not yet at threshold


def test_untracked_ledger_records_and_sums(tmp_path):
    led = UntrackedLedger(tmp_path / "u.json")
    assert led.total() == 0.0
    led.record(reason="orphan_auto_close", symbol="NIFTY30JUN26F", qty=65, est_cost=468.0)
    led.record(reason="engine_ghost_cleared", est_cost=0.0)
    assert led.total() == 468.0
    assert led.day_cost() == 468.0               # both today
    assert len(led.all()) == 2
    assert led.all()[0]["reason"] == "engine_ghost_cleared"   # newest first


def test_untracked_ledger_persists(tmp_path):
    p = tmp_path / "u.json"
    UntrackedLedger(p).record(reason="x", est_cost=10.0)
    assert UntrackedLedger(p).total() == 10.0    # reloaded from disk
