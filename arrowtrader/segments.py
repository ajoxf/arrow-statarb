"""Exchanges and segments — data, not a hardcoded map with a refusal.

MT5-Trader has no concept of a segment: a symbol belongs to a terminal
and that is the whole of it. Here every instrument carries an
`ExchSeg` (the master's exchange+segment field, e.g. `MCXFO`), and
orders and quotes carry an `Exchange` enum value (`MCX`, `NFO`, ...).
The two are not the same field and conflating them is how an order
goes to the wrong exchange.

The stat-arb broker (`arrow_statarb/brokers/arrow_broker.py`) hardcodes
four segments and refuses MCX outright:

    if exchange_segment.lower() in ("mcx_fo", "mcx"):
        return {... "message": "Arrow does not support MCX."}

That refusal is a finding from an account that had no commodity
segment enabled, not a property of the exchange. Here the segment
table is DATA — extendable from config, and checked against what the
instrument master actually contains — so "MCX first, NSE next" is one
switch rather than a fork.

WHETHER ARROW ROUTES MCX FOR A GIVEN ACCOUNT IS STILL AN OPEN
QUESTION (blocker 3.1 in the build prompt). This module makes MCX
expressible; it does not claim it is enabled. `available_segments()`
answers that from the master, at runtime, and the Exchanges page shows
the answer.
"""


class Segment:
    """One tradeable segment: what the master calls it, what an order
    calls it, and what kind of thing lives there."""

    def __init__(self, key, exch_seg, exchange, label, kinds=('future',)):
        #: our own stable key, used in config and in a pair's legs.
        self.key = key
        #: the master's `ExchSeg` value — how instruments are grouped.
        self.exch_seg = exch_seg
        #: the `Exchange` enum value an order and a quote carry.
        self.exchange = exchange
        self.label = label
        self.kinds = tuple(kinds)

    def to_dict(self):
        return {'key': self.key, 'exch_seg': self.exch_seg,
                'exchange': self.exchange, 'label': self.label,
                'kinds': list(self.kinds)}

    def __repr__(self):
        return f'<Segment {self.key} {self.exch_seg}->{self.exchange}>'


#: The segments this terminal knows how to name. MCX FIRST, because
#: that is the product; NSE and BSE follow the same shape, which is the
#: point of the table.
#:
#: `MCXFO` is the master's own spelling for the MCX futures segment and
#: `MCX` the order-side exchange. Both are asserted against the master
#: at connect time rather than trusted — see `available_segments`.
BUILT_IN = (
    Segment('mcx_fo', 'MCXFO', 'MCX', 'MCX futures',
            kinds=('future', 'option')),
    Segment('nse_fo', 'NSEFO', 'NFO', 'NSE F&O',
            kinds=('future', 'option')),
    Segment('nse_cm', 'NSECM', 'NSE', 'NSE cash', kinds=('cash',)),
    Segment('bse_fo', 'BSEFO', 'BFO', 'BSE F&O',
            kinds=('future', 'option')),
    Segment('bse_cm', 'BSECM', 'BSE', 'BSE cash', kinds=('cash',)),
)


class SegmentTable:
    """The segments in play, built-ins plus anything config adds.

    Config wins on a key collision: a broker that spells the MCX
    segment differently is a config line, not a code change.
    """

    def __init__(self, extra=None):
        self._by_key = {segment.key: segment for segment in BUILT_IN}
        for key, raw in (extra or {}).items():
            raw = raw or {}
            self._by_key[key] = Segment(
                key,
                raw.get('exch_seg') or key.upper(),
                raw.get('exchange') or key.upper(),
                raw.get('label') or key,
                kinds=raw.get('kinds') or ('future',))

    def __contains__(self, key):
        return self._normal(key) in self._by_key

    def __iter__(self):
        return iter(self._by_key.values())

    @staticmethod
    def _normal(key):
        return str(key or '').strip().lower()

    def get(self, key):
        """The segment, or None. None is 'we do not know this segment',
        which is not the same as 'the exchange refuses it'."""
        return self._by_key.get(self._normal(key))

    def exchange_for(self, key):
        segment = self.get(key)
        return segment.exchange if segment else None

    def exch_seg_for(self, key):
        segment = self.get(key)
        return segment.exch_seg if segment else None

    def key_for_exch_seg(self, exch_seg):
        """The reverse lookup: the master says `MCXFO`, we say `mcx_fo`."""
        needle = str(exch_seg or '').strip().upper()
        for segment in self._by_key.values():
            if segment.exch_seg == needle:
                return segment.key
        return None

    def to_dict(self):
        return {key: segment.to_dict()
                for key, segment in self._by_key.items()}


def available_segments(table, master_exch_segs, sdk_exchanges=None):
    """Which segments are ACTUALLY usable, and why each other one is not.

    Two independent facts have to line up before a segment can be
    traded, and they fail in different ways with the same symptom:

    - the instrument master has to carry contracts for it (otherwise
      the account is not entitled to the segment, or the master is
      still loading);
    - the SDK's `Exchange` enum has to carry the value an order needs
      (otherwise the order cannot even be addressed).

    Both are reported, per segment, in words — because "MCX does not
    work" has two completely different fixes depending which one it is.

    `sdk_exchanges` is None when it could not be read. Unmeasured is
    not zero: the enum check is then reported as unknown rather than as
    a failure.
    """
    seen = {str(value or '').strip().upper()
            for value in (master_exch_segs or ())}
    known = (None if sdk_exchanges is None
             else {str(value or '').strip().upper()
                   for value in sdk_exchanges})
    out = {}
    for segment in table:
        in_master = segment.exch_seg in seen
        in_sdk = None if known is None else segment.exchange in known
        if in_master and in_sdk is not False:
            note = f'{segment.label} is ready'
        elif not in_master:
            note = (f'the instrument master carries no {segment.exch_seg} '
                    f'contracts — this account may not be entitled to '
                    f'{segment.label}. Ask Arrow to enable the segment.')
        else:
            note = (f"the SDK's Exchange enum has no {segment.exchange} "
                    f"value — this build of pyarrow-client cannot address "
                    f"{segment.label} at all. Upgrade the SDK.")
        out[segment.key] = {
            'key': segment.key,
            'label': segment.label,
            'exch_seg': segment.exch_seg,
            'exchange': segment.exchange,
            'in_master': in_master,
            #: None = could not be read. NOT False.
            'in_sdk': in_sdk,
            'ready': bool(in_master and in_sdk is not False),
            'note': note,
        }
    return out
