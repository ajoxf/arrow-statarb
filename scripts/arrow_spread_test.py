#!/usr/bin/env python3
"""A controlled live probe for ONE spread, on the static-IP box.

Phase 8. Read this whole docstring before running it with `--yes`.

This is the deliberate, watched, one-lot first trade — the thing you do
BEFORE trusting a ladder with a real click. It is separate from the
terminal on purpose: everything here is explicit, nothing is on a
timer, and every step is printed before it happens.

    # 1. read-only. Proves the session, the master and the BOOK.
    python scripts/arrow_spread_test.py \\
        --leg-a GOLD05DEC25F --leg-b GOLD05FEB26F --segment mcx_fo

    # 2. a resting limit, far from the market, placed then cancelled.
    #    Proves place / modify / cancel and the order lifecycle.
    python scripts/arrow_spread_test.py ... --place-limit

    # 3. ONE LOT, BOTH LEGS, AT MARKET, then flat again. Guarded.
    python scripts/arrow_spread_test.py ... --round-trip --yes

## WHAT IT REFUSES TO DO

- It will not run step 3 without `--yes`, and `--yes` is not stored
  anywhere.
- It will not run at all if the book is LTP-only: a spread priced off
  last trades is a spread priced off nothing, and it says so.
- It will not size anything without a lot size from the master. Units
  are lots x LotSize; a defaulted 1 fills at a hundredth of the size on
  GOLD, and it FILLS.
- It will not unwind an UNRESOLVED order. Trading against an order that
  is still working opens the opposite position, which then fills
  against the leg you were cancelling. It stops and tells you to look.

## WHAT TO WATCH WHILE IT RUNS

The number that matters is not the P&L — one lot for thirty seconds
proves nothing about edge. It is:

- the two fills' prices against the two touches printed before them
  (that is the slippage the ladder's guard will be measured in);
- how long the second leg took after the first (the naked window);
- whether the exchange's net matches what this script thinks it did.

Have the broker's own order book open in another window. If this
script and that screen ever disagree, the screen is right.
"""

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def say(message=''):
    print(message, flush=True)


def rule(title):
    say()
    say(f'--- {title} ' + '-' * max(0, 60 - len(title)))


