"""The A − B spread ladder, sizes derived from both legs' depth."""

from arrow_statarb.core import spread_ladder


def _d(bids, asks):
    return ([{"type": "bid", "price": p, "volume": v} for p, v in bids]
            + [{"type": "ask", "price": p, "volume": v} for p, v in asks])


A = _d(bids=[(9110, 2), (9109, 6)], asks=[(9111, 5), (9112, 3)])
B = _d(bids=[(8522, 4), (8521, 10)], asks=[(8524, 3), (8525, 5)])


def _by_level(rows):
    return {r["level"]: r for r in rows}


def test_buy_side_walks_A_asks_against_B_bids():
    # buy spread = ask_A − bid_B: 9111−8522=589 ×4, 9111−8521=590 ×1, 9112−8521=591 ×3
    rows = _by_level(spread_ladder.build(A, B, 1.0, 1, 1, sell_spread=586.0,
                                         buy_spread=589.0, increment=1.0, count=21))
    assert rows[589]["ask_size"] == 4 and rows[590]["ask_size"] == 1 and rows[591]["ask_size"] == 3
    assert rows[589]["is_best_ask"] and not rows[590]["is_best_ask"]


def test_sell_side_walks_A_bids_against_B_asks():
    # sell spread = bid_A − ask_B: 9110−8524=586 ×2, 9109−8524=585 ×1, 9109−8525=584 ×5
    rows = _by_level(spread_ladder.build(A, B, 1.0, 1, 1, sell_spread=586.0,
                                         buy_spread=589.0, increment=1.0, count=21))
    assert rows[586]["bid_size"] == 2 and rows[585]["bid_size"] == 1 and rows[584]["bid_size"] == 5
    assert rows[586]["is_best_bid"]


def test_highest_price_first_and_hedge_ratio_scales_leg_A():
    rows = spread_ladder.build(A, B, 2.0, 1, 1, sell_spread=2 * 9110 - 8524,
                               buy_spread=2 * 9111 - 8522, increment=1.0, count=41)
    assert [r["level"] for r in rows] == sorted((r["level"] for r in rows), reverse=True)
    assert _by_level(rows)[2 * 9111 - 8522]["ask_size"] == 4


def test_clips_are_units_per_lot_and_a_missing_book_is_None():
    rows = _by_level(spread_ladder.build(A, B, 1.0, 2, 2, sell_spread=586.0,
                                         buy_spread=589.0, increment=1.0))
    assert rows[589]["ask_size"] == 2               # 4 units ÷ 2 units per clip
    rows = spread_ladder.build(A, None, 1.0, 1, 1, sell_spread=586.0,
                               buy_spread=589.0, increment=1.0)
    assert all(r["ask_size"] is None and r["bid_size"] is None for r in rows)
    assert spread_ladder.build(A, B, 1.0, 1, 1, None, 589.0, 1.0) == []
