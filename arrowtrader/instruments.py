"""The instrument master: what a contract IS, read rather than typed.

MT5-Trader reads `trade_contract_size`, `volume_min`, `volume_step`,
`point` and `expiration_time` off MT5 on every resolve, and refuses to
let anyone type them, because a contract size somebody typed is a
contract size that can be wrong. This is the same discipline against
Arrow's `/all` master: ~223k TitleCase rows, delivered as an
octet-stream that may or may not be gzipped and may be JSON or CSV.

THE ONE RULE THAT IS DIFFERENT FROM THE STAT-ARB VERSION.

`arrow_statarb`'s `resolve_lot_size` ends:

    logger.warning("lot size unknown for %s — using 1 (order may fail)")
    return 1

That is exactly the failure this product cannot have. Quantity on the
wire is `lots x LotSize`; a LotSize that silently reads 1 does not make
the order fail, it makes it succeed at a hundredth of the intended
size — or, on the other side of the same arithmetic, sizes a hedge that
does not hedge. **Unknown is None here, and None refuses.**

Fields, from Arrow's master (read case-insensitively, always):

    ExchSeg        MCXFO / NSEFO / NSECM ...  the exchange AND segment
    Exchange       just "NSE" — NOT the segment; do not use it
    TradingSymbol  the tradeable/order symbol, e.g. GOLD05DEC25F
    Symbol         the underlying, e.g. GOLD
    OptionType     CE / PE; empty means a future
    StrikePrice    for options
    Expiry         30-Jun-2026
    LotSize        UNITS PER LOT, and it varies per expiry
    Token          int — what the WebSocket feed subscribes by
    TickSize       where the master carries it (see `tick_size`)
"""

import csv
import datetime
import gzip
import io
import json
import re


