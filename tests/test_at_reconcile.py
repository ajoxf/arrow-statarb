"""Our ledger against the exchange's net — and the case code cannot fix.

MT5-Trader matches TICKETS. There are none here, so this compares NETS
— and on a shared account an excess is genuinely unattributable.
"""

import pytest

from arrowtrader.book import Book
from arrowtrader.models import LegFill, SpreadPosition
from arrowtrader.reconcile import Reconciler


class Pair:
    key = 'GOLD05DEC25F|GOLD05FEB26F'
    symbol_a = 'GOLD05DEC25F'
    symbol_b = 'GOLD05FEB26F'
    product = 'NRML'
    meta_a = {'contract_size': 100}
    meta_b = {'contract_size': 100}


class Account:
    def __init__(self, dedicated=True):
        self.dedicated = dedicated
        self.name = 'arrow'


class Config:
    def __init__(self, dedicated=True):
        self.account = Account(dedicated)
        self.pairs = {Pair.key: Pair()}
        self.settings = {'CLOSE_ATTEMPTS': 3}

    def get(self, name, default=None):
        return self.settings.get(name, default)


class Leg:
    """The exchange's own net, or None when it cannot be read."""

    def __init__(self, nets=None, readable=True):
        self.nets = nets if nets is not None else {}
        self.readable = readable
        self.closes = []

    def positions(self, symbol=None):
        if not self.readable:
            return None
        return [{'symbol': name, 'product': product,
                 'side': 'BUY' if lots > 0 else 'SELL',
                 'volume': abs(lots), 'lot_size': 100}
                for (name, product), lots in self.nets.items() if lots]

    def close_reduce(self, symbol, entry_side, volume, product='NRML',
                     comment='', **kwargs):
        self.closes.append({'symbol': symbol, 'entry_side': entry_side,
                            'volume': volume, 'comment': comment})
        return {'ok': True, 'unresolved': False, 'filled_volume': volume,
                'price': 75000.0}


def held(book, side='BUY', lots=2.0):
    """One recorded spread position: long the far leg, short the near."""
    side_a, side_b = ('SELL', 'BUY') if side == 'BUY' else ('BUY', 'SELL')
    return book.add_position(SpreadPosition(
        Pair.key, side, lots,
        LegFill('arrow', Pair.symbol_a, side_a, lots, 75000.0,
                contract_size=100, product='NRML'),
        LegFill('arrow', Pair.symbol_b, side_b, lots, 75500.0,
                contract_size=100, product='NRML'),
        500.0, 'MARKET', lots * 100))


def reconciler(nets, dedicated=True, readable=True, complete=True):
    book = Book()
    held(book)
    built = Reconciler(Config(dedicated), {'arrow': Leg(nets, readable)},
                       book, None, clock=lambda: 1000.0)
    built.book_complete = complete
    return built


#: What the book above expects: short 2 lots of the near leg, long 2 of
#: the far one.
AGREES = {(Pair.symbol_a, 'NRML'): -2.0, (Pair.symbol_b, 'NRML'): 2.0}


# -- agreement is silent ------------------------------------------------------

def test_a_book_that_matches_the_exchange_finds_nothing():
    assert reconciler(AGREES).run()['findings'] == []


def test_the_ledger_sums_a_contract_shared_by_two_ladders():
    """A GOLD Dec/Feb and a GOLD Feb/Apr share the February leg, and
    the exchange nets them into one number that belongs to neither
    ladder alone."""
    built = reconciler(AGREES)
    held(built.book, lots=3.0)
    wanted = built.expected()
    assert wanted[(Pair.symbol_b, 'NRML')] == 5.0
    assert wanted[(Pair.symbol_a, 'NRML')] == -5.0


# -- None is not empty ---------------------------------------------------------

def test_an_UNREADABLE_account_produces_no_findings_at_all():
    """Treating it as flat would turn every position we hold into a
    shortfall and clear the book while the money is still out there."""
    built = reconciler({}, readable=False)
    report = built.run()
    assert report['findings'] == []
    assert report['unknown_accounts'] == ['arrow']
    assert 'could not be read' in report['skipped']
    # CONTROL: readable and genuinely flat DOES produce findings.
    assert reconciler({}).run()['findings']


def test_a_net_with_no_lot_size_is_skipped_not_read_as_zero():
    book = Book()
    held(book)
    leg = Leg()
    leg.positions = lambda symbol=None: [
        {'symbol': Pair.symbol_a, 'product': 'NRML', 'side': 'SELL',
         'volume': None}]
    built = Reconciler(Config(), {'arrow': leg}, book, None,
                       clock=lambda: 1.0)
    built.book_complete = True
    nets, _unknown = built.actual()
    assert nets == {}


