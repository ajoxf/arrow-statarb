"""The price ladder, and the SIZE at each of its levels.

A spread has no order book. The exchange publishes one for GOLD05DEC25F
and one for GOLD05FEB26F, and nothing at all for the difference between
them — so every number in the ladder's two size columns has to be
DERIVED from the two legs' depth of market, or left empty.

Left empty is a real answer and it is used a lot here. An invented size
is a size a trader clicks on, and a click that finds a tenth of the
quantity it was shown is a half-hedged spread — one naked leg, on a
netting account, discovered after the fact. So:

  * one leg publishing no book means NO SIZE ON THE LADDER, not a size
    computed from the other leg;
  * a lot size we do not know means no size, because units cannot be
    turned into clips without it;
  * a level nothing can fill at shows nothing, never a zero.

WHAT THE SIZE MEANS
-------------------
One "clip" is one Qty on the ladder: `units_a` of leg A against
`units_b` of leg B. To BUY the spread you lift B's asks and hit A's
bids, so a buy at level `L` needs a pair of levels with

    ask_B[i] - beta * bid_A[j] <= L

and the clips that pair can fill is the smaller of what the two levels
hold. To SELL you hit B's bids and lift A's asks, and the comparison
turns around.

The pairing is a merge, and it is exact rather than an approximation.
The cost of a pair separates into one term per leg — `ask_B[i]` plus
`-beta * bid_A[j]` — so the pairs usable at any threshold form a
staircase, and walking both books from their touches outward, always
consuming the level that runs out first, both fills the maximum
possible quantity and produces it in cheapest-first order. The total is
`min(all of B, all of A)` in clips, which is the obvious sanity check.

WHY CUMULATIVE, THEN DIFFERENCED
--------------------------------
Depth is answered as "how much can I get at this price or better",
which is cumulative. A ladder column is read DOWN, level by level, and
a trader adding two rows expects the sum to be what both rows hold. So
the curve is computed cumulatively and then differenced onto the grid:
each row shows what that row adds, and the rows sum to the total.
"""

import math


#: Sizes below this are noise from floating-point division, not depth.
_EPSILON = 1e-9


def synthetic_book(near, far, beta, units_near, units_far, sign=1):
    """Merge two legs' books into one side of the spread's book.

    `near` is the side of leg B being taken and `far` the side of leg A,
    each a list of `{'price', 'volume'}` ordered best-first. Returns
    `[(spread_price, clips), ...]` in the order the market would fill
    them — ascending for a buy, descending for a sell — or None where
    either book, or either lot size, is unknown.

    `sign` is +1 when a HIGHER spread price is worse (buying) and -1
    when a LOWER one is (selling). It is not used in the arithmetic:
    the ordering falls out of the two books' own ordering. It is here
    so the caller states which side it is asking about and the result
    can be asserted against it.
    """
    if not near or not far or not units_near or not units_far:
        # UNMEASURED IS NOT ZERO. No book, or no lot size, means the
        # size is unknown — never that there is none.
        return None
    capacity_near = [level['volume'] / float(units_near) for level in near]
    capacity_far = [level['volume'] / float(units_far) for level in far]
    out, i, j = [], 0, 0
    while i < len(near) and j < len(far):
        take = min(capacity_near[i], capacity_far[j])
        if take > _EPSILON:
            out.append((near[i]['price'] - beta * far[j]['price'], take))
        capacity_near[i] -= take
        capacity_far[j] -= take
        # Whichever level ran out is the one we move past. When both
        # did, both move — and a level that held nothing to begin with
        # is stepped over rather than looped on.
        if capacity_near[i] <= _EPSILON:
            i += 1
        if j < len(far) and capacity_far[j] <= _EPSILON:
            j += 1
    del sign
    return out


def _grid(level, base, increment, side):
    """The ladder row a fillable price belongs on.

    A price you can BUY at rounds UP to the next row: the row says "at
    this price or better", and rounding down would advertise size at a
    price that cannot actually be got. Selling rounds down, for the
    same reason in the other direction.
    """
    steps = (level - base) / increment
    return base + increment * (math.ceil(steps - 1e-9) if side == 'ask'
                               else math.floor(steps + 1e-9))


