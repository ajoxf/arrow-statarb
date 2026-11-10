"""The journal in step with Arrow: rate-card charges, the ×1 repair on start,
and the tradebook import that rebuilds trades placed outside Nexus.

The fixture is a CRUDEOILM NOV/JAN calendar over 30 Oct – 9 Nov 2026 whose
Arrow statement reads: realised +₹830, charges ₹621.97 (brokerage 380, txn
42.51, CTT 101, stamp 20, SEBI 2.03, GST), net +₹208.03. Nexus journaled
three of its five round trips, at ×1, one of them 'marked flat' late.
"""

from datetime import datetime, timedelta, timezone
from unittest import mock

import pytest

from arrow_statarb.core import costs, fill_check, journal_sync as js
from arrow_statarb.core.contracts import mcx_multiplier
from arrow_statarb.core.trade_log import TradeLog

IST = timezone(timedelta(hours=5, minutes=30))
NAME = "CRUDEOILM19NOV26F − CRUDEOILM19JAN27F"
PAIR = ("CRUDEOILM19NOV26F", "CRUDEOILM19JAN27F")

TRADEBOOK = """Symbol,Exchange,Date,Time,Buy sell,Order ID,Trade ID,Qty,Avg price
"CRUDEOILM19NOV26F","MCX","30-10-2026","12:57:57","SELL","O01","T01","10.00","9850.00"
"CRUDEOILM19JAN27F","MCX","30-10-2026","12:57:58","BUY","O02","T02","10.00","9560.00"
"CRUDEOILM19JAN27F","MCX","30-10-2026","13:50:49","BUY","O03","T03","10.00","9542.00"
"CRUDEOILM19NOV26F","MCX","30-10-2026","13:51:01","SELL","O04","T04","10.00","9849.00"
"CRUDEOILM19NOV26F","MCX","02-11-2026","11:06:20","BUY","O05","T05","10.00","9530.00"
"CRUDEOILM19NOV26F","MCX","02-11-2026","11:06:20","BUY","O05","T06","10.00","9531.00"
"CRUDEOILM19JAN27F","MCX","02-11-2026","11:06:21","SELL","O06","T07","20.00","9300.00"
"CRUDEOILM19NOV26F","MCX","02-11-2026","14:18:06","SELL","O07","T08","10.00","9517.00"
"CRUDEOILM19JAN27F","MCX","02-11-2026","14:18:07","BUY","O08","T09","10.00","9283.00"
"CRUDEOILM19JAN27F","MCX","02-11-2026","14:18:17","BUY","O09","T10","10.00","9283.00"
"CRUDEOILM19NOV26F","MCX","02-11-2026","16:15:36","BUY","O10","T11","10.00","9476.00"
"CRUDEOILM19JAN27F","MCX","02-11-2026","16:15:37","SELL","O11","T12","10.00","9239.00"
"CRUDEOILM19JAN27F","MCX","02-11-2026","16:15:37","SELL","O11","T13","10.00","9239.00"
"CRUDEOILM19NOV26F","MCX","02-11-2026","16:43:01","SELL","O12","T14","10.00","9456.00"
"CRUDEOILM19JAN27F","MCX","02-11-2026","16:43:01","BUY","O13","T15","10.00","9230.00"
"CRUDEOILM19NOV26F","MCX","02-11-2026","20:12:04","BUY","O14","T16","10.00","9218.00"
"CRUDEOILM19JAN27F","MCX","02-11-2026","20:12:07","SELL","O15","T17","10.00","9005.00"
"CRUDEOILM19NOV26F","MCX","09-11-2026","14:16:17","BUY","O16","T18","10.00","8432.00"
"CRUDEOILM19JAN27F","MCX","09-11-2026","14:16:19","SELL","O17","T19","10.00","8259.00"
"CRUDEOILM19NOV26F","MCX","09-11-2026","18:53:29","SELL","O18","T20","10.00","8232.00"
"CRUDEOILM19JAN27F","MCX","09-11-2026","18:53:31","BUY","O19","T21","10.00","8078.00"
"""

RATES = js.default_rates_for(lambda s: None)


def _at(*a):
    return datetime(*a, tzinfo=IST).timestamp()