# -- three strikes -------------------------------------------------------------

def test_a_difference_needs_THREE_polls_before_it_is_ready():
    """A single poll can catch the exchange mid-fill, and acting on
    that is how a healthy position is closed for being invisible."""
    built = reconciler({(Pair.symbol_a, 'NRML'): -2.0,
                        (Pair.symbol_b, 'NRML'): 5.0})
    for expected in (1, 2, 3):
        finding = built.run()['findings'][0]
        assert finding['strikes'] == expected
        assert finding['ready'] is (expected >= 3)


def test_strikes_are_CONSECUTIVE_not_cumulative():
    """A flicker every hour would otherwise accumulate into an action
    nobody watched happen."""
    leg = Leg({(Pair.symbol_a, 'NRML'): -2.0, (Pair.symbol_b, 'NRML'): 5.0})
    book = Book()
    held(book)
    built = Reconciler(Config(), {'arrow': leg}, book, None,
                       clock=lambda: 1.0)
    built.book_complete = True
    built.run()
    built.run()
    leg.nets = dict(AGREES)          # it agrees for one pass
    built.run()
    leg.nets = {(Pair.symbol_a, 'NRML'): -2.0, (Pair.symbol_b, 'NRML'): 5.0}
    assert built.run()['findings'][0]['strikes'] == 1


# -- excess vs shortfall -------------------------------------------------------

def test_MORE_at_the_exchange_than_we_expect_is_an_EXCESS():
    built = reconciler({(Pair.symbol_a, 'NRML'): -2.0,
                        (Pair.symbol_b, 'NRML'): 5.0})
    finding = built.run()['findings'][0]
    assert finding['kind'] == 'EXCESS'
    assert finding['difference_lots'] == 3.0


def test_LESS_at_the_exchange_than_we_expect_is_a_SHORTFALL():
    built = reconciler({(Pair.symbol_a, 'NRML'): -2.0})
    finding = built.run()['findings'][0]
    assert finding['kind'] == 'SHORTFALL'
    assert 'MIS square-off' in finding['note']
    assert 'may now be NAKED' in finding['note']


def test_a_net_the_OTHER_WAY_ROUND_is_neither_an_excess_nor_a_shortfall():
    """Expecting +2 lots and finding -2 is the same SIZE and the
    opposite exposure. It reads as neither while being worse than both:
    the position is four lots from where the book thinks it is, and the
    hedge is inverted rather than merely mis-sized."""
    built = reconciler({(Pair.symbol_a, 'NRML'): -2.0,
                        (Pair.symbol_b, 'NRML'): -2.0})
    finding = built.run()['findings'][0]
    assert finding['kind'] == 'REVERSED'
    assert 'OTHER WAY ROUND' in finding['note']
    assert '4 lots from where' in finding['note']


def test_a_REVERSED_net_is_ours_to_explain_on_a_shared_account_too():
    """No amount of somebody else's dealing turns our long into a
    short."""
    built = reconciler({(Pair.symbol_a, 'NRML'): -2.0,
                        (Pair.symbol_b, 'NRML'): -2.0}, dedicated=False)
    for _ in range(3):
        finding = built.run()['findings'][0]
    assert finding['actionable'] is True


def test_a_SHORTFALL_is_ours_to_explain_whoever_shares_the_account():
    """It is OUR book that is wrong."""
    built = reconciler({(Pair.symbol_a, 'NRML'): -2.0}, dedicated=False)
    for _ in range(3):
        finding = built.run()['findings'][0]
    assert finding['actionable'] is True


# -- the case code cannot fix --------------------------------------------------

def test_an_EXCESS_on_a_SHARED_account_is_never_actionable():
    """Our net and the trader's own dealing in the same contract are
    one number, and no API field separates them."""
    built = reconciler({(Pair.symbol_a, 'NRML'): -2.0,
                        (Pair.symbol_b, 'NRML'): 5.0}, dedicated=False)
    for _ in range(3):
        finding = built.run()['findings'][0]
    assert finding['ready'] is True
    assert finding['actionable'] is False
    assert 'NOT DECLARED DEDICATED' in finding['note']
    assert 'may be your own dealing' in finding['note']
    # ...and it is put in front of a person instead.
    assert built.snapshot()['unclaimed']


def test_an_EXCESS_on_a_DEDICATED_account_IS_actionable():
    """The control for the test above: the same difference, the same
    three strikes, and the only thing that changed is the declaration."""
    built = reconciler({(Pair.symbol_a, 'NRML'): -2.0,
                        (Pair.symbol_b, 'NRML'): 5.0}, dedicated=True)
    for _ in range(3):
        finding = built.run()['findings'][0]
    assert finding['actionable'] is True
    assert 'nothing else should have put it there' in finding['note']