def implied_sizes(entries, levels, increment, side):
    """`{level: clips}` for one side, from a merged book.

    The entries are cumulative by construction — each one is fillable
    only after the ones before it — so they are bucketed onto the grid
    and each row carries what IT adds. The rows then sum to the total,
    which is what a trader reading the column down is doing.
    """
    if entries is None:
        return {}
    on_grid, base = {}, levels[0] if levels else 0.0
    for price, clips in entries:
        row = _grid(price, base, increment, side)
        on_grid[row] = on_grid.get(row, 0.0) + clips
    # Size at a price the ladder does not show is DROPPED, never
    # folded onto the edge row. Piling it onto the last visible level
    # would print a quantity at a price it cannot be filled at, which
    # is the one thing these columns must never do — scroll the ladder
    # and the size would move with the window rather than staying with
    # the price it belongs to.
    if levels:
        visible = set(levels)
        on_grid = {row: clips for row, clips in on_grid.items()
                   if row in visible}
    return on_grid


def rows(market, increment, count, units_a, units_b, anchor=None):
    """The ladder: `count` levels around the market, with their sizes.

    Returns the list the front end draws, HIGHEST PRICE FIRST, or an
    empty list where the spread cannot be priced at all — which is not
    a blank ladder by accident but a ladder that is honestly empty,
    with the reason on the error line beside it.
    """
    if not market or not increment or increment <= 0 or count <= 0:
        return []
    mid = market.get('spread')
    short = market.get('short_spread')
    long_ = market.get('long_spread')
    if mid is None or short is None or long_ is None:
        return []

    centre = anchor if anchor is not None else mid
    # The grid is anchored on a MULTIPLE OF THE INCREMENT, not on the
    # mid itself: a grid that moves with the mid renumbers every row on
    # every tick, and a price that shifts under the pointer is a click
    # landing somewhere nobody chose.
    base = round(centre / increment) * increment
    half = count // 2
    levels = [round(base + increment * step, 10)
              for step in range(half, half - count, -1)]

    beta = float(market.get('hedge_ratio') or 1.0)
    depth_a = market.get('leg_a_depth')
    depth_b = market.get('leg_b_depth')

    # BUY the spread: lift B's asks, hit A's bids.
    asks = implied_sizes(
        synthetic_book(_side(depth_b, 'ask'), _side(depth_a, 'bid'),
                       beta, units_b, units_a, sign=1),
        levels, increment, 'ask')
    # SELL the spread: hit B's bids, lift A's asks.
    bids = implied_sizes(
        synthetic_book(_side(depth_b, 'bid'), _side(depth_a, 'ask'),
                       beta, units_b, units_a, sign=-1),
        levels, increment, 'bid')

    priced = bool(asks) or bool(bids)
    best_bid = _nearest(levels, short)
    best_ask = _nearest(levels, long_)
    out = []
    for level in levels:
        out.append({
            'level': level,
            'is_mid': level == _nearest(levels, mid),
            'is_anchor': anchor is not None
            and level == _nearest(levels, anchor),
            'is_best_bid': level == best_bid,
            'is_best_ask': level == best_ask,
            # NONE, NOT ZERO, where there is no book to derive from.
            # The front end renders None as an empty cell and 0 as a
            # zero, and "the market can do nothing here" is a different
            # statement from "we do not know".
            'bid_size': _clips(bids.get(level)) if priced else None,
            'ask_size': _clips(asks.get(level)) if priced else None,
        })
    return out


def _side(depth, which):
    """One side of a normalised book, best first, or None."""
    if not depth:
        return None
    levels = [level for level in depth if level['type'] == which]
    return levels or None


def _clips(value):
    """Whole clips. A part of a clip is not a spread anybody can trade,
    and rounding it up would advertise size that is not there."""
    if value is None:
        return None
    whole = int(value + _EPSILON)
    return whole or None


def _nearest(levels, price):
    if price is None or not levels:
        return None
    return min(levels, key=lambda level: abs(level - price))
