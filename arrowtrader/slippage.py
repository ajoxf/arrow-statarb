"""What the clicks actually cost — and what could not be measured.

Ported from MT5-Trader's `slippage.py`. The shape of the report is the
same because the question is: entries measured against the touch the
click was taken at, exits against the CLOSING fills, positive is a cost
at both ends, and MARKET and LIMIT reported side by side because that
is the split the peg has to justify itself against.

TWO RULES CARRY THE WHOLE THING, AND BOTH ARE ABOUT ABSENCE.

**Unmeasured is never zero.** A fill that could not be priced is
counted as unmeasured and excluded from every average. Averaged in as
zero it would drag the mean towards "no slippage" in exact proportion
to how badly the recording was working — the report would look best
precisely when it knew least. Half the value here is the unmeasured
COUNT.

**The window is on the exchange's clock, or there is no window.** A
report cut at a cutoff computed on this machine's clock loses the
evening's trades on any box that is not in IST. Where the exchange
clock has not been measured, the caller passes None and the report
covers everything it has — and says so.

And one thing that is different here: on Arrow the clicked price is the
ONLY slippage protection in the system. `mt5.order_send` takes a
`deviation` the server enforces; `place_order` takes nothing. So this
report is not a curiosity about execution quality — it is the only
evidence that the guard is set to the right number.
"""

import csv
import io


def report(store, window=None, names=None):
    """The whole report, from the position ledger.

    Anchored on `opened_at`: a position belongs to the session it was
    PUT ON in, which is the session whose click its entry slippage was
    measured against. Anchoring on the close would move a trade carried
    overnight into the next day and credit its entry to a click nobody
    made that day.
    """
    names = names or {}
    start, end = (window or (None, None))
    try:
        positions = store.positions_between(start=start, end=end)
    except Exception as error:                          # noqa: BLE001
        return {'ok': False, 'error': f'the report could not be read: {error}'}

    entries, exits, unmeasured = [], [], {'entry': 0, 'exit': 0}
    by_type, by_pair, worst = {}, {}, []
    open_count = 0

    for row in positions:
        if row.get('closed_at') is None:
            open_count += 1
        key = row.get('pair_key') or '?'
        order_type = row.get('order_type') or 'MARKET'
        units = float(row.get('spread_units') or 0.0)

        entry = row.get('entry_slippage')
        if entry is None:
            unmeasured['entry'] += 1
        else:
            entries.append(entry)
            _bucket(by_type, order_type, 'entry', entry, units)
            _bucket(by_pair, key, 'entry', entry, units)
            worst.append({
                'pair_key': key, 'name': names.get(key, key),
                'order_type': order_type,
                'points': entry,
                # RANKED IN MONEY, not points: a point of a GOLD spread
                # and a point of a NATURALGAS spread are different
                # amounts of money, and the desk cares about the money.
                'money': entry * units,
                'at': row.get('opened_at'),
            })

        leaving = row.get('exit_slippage')
        if leaving is None:
            # An OPEN position has not exited; that is not a failure to
            # measure, and counting it as one would make every live
            # book look badly recorded.
            if row.get('closed_at') is not None:
                unmeasured['exit'] += 1
        else:
            exits.append(leaving)
            _bucket(by_type, order_type, 'exit', leaving, units)
            _bucket(by_pair, key, 'exit', leaving, units)

    worst.sort(key=lambda entry: -abs(entry['money']))
    for bucket in list(by_pair.values()) + list(by_type.values()):
        bucket['entry'] = _summarise(bucket['entry'])
        bucket['exit'] = _summarise(bucket['exit'])
    for key, bucket in by_pair.items():
        bucket['name'] = names.get(key, key)

    return {
        'ok': True,
        'window': {'start': start, 'end': end,
                   'note': ('every fill on record — the exchange clock has '
                            'not been measured, so there is no session to '
                            'cut at' if window is None else
                            'this session, cut on the exchange clock')},
        'counts': {'positions': len(positions), 'open': open_count},
        'overall': {
            'entry': _summarise(entries),
            'exit': _summarise(exits),
            'round_trip': _summarise(entries + exits),
        },
        #: THE NUMBER THAT KEEPS THE REST HONEST. Averaged in as zero,
        #: these would make the report look best when it knew least.
        'unmeasured': unmeasured,
        'by_order_type': by_type,
        'by_pair': by_pair,
        'worst': worst[:10],
        'journal': _journal(store, start, end),
        'currency': 'INR',
    }


def _bucket(table, key, end, value, units):
    row = table.setdefault(key, {'entry': [], 'exit': [], 'units': units})
    row[end].append(value)


def _summarise(values):
    """mean / worst / count, or a row of None. NEVER a zero mean.

    An empty bucket means nothing was measured, and `None` is what the
    screen renders as an em dash. A mean of 0.0 over no samples reads
    as perfect execution.
    """
    if not values:
        return {'count': 0, 'mean': None, 'worst': None, 'best': None}
    return {
        'count': len(values),
        'mean': sum(values) / len(values),
        # Positive is a cost, so the WORST is the largest.
        'worst': max(values),
        'best': min(values),
    }


def _journal(store, start, end):
    """The fills over the same window, as a check on coverage.

    Counted here so a reader can see the report is drawn from
    something: 4 positions against 120 fills is a report about 3% of
    the session, and it should be obvious rather than inferred.
    """
    try:
        fills = store.fills_between(start=start, end=end, ours_only=True)
    except Exception:                                   # noqa: BLE001
        return {'fills': None, 'note': 'the journal could not be read'}
    unattributed = 0
    try:
        unattributed = len(store.unattributed_fills())
    except Exception:                                   # noqa: BLE001
        pass
    return {
        'fills': len(fills),
        'unattributed': unattributed,
        'note': ('ownership on this venue is an INFERENCE — there is no '
                 'magic number on a netted fill' if unattributed else None),
    }


def as_csv(store, window=None, names=None):
    """The report as a file. EMPTY CELLS where nothing was measured.

    Not zeros. A zero in a slippage column is a claim that the fill was
    perfect, and a spreadsheet will happily average it.
    """
    built = report(store, window=window, names=names)
    out = io.StringIO()
    writer = csv.writer(out)
    if not built.get('ok'):
        writer.writerow(['error', built.get('error')])
        return out.getvalue()
    writer.writerow(['pair', 'name', 'order_type', 'end', 'count',
                     'mean_points', 'worst_points', 'currency'])
    for key, row in built['by_pair'].items():
        for end in ('entry', 'exit'):
            found = row[end]
            writer.writerow([
                key, row.get('name', key), '', end, found['count'],
                '' if found['mean'] is None else f"{found['mean']:.6f}",
                '' if found['worst'] is None else f"{found['worst']:.6f}",
                'INR'])
    writer.writerow([])
    writer.writerow(['unmeasured entries', built['unmeasured']['entry']])
    writer.writerow(['unmeasured exits', built['unmeasured']['exit']])
    writer.writerow(['window', built['window']['note']])
    return out.getvalue()