def test_the_declaration_is_on_the_screen_beside_every_finding():
    """It is a declaration, not a measurement — nothing at the exchange
    can confirm it — so it is reported rather than quietly relied on."""
    assert reconciler(AGREES, dedicated=False).snapshot()['dedicated'] is False
    assert reconciler(AGREES, dedicated=True).snapshot()['dedicated'] is True


# -- nothing acts before recovery finishes ------------------------------------

def test_NOTHING_is_actionable_until_the_book_is_complete():
    """An orphan is only an orphan if we are sure it is not ours."""
    built = reconciler({(Pair.symbol_a, 'NRML'): -2.0,
                        (Pair.symbol_b, 'NRML'): 5.0}, complete=False)
    for _ in range(3):
        finding = built.run()['findings'][0]
    assert finding['ready'] is True
    assert finding['actionable'] is False
    assert 'Recovery has not finished' in finding['note']


# -- what a person can do ------------------------------------------------------

def test_EVERY_ready_finding_goes_in_front_of_a_person():
    """MT5-Trader auto-closes an orphan after three strikes, and it
    can: the magic number proves the position is its own. Nothing
    proves that here, so `actionable` means a person MAY act — not that
    we will."""
    built = reconciler({(Pair.symbol_a, 'NRML'): -2.0,
                        (Pair.symbol_b, 'NRML'): 5.0}, dedicated=True)
    for _ in range(3):
        built.run()
    unclaimed = built.snapshot()['unclaimed']
    assert len(unclaimed) == 1
    assert unclaimed[0]['actionable'] is True


def test_ADOPT_stops_asking_and_does_NOT_invent_a_position():
    """Our ledger records what this system opened, at what price.
    Adopting a net we did not open would put a fabricated entry price
    into every P&L figure downstream."""
    built = reconciler({(Pair.symbol_a, 'NRML'): -2.0,
                        (Pair.symbol_b, 'NRML'): 5.0}, dedicated=False)
    for _ in range(3):
        built.run()
    before = len(built.book.positions())
    built.adopt(Pair.symbol_b, 'NRML')
    assert built.snapshot()['unclaimed'] == []
    assert len(built.book.positions()) == before


def test_CLOSING_an_unclaimed_net_crosses_the_other_way():
    built = reconciler({(Pair.symbol_a, 'NRML'): -2.0,
                        (Pair.symbol_b, 'NRML'): 5.0})
    for _ in range(3):
        built.run()
    result = built.close_excess(Pair.symbol_b, 'NRML')
    assert result['ok'] is True
    sent = built.legs['arrow'].closes[-1]
    assert sent['entry_side'] == 'BUY'      # it is long, so sell it back
    assert sent['volume'] == 3.0
    assert built.snapshot()['untracked_closes']


def test_closing_something_not_unclaimed_is_refused():
    built = reconciler(AGREES)
    assert built.close_excess('GOLD05DEC25F')['ok'] is False


def test_run_NEVER_closes_anything_by_itself():
    """Only ever from a person's click."""
    built = reconciler({(Pair.symbol_a, 'NRML'): -2.0,
                        (Pair.symbol_b, 'NRML'): 9.0})
    for _ in range(5):
        built.run()
    assert built.legs['arrow'].closes == []


# -- the consequence a bare number does not convey -----------------------------

def test_ONE_leg_gone_is_reported_as_an_OUTRIGHT_not_as_a_number():
    """An MIS square-off takes one contract, and what is left of a
    spread with one leg gone is an outright in a commodity."""
    built = reconciler({(Pair.symbol_a, 'NRML'): -2.0})
    naked = built.naked_legs()
    assert len(naked) == 1
    assert naked[0]['on'] == Pair.symbol_a
    assert naked[0]['gone'] == Pair.symbol_b
    assert 'it is an outright' in naked[0]['note']
    # CONTROL: both legs present, nothing naked.
    assert reconciler(AGREES).naked_legs() == []


def test_an_unreadable_account_reports_no_naked_legs_either():
    assert reconciler({}, readable=False).naked_legs() == []


# -- an unknown lot size REFUSES rather than assuming one ----------------------

def test_an_unknown_lot_size_is_NONE_not_one():
    """MT5-Trader returns 1.0 and says so, because a silent 1.0
    understated a P&L. Here units are lots x LotSize, so a silent 1
    sends an order a hundred times the wrong size."""
    built = reconciler(AGREES)
    assert built.lot_size(Pair.symbol_a) == (100.0, False)
    assert built.lot_size('NOT-A-CONTRACT') == (None, True)