def journal_as_nexus_wrote_it(path=None):
    """The three round trips Nexus journaled: ×1 (the MCX order lot), the
    first one 'marked flat' at 14:09 although Arrow closed it at 11:06."""
    log = TradeLog(path, brokerage_per_lot=20)
    for act, d, ts, a, b, why in [
            ("OPEN", "SHORT_SPREAD", _at(2026, 10, 30, 12, 57, 58), 9850, 9560, ""),
            ("CLOSE", "SHORT_SPREAD", _at(2026, 11, 2, 14, 9), 9530.5, 9300,
             "closed outside Nexus — marked flat (at Arrow's fills)"),
            ("OPEN", "SHORT_SPREAD", _at(2026, 11, 2, 16, 43, 1), 9456, 9230, ""),
            ("CLOSE", "SHORT_SPREAD", _at(2026, 11, 2, 20, 12, 7), 9218, 9005, "working order"),
            ("OPEN", "LONG_SPREAD", _at(2026, 11, 9, 14, 16, 19), 8432, 8259, ""),
            ("CLOSE", "LONG_SPREAD", _at(2026, 11, 9, 18, 53, 31), 8232, 8078, "")]:
        with mock.patch("time.time", return_value=ts):
            log.record(action=act, direction=d, lots=1, spread=a - b, dry_run=False,
                       status="LIVE", source="manual", lot_size=1, leg_a_price=a,
                       leg_b_price=b, name=NAME, stt_pct=0.02, exit_reason=why)
    return log


def _fills():
    return fill_check.read_tradebook(TRADEBOOK.encode(), "Tradebook.csv")


def _plan(log):
    return js.plan_import([dict(r) for r in log._trades], _fills(), multiplier_for=mcx_multiplier,
                          rates_for=RATES, brokerage_per_order=20.0, pair=PAIR)


def _closes(log):
    return [r for r in log._trades if r["action"] == "CLOSE"]


# ── rate card ────────────────────────────────────────────────────────────────

def test_rate_card_on_the_tradebook_matches_arrows_charges_summary():
    plan = _plan(TradeLog(None))
    ch = plan["arrow"]["charges_detail"]
    # Arrow: brokerage 380 · txn 42.51 · CTT 101 · stamp 20 · SEBI 2.03 · total 621.97
    assert ch["brokerage"] == 380.0                 # ₹20 × 19 ORDERS (a 20-qty order once)
    assert ch["txn"] == pytest.approx(42.51, abs=0.01)
    assert ch["stt"] == 101.0 and ch["stamp"] == 20.0   # rounded per day, as the contract note
    assert ch["total"] == pytest.approx(621.97, abs=0.05)
    assert plan["arrow"]["gross"] == 830.0
    assert plan["arrow"]["net"] == pytest.approx(208.03, abs=0.05)


def test_brokerage_is_per_order_shared_pro_rata():
    f = [{"side": "buy", "units": 10, "price": 100.0, "order_units": 20}]
    assert costs.fills_charges(f, lambda _f: {}, 20.0)["brokerage"] == 10.0


# ── repair on start ──────────────────────────────────────────────────────────

def test_repair_rescales_x1_rows_and_recosts_on_the_rate_card(tmp_path):
    log = journal_as_nexus_wrote_it(tmp_path / "trades.json")
    assert [c["spread_pnl"] for c in _closes(log)] == [59.5, 13.0, -19.0]
    r = js.repair_on_start(log, mcx_multiplier, RATES, 20.0)
    assert r["rescaled"] == 6 and r["recosted"] == 3 and r["backup"]
    closes = _closes(TradeLog(tmp_path / "trades.json"))
    assert [c["spread_pnl"] for c in closes] == [595.0, 130.0, -190.0]
    assert all(c["charges_model"] == "arrow_rate_card" and c["lot_size"] == 10 for c in closes)
    # one order per leg per side → ₹80 brokerage per round trip
    assert all(c["charges"]["brokerage"] == 80.0 for c in closes)
    # idempotent
    assert js.repair_on_start(log, mcx_multiplier, RATES, 20.0)["rescaled"] == 0


def test_a_close_no_longer_double_counts_entry_brokerage():
    log = TradeLog(None, brokerage_per_lot=20)
    log.record(action="OPEN", direction="LONG_SPREAD", lots=1, spread=10.0, dry_run=False,
               status="LIVE", lot_size=10)
    rec = log.record(action="CLOSE", direction="LONG_SPREAD", lots=1, spread=12.0, dry_run=False,
                     status="LIVE", lot_size=10)
    assert rec["net_pnl"] == pytest.approx(20.0 - 80.0)
    assert log.day_pnl() == pytest.approx(rec["net_pnl"])     # the OPEN no longer adds −₹40
    assert log.stats()["total_pnl"] == pytest.approx(rec["net_pnl"])


