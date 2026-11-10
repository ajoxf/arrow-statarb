"""MCX contract multipliers and the journal repair for rows recorded at ×1."""

import pytest

from arrow_statarb.core.contracts import mcx_base, mcx_multiplier
from arrow_statarb.core.trade_log import TradeLog


def test_mcx_base_and_multiplier():
    assert mcx_base("CRUDEOILM19NOV26F") == "CRUDEOILM"
    assert mcx_base("CRUDEOILM17SEP26C8950") == "CRUDEOILM"
    assert mcx_multiplier("CRUDEOILM19NOV26F") == 10
    assert mcx_multiplier("CRUDEOIL19NOV26F") == 100
    assert mcx_multiplier("NATURALGAS24NOV26F") == 1250
    assert mcx_multiplier("NIFTY30JUN26F") is None      # NSE: not in the table
    assert mcx_multiplier("GOLDBEES") is None           # not a GOLD contract
    assert mcx_multiplier("") is None


def _journal_at_x1(tmp_path):
    log = TradeLog(tmp_path / "trades.json", brokerage_per_lot=20)
    name = "CRUDEOILM19NOV26F − CRUDEOILM19JAN27F"
    log.record(action="OPEN", direction="SHORT_SPREAD", lots=1, spread=290.0,
               dry_run=False, status="LIVE", lot_size=1, name=name,
               leg_a_price=9850.0, leg_b_price=9560.0, zscore=0.89)
    log.record(action="CLOSE", direction="SHORT_SPREAD", lots=1, spread=230.5,
               dry_run=False, status="LIVE", lot_size=1, name=name,
               leg_a_price=9530.5, leg_b_price=9300.0, zscore=-4.29,
               stt_pct=0.01)
    return log


def test_rescale_repairs_rows_journaled_at_x1(tmp_path):
    log = _journal_at_x1(tmp_path)
    before = [t for t in log.all() if t["action"] == "CLOSE"][0]
    assert before["spread_pnl"] == pytest.approx(59.5)
    stt_x1 = before["stt"]

    dry = log.rescale_lot_size(lambda r: 10, apply=False)
    assert len(dry) == 2
    assert [t for t in log.all() if t["action"] == "CLOSE"][0]["lot_size"] == 1  # untouched

    log.rescale_lot_size(lambda r: 10)
    after = [t for t in TradeLog(tmp_path / "trades.json").all() if t["action"] == "CLOSE"][0]
    assert after["lot_size"] == 10 and after["lot_size_fixed_from"] == 1
    assert after["spread_pnl"] == pytest.approx(595.0)
    assert after["stt"] == pytest.approx(stt_x1 * 10, abs=0.02)
    # brokerage per lot is unchanged: 2 legs × entry + exit × ₹20 = ₹80
    assert after["net_pnl"] == pytest.approx(595.0 - 80.0 - after["stt"], abs=0.02)

    # idempotent: a second run finds nothing to change
    assert log.rescale_lot_size(lambda r: 10) == []


def test_rescale_leaves_unknown_rows_alone(tmp_path):
    log = _journal_at_x1(tmp_path)
    assert log.rescale_lot_size(lambda r: None) == []
