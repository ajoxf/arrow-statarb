"""What the instrument master ACTUALLY says — the raw rows.

    python scripts/arrow_master_dump.py CRUDEOIL

Every theory about why a contract is missing is a guess until somebody
looks at the row. This logs in with the credentials already in `.env`,
downloads the master, and prints the rows matching a search term
VERBATIM — the broker's own field names and values — beside how this
build classifies each one and why.

It places no orders and changes nothing. It is safe to run against a
live account during market hours.

CREDENTIALS ARE READ FROM `.env` AND NEVER PRINTED.
"""

import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('needle', nargs='?', default='CRUDEOIL',
                        help='part of a trading symbol or underlying')
    parser.add_argument('--config', default='config.json')
    parser.add_argument('--env', default='.env')
    parser.add_argument('--rows', type=int, default=8,
                        help='how many raw rows to print in full')
    parser.add_argument('--probe', metavar='SYMBOL',
                        help='ask Arrow for a QUOTE on this symbol under '
                             'every Exchange value the SDK has, and report '
                             'which one answers with a book. Read-only.')
    args = parser.parse_args(argv)

    from arrowtrader.broker import ArrowSession
    from arrowtrader.config import TraderConfig, load_env
    from arrowtrader.segments import SegmentTable

    # Read `.env` OURSELVES. Depending on python-dotenv meant that
    # running from outside the virtualenv skipped the file in silence
    # and reported every credential missing, with `.env` sitting right
    # there holding all of them.
    loaded = load_env(args.env)
    print(f'{args.env}: {len(loaded)} value(s) read'
          if loaded else
          f'{args.env}: nothing read from it '
          f'({"no such file" if not os.path.exists(args.env) else "it set nothing new"})')

    config = TraderConfig.from_file(args.config)
    missing = config.account.missing_secrets()
    if missing:
        print('these credentials are not set:', ', '.join(missing))
        print(f'they are read from {os.path.abspath(args.env)} — check that '
              f'file, or pass --env with the right path')
        return 1

    session = ArrowSession(config.account, SegmentTable(
        config.get('SEGMENTS_EXTRA')))
    print('connecting…')
    if not session.initialize():
        print('could not connect:', session.last_error)
        return 1

    master = session.master
    print(f'master: {master.rows} contracts, '
          f'from {session.master_sources}')
    print('segments in the master:')
    for name in sorted(master.exch_segs):
        count = master.exch_seg_counts.get(name, 0)
        key = session.segments.key_for_exch_seg(name)
        known = key or 'THIS BUILD KNOWS NO SEGMENT FOR IT'
        print(f'   {name:<8} {count:>8,}   -> {known}')
    if master.unknown_exch_segs:
        print('rows this build could not group:', master.unknown_exch_segs)

    needle = args.needle.upper()
    matches = [contract for contract in master.every()
               if needle in contract.trading_symbol
               or needle in (contract.underlying or '')]
    print(f'\n{len(matches)} contracts match {needle!r}')
    # BY SEGMENT AS WELL AS BY KIND. A contract on a segment this build
    # does not know has `segment=None` and is invisible to every
    # segment-filtered search — which looks exactly like a contract
    # that is not there.
    breakdown = {}
    for contract in matches:
        key = (contract.exch_seg, contract.segment, contract.kind)
        breakdown[key] = breakdown.get(key, 0) + 1
    print('   by segment and kind:')
    for (exch, segment, kind), count in sorted(breakdown.items()):
        mark = '' if segment else '   <-- NO SEGMENT: INVISIBLE TO THE PICKER'
        print(f'      {exch:<8} {str(segment):<10} {kind:<8} {count:>7,}{mark}')

    futures = [c for c in matches if c.kind == 'future']
    print(f'\nFUTURES ({len(futures)}):')
    for contract in futures[:20]:
        print(f'   {contract.trading_symbol:<22} {contract.exch_seg:<7} '
              f'seg={str(contract.segment):<8} expiry={contract.expiry} '
              f'lot={contract.lot_size} tick={contract.tick_size}')
    if not futures:
        print('   NONE — and that is the thing to explain.')

    print(f'\nThe first {args.rows} matching rows, EXACTLY as Arrow sent '
          f'them:')
    for contract in matches[:args.rows]:
        print(f'\n   {contract.trading_symbol}  ->  {contract.kind}')
        print('   ' + json.dumps(contract.raw, indent=6, default=str)
              .replace('\n', '\n   '))
    if not matches:
        print('   (nothing matched — try a shorter search, e.g. CRUDE)')
    if args.probe:
        probe(session, args.probe)
    session.shutdown()
    return 0


def probe(session, symbol):
    """Which `Exchange` value does this contract actually quote under?

    THIS IS A MEASUREMENT, NOT A GUESS. The master says
    `Exchange: NSE, Segment: CO, ExchSeg: NSECO`, and none of those
    three is necessarily the value an order carries — MCX turned out to
    need `MCXFO` where the master said `MCX`. The SDK's enum is short,
    the call is read-only, and asking is strictly better than reasoning
    about it.

    A value that answers with a BID AND AN ASK is the one to use. One
    that answers with nothing is not refused — it is simply wrong, and
    that is the shape of failure that reads on a ladder as a contract
    which is not trading.
    """
    import pyarrow_client as arrow
    from arrowtrader import quotes

    contract = session.master.contract(symbol)
    if contract is None:
        print(f'\n{symbol} is not in the master')
        return
    print(f'\nprobing {symbol} ({contract.exch_seg}) for the Exchange value '
          f'it quotes under:')
    mode = session._quote_mode()
    for name in sorted(e.name for e in arrow.Exchange):
        value = getattr(arrow.Exchange, name)
        try:
            raw = session._client.get_quotes(mode, [(symbol, value)])
        except Exception as error:                      # noqa: BLE001
            print(f'   {name:<8} refused: {str(error)[:90]}')
            continue
        row = raw[0] if isinstance(raw, list) and raw else raw
        tick = quotes.normalise_quote(row, scale=session.quote_scale) \
            if isinstance(row, dict) else None
        if tick and tick.get('executable'):
            print(f'   {name:<8} ANSWERED: bid {tick["bid"]} ask '
                  f'{tick["ask"]} <-- USE THIS ONE')
            key = session.segments.key_for_exch_seg(contract.exch_seg)
            if key and session.segments.exchange_for(key) != value.value:
                print(f'\n   Put this in config.json under "settings" and '
                      f'restart — no code change:\n')
                print('   "SEGMENTS_EXTRA": {"%s": {"exch_seg": "%s", '
                      '"exchange": "%s", "label": "%s", '
                      '"kinds": ["future", "option"]}}'
                      % (key, contract.exch_seg, value.value,
                         session.segments.get(key).label))
        elif tick and tick.get('last') is not None:
            print(f'   {name:<8} answered a last trade ({tick["last"]}) and '
                  f'no book')
        else:
            print(f'   {name:<8} nothing')


if __name__ == '__main__':
    raise SystemExit(main())