# ── tradebook import ─────────────────────────────────────────────────────────

def test_import_rebuilds_the_two_trades_placed_outside_nexus():
    plan = _plan(journal_as_nexus_wrote_it())
    trips = {t["opened"][:12]: t for t in plan["new_trips"]}
    x1, x2 = trips["30 Oct 13:50"], trips["02 Nov 14:18"]
    assert x1["gross"] == 765.0 and x1["balanced"] and x1["direction"] == "SHORT_SPREAD"
    # it shares Arrow's 20-qty closing orders with the journaled trade (VWAP 9530.5)
    assert x1["leg_a"]["exit"] == 9530.5 and x1["leg_b"]["exit"] == 9300.0
    assert x2["gross"] == -470.0 and not x2["balanced"]
    assert (x2["leg_a"]["lots"], x2["leg_b"]["lots"]) == (1.0, 2.0)    # JAN bought twice
    assert plan["unmatched_journal"] == [] and plan["still_open"] == []


def test_import_corrects_a_late_marked_flat_close_to_arrows_fill_time():
    plan = _plan(journal_as_nexus_wrote_it())
    (c,) = plan["corrections"]
    assert c["action"] == "CLOSE" and "11:06:21" in c["changes"][0]


def test_after_import_the_journal_adds_up_to_arrows_statement(tmp_path):
    log = journal_as_nexus_wrote_it(tmp_path / "trades.json")
    js.repair_on_start(log, mcx_multiplier, RATES, 20.0)
    plan = _plan(log)
    assert plan["journal_after"]["gross"] == 830.0
    assert plan["journal_after"]["net"] == pytest.approx(208.03, abs=0.05)
    res = js.apply_import(log, plan)
    assert res["applied"] and res["added"] == 2 and res["backup"]

    rt = TradeLog(tmp_path / "trades.json").round_trips()
    assert rt["count"] == 5
    assert rt["total_pnl"] == pytest.approx(208.03, abs=0.05)
    j3 = [t for t in rt["trips"] if t.get("exit_reason", "").startswith("closed outside")][0]
    assert j3["held_sec"] == pytest.approx(_at(2026, 11, 2, 11, 6, 21) - _at(2026, 10, 30, 12, 57, 58))
    arrow_rows = [t for t in rt["trips"] if t.get("entry_source") == "arrow"]
    assert len(arrow_rows) == 2 and all(t["arrow_verified"] for t in rt["trips"])
    # and the per-contract check agrees, unbalanced trade included
    r = fill_check.reconcile(_fills(), fill_check.journal_fills(
        TradeLog(tmp_path / "trades.json").all(), "2026-10-30", "2026-11-09"))
    assert r["matches"] is True


def test_importing_the_same_tradebook_twice_changes_nothing(tmp_path):
    log = journal_as_nexus_wrote_it(tmp_path / "trades.json")
    js.apply_import(log, _plan(log))
    again = _plan(log)
    assert again["changes"] is False and again["new_trips"] == []


def test_a_journal_fill_arrow_never_made_is_reported_not_rewritten():
    log = journal_as_nexus_wrote_it()
    log._trades[0]["leg_a_price"] = 9850
    log._trades[0]["lots"] = 3                     # Arrow only ever filled 1 lot then
    plan = _plan(log)
    assert any(u["action"] == "OPEN" for u in plan["unmatched_journal"])
    assert 0 not in plan["updates"] or "leg_a_price" not in plan["updates"][0]


def test_a_position_still_open_at_the_end_is_not_added():
    fills = fill_check.read_tradebook((TRADEBOOK.splitlines()[0] + "\n"
                                       + TRADEBOOK.splitlines()[1] + "\n").encode())
    plan = js.plan_import([], fills, multiplier_for=mcx_multiplier, rates_for=RATES)
    assert plan["new_trips"] == [] and len(plan["still_open"]) == 1


def test_tradebook_and_pnl_report_are_told_apart():
    assert fill_check.read_arrow_file(TRADEBOOK.encode(), "t.csv")["kind"] == "tradebook"
    from tests.test_fill_check import REPORT
    assert fill_check.read_arrow_file(REPORT.encode(), "p.csv")["kind"] == "pnl"
    with pytest.raises(ValueError):
        fill_check.read_arrow_file(b"a,b\n1,2\n", "x.csv")
