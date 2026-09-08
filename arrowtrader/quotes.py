"""Ticks and the order book — normalised, and honest about what is missing.

THIS MODULE EXISTS BECAUSE OF BLOCKER 3.2.

MT5-Trader's first hard rule is that the spread is built from the MID
OF THE BOOK, never `tick.last`. The stat-arb Arrow layer reads
`QuoteMode.LTP` and `DataMode.LTP` and nothing else — which is
`tick.last` and only that. On LTP alone there is no mid to centre on,
no executable bid or offer, no `spread_cost`, no implied size, and no
touch to measure slippage against: every price on the ladder would be
a fiction.

So the leg layer asks for the full quote, and this module turns
whatever comes back into one shape:

    {'bid': float|None, 'ask': float|None, 'last': float|None,
     'time': epoch seconds, 'depth': [ {type,price,volume}, ... ]|None}

Three rules, each of which is a way to lie with a price:

1. **A missing side is None, not the last trade.** Substituting LTP for
   a missing bid produces a spread that looks executable and is not.
2. **An empty book is None, not an empty list of zero sizes.** The
   ladder's size columns then stay empty rather than claiming the
   market can do nothing.
3. **Paise are converted once, per field, from a declared scale.** The
   stream sends integers in paise; whether REST does is a per-build
   question, so the scale is a parameter and never a guess sprinkled
   through the parsing.
"""

import time as time_mod

from .instruments import field


#: Arrow's WebSocket sends prices as integers in paise. REST quotes may
#: or may not — it is per build, and it is blocker 3.2's second half.
#: The scale is passed in rather than assumed, so that a wrong answer
#: is one config line instead of a hundredfold price error scattered
#: through the parser.
PAISE = 100.0
RUPEES = 1.0


def _price(value, scale):
    """A price, or None. Zero is None: an exchange does not quote 0."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number <= 0:
        return None
    return number / scale if scale and scale != 1.0 else number


def _size(value):
    """A size, or None. Zero IS a real size at a price level that has
    just been cleared, so it is kept — but a missing one is None."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def normalise_quote(raw, scale=RUPEES, clock=time_mod.time):
    """One broker quote payload → the tick shape the coordinator reads.

    Tolerant of TitleCase, camelCase and snake_case in the same
    payload, because Arrow's responses have been all three.
    """
    if not isinstance(raw, dict):
        return None
    bid = _price(field(raw, 'BestBidPrice', 'bidPrice', 'bp', 'bid',
                       'buyPrice', 'bid_price'), scale)
    ask = _price(field(raw, 'BestAskPrice', 'askPrice', 'sp', 'ask',
                       'sellPrice', 'ask_price', 'offer'), scale)
    last = _price(field(raw, 'Ltp', 'lastPrice', 'ltp', 'last',
                        'lastTradedPrice', 'last_traded_price'), scale)
    stamp = field(raw, 'ExchFeedTime', 'feedTime', 'timestamp', 'time', 'ft')
    depth = normalise_depth(raw, scale=scale)
    return {
        # A MISSING SIDE STAYS MISSING. Never backfilled from `last`.
        'bid': bid,
        'ask': ask,
        'last': last,
        'time': _stamp(stamp, clock),
        #: The broker's own stamp, verbatim, beside our reading of it —
        #: so "the feed is frozen" can be told from "we cannot read the
        #: broker's clock".
        'exchange_time_raw': stamp,
        'depth': depth,
        #: Best sizes, where the book carried them. Used by the ladder's
        #: implied-size columns, and None where there is no book.
        'bid_size': depth[0]['volume'] if depth and depth[0]['type'] == 'bid'
        else _size(field(raw, 'BestBidQty', 'bidQty', 'bq', 'bid_qty')),
        'ask_size': _best_ask_size(depth) if depth
        else _size(field(raw, 'BestAskQty', 'askQty', 'sq', 'ask_qty')),
        #: Whether this quote can price an order at all. A quote with
        #: no bid and no ask is a quote the ladder must not draw a
        #: touch from, however recent it is.
        'executable': bid is not None and ask is not None,
    }


def _best_ask_size(depth):
    for level in depth:
        if level['type'] == 'ask':
            return level['volume']
    return None


def _stamp(value, clock):
    """The broker's stamp in epoch seconds, or OUR clock as a fallback.

    Falling back is deliberate and is not a measurement: `QuoteAgeTracker`
    ages a quote by whether its CONTENT changed, not by this number, so
    a fallback stamp cannot make a stale quote look fresh.
    """
    try:
        number = float(value)
    except (TypeError, ValueError):
        return clock()
    if number > 10 ** 11:          # milliseconds
        number /= 1000.0
    return number if number > 10 ** 8 else clock()


def normalise_depth(raw, scale=RUPEES):
    """The order book as a flat list of levels, or **None** where there
    is none.

    MCX and NSE both publish five levels. A broker that publishes none
    comes back as None and the ladder's size columns stay EMPTY — it
    never invents a size from one leg, and it never renders an absent
    book as a row of zeroes, which reads as "the market can do nothing"
    rather than "we do not know".
    """
    if not isinstance(raw, dict):
        return None
    bids = field(raw, 'Bids', 'bids', 'bidValues', 'depth_buy', 'buy')
    asks = field(raw, 'Asks', 'asks', 'askValues', 'depth_sell', 'sell')
    if isinstance(raw.get('depth'), dict):
        inner = raw['depth']
        bids = bids or field(inner, 'buy', 'bids')
        asks = asks or field(inner, 'sell', 'asks')
    levels = []
    for side, rows in (('bid', bids), ('ask', asks)):
        if not isinstance(rows, (list, tuple)):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            price = _price(field(row, 'price', 'Price', 'p'), scale)
            if price is None:
                continue
            levels.append({
                'type': side,
                'price': price,
                'volume': _size(field(row, 'quantity', 'qty', 'Quantity',
                                      'volume', 'v')) or 0.0,
                'orders': _size(field(row, 'orders', 'numberOfOrders', 'no')),
            })
    if not levels:
        return None
    levels.sort(key=lambda level: (-level['price'] if level['type'] == 'bid'
                                   else level['price']))
    return levels


def tick_from_ltp(last, scale=PAISE, clock=time_mod.time):
    """A tick carrying ONLY a last trade — bid and ask stay None.

    This is what the LTP-only feed can produce, and it is deliberately
    not enough to price anything. It exists so the degraded case is
    representable and visibly degraded, rather than being papered over
    by copying `last` into both sides.
    """
    price = _price(last, scale)
    return {'bid': None, 'ask': None, 'last': price, 'time': clock(),
            'exchange_time_raw': None, 'depth': None,
            'bid_size': None, 'ask_size': None, 'executable': False}


def missing_book_reason(tick_a, tick_b, symbol_a='leg A', symbol_b='leg B'):
    """Why this pair cannot price an order, in one line, or None.

    Named so the ladder can print the reason on the row instead of
    drawing a touch nobody can trade at.
    """
    for tick, name in ((tick_a, symbol_a), (tick_b, symbol_b)):
        if not tick:
            return f'{name} has no quote at all'
        if tick.get('bid') is None and tick.get('ask') is None:
            return (f'{name} is quoting a last trade but no bid and no ask — '
                    f'the feed is in LTP mode, and a ladder needs the book')
        if tick.get('bid') is None:
            return f'{name} has no bid — nothing can be sold into'
        if tick.get('ask') is None:
            return f'{name} has no offer — nothing can be bought from'
    return None
