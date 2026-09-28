"""The spread ladder for spread = k × A − B, with sizes derived from both books.

A spread has no order book of its own, so every size on the ladder is
DERIVED from the two legs' depth (five levels a side), using Arrow Trader's
tested merge (``arrowtrader.ladder.synthetic_book`` / ``implied_sizes``).
That merge computes ``near − beta × far``; with leg A's prices pre-scaled by
the hedge ratio k and beta = 1 it computes exactly this system's spread:

    BUY the spread  (buy A, sell B): k × ask_A − bid_B   → the ASKS column
    SELL the spread (sell A, buy B): k × bid_A − ask_B   → the BIDS column

so the best ask row is the BUY spread and the best bid row the SELL spread
shown on Signal & Position. A leg without a book gives NO size (None), never
a size borrowed from the other leg; a lot size we do not know gives no size.
"""

from __future__ import annotations

import math
from typing import Dict, List, Optional

from arrowtrader.ladder import implied_sizes, synthetic_book, _clips


def _scaled(depth: Optional[List[Dict]], side: str, k: float) -> Optional[List[Dict]]:
    if not depth:
        return None
    rows = [dict(lv, price=lv["price"] * k) for lv in depth if lv.get("type") == side]
    return rows or None


def _only(depth: Optional[List[Dict]], side: str) -> Optional[List[Dict]]:
    if not depth:
        return None
    rows = [lv for lv in depth if lv.get("type") == side]
    return rows or None


def build(depth_a: Optional[List[Dict]], depth_b: Optional[List[Dict]], k: float,
          units_a: float, units_b: float, sell_spread: Optional[float],
          buy_spread: Optional[float], increment: float, count: int = 21,
          anchor: Optional[float] = None) -> List[Dict]:
    """Ladder rows, HIGHEST PRICE FIRST. Empty when the spread cannot be priced.

    ``units_a`` / ``units_b`` are broker units per clip (one lot of each leg
    × its ratio). ``anchor`` pins the grid (e.g. the entry spread) so rows do
    not renumber on every tick; by default it centres on the mid of the two
    executable spreads, snapped to a multiple of the increment."""
    if sell_spread is None or buy_spread is None or not increment or increment <= 0:
        return []
    mid = (sell_spread + buy_spread) / 2.0
    centre = anchor if anchor is not None else mid
    base = round(centre / increment) * increment
    half = count // 2
    levels = [round(base + increment * step, 10) for step in range(half, half - count, -1)]

    k = float(k or 1.0)
    # BUY: lift A's asks (×k), hit B's bids.  SELL: hit A's bids (×k), lift B's asks.
    asks = implied_sizes(synthetic_book(_scaled(depth_a, "ask", k), _only(depth_b, "bid"),
                                        1.0, units_a, units_b, sign=1),
                         levels, increment, "ask")
    bids = implied_sizes(synthetic_book(_scaled(depth_a, "bid", k), _only(depth_b, "ask"),
                                        1.0, units_a, units_b, sign=-1),
                         levels, increment, "bid")
    priced = bool(asks) or bool(bids)

    def _row_of(price, side):
        steps = (price - levels[-1]) / increment
        r = levels[-1] + increment * (math.ceil(steps - 1e-9) if side == "ask"
                                      else math.floor(steps + 1e-9))
        return round(r, 10)

    best_ask = _row_of(buy_spread, "ask")
    best_bid = _row_of(sell_spread, "bid")
    mid_row = min(levels, key=lambda lv: abs(lv - mid))
    out = []
    for lv in levels:
        out.append({
            "level": lv,
            "is_mid": lv == mid_row,
            "is_best_ask": lv == best_ask,      # BUY the spread here
            "is_best_bid": lv == best_bid,      # SELL the spread here
            # None, NOT zero, where no book could be derived.
            "ask_size": _clips(asks.get(lv)) if priced else None,
            "bid_size": _clips(bids.get(lv)) if priced else None,
        })
    return out
