"""Untracked-fills check: Arrow's P&L report / order book vs the journal."""

from datetime import datetime, timedelta, timezone

import pytest

from arrow_statarb.core import fill_check as fc

IST = timezone(timedelta(hours=5, minutes=30))
PAIR = "CRUDEOILM19NOV26F − CRUDEOILM19JAN27F"

# Arrow's P&L report for 01 Oct – 09 Nov 2026, as downloaded (summary block on top)
REPORT = """Profit & Loss
Realised P&L,830.00
Symbol,Buy Qty.,Buy Avg.,Buy Value,Sell Qty.,Sell Avg.,Sell Value,Charges,Realised P&L,Net P&L,Unrealised P&L
CRUDEOILM 19th NOV 26 FUT,50,"9,237.40","4,61,870.00",50,"9,380.80","4,69,040.00",256.62,"7,170.00","7,170.00",-
CRUDEOILM 19th JAN 27 FUT,60,"9,162.67","5,49,760.00",60,"9,057.00","5,43,420.00",289.38,"-6,340.00","-6,340.00",-
"""


def _journal(status="LIVE"):
    """The three 1-lot trades in the Nexus journal (lot_size = ₹10/point)."""
    rows = []
    for d, a, b, c, e, dt in [
            ("SHORT_SPREAD", 9850, 9560, 9530.5, 9300, datetime(2026, 11, 2, 14, 9, tzinfo=IST)),
            ("SHORT_SPREAD", 9456, 9230, 9218, 9005, datetime(2026, 11, 2, 20, 12, tzinfo=IST)),
            ("LONG_SPREAD", 8432, 8259, 8232, 8078, datetime(2026, 11, 9, 18, 53, tzinfo=IST))]:
        ts = dt.timestamp()
        base = dict(direction=d, lots=1, lot_size=10, status=status, dry_run=status != "LIVE", name=PAIR)
        rows.append(dict(base, action="OPEN", leg_a_price=a, leg_b_price=b, ts=ts - 3600))
        rows.append(dict(base, action="CLOSE", leg_a_price=c, leg_b_price=e, ts=ts))
    return rows


@pytest.mark.parametrize("raw", ["CRUDEOILM19NOV26F", "CRUDEOILM 19th NOV 26 FUT",
                                 "CRUDEOILM 19-NOV-2026", "crudeoilm19nov26f"])
def test_contract_key_spellings_agree(raw):
    assert fc.contract_key(raw) == "CRUDEOILM 19NOV26"


def test_option_is_not_merged_with_its_future():
    assert fc.contract_key("CRUDEOILM17SEP26C8950") == fc.contract_key("CRUDEOILM 17th SEP 26 8950 CE")
    assert fc.contract_key("CRUDEOILM17SEP26C8950") != fc.contract_key("CRUDEOILM17SEP26F")


def test_report_reconciles_to_the_rupee():
    rows = fc.read_pnl_report(REPORT.encode(), "pnl.csv")
    assert len(rows) == 2 and rows[0]["charges"] == pytest.approx(256.62)
    r = fc.reconcile(rows, fc.journal_fills(_journal(), "2026-10-01", "2026-11-09"))
    assert r["totals"] == {"arrow": 830.0, "journal": 535.0, "untracked": 295.0}
    assert r["matches"] is False
    by = {c["key"]: c for c in r["contracts"]}
    nov, jan = by["CRUDEOILM 19NOV26"], by["CRUDEOILM 19JAN27"]
    # 2 NOV lots bought @ 9,503.25 and sold @ 9,683.00 were never journaled
    assert nov["residual"]["buy_units"] == 20 and nov["residual"]["buy_avg"] == pytest.approx(9503.25)
    assert nov["residual"]["sell_avg"] == pytest.approx(9683.0)
    assert nov["untracked_pnl"] == pytest.approx(3595.0)
    assert jan["residual"]["buy_units"] == 30 and jan["untracked_pnl"] == pytest.approx(-3300.0)


def test_only_live_rows_and_the_date_range_count():
    assert fc.journal_fills(_journal(status="DRY-RUN")) == []
    assert fc.journal_fills(_journal(status="LIVE-SIM")) == []
    only_9th = fc.journal_fills(_journal(), "2026-11-09", "2026-11-09")
    assert len(only_9th) == 4                       # one round trip × two legs
    # LONG_SPREAD opens buy A / sell B; its close reverses
    sides = [(f["action"], f["symbol"][-8:], f["side"]) for f in only_9th]
    assert ("OPEN", "19NOV26F", "buy") in sides and ("CLOSE", "19NOV26F", "sell") in sides


def test_a_fully_journaled_day_matches():
    j = fc.journal_fills(_journal(), "2026-11-09", "2026-11-09")
    orders = [{"status": "COMPLETE", "side": f["side"].upper(), "symbol": f["symbol"],
               "fill_qty": 1, "fill_price": f["price"], "order_id": str(i)} for i, f in enumerate(j)]
    arrow = fc.broker_fills_from_orders(orders, lambda o: 10)   # MCX: order lot 1 = 10 bbl
    r = fc.reconcile(arrow, j)
    assert r["matches"] is True and r["totals"]["untracked"] == 0
    assert fc.unmatched_fills(arrow, j) == {"not_in_journal": [], "missing_at_arrow": []}


def test_order_book_extras_are_listed_fill_by_fill():
    j = fc.journal_fills(_journal(), "2026-11-09", "2026-11-09")
    orders = [{"status": "COMPLETE", "side": f["side"], "symbol": f["symbol"],
               "fill_qty": 1, "fill_price": f["price"]} for f in j]
    orders += [{"status": "COMPLETE", "side": "B", "symbol": "CRUDEOILM19JAN27F", "fill_qty": 1,
                "fill_price": 8100.0, "time": "19:40:01", "order_id": "X1"},
               {"status": "REJECTED", "side": "S", "symbol": "CRUDEOILM19JAN27F", "fill_qty": 0,
                "fill_price": 0}]
    arrow = fc.broker_fills_from_orders(orders, lambda o: 10)
    u = fc.unmatched_fills(arrow, j)
    assert [(f["order_id"], f["side"], f["units"]) for f in u["not_in_journal"]] == [("X1", "buy", 10)]
    r = fc.reconcile(arrow, j)
    jan = {c["key"]: c for c in r["contracts"]}["CRUDEOILM 19JAN27"]
    assert jan["residual"]["open_units"] == 10      # a buy with no sell: open, not valued


def test_journal_fill_arrow_does_not_show_is_flagged():
    j = fc.journal_fills(_journal(), "2026-11-09", "2026-11-09")
    u = fc.unmatched_fills([], j)
    assert len(u["missing_at_arrow"]) == 4
    r = fc.reconcile([], j)
    assert all(c["residual"]["journal_has_extra"] for c in r["contracts"])


def test_report_without_the_table_is_refused():
    with pytest.raises(ValueError):
        fc.read_pnl_report(b"Date,Amount\n2026-11-01,5\n", "x.csv")
