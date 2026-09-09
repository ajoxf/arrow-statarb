"""What a round trip actually costs — and the half of it that is
asymmetric, which MT5's model has no place for.

The split from MT5-Trader is kept exactly, because it is the thing that
stops a cost being charged twice:

- **crossing** is the bid-ask both legs pay, in at one side and out at
  the other. A position entered at its real fill and marked at the
  exit-side touch has ALREADY paid all of it — it is in the two prices.
  Subtracting it again from that mark charges the spread twice.
- **charges** are never in a price, and are what remains to subtract.

`spread_cost x k` (from spread.py) and `crossing_cost` here are two
views of ONE quantity.

## WHAT IS DIFFERENT, AND IT IS THE WHOLE MODULE

MT5's charge model is one number per lot per leg, plus a swap per
night. India has neither. There is no swap — a futures carry is IN the
price, which is what the basis is — and the charge stack is six items,
one of which lands on ONE SIDE ONLY:

    brokerage             per lot, per order, or a % of turnover
    exchange transaction  % of turnover, per exchange and segment
    SEBI turnover fee     % of turnover
    stamp duty            % of turnover, **BUY side only**, by state
    GST                   18% of (brokerage + exchange transaction)
    CTT                   % of turnover, **SELL side only**, MCX futures

So a leg costs a different amount to buy than to sell, and a spread —
which is long one contract and short another — pays the buy stack on
one leg and the sell stack on the other, at BOTH ends. A model that
charges one symmetric number per lot gets a calendar spread's true cost
wrong in both directions at once.

## EVERY RATE DEFAULTS TO ZERO, AND SAYS SO

A fabricated cost is charged against every trade and the operator
cannot tell it was never theirs. So nothing here has a plausible
default: the schedule starts empty, `configured` is False, and the
screen says "not configured" rather than showing a number that came
from nowhere. The one exception is the GST rate, which is statutory and
applies only to charges that are themselves zero until set — 18% of
nothing is nothing.

Fill it in from the desk's own Arrow contract, not from a website.
"""


#: The rates one segment charges. Percentages are OF TURNOVER
#: (units x price) unless the name says otherwise.
DEFAULT_SCHEDULE = {
    'brokerage_per_lot': 0.0,
    'brokerage_per_order': 0.0,
    'brokerage_pct': 0.0,
    'exchange_txn_pct': 0.0,
    'sebi_pct': 0.0,
    #: BUY SIDE ONLY, and it varies by the state the account is
    #: registered in.
    'stamp_duty_pct_buy': 0.0,
    #: Applied to (brokerage + exchange transaction), not to turnover.
    'gst_pct': 18.0,
    #: SELL SIDE ONLY. Commodities Transaction Tax, on MCX futures.
    'ctt_pct_sell': 0.0,
}

#: The fields that must be set before a cost figure means anything.
#: `gst_pct` is deliberately not among them: it is statutory, and it
#: multiplies charges that are zero until the rest of this is filled in.
_MUST_BE_SET = ('brokerage_per_lot', 'brokerage_per_order', 'brokerage_pct',
                'exchange_txn_pct', 'sebi_pct', 'stamp_duty_pct_buy',
                'ctt_pct_sell')


def schedule_for(settings, segment=None):
    """The rates in force for one segment, defaults filled in.

    Per SEGMENT, because MCX and NSE F&O charge differently and CTT
    applies to one of them. A pair whose legs are on two segments reads
    this twice.
    """
    settings = settings or {}
    table = settings.get('CHARGES') or {}
    rates = dict(DEFAULT_SCHEDULE)
    rates.update(table.get('*') or {})
    if segment:
        rates.update(table.get(segment) or {})
    return rates


def is_configured(rates):
    """Has anybody actually entered this desk's rates?

    False means every cost figure downstream is zero because nothing
    was set — NOT because the trading is free. The screen has to be
    able to tell those apart.
    """
    return any(float(rates.get(name) or 0.0) for name in _MUST_BE_SET)


