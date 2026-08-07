"""What the signal wanted versus what the broker actually gave us.

Ported from the W3 basis system (INR). The honest answer needs THREE prices
per leg, not two, because the signal and the fill are not quoted on the same
thing:

    mid    the price the strategy sees — the spread is built from mids, so this
           is what the z-score, edge filter and exit ladder were evaluated on.
    quote  the executable touch at that instant (ask to buy, bid to sell) —
           what the decision was actually worth.
    fill   what came back from the broker.

Splitting them separates two costs that behave completely differently:

    crossing  = mid → quote. Known BEFORE the trade, on-screen, already modelled
                in the cost stack. Nothing has gone wrong when you pay it.
    slippage  = quote → fill. The surprise: the market moved between decision
                and fill, or the broker filled away from its quote. This is the
                number that tells you whether the cost model still holds.

Sign convention everywhere: POSITIVE IS A COST. Negative slippage is price
improvement (it happens on limit fills), so the sign is kept, not abs()'d.

Spread→₹ conversion uses ``k`` = ``sizing.spread_units`` (L_B×C_B, ₹ per 1.0 of
spread), the contract-aware multiplier — NOT leg-A units.
"""

from __future__ import annotations

from .exits import SELL_BASIS, BUY_BASIS, _dir

BUY = "BUY"
SELL = "SELL"


def touch(bid, ask, side):
    """The price we must actually pay/receive to trade NOW."""
    if bid is None or ask is None:
        return None
    return ask if str(side).upper() == BUY else bid


def mid(bid, ask):
    if bid is None or ask is None:
        return None
    return (bid + ask) / 2.0


def leg_report(side, bid, ask, fill, symbol=None):
    """One leg's decision-to-fill account. ``bid``/``ask`` are the quote at
    DECISION time — the operator is asking what the signal thought it got."""
    quote = touch(bid, ask, side)
    reference = mid(bid, ask)
    buying = str(side).upper() == BUY
    report = {
        "symbol": symbol,
        "side": str(side).upper(),
        "mid": reference,
        "quote": quote,
        "fill": fill,
        "crossing": None,
        "slippage": None,
        "total": None,
    }
    if quote is not None and reference is not None:
        report["crossing"] = (quote - reference) if buying else (reference - quote)
    if quote is not None and fill is not None:
        report["slippage"] = (fill - quote) if buying else (quote - fill)
    if reference is not None and fill is not None:
        report["total"] = (fill - reference) if buying else (reference - fill)
    return report


def selling_the_spread(direction, closing):
    """Is this order SELLING the spread (wants a high level) or BUYING it (wants
    a low one)? A short-spread position sells to get in and buys to get out, so
    the same direction flips sign between entry and exit."""
    short = _dir(direction) == SELL_BASIS
    return short != bool(closing)


def _signed(selling, quote_level, exec_level):
    """Positive = the level we got was worse than the level quoted."""
    if quote_level is None or exec_level is None:
        return None
    return (quote_level - exec_level) if selling else (exec_level - quote_level)


def pair_report(direction, closing, beta, k, spot, futures):
    """Combine two ``leg_report``s into the spread-level account. ``spot`` and
    ``futures`` are leg reports; ``beta`` is the hedge ratio; ``k`` is
    ₹-per-1.0-spread (sizing.spread_units), so the spread numbers become ₹.
    The spread is ``futures − beta*spot`` so each leg enters with the same
    weight it has in the spread — the leg numbers add up to the spread number
    exactly (tests assert it)."""
    selling = selling_the_spread(direction, closing)

    def level(key_spot, key_fut):
        if spot.get(key_spot) is None or futures.get(key_fut) is None:
            return None
        return futures[key_fut] - beta * spot[key_spot]

    mid_spread = level("mid", "mid")
    quote_spread = level("quote", "quote")
    exec_spread = level("fill", "fill")

    crossing = _signed(selling, mid_spread, quote_spread)
    slip = _signed(selling, quote_spread, exec_spread)
    total = _signed(selling, mid_spread, exec_spread)

    def inr(value):
        return None if value is None or not k else value * k

    return {
        "selling_spread": selling,
        "closing": bool(closing),
        "hedge_ratio": beta,
        "k": k,
        "decision_spread": mid_spread,     # what the signal saw
        "quoted_spread": quote_spread,     # what it was executable at
        "executed_spread": exec_spread,    # what the broker gave us
        "crossing_spread": crossing,
        "slippage_spread": slip,
        "total_spread": total,
        "crossing_inr": inr(crossing),
        "slippage_inr": inr(slip),
        "total_inr": inr(total),
        "legs": {"spot": spot, "futures": futures},
    }


def build(direction, closing, beta, k, spot_side, futures_side,
          reference, spot_fill, futures_fill,
          spot_symbol=None, futures_symbol=None):
    """The whole report from a decision-time snapshot (``reference`` carries
    spot_bid/spot_ask/futures_bid/futures_ask). Returns None when the snapshot
    is missing — an unmeasurable trade reports nothing rather than a zero,
    because a zero here would read as perfect execution."""
    if not reference:
        return None
    spot = leg_report(spot_side, reference.get("spot_bid"),
                      reference.get("spot_ask"), spot_fill, spot_symbol)
    futures = leg_report(futures_side, reference.get("futures_bid"),
                         reference.get("futures_ask"), futures_fill,
                         futures_symbol)
    return pair_report(direction, closing, beta, k, spot, futures)


def summarise(report, digits=4):
    """One log line. Spread units first, ₹ second."""
    if not report:
        return "slippage: not measured (no decision snapshot)"
    if report.get("executed_spread") is None:
        return "slippage: not measured (a leg did not fill)"

    def fmt(value):
        return "n/a" if value is None else f"{value:+.{digits}f}"

    money = report.get("slippage_inr")
    return (
        f"decision {report['decision_spread']:.{digits}f} → quoted "
        f"{report['quoted_spread']:.{digits}f} → filled "
        f"{report['executed_spread']:.{digits}f} | crossing "
        f"{fmt(report['crossing_spread'])} + slippage "
        f"{fmt(report['slippage_spread'])} = {fmt(report['total_spread'])}"
        + (f" (₹{money:+,.2f} slipped)" if money is not None else "")
    )
