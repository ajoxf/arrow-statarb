"""Working (resting) spread orders: GTC, watch-and-fire on the live spread."""

import json

from arrow_statarb.core.working_orders import WorkingOrders


class Market:
    def __init__(self, sell=100.0, buy=104.0, fresh=True):
        self.sell, self.buy, self.fresh = sell, buy, fresh

    def __call__(self):
        return self.sell, self.buy, self.fresh


def _book(tmp_path, market, gate=None, fire=None):
    sent = []

    def default_fire(o, how):
        sent.append((o["side"], o["lots"], how))
        return {"success": True, "message": "ok"}

    wo = WorkingOrders(tmp_path / "wo.json", market, gate or (lambda o: (True, "open")),
                       fire or default_fire)
    return wo, sent


def test_sell_waits_above_the_market_and_fires_when_the_sell_spread_reaches_it(tmp_path):
    m = Market(sell=100.0, buy=104.0)
    wo, sent = _book(tmp_path, m)
    o = wo.place("SELL", 110.0, 2)
    assert wo.check_once() == [] and sent == []
    m.sell = 109.9
    wo.check_once()
    assert sent == []
    m.sell = 110.0                                   # Sell spread reached the order's price
    wo.check_once()
    assert sent == [("SELL", 2, "open")]
    assert [x for x in wo.all() if x["id"] == o["id"]][0]["status"] == "filled"
    m.sell = 111.0
    wo.check_once()
    assert len(sent) == 1                            # sent once, never again


def test_buy_waits_below_the_market(tmp_path):
    m = Market(sell=100.0, buy=104.0)
    wo, sent = _book(tmp_path, m)
    wo.place("BUY", 98.0, 1)
    wo.check_once()
    assert sent == []
    m.buy = 98.0
    wo.check_once()
    assert sent == [("BUY", 1, "open")]


def test_an_order_at_or_through_the_market_fires_at_once(tmp_path):
    wo, sent = _book(tmp_path, Market(sell=100.0, buy=104.0))
    wo.place("SELL", 95.0, 1)                         # below the Sell spread: marketable
    wo.place("BUY", 105.0, 1)                         # above the Buy spread: marketable
    wo.check_once()
    assert {s for s, _, _ in sent} == {"SELL", "BUY"}


def test_nothing_fires_on_stale_quotes_or_a_closed_market(tmp_path):
    m = Market(sell=120.0, buy=124.0, fresh=False)
    wo, sent = _book(tmp_path, m)
    wo.place("SELL", 110.0, 1)
    wo.check_once()
    assert sent == []
    m.fresh = True                                   # control: same prices, fresh → fires
    wo.check_once()
    assert len(sent) == 1


def test_a_blocked_order_is_held_not_dropped_and_fires_later(tmp_path):
    gate = {"ok": False}
    wo, sent = _book(tmp_path, Market(sell=120.0),
                     gate=lambda o: (True, "open") if gate["ok"] else (False, "held — a position is open"))
    o = wo.place("SELL", 110.0, 1)
    wo.check_once()
    st = [x for x in wo.active() if x["id"] == o["id"]][0]
    assert st["status"] == "held" and "position is open" in st["note"] and sent == []
    gate["ok"] = True
    wo.check_once()
    assert sent == [("SELL", 1, "open")]


def test_a_rejected_order_is_never_resent(tmp_path):
    calls = []

    def fire(o, how):
        calls.append(o["id"])
        return {"success": False, "error": "RMS: margin shortfall"}

    wo, _ = _book(tmp_path, Market(sell=120.0), fire=fire)
    o = wo.place("SELL", 110.0, 1)
    for _ in range(3):
        wo.check_once()
    assert calls == [o["id"]]
    rec = [x for x in wo.all() if x["id"] == o["id"]][0]
    assert rec["status"] == "rejected" and "margin shortfall" in rec["note"]


def test_a_momentary_clash_is_retried(tmp_path):
    answers = [{"success": False, "error": "Another manual order is still executing."},
               {"success": True}]
    wo, _ = _book(tmp_path, Market(sell=120.0), fire=lambda o, how: answers.pop(0))
    o = wo.place("SELL", 110.0, 1)
    wo.check_once()
    assert [x for x in wo.all() if x["id"] == o["id"]][0]["status"] == "working"
    wo.check_once()
    assert [x for x in wo.all() if x["id"] == o["id"]][0]["status"] == "filled"


def test_several_orders_and_cancel(tmp_path):
    wo, sent = _book(tmp_path, Market(sell=100.0, buy=104.0))
    a = wo.place("SELL", 110.0, 1)
    b = wo.place("SELL", 115.0, 1)
    c = wo.place("BUY", 95.0, 1)
    assert len(wo.active()) == 3
    assert wo.cancel(b["id"])["status"] == "cancelled"
    assert wo.cancel(b["id"]) is None                # already cancelled
    assert {x["id"] for x in wo.active()} == {a["id"], c["id"]}
    assert wo.cancel_all("algo ON") == 2 and wo.active() == []


def test_orders_are_good_till_cancelled_across_a_restart(tmp_path):
    m = Market(sell=100.0)
    wo, _ = _book(tmp_path, m)
    o = wo.place("SELL", 110.0, 3)
    again, sent = _book(tmp_path, m)
    assert [x["id"] for x in again.active()] == [o["id"]]
    m.sell = 111.0
    again.check_once()
    assert sent == [("SELL", 3, "open")]


def test_an_order_caught_mid_send_by_a_restart_is_not_resent(tmp_path):
    p = tmp_path / "wo.json"
    p.write_text(json.dumps({"orders": [{"id": "x1", "side": "SELL", "price": 100.0, "lots": 1,
                                          "status": "firing", "note": "", "created": 1.0,
                                          "updated": 1.0, "source": "manual"}]}))
    wo, sent = _book(tmp_path, Market(sell=200.0))
    wo.check_once()
    assert sent == []
    rec = wo.all()[0]
    assert rec["status"] == "rejected" and "check the Order Log" in rec["note"]


def test_bad_orders_are_refused():
    wo = WorkingOrders(None, Market(), lambda o: (True, "open"), lambda o, h: {"success": True})
    for side, price, lots in (("HOLD", 1.0, 1), ("SELL", 1.0, 0)):
        try:
            wo.place(side, price, lots)
        except ValueError:
            continue
        raise AssertionError("accepted a bad order")