def leg_charges(units, price, side, rates, orders=1):
    """What one leg costs on ONE side, itemised, in rupees.

    `side` decides two of the six items and is not optional: stamp duty
    is charged to the buyer and CTT to the seller, so passing the wrong
    one understates a cost and overstates the other by the same amount.

    Returns a dict rather than a total, because a cost the operator
    cannot break down is a cost they cannot check against their
    contract note.
    """
    try:
        units = float(units or 0.0)
        price = float(price or 0.0)
    except (TypeError, ValueError):
        return _empty()
    if units <= 0 or price <= 0:
        # Unmeasured is not zero — but neither is it a charge. A caller
        # with no price gets an empty breakdown and a None total, and
        # renders an em dash.
        return _empty()

    buying = str(getattr(side, 'value', side)).upper() == 'BUY'
    turnover = units * price
    lots = units / float(rates.get('units_per_lot') or units or 1.0) \
        if rates.get('units_per_lot') else 0.0

    brokerage = (float(rates.get('brokerage_per_order') or 0.0) * orders
                 + float(rates.get('brokerage_per_lot') or 0.0) * lots
                 + turnover * float(rates.get('brokerage_pct') or 0.0) / 100.0)
    exchange = turnover * float(rates.get('exchange_txn_pct') or 0.0) / 100.0
    sebi = turnover * float(rates.get('sebi_pct') or 0.0) / 100.0
    # ONE SIDE EACH. This is the asymmetry.
    stamp = (turnover * float(rates.get('stamp_duty_pct_buy') or 0.0) / 100.0
             if buying else 0.0)
    ctt = (0.0 if buying else
           turnover * float(rates.get('ctt_pct_sell') or 0.0) / 100.0)
    # GST is on the SERVICES, not on the turnover.
    gst = (brokerage + exchange) * float(rates.get('gst_pct') or 0.0) / 100.0

    return {'brokerage': brokerage, 'exchange': exchange, 'sebi': sebi,
            'stamp_duty': stamp, 'ctt': ctt, 'gst': gst,
            'turnover': turnover,
            'total': brokerage + exchange + sebi + stamp + ctt + gst}


def _empty():
    return {'brokerage': 0.0, 'exchange': 0.0, 'sebi': 0.0, 'stamp_duty': 0.0,
            'ctt': 0.0, 'gst': 0.0, 'turnover': 0.0, 'total': None}


def round_turn(legs, settings):
    """Both legs, both ends, itemised and totalled.

    `legs` is a list of dicts: `{units, price, side, segment}` — the
    side each leg is ENTERED on. The exit charges the OPPOSITE side of
    each, which is where the asymmetry earns its keep: a spread that is
    long the near month and short the far one pays stamp duty on one leg
    going in and CTT on the other, then swaps them coming out.
    """
    itemised = {'entry': [], 'exit': []}
    total = 0.0
    measured = False
    for leg in legs or ():
        rates = schedule_for(settings, leg.get('segment'))
        side = str(getattr(leg.get('side'), 'value',
                           leg.get('side'))).upper()
        opposite = 'SELL' if side == 'BUY' else 'BUY'
        for end, at_side in (('entry', side), ('exit', opposite)):
            charge = leg_charges(leg.get('units'), leg.get('price'), at_side,
                                 rates)
            itemised[end].append(dict(charge, side=at_side,
                                      symbol=leg.get('symbol')))
            if charge['total'] is not None:
                total += charge['total']
                measured = True
    return {'legs': itemised,
            # None where nothing could be priced. NEVER 0.0, which
            # would read as "this trade is free".
            'total': total if measured else None}


def crossing_cost(md, lots_a, contract_a, lots_b, contract_b, settings=None):
    """The bid-ask both legs pay, in rupees, each leg in its OWN units.

    Pricing both legs' bid-asks off leg A's units is exact only when the
    legs match in lots and lot size. On a GOLD/GOLDM pair that is a
    tenfold error, and the stat-arb engine made exactly this mistake on
    XAGUSD/XAUUSD: gold's 0.24 spread charged against silver's 5,000
    units, $1,200 for a leg whose real cost was $28.
    """
    settings = settings or {}
    units_a = (lots_a or 0.0) * (contract_a or 0.0)
    units_b = (lots_b or 0.0) * (contract_b or 0.0)
    width_a = (md or {}).get('leg_a_width') or 0.0
    width_b = (md or {}).get('leg_b_width') or 0.0
    factor = settings.get('SPREAD_COST_FACTOR', 1.0)
    return (width_a * units_a + width_b * units_b) * factor


def mark_fees(position, settings, exit_prices=None):
    """The charges still outstanding on a position marked at the CLOSING
    touch — both legs, both ends.

    The crossing is already in the two prices; this is the number
    subtracted from `SpreadPosition.mark()` to get net P&L, and it is
    what EXIT_IF_PROFIT reads.

    Returns 0.0 rather than None when nothing is configured, because
    this one feeds arithmetic — but `is_configured` is what the SCREEN
    reads, and it says whether the zero means anything.
    """
    legs = []
    for key, fill in (('a', getattr(position, 'leg_a', None)),
                      ('b', getattr(position, 'leg_b', None))):
        if fill is None or not fill.contract_size:
            continue
        price = (exit_prices or {}).get(key) or fill.price
        legs.append({'units': (fill.volume or 0.0) * fill.contract_size,
                     'price': price, 'side': fill.side,
                     'segment': fill.segment, 'symbol': fill.symbol})
    return round_turn(legs, settings)['total'] or 0.0