#: Where a lot size stops being plausible. A master row with LotSize 0
#: or a negative is corrupt, not "one".
def _positive_int(value):
    try:
        number = int(float(value))
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _positive_float(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def field(row, *names):
    """First present, non-empty value among `names`, case-insensitively.

    Arrow's master is TitleCase, its CSV export is not always, and
    different builds spell the same column three ways. Every read of a
    master row goes through here.
    """
    if not isinstance(row, dict):
        return None
    lowered = {str(key).lower(): value for key, value in row.items()}
    for name in names:
        value = lowered.get(str(name).lower())
        if value not in (None, ''):
            return value
    return None


def parse_master(payload):
    """Arrow's `/all` response → a list of dicts, whatever shape it came in.

    In practice it is `application/octet-stream`, so the SDK hands back
    raw bytes; depending on the CDN those bytes are gzip or not, and
    JSON or CSV. Every plausible shape is handled, because the
    alternative is an empty master that looks exactly like an account
    with no entitlements.
    """
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if isinstance(payload, dict):
        for key in ('data', 'instruments', 'result', 'items'):
            inner = payload.get(key)
            if isinstance(inner, list):
                return [row for row in inner if isinstance(row, dict)]
        return [payload]
    if not isinstance(payload, (bytes, bytearray, str)):
        return []

    raw = payload.encode() if isinstance(payload, str) else bytes(payload)
    if raw[:2] == b'\x1f\x8b':
        raw = gzip.decompress(raw)
    text = raw.decode('utf-8', errors='replace').strip()
    if not text:
        return []
    if text[0] in '[{':
        return parse_master(json.loads(text))
    sample = text[:4096]
    delimiter = '\t' if sample.count('\t') > sample.count(',') else ','
    return [dict(row) for row in csv.DictReader(io.StringIO(text),
                                                delimiter=delimiter)]


_MONTHS = {name: number for number, name in enumerate(
    ['JAN', 'FEB', 'MAR', 'APR', 'MAY', 'JUN', 'JUL', 'AUG', 'SEP', 'OCT',
     'NOV', 'DEC'], start=1)}


def parse_expiry(value):
    """A `datetime.date` from any of the four spellings Arrow uses, or None.

    ISO `2025-06-26`, `DD-Mon-YYYY` `30-Jun-2026`, `DDMonYY` `30JUN26`
    embedded in a trading symbol, and epoch seconds or milliseconds.
    None where no date is there — an expiry that cannot be read is not
    an expiry far in the future, and sorting one into next decade puts
    the wrong contract at the top of the picker.
    """
    text = str(value or '').strip().upper()
    if not text:
        return None
    if text.isdigit() and len(text) >= 8:
        try:
            epoch = int(text)
            if epoch > 10 ** 11:
                epoch //= 1000
            stamp = datetime.datetime.utcfromtimestamp(epoch)
            return datetime.date(stamp.year, stamp.month, stamp.day)
        except (ValueError, OSError, OverflowError):
            pass
    found = re.search(r'(\d{4})[-/](\d{1,2})[-/](\d{1,2})', text)
    if found:
        return _safe_date(int(found.group(1)), int(found.group(2)),
                          int(found.group(3)))
    found = re.search(r'(\d{1,2})?[-\s]*'
                      r'(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)'
                      r'[-\s]*(\d{2,4})', text)
    if found:
        day = int(found.group(1)) if found.group(1) else 1
        year = int(found.group(3))
        year += 2000 if year < 100 else 0
        return _safe_date(year, _MONTHS[found.group(2)], day)
    found = re.search(r'(\d{1,2})[-/](\d{1,2})[-/](\d{4})', text)
    if found:
        return _safe_date(int(found.group(3)), int(found.group(2)),
                          int(found.group(1)))
    return None


def _safe_date(year, month, day):
    try:
        return datetime.date(year, month, day)
    except ValueError:
        return None


class Contract:
    """One tradeable contract, as the master describes it.

    Every field that could not be read is None, and stays None all the
    way to the screen. A tick size of None draws an em dash; a lot size
    of None refuses an order. Neither is filled in with a plausible
    number on the way past.
    """

    __slots__ = ('trading_symbol', 'underlying', 'exch_seg', 'segment',
                 'kind', 'option_type', 'strike', 'expiry', 'lot_size',
                 'tick_size', 'token', 'freeze_qty', 'raw')

    def __init__(self, trading_symbol, underlying=None, exch_seg=None,
                 segment=None, kind='future', option_type=None, strike=None,
                 expiry=None, lot_size=None, tick_size=None, token=None,
                 freeze_qty=None, raw=None):
        self.trading_symbol = trading_symbol
        self.underlying = underlying
        self.exch_seg = exch_seg
        #: our own segment key (`mcx_fo`), or None where the master
        #: carries an ExchSeg this build does not know.
        self.segment = segment
        self.kind = kind
        self.option_type = option_type or None
        self.strike = strike
        self.expiry = expiry
        #: UNITS PER LOT. None means unknown, and unknown REFUSES.
        self.lot_size = lot_size
        self.tick_size = tick_size
        self.token = token
        #: The exchange's per-order maximum. This is the Indian
        #: `volume_max`, and it belongs to the exchange rather than to
        #: the broker.
        self.freeze_qty = freeze_qty
        self.raw = raw or {}

    def days_to_expiry(self, today=None):
        """Calendar days until expiry, or None where the expiry is unknown."""
        if self.expiry is None:
            return None
        today = today or datetime.date.today()
        return (self.expiry - today).days

    def to_dict(self):
        return {
            'trading_symbol': self.trading_symbol,
            'underlying': self.underlying,
            'exch_seg': self.exch_seg,
            'segment': self.segment,
            'kind': self.kind,
            'option_type': self.option_type,
            'strike': self.strike,
            'expiry': self.expiry.isoformat() if self.expiry else None,
            'lot_size': self.lot_size,
            'tick_size': self.tick_size,
            'token': self.token,
            'freeze_qty': self.freeze_qty,
        }

    def __repr__(self):
        return f'<Contract {self.trading_symbol} lot={self.lot_size}>'


#: Every spelling an option type has been seen in. NSE and BSE answer
#: `CE` / `PE`; MCX has been seen to answer the single letter, and to
#: leave the field empty altogether.
_OPTION_TYPES = ('CE', 'PE', 'C', 'P', 'CALL', 'PUT')

#: A contract symbol, taken apart: underlying, expiry, and what comes
#: AFTER the expiry — `F` for a future, or the option letter and its
#: strike. `CRUDEOILM17SEP26C8950`, `GOLD05DEC25F`, `SILVER05MAR26`.
#:
#: THE EXPIRY IS MATCHED EXPLICITLY, and that is the whole point of
#: this pattern rather than a simpler one. Looking for an option letter
#: followed by digits at the END of the symbol finds the P of SEP and
#: the C of DEC and OCT: `CRUDEOIL17SEP26` ends `P26`, so every
#: September future whose symbol stops at the expiry was read as a
#: call. Three months out of twelve, silently, on the exchange this
#: terminal exists for.
_SYMBOL = re.compile(
    r'^(?P<underlying>[A-Z&\-]+?)'
    r'(?P<day>\d{1,2})?'
    r'(?P<month>JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)'
    r'(?P<year>\d{2,4})'
    r'(?P<tail>.*)$')

#: What an option's tail looks like: the type letter and the strike.
_OPTION_TAIL = re.compile(r'^(CE|PE|C|P)\d')


def option_from_symbol(trading_symbol):
    """Is this symbol an option, judged from its own shape?

    True, False, or **None where the shape says nothing** — an
    unrecognised symbol is not evidence of a future.
    """
    found = _SYMBOL.match(str(trading_symbol or '').upper())
    if not found:
        return None
    tail = found.group('tail')
    if not tail or tail == 'F':
        return False
    if _OPTION_TAIL.match(tail):
        return True
    return None


def classify(exch_seg, option_type, trading_symbol=None, strike=None):
    """cash / option / future.

    THE FIELD IS NOT ALWAYS FILLED IN. `OptionType` is documented as
    `CE` / `PE` with empty meaning a future, and on MCX rows it has
    been seen empty on options — which classified an option AS A
    FUTURE. A picker filtered to futures then offers
    `CRUDEOILM17SEP26C8950`, a call, as though it were the September
    contract, and a spread built on it is not the spread anybody meant.

    So three things are asked, strongest first: the type field, then
    the STRIKE (an option has one and a future does not), then the
    symbol's own shape. Nothing here guesses from a partial match: a
    symbol this build cannot parse leaves the answer to the fields.
    """
    if str(exch_seg or '').upper().endswith('CM'):
        return 'cash'
    if str(option_type or '').strip().upper() in _OPTION_TYPES:
        return 'option'
    try:
        if strike is not None and float(strike) > 0:
            return 'option'
    except (TypeError, ValueError):
        pass
    if option_from_symbol(trading_symbol) is True:
        return 'option'
    return 'future'


_UNDERLYING = re.compile(r'^([A-Z&]{2,})')


def derive_underlying(trading_symbol):
    """The underlying, where the master leaves `Symbol` blank.

    The leading alphabetic run before the first digit: `GOLD05DEC25F`
    is GOLD, `CRUDEOILM19DEC25F` is CRUDEOILM. Note that CRUDEOILM is
    its own underlying and NOT crude oil — a mini contract is a
    different instrument with a different lot size, and folding it into
    its parent is how a ratio pair gets sized as a calendar.
    """
    symbol = str(trading_symbol or '').upper().strip()
    if symbol.endswith('-EQ'):
        return symbol[:-3]
    found = _UNDERLYING.match(symbol)
    return found.group(1) if found else (symbol or None)


class Master:
    """The instrument master, indexed once, read many times.

    Built for three questions the terminal asks constantly:

    - what is this contract (lot size, tick size, expiry, token)?
    - what underlyings does this segment have?
    - what contracts does this underlying have, oldest expiry first?
    """

    def __init__(self, rows=None, segments=None, freeze_quantities=None,
                 tick_sizes=None):
        self.segments = segments
        #: Segment-and-underlying keyed overrides for the two fields
        #: the master does not always carry. Config, not code — and
        #: shown on screen as an override rather than as a reading.
        self.freeze_quantities = {
            str(key).upper(): _positive_int(value)
            for key, value in (freeze_quantities or {}).items()}
        self.tick_sizes = {
            str(key).upper(): _positive_float(value)
            for key, value in (tick_sizes or {}).items()}
        self.rows = 0
        self.exch_segs = set()
        #: {ExchSeg: rows}. The Exchanges page has a Contracts column,
        #: and "MCXFO: 1,842" is the fastest confirmation there is that
        #: the master really did arrive for the segment being asked
        #: about.
        self.exch_seg_counts = {}
        self._by_symbol = {}
        self._underlyings = {}
        self._contracts = {}
        #: Rows the master carried that this build has no segment for.
        #: Counted rather than dropped silently: a master full of
        #: MCXFO rows and a terminal that knows no MCX segment is a
        #: one-line config fix, and it has to be visible to be fixed.
        self.unknown_exch_segs = {}
        if rows:
            self.load(rows)

    # -- building ---------------------------------------------------------

    def load(self, rows):
        for row in rows:
            contract = self._contract(row)
            if contract is None:
                continue
            self.rows += 1
            self.exch_segs.add(contract.exch_seg)
            self.exch_seg_counts[contract.exch_seg] = \
                self.exch_seg_counts.get(contract.exch_seg, 0) + 1
            # FIRST WINS on a duplicate trading symbol. The master has
            # been seen to carry a symbol twice across segments; taking
            # the last would silently re-point a live pair's leg.
            self._by_symbol.setdefault(contract.trading_symbol, contract)
            if contract.segment is None:
                self.unknown_exch_segs[contract.exch_seg] = \
                    self.unknown_exch_segs.get(contract.exch_seg, 0) + 1
                continue
            group = (contract.segment, contract.kind)
            self._underlyings.setdefault(group, set()).add(contract.underlying)
            self._contracts.setdefault(
                group + (contract.underlying,), []).append(contract)
        for contracts in self._contracts.values():
            contracts.sort(key=_chronological)
        return self

    def _contract(self, row):
        trading_symbol = field(row, 'TradingSymbol', 'trading_symbol', 'tsym')
        exch_seg = field(row, 'ExchSeg', 'exch_seg', 'exchseg', 'segment')
        if not trading_symbol or not exch_seg:
            return None
        trading_symbol = str(trading_symbol).strip().upper()
        exch_seg = str(exch_seg).strip().upper()
        option_type = field(row, 'OptionType', 'option_type', 'optiontype')
        underlying = field(row, 'Symbol', 'Underlying', 'symbol')
        underlying = (str(underlying).strip().upper() if underlying
                      else derive_underlying(trading_symbol))
        segment = (self.segments.key_for_exch_seg(exch_seg)
                   if self.segments is not None else None)
        strike = _positive_float(field(row, 'StrikePrice', 'strike'))
        return Contract(
            trading_symbol,
            underlying=underlying,
            exch_seg=exch_seg,
            segment=segment,
            kind=classify(exch_seg, option_type, trading_symbol, strike),
            option_type=(str(option_type).strip().upper()
                         if option_type else None),
            strike=strike,
            expiry=parse_expiry(field(row, 'Expiry', 'expiry',
                                      'expiry_date') or trading_symbol),
            # UNKNOWN IS None. Never 1.
            lot_size=_positive_int(field(row, 'LotSize', 'lot_size', 'lotsize',
                                         'lotqty', 'marketlot',
                                         'boardlotquantity')),
            tick_size=self._tick_size(row, underlying),
            token=_positive_int(field(row, 'Token', 'token',
                                      'instrument_token')),
            freeze_qty=self._freeze(row, underlying),
            raw=row)

    def _tick_size(self, row, underlying):
        found = _positive_float(field(row, 'TickSize', 'tick_size', 'ticksize',
                                      'minTickSize'))
        if found is not None:
            return found
        return self.tick_sizes.get(str(underlying or '').upper())

    def _freeze(self, row, underlying):
        found = _positive_int(field(row, 'FreezeQty', 'freeze_qty',
                                    'freezeQuantity', 'MaxOrderQty',
                                    'maxSingleOrderQty'))
        if found is not None:
            return found
        return self.freeze_quantities.get(str(underlying or '').upper())

    # -- reading ----------------------------------------------------------

    def contract(self, trading_symbol):
        """The contract, or None. None is 'not in the master', which the
        caller must report as such — never as a contract with defaults."""
        return self._by_symbol.get(str(trading_symbol or '').strip().upper())

    def lot_size(self, trading_symbol):
        """UNITS PER LOT, or **None**.

        None means the order cannot be sized and must be refused. It
        does NOT mean 1. See the module docstring — this is the single
        most expensive difference between this module and the stat-arb
        one it is adapted from.
        """
        contract = self.contract(trading_symbol)
        return contract.lot_size if contract else None

    def tick_size(self, trading_symbol):
        contract = self.contract(trading_symbol)
        return contract.tick_size if contract else None

    def token(self, trading_symbol):
        contract = self.contract(trading_symbol)
        return contract.token if contract else None

    def underlyings(self, segment, kind='future'):
        return sorted(self._underlyings.get((segment, kind), ()))

    def contracts(self, segment, underlying, kind='future'):
        """Every contract on this underlying, **oldest expiry first**."""
        return list(self._contracts.get(
            (segment, kind, str(underlying or '').upper()), ()))

    def search(self, needle, segment=None, kind=None, limit=40):
        """Symbols whose name or underlying matches — the picker's search.

        The operator does not know how a broker spells an instrument
        (`GOLD`, `GOLDM`, `GOLDGUINEA`, `GOLDPETAL` are four different
        contracts), so they search rather than guess.

        EVERY match is collected before sorting. It used to stop at
        `limit * 4` and sort what it had, which on a crowded underlying
        is not the same list at all: MCX lists a few futures against
        thousands of options on CRUDEOIL, so the first 160 rows in
        insertion order were all options and the futures — the only
        thing this terminal trades — never reached the sort, let alone
        the dropdown.

        With no `kind` asked for, FUTURES COME FIRST. A spread ladder
        is two futures; an option is a deliberate choice and it is not
        the one a search for "crude" is making.
        """
        needle = str(needle or '').strip().upper()
        found = []
        for contract in self._by_symbol.values():
            if segment and contract.segment != segment:
                continue
            if kind and contract.kind != kind:
                continue
            if needle and needle not in contract.trading_symbol \
                    and needle not in (contract.underlying or ''):
                continue
            found.append(contract)
        rank = {'future': 0, 'cash': 1, 'option': 2}
        found.sort(key=lambda contract: (rank.get(contract.kind, 3),)
                   + _chronological(contract))
        return found[:limit]

    def next_contracts(self, trading_symbol, count=2):
        """The contracts AFTER this one on the same underlying.

        The roll affordance: an MCX calendar has to be re-pointed at the
        next pair of contracts every month or two, and doing that by
        hand through New Pair is how the wrong contract gets traded.
        """
        contract = self.contract(trading_symbol)
        if contract is None or contract.segment is None:
            return []
        siblings = self.contracts(contract.segment, contract.underlying,
                                  contract.kind)
        for index, other in enumerate(siblings):
            if other.trading_symbol == contract.trading_symbol:
                return siblings[index + 1:index + 1 + count]
        return []

    def report(self, trading_symbol):
        """Everything the connectivity checklist needs about one leg.

        Shaped like MT5-Trader's `symbol_report`: does it exist, what
        are the specs the sizing depends on, and — where something is
        missing — the step that fixes it, in words.
        """
        contract = self.contract(trading_symbol)
        if contract is None:
            return {'symbol': trading_symbol, 'found': False,
                    'error': f'{trading_symbol} is not in the instrument '
                             f'master. Check the spelling against the '
                             f'picker, or reload the master.'}
        row = dict(contract.to_dict(), symbol=contract.trading_symbol,
                   found=True, days_to_expiry=contract.days_to_expiry())
        problems = []
        if contract.lot_size is None:
            problems.append(
                f'{contract.trading_symbol} has no LotSize in the master, so '
                f'nothing can be sized on it — an order needs units, and '
                f'units are lots x LotSize. Reload the master; if it is still '
                f'missing, this contract cannot be traded here.')
        if contract.tick_size is None:
            problems.append(
                f'{contract.trading_symbol} has no TickSize in the master, so '
                f'the ladder increment cannot be derived from it. Set the '
                f'increment on the pair, or add a tick size for '
                f'{contract.underlying} to the config.')
        if contract.token is None:
            problems.append(
                f'{contract.trading_symbol} has no Token in the master, so it '
                f'cannot be subscribed to the live feed — prices would come '
                f'from polling alone.')
        row['problems'] = problems
        row['ok'] = not problems
        return row


#: Oldest expiry first, then strike, then symbol. An UNKNOWN expiry
#: sorts LAST rather than first: a contract whose expiry could not be
#: read must not be offered as the front month.
_FAR_FUTURE = datetime.date(9999, 12, 31)


def _chronological(contract):
    return (contract.expiry or _FAR_FUTURE,
            contract.strike if contract.strike is not None else 0.0,
            contract.trading_symbol)