def main(argv=None):
    parser = argparse.ArgumentParser(
        description='A controlled live probe for one spread.')
    parser.add_argument('--leg-a', required=True)
    parser.add_argument('--leg-b', required=True)
    parser.add_argument('--segment', default='mcx_fo')
    parser.add_argument('--lots', type=float, default=1.0)
    parser.add_argument('--product', default='NRML', choices=('NRML', 'MIS'))
    parser.add_argument('--place-limit', action='store_true',
                        help='place a resting limit far from the market, '
                             'then modify it, then cancel it')
    parser.add_argument('--round-trip', action='store_true',
                        help='REAL orders: both legs on, then both off')
    parser.add_argument('--yes', action='store_true',
                        help='required for --round-trip')
    parser.add_argument('--hold-sec', type=float, default=10.0)
    args = parser.parse_args(argv)

    try:
        from dotenv import load_dotenv
        load_dotenv('.env')
    except ImportError:
        pass

    from arrowtrader.broker import ArrowSession
    from arrowtrader.config import AccountConfig, load_raw
    from arrowtrader.legs import ArrowLeg
    from arrowtrader.segments import SegmentTable
    from arrowtrader.sizing import units

    raw = load_raw('config.json')
    account = AccountConfig.from_dict(
        (raw.get('account') or {}).get('name', 'arrow'), raw.get('account'))
    missing = account.missing_secrets()
    if missing:
        say(f'REFUSING: these credentials are not set: {", ".join(missing)}')
        return 2

    rule('session')
    session = ArrowSession(account, SegmentTable())
    if not session.initialize():
        say(f'REFUSING: {session.last_error}')
        return 2
    say(f'connected. master: {session.master.rows} contracts across '
        f'{sorted(session.master.exch_segs)}')
    leg = ArrowLeg(account.name, session)

    rule('contracts')
    specs = {}
    for name, symbol in (('A', args.leg_a), ('B', args.leg_b)):
        report = session.symbol_report(symbol)
        if not report.get('found'):
            say(f'REFUSING: {report.get("error")}')
            return 2
        specs[name] = report
        say(f'leg {name}  {symbol}')
        say(f'          lot size {report["lot_size"]} units/lot, '
            f'tick {report["tick_size"]}, expires {report["expiry"]} '
            f'({report["days_to_expiry"]} days), '
            f'freeze {report["freeze_qty"]}')
        for problem in report.get('problems') or ():
            say(f'          PROBLEM: {problem}')
        if report['lot_size'] is None:
            say('REFUSING: nothing can be sized without a lot size. Units '
                'are lots x LotSize, and a defaulted 1 does not fail — it '
                'fills at a hundredth of the size.')
            return 2
        if report['days_to_expiry'] is not None \
                and report['days_to_expiry'] <= 7:
            say(f'          WARNING: inside the tender window. MCX settles '
                f'PHYSICALLY; a position carried in can be assigned.')

    rule('the book')
    ticks = {}
    for name, symbol in (('A', args.leg_a), ('B', args.leg_b)):
        tick = leg.tick(symbol)
        ticks[name] = tick
        say(f'leg {name}  bid {(tick or {}).get("bid")}  '
            f'ask {(tick or {}).get("ask")}  '
            f'last {(tick or {}).get("last")}  '
            f'depth {"yes" if (tick or {}).get("depth") else "NONE"}')
    if not all((tick or {}).get('executable') for tick in ticks.values()):
        say()
        say('REFUSING: at least one leg is quoting a last trade and no book.')
        say('A spread priced off last trades is priced off nothing: there is')
        say('no mid to centre on, no executable touch, and no slippage to')
        say('measure. Get QuoteMode.FULL / DataMode.DEPTH working first —')
        say('this is blocker 3.2 and it gates everything below.')
        return 3

    short = ticks['B']['bid'] - ticks['A']['ask']
    long_ = ticks['B']['ask'] - ticks['A']['bid']
    say()
    say(f'spread  sell at {short:.4f}   buy at {long_:.4f}   '
        f'round turn {long_ - short:.4f}')

    units_a = units(args.lots, specs['A']['lot_size'])
    units_b = units(args.lots, specs['B']['lot_size'])
    say(f'size    {args.lots:g} lot(s) = {units_a} units of A / '
        f'{units_b} units of B, product {args.product}')

    rule('margin')
    margin = leg.margin_for([(args.leg_a, 'SELL', units_a),
                             (args.leg_b, 'BUY', units_b)])
    say(f'both legs together: {margin if margin is not None else "—"}')
    if margin is None:
        say('the SDK exposes no margin calculator this build could find, so')
        say('the take-profit target is disabled in the terminal. It is NOT')
        say('derived from notional — that would be a guess presented as a')
        say('figure.')

    if args.place_limit:
        rule('a resting limit, far from the market')
        far = round(ticks['B']['bid'] * 0.90 / specs['B']['tick_size']) \
            * specs['B']['tick_size']
        say(f'placing BUY {units_b} {args.leg_b} @ {far} (10% below the bid)')
        placed = leg.place_limit(args.leg_b, 'BUY', args.lots, far,
                                 product=args.product, comment='PROBE')
        say(f'  -> {placed}')
        if not placed.get('ok'):
            say('REFUSING to go further: if a resting limit will not place, '
                'nothing above it will work either.')
            return 4
        time.sleep(1.0)
        moved = round(far * 1.01 / specs['B']['tick_size']) \
            * specs['B']['tick_size']
        say(f'modifying to {moved} (this is the re-peg the quoter relies on)')
        say(f'  -> {leg.modify_order(placed["ticket"], moved, args.leg_b)}')
        time.sleep(1.0)
        say('cancelling')
        cancelled = leg.cancel_order(placed['ticket'])
        say(f'  -> {cancelled}')
        if cancelled.get('leaked_fill'):
            say('!! IT FILLED BEFORE THE CANCEL. You are LONG '
                f'{cancelled.get("filled_volume")} lots of {args.leg_b} and '
                f'it is NOT hedged. Flatten it by hand, now.')
            return 5

    if not args.round_trip:
        rule('done')
        say('read-only checks passed. Add --place-limit for the order')
        say('lifecycle, then --round-trip --yes for one real lot.')
        return 0

    if not args.yes:
        say()
        say('REFUSING: --round-trip sends REAL orders on BOTH legs. Pass '
            '--yes if that is what you mean.')
        return 2

    rule('ROUND TRIP — REAL ORDERS')
    say(f'buying the spread: SELL {args.lots:g} {args.leg_a} / '
        f'BUY {args.lots:g} {args.leg_b}')
    say(f'the touch we are aiming at: {long_:.4f}')
    say()
    fills = {}
    started = time.time()
    for name, symbol, side in (('A', args.leg_a, 'SELL'),
                               ('B', args.leg_b, 'BUY')):
        say(f'  leg {name}: {side} {args.lots:g} {symbol} ...')
        result = leg.order(symbol, side, args.lots, product=args.product,
                           comment='PROBE')
        fills[name] = result
        say(f'    -> filled {result.get("filled_volume")} lots '
            f'@ {result.get("price")} (asked at '
            f'{result.get("requested_price")}) in '
            f'{(time.time() - started) * 1000:.0f}ms')
        if result.get('unresolved'):
            say()
            say('!! UNRESOLVED. This order is neither filled nor rejected.')
            say('   NOTHING has been unwound and nothing will be: trading')
            say('   against an order that is still working opens the')
            say('   opposite position, which then fills against the leg you')
            say('   were cancelling. Look at the broker\'s own order book.')
            say(f'   order id: {result.get("ticket")}')
            return 6
        if not result.get('ok'):
            say(f'!! REJECTED: {result.get("error")}')
            if name == 'B' and fills['A'].get('ok'):
                say('   leg A IS ON and unhedged. Unwinding it now.')
                say(f'   -> {leg.close_reduce(args.leg_a, "SELL", args.lots, product=args.product, comment="UNWIND")}')
            return 7

    entry = fills['B']['price'] - fills['A']['price']
    say()
    say(f'ON at {entry:.4f}. Slippage against the touch: '
        f'{entry - long_:+.4f} (positive is a cost).')
    say(f'net at the exchange: {leg.positions()}')
    say()
    say(f'holding {args.hold_sec:g}s ...')
    time.sleep(args.hold_sec)

    rule('flat again')
    for name, symbol, entry_side in (('A', args.leg_a, 'SELL'),
                                     ('B', args.leg_b, 'BUY')):
        result = leg.close_reduce(symbol, entry_side, args.lots,
                                  product=args.product, comment='PROBE')
        say(f'  leg {name}: closed {result.get("filled_volume")} '
            f'@ {result.get("price")}  {result.get("error") or ""}')
    say()
    say(f'net at the exchange: {leg.positions()}')
    say()
    say('CHECK THIS AGAINST THE BROKER\'S OWN SCREEN before you believe it.')
    session.shutdown()
    return 0


if __name__ == '__main__':
    sys.exit(main())
